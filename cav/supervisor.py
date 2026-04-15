"""Namespace setup and overlay supervisor.

Runs inside a fresh user+mount+net namespace created by unshare(1).
Signals the parent that the namespace is ready, mounts the nix store
overlay, then runs the bwrap sandbox wrapper as a child. Stays alive
to unmount the kernel overlay when bwrap exits.

Environment variables (set by sandbox.py):
    CODING_CAVE_NS_SYNC_FD           - fd to write sync byte to
    CODING_CAVE_NS_OVERLAY_MERGED    - overlay merged mountpoint
    CODING_CAVE_NS_OVERLAY_UPPER     - overlay upper dir
    CODING_CAVE_NS_OVERLAY_WORK      - overlay work dir
    CODING_CAVE_NS_MOUNTPOINT        - absolute path to mountpoint binary
"""

import ctypes
import ctypes.util
import glob
import os
import select
import stat
import struct
import subprocess
import sys
import threading


def _signal_sync(fd: int) -> None:
    """Signal parent: namespace exists, safe to apply iptables + slirp."""
    os.write(fd, b"1")
    os.close(fd)


# -- kernel overlayfs --
# Mounted directly via mount() syscall. Requires kernel 5.11+ with
# CONFIG_OVERLAY_FS enabled.

_MNT_DETACH = 2

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _mount_kernel_overlay(merged: str, upper: str, work: str) -> None:
    """Mount kernel overlayfs in the user namespace.

    The CVE-2023-0386 check only blocks copy-up (writes that promote
    lower-layer content to upper), not reads. With shadow files preventing
    lock file copy-up and DB seeding preventing all other copy-up, unmapped
    lower-layer uids are never an issue.
    """
    variants = [
        f"lowerdir=/nix/store,upperdir={upper},workdir={work}"
        f",userxattr,uuid=off",
        f"lowerdir=/nix/store,upperdir={upper},workdir={work}"
        f",userxattr",
    ]
    for opts in variants:
        ret = _libc.mount(b"overlay", merged.encode(), b"overlay",
                          ctypes.c_ulong(0), opts.encode())
        if ret == 0:
            return
    err = ctypes.get_errno()
    print(f"supervisor: kernel overlayfs mount failed: {os.strerror(err)}",
          file=sys.stderr)
    sys.exit(1)


def _umount_kernel(path: str) -> None:
    """Unmount a kernel overlay."""
    ret = _libc.umount2(path.encode(), _MNT_DETACH)
    if ret < 0:
        err = ctypes.get_errno()
        print(f"supervisor: umount2 {path}: {os.strerror(err)}",
              file=sys.stderr)


def _is_nix_lock_file(path: str) -> bool:
    """True for transient nix lock files, false for source derivations.

    Nix lock files are always empty (0 bytes), used only for flock().
    Source derivations ending in .lock (e.g. Cargo.lock) have content.
    """
    try:
        s = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(s.st_mode) and s.st_size == 0


def _shadow_host_lock_files(upper: str) -> None:
    """Pre-populate upper layer with empty files that shadow host lock files.

    Host lock files are owned by root (mode 600), unmapped in the cave's
    user namespace. Creating empty files with the same names in the upper
    directory before mounting makes the overlay prefer the writable
    upper-layer copies. The cave's nix then creates, locks, and
    unlinks its own independent lock files.

    Must run BEFORE the overlay mount so it sees the upper-layer files
    on its initial directory scan.
    """
    for path in glob.glob("/nix/store/*.lock"):
        if not _is_nix_lock_file(path):
            continue
        name = os.path.basename(path)
        shadow = os.path.join(upper, name)
        try:
            os.lstat(shadow)
            continue  # Already exists in upper layer.
        except FileNotFoundError:
            pass
        fd = os.open(shadow, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)


# -- inotify lock file watcher --
# Watches /nix/store for new *.lock files and creates shadows in the
# upper directory at runtime. Complements the startup shadow pass
# which only covers lock files that exist before the overlay mount.

# inotify constants
_IN_CREATE = 0x00000100
_IN_MOVED_TO = 0x00000080
_IN_NONBLOCK = 0x00000800
_INOTIFY_EVENT_HDR = struct.Struct("iIII")  # wd, mask, cookie, len


def _create_shadow(upper: str, name: str) -> None:
    """Create an empty shadow file in the upper directory."""
    shadow = os.path.join(upper, name)
    try:
        fd = os.open(shadow, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        pass


def _inotify_read_events(inotify_fd: int) -> list[str]:
    """Read pending inotify events, return list of created filenames."""
    try:
        buf = os.read(inotify_fd, 8192)
    except OSError:
        return []
    names = []
    offset = 0
    hdr_size = _INOTIFY_EVENT_HDR.size
    while offset + hdr_size <= len(buf):
        _, _, _, name_len = _INOTIFY_EVENT_HDR.unpack_from(buf, offset)
        offset += hdr_size
        if name_len > 0:
            name = buf[offset:offset + name_len].rstrip(b"\x00").decode(
                errors="replace")
            names.append(name)
        offset += name_len
    return names


def _lock_watcher_loop(inotify_fd: int, upper: str,
                       stop_r: int) -> None:
    """Watch for new *.lock files and create shadows. Runs in a thread."""
    while True:
        ready, _, _ = select.select([inotify_fd, stop_r], [], [])
        if stop_r in ready:
            break
        for name in _inotify_read_events(inotify_fd):
            if name.endswith(".lock"):
                _create_shadow(upper, name)


def start_lock_watcher(store: str, upper: str):
    """Start inotify watcher for lock files. Returns stop function.

    Watches `store` (the host's /nix/store, visible from the supervisor
    namespace) for IN_CREATE and IN_MOVED_TO events. When a *.lock file
    appears, creates an empty shadow in `upper` so the overlay prefers
    the writable copy over the unreadable host original.

    Returns a callable that stops the watcher, or None if inotify
    setup fails.
    """
    inotify_fd = _libc.inotify_init1(_IN_NONBLOCK)
    if inotify_fd < 0:
        print("supervisor: inotify_init failed, lock file watcher disabled",
              file=sys.stderr)
        return None

    wd = _libc.inotify_add_watch(
        inotify_fd, store.encode(), _IN_CREATE | _IN_MOVED_TO)
    if wd < 0:
        os.close(inotify_fd)
        print("supervisor: inotify_add_watch failed, lock file watcher disabled",
              file=sys.stderr)
        return None

    stop_r, stop_w = os.pipe()
    t = threading.Thread(
        target=_lock_watcher_loop,
        args=(inotify_fd, upper, stop_r),
        daemon=True,
    )
    t.start()

    def stop():
        os.write(stop_w, b"x")
        os.close(stop_w)
        t.join(timeout=2)
        os.close(stop_r)
        os.close(inotify_fd)

    return stop


def main() -> None:
    env = os.environ

    # Signal parent: namespace exists.
    sync_fd = int(env["CODING_CAVE_NS_SYNC_FD"])
    _signal_sync(sync_fd)

    # Shadow host lock files before mounting overlay. Empty files in
    # the upper dir must exist before the overlay mount so it prefers
    # the writable copies over the inaccessible host originals.
    upper = env.get("CODING_CAVE_NS_OVERLAY_UPPER")
    if upper:
        _shadow_host_lock_files(upper)

    # Mount kernel overlayfs.
    merged = env.get("CODING_CAVE_NS_OVERLAY_MERGED")
    if merged and upper:
        _mount_kernel_overlay(
            merged, upper, env["CODING_CAVE_NS_OVERLAY_WORK"])

    # Watch for new lock files at runtime. /nix/store from the
    # supervisor's namespace is the parent's store: the host's real
    # store at depth 1, or the parent cave's merged overlay view at
    # depth > 1 (bind-mounted by bwrap). The overlay mounts at
    # $OVERLAY_MERGED, not at /nix/store.
    stop_watcher = None
    if upper:
        stop_watcher = start_lock_watcher("/nix/store", upper)

    # Run bwrap as a child (not exec) so we stay alive to clean up.
    try:
        result = subprocess.run(sys.argv[1:])
    finally:
        if stop_watcher:
            stop_watcher()
        if merged and upper:
            _umount_kernel(merged)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
