#!/usr/bin/env python3
"""Send a message to a cave session's PTY relay via Unix domain socket.

Usage:
    cave-send <session-id> <message>
    cave-send --sock <path> <message>
    echo "message" | cave-send <session-id>
    cave-send --list

The message is injected into the target session's terminal as if
typed by the user, followed by Enter (CR).
"""
import json
import os
import socket
import sys
import time
from pathlib import Path


IPC_DIR = Path.home() / "ipc"


def _sender_tag():
    """Auto-detect sender identity from environment.

    Inside a cave session: returns '[project#N]' (project + session index).
    Outside (host/user): returns '[user]'.
    """
    idx = os.environ.get("CODING_CAVE_SESSION_INDEX")
    if idx:
        proj = os.environ.get("CODING_CAVE_PROJECT_PATH", "")
        proj = os.path.basename(proj) if proj else ""
        if proj:
            return f"[{proj}#{idx}]"
        return f"[#{idx}]"
    return "[user]"


def send(sock_path, message, tag=True):
    if isinstance(message, bytes):
        message = message.decode()
    if tag:
        message = f"{_sender_tag()} {message}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall(message.encode())
    s.shutdown(socket.SHUT_WR)
    s.close()


def resolve_index(index):
    """Resolve a session index (e.g. '4' or '#4') to (sid, sock_path).

    Returns (sid, sock_path) or (None, None) if not found.
    """
    index = str(index).lstrip("#")
    for sid, meta in list_sessions():
        if meta.get("index") == index:
            return sid, str(IPC_DIR / f"{sid}.sock")
    return None, None


def list_sessions():
    """Return list of (sid, metadata_dict) for sessions with active sockets."""
    if not IPC_DIR.is_dir():
        return []
    sessions = []
    for sock in IPC_DIR.glob("*.sock"):
        sid = sock.stem
        meta_path = sock.with_suffix(".json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        sessions.append((sid, meta))
    return sessions


def _format_age(started):
    if not started:
        return "?"
    age = int(time.time() - started)
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    h, m = divmod(age, 3600)
    return f"{h}h{m // 60:02d}m"


def main():
    raw = "--raw" in sys.argv
    if raw:
        sys.argv.remove("--raw")

    if len(sys.argv) < 2:
        print("Usage: cave-send <session-id> <message>", file=sys.stderr)
        print("       cave-send --list", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--list":
        sessions = list_sessions()
        my_sid = os.environ.get("CODING_CAVE_SESSION_ID", "")
        for sid, meta in sorted(sessions, key=lambda s: s[1].get("index", "")):
            idx = meta.get("index", "?")
            proj = meta.get("project", "?")
            age = _format_age(meta.get("started"))
            me = " (me)" if sid == my_sid else ""
            task = meta.get("task", "")
            task_str = f"  {task}" if task else ""
            print(f"  #{idx}  {proj}  {sid}  [{age}]{me}{task_str}")
        if not sessions:
            print("  No active sessions.")
        return

    if sys.argv[1] == "--sock":
        if len(sys.argv) < 3:
            print("Usage: cave-send --sock <path> <message>", file=sys.stderr)
            sys.exit(1)
        sock_path = sys.argv[2]
        if len(sys.argv) >= 4:
            message = " ".join(sys.argv[3:])
        elif not os.isatty(sys.stdin.fileno()):
            message = sys.stdin.read()
        else:
            print("No message provided.", file=sys.stderr)
            sys.exit(1)
    else:
        sid = sys.argv[1]
        sock_path = str(IPC_DIR / f"{sid}.sock")
        if len(sys.argv) >= 3:
            message = " ".join(sys.argv[2:])
        elif not os.isatty(sys.stdin.fileno()):
            message = sys.stdin.read()
        else:
            print("No message provided.", file=sys.stderr)
            sys.exit(1)

    send(sock_path, message, tag=not raw)


if __name__ == "__main__":
    main()
