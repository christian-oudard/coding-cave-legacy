"""Namespace setup and fuse-overlayfs supervisor.

Runs inside a fresh user+mount+net namespace created by unshare(1).
Signals the parent that the namespace is ready, mounts the nix store
overlay if needed, then runs the bwrap sandbox wrapper as a child.
Stays alive to own the fuse-overlayfs process and clean it up when
bwrap exits.

Environment variables (set by sandbox.py):
    CODING_CAVE_NS_SYNC_FD           - fd to write sync byte to
    CODING_CAVE_NS_OVERLAY_MERGED    - fuse-overlayfs merged mountpoint
    CODING_CAVE_NS_OVERLAY_UPPER     - fuse-overlayfs upper dir
    CODING_CAVE_NS_OVERLAY_WORK      - fuse-overlayfs work dir
    CODING_CAVE_NS_MOUNTPOINT        - absolute path to mountpoint binary
    CODING_CAVE_NS_FUSE_OVERLAYFS    - absolute path to fuse-overlayfs binary
    CODING_CAVE_NS_FUSERMOUNT        - absolute path to fusermount/fusermount3
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
import time


def _signal_sync(fd: int) -> None:
    """Signal parent: namespace exists, safe to apply iptables + slirp."""
    os.write(fd, b"1")
    os.close(fd)


def _is_mounted(mountpoint_bin: str, path: str) -> bool:
    return subprocess.run(
        [mountpoint_bin, "-q", path],
        capture_output=True,
    ).returncode == 0


def _overlay_functional(merged: str) -> bool:
    """Check if an existing overlay mount is functional via write probe."""
    probe = os.path.join(merged, ".overlay_probe")
    try:
        fd = os.open(probe, os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def _unmount(fusermount_bin: str | None, path: str) -> None:
    if not fusermount_bin:
        print(f"supervisor: cannot unmount {path}: no fusermount binary",
              file=sys.stderr)
        return
    r = subprocess.run(
        [fusermount_bin, "-u", path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"supervisor: unmount {path} failed: {r.stderr.strip()}",
              file=sys.stderr)


def _mount_overlay(env: dict) -> subprocess.Popen | None:
    """Mount fuse-overlayfs if needed. Returns the daemon Popen or None."""
    merged = env.get("CODING_CAVE_NS_OVERLAY_MERGED")
    fuse_bin = env.get("CODING_CAVE_NS_FUSE_OVERLAYFS")
    if not merged or not fuse_bin:
        return None

    mountpoint_bin = env.get("CODING_CAVE_NS_MOUNTPOINT")
    fusermount_bin = env.get("CODING_CAVE_NS_FUSERMOUNT")
    upper = env["CODING_CAVE_NS_OVERLAY_UPPER"]
    work = env["CODING_CAVE_NS_OVERLAY_WORK"]

    # Check if already mounted and functional.
    if mountpoint_bin:
        if _is_mounted(mountpoint_bin, merged):
            if _overlay_functional(merged):
                return None
            # Stale mount.
            _unmount(fusermount_bin, merged)
    # No mountpoint binary: assume we need to mount.

    # fusermount must be on PATH for fuse-overlayfs to work.
    if fusermount_bin:
        os.environ["PATH"] = (
            os.path.dirname(fusermount_bin)
            + ":" + os.environ.get("PATH", "")
        )

    # squash_to_uid must match the namespace uid, not the parent's.
    # unshare --map-user=0 remaps the parent uid to 0 here.
    opts = (
        f"lowerdir=/nix/store,upperdir={upper},workdir={work}"
        f",squash_to_uid=0,squash_to_gid=0"
    )

    # Foreground mode: fuse-overlayfs stays as our direct child.
    # PID is trivially known (no /proc scanning, no race conditions).
    proc = subprocess.Popen(
        [fuse_bin, "-f", "-o", opts, merged],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait for mount to appear. Poll with short sleeps rather than
    # busy-looping.
    if mountpoint_bin:
        for _ in range(200):  # 10s max
            if _is_mounted(mountpoint_bin, merged):
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace").strip()
                print(
                    f"supervisor: fuse-overlayfs died during mount: {stderr}",
                    file=sys.stderr,
                )
                sys.exit(1)
            time.sleep(0.05)
        else:
            proc.terminate()
            print("supervisor: fuse-overlayfs mount timed out", file=sys.stderr)
            sys.exit(1)

    return proc


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

    The fuse-overlayfs daemon runs as host uid 1000 (mapped to namespace
    uid 0) and cannot read host-root-owned lock files (mode 600). When
    nix tries to open one, the daemon's copy-up fails with EPERM.

    Creating empty files with the same names in the upper directory
    before the daemon starts makes the overlay prefer the writable
    upper-layer copies. The cave's nix then creates, locks, and
    unlinks its own independent lock files.

    Must run BEFORE fuse-overlayfs starts so the daemon sees the
    upper-layer files on its initial directory scan.
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
# which only covers lock files that exist before fuse-overlayfs starts.

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

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
    appears, creates an empty shadow in `upper` so fuse-overlayfs
    prefers the writable copy over the unreadable host original.

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
    # the upper dir must exist before fuse-overlayfs starts so the
    # daemon prefers the writable copies over the inaccessible host ones.
    upper = env.get("CODING_CAVE_NS_OVERLAY_UPPER")
    if upper:
        _shadow_host_lock_files(upper)

    # Mount overlay if needed.
    fuse_proc = _mount_overlay(env)

    # Watch for new lock files at runtime. /nix/store from the
    # supervisor's namespace is the host's actual store (the lower
    # layer), since fuse-overlayfs mounts at $OVERLAY_MERGED.
    stop_watcher = None
    if upper:
        stop_watcher = start_lock_watcher("/nix/store", upper)

    # Run bwrap as a child (not exec) so we stay alive to clean up fuse.
    try:
        result = subprocess.run(sys.argv[1:])
    finally:
        if stop_watcher:
            stop_watcher()
        if fuse_proc:
            _unmount(env.get("CODING_CAVE_NS_FUSERMOUNT"), env["CODING_CAVE_NS_OVERLAY_MERGED"])
            fuse_proc.terminate()
            try:
                fuse_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fuse_proc.kill()

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
