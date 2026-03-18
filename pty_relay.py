#!/usr/bin/env python3
"""Session wrapper: register IPC metadata, exec child, clean up on exit.

Replaces the old PTY relay. Messages are now delivered via Claude Code
hooks (cave-send hook), so PTY injection is no longer needed. This
wrapper just manages session metadata for cave-send --list discovery.

Usage:
    pty-relay -- command [args...]

Writes ~/ipc/<sid>.json with session metadata and ~/ipc/<sid>.pid with
the child PID. Cleans up both on exit.
"""
import json
import os
import signal
import sys
import time


def _meta_path():
    sid = os.environ.get("CODING_CAVE_SESSION_ID", "")
    if not sid:
        return None, None
    ipc_dir = os.path.join(os.environ.get("HOME", ""), "ipc")
    return (
        os.path.join(ipc_dir, f"{sid}.json"),
        os.path.join(ipc_dir, f"{sid}.pid"),
    )


def _write_meta(meta_path, pid_path, child_pid):
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    meta = {
        "sid": os.environ.get("CODING_CAVE_SESSION_ID", ""),
        "index": os.environ.get("CODING_CAVE_SESSION_INDEX", ""),
        "project": os.path.basename(
            os.environ.get("CODING_CAVE_PROJECT_PATH", "")),
        "started": time.time(),
        "pid": child_pid,
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    with open(pid_path, "w") as f:
        f.write(str(child_pid))


def _remove_meta(meta_path, pid_path):
    for p in (meta_path, pid_path):
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

    meta_path, pid_path = _meta_path()

    # Fork so we can write metadata with the child's PID and wait for it.
    pid = os.fork()
    if pid == 0:
        # Child: exec the command directly on the terminal.
        os.execvp(argv[0], argv)
        os._exit(127)

    # Parent: write metadata and wait.
    if meta_path:
        _write_meta(meta_path, pid_path, pid)

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
            _remove_meta(meta_path, pid_path)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
