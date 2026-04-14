"""Namespace setup and fuse-overlayfs supervisor.

Runs inside a fresh user+mount+net namespace created by unshare(1).
Signals the parent that the namespace is ready, mounts the nix store
overlay if needed, then runs the bwrap sandbox wrapper as a child.
Stays alive to own the fuse-overlayfs process and clean it up when
bwrap exits.

Environment variables (set by sandbox.py):
    CAVE_NS_SYNC_FD           - fd to write sync byte to
    CAVE_NS_OVERLAY_MERGED    - fuse-overlayfs merged mountpoint
    CAVE_NS_OVERLAY_UPPER     - fuse-overlayfs upper dir
    CAVE_NS_OVERLAY_WORK      - fuse-overlayfs work dir
    CAVE_NS_MOUNTPOINT        - absolute path to mountpoint binary
    CAVE_NS_FUSE_OVERLAYFS    - absolute path to fuse-overlayfs binary
    CAVE_NS_FUSERMOUNT        - absolute path to fusermount/fusermount3
"""

import os
import subprocess
import sys
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
    if fusermount_bin:
        subprocess.run(
            [fusermount_bin, "-u", path],
            capture_output=True,
        )


def _mount_overlay(env: dict) -> subprocess.Popen | None:
    """Mount fuse-overlayfs if needed. Returns the daemon Popen or None."""
    merged = env.get("CAVE_NS_OVERLAY_MERGED")
    fuse_bin = env.get("CAVE_NS_FUSE_OVERLAYFS")
    if not merged or not fuse_bin:
        return None

    mountpoint_bin = env.get("CAVE_NS_MOUNTPOINT")
    fusermount_bin = env.get("CAVE_NS_FUSERMOUNT")
    upper = env["CAVE_NS_OVERLAY_UPPER"]
    work = env["CAVE_NS_OVERLAY_WORK"]

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


def main() -> None:
    env = os.environ

    # Signal parent: namespace exists.
    sync_fd = int(env["CAVE_NS_SYNC_FD"])
    _signal_sync(sync_fd)

    # Mount overlay if needed.
    fuse_proc = _mount_overlay(env)

    # Run bwrap as a child (not exec) so we stay alive to clean up fuse.
    try:
        result = subprocess.run(sys.argv[1:])
    finally:
        if fuse_proc:
            _unmount(env.get("CAVE_NS_FUSERMOUNT"), env["CAVE_NS_OVERLAY_MERGED"])
            fuse_proc.terminate()
            try:
                fuse_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fuse_proc.kill()

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
