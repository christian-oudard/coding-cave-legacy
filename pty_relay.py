#!/usr/bin/env python3
"""Session wrapper: register IPC metadata, exec child, clean up on exit.

Replaces the old PTY relay. Messages are now delivered via Claude Code
hooks (cave-send hook), so PTY injection is no longer needed. This
wrapper just manages session metadata for cave-send --list discovery.

Usage:
    pty-relay -- command [args...]

Writes ~/ipc/<sid>.json with session metadata and holds an exclusive
flock on ~/ipc/<sid>.lock for the session lifetime. Other sessions
detect liveness by trying a non-blocking flock on the lock file.
"""
import fcntl
import json
import os
import signal
import sys
import time


def _ipc_paths():
    sid = os.environ.get("CODING_CAVE_SESSION_ID", "")
    if not sid:
        return None, None
    ipc_dir = os.path.join(os.environ.get("HOME", ""), "ipc")
    return (
        os.path.join(ipc_dir, f"{sid}.json"),
        os.path.join(ipc_dir, f"{sid}.lock"),
    )


def _write_meta(meta_path):
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    meta = {
        "sid": os.environ.get("CODING_CAVE_SESSION_ID", ""),
        "index": os.environ.get("CODING_CAVE_SESSION_INDEX", ""),
        "project": os.path.basename(
            os.environ.get("CODING_CAVE_PROJECT_PATH", "")),
        "started": time.time(),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f)


def _acquire_lock(lock_path):
    """Acquire exclusive lock. Held until process exits."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd  # keep fd open to hold the lock


def _remove_meta(meta_path, lock_path):
    for p in (meta_path, lock_path):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def main():
    # Parse: pty-relay -- command [args...]
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    # Accept and ignore --sock for backwards compat
    if len(argv) >= 2 and argv[0] == "--sock":
        argv = argv[2:]
        if argv and argv[0] == "--":
            argv = argv[1:]
    if not argv:
        print("Usage: pty-relay -- command [args...]", file=sys.stderr)
        sys.exit(1)

    meta_path, lock_path = _ipc_paths()

    # Acquire session lock and write metadata before forking.
    lock_fd = None
    if meta_path:
        lock_fd = _acquire_lock(lock_path)
        _write_meta(meta_path)

    # Fork so we can wait for the child and clean up.
    pid = os.fork()
    if pid == 0:
        # Child: exec the command directly on the terminal.
        os.execvp(argv[0], argv)
        os._exit(127)

    # Forward signals to child
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda s, f: os.kill(pid, s))

    exit_code = 1
    try:
        while True:
            wpid, status = os.waitpid(pid, 0)
            if wpid == pid:
                if os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    exit_code = 128 + os.WTERMSIG(status)
                break
    except ChildProcessError:
        pass
    finally:
        if meta_path:
            _remove_meta(meta_path, lock_path)
        if lock_fd is not None:
            os.close(lock_fd)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
