#!/usr/bin/env python3
"""Send a message to a cave session's PTY relay via Unix domain socket.

Usage:
    cave-send <target> <message>
    echo "message" | cave-send <target>
    cave-send --all-project <message>   # send to all sessions on same project
    cave-send --list

Target can be a session index (e.g. 4) or a full session ID.

Messages are routed through the sender's own relay, which stamps
the sender identity (verified via SO_PEERCRED). The relay forwards
to the target session's socket.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path


IPC_DIR = Path.home() / "ipc"


def send(target_sid, message):
    """Send a message by forwarding through our own relay."""
    my_sid = os.environ.get("CODING_CAVE_SESSION_ID")
    if not my_sid:
        # Outside a cave session (e.g. host user). Send directly.
        target_sock = str(IPC_DIR / f"{target_sid}.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(target_sock)
        s.sendall(f"user: {message}".encode())
        s.shutdown(socket.SHUT_WR)
        s.close()
        return
    if my_sid == target_sid:
        raise ValueError("Cannot send to yourself.")
    # Route through own relay for verified sender tagging
    my_sock = str(IPC_DIR / f"{my_sid}.sock")
    payload = f"FWD {target_sid}\n{message}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(my_sock)
    s.sendall(payload.encode())
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
    if len(sys.argv) < 2:
        print("Usage: cave-send <target> <message>", file=sys.stderr)
        print("       cave-send --list", file=sys.stderr)
        print("", file=sys.stderr)
        print("Target: session index (e.g. 4) or full session ID", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--list":
        sessions = list_sessions()
        my_sid = os.environ.get("CODING_CAVE_SESSION_ID", "")
        for sid, meta in sorted(sessions, key=lambda s: s[1].get("index", "")):
            idx = meta.get("index", "?")
            proj = meta.get("project", "?")
            age = _format_age(meta.get("started"))
            me = " (me)" if sid == my_sid else ""
            print(f"  #{idx}  {proj}  {sid}  [{age}]{me}")
        if not sessions:
            print("  No active sessions.")
        return

    if sys.argv[1] == "--all-project":
        if len(sys.argv) < 3:
            print("Usage: cave-send --all-project <message>", file=sys.stderr)
            sys.exit(1)
        message = " ".join(sys.argv[2:])
        my_sid = os.environ.get("CODING_CAVE_SESSION_ID", "")
        my_meta = {}
        for sid, meta in list_sessions():
            if sid == my_sid:
                my_meta = meta
                break
        my_project = my_meta.get("project")
        if not my_project:
            sys.exit(1)
        sent = 0
        for sid, meta in list_sessions():
            if sid == my_sid:
                continue
            if meta.get("project") != my_project:
                continue
            try:
                send(sid, message)
                sent += 1
            except (ValueError, ConnectionRefusedError,
                    FileNotFoundError, OSError):
                pass
        return

    target = sys.argv[1]
    # Resolve target to session ID
    sid, _ = resolve_index(target)
    if sid is None and (target.startswith("#") or target.isdigit()):
        print(f"No session with index {target}", file=sys.stderr)
        sys.exit(1)
    elif sid is None:
        sid = target

    if len(sys.argv) >= 3:
        message = " ".join(sys.argv[2:])
    elif not os.isatty(sys.stdin.fileno()):
        message = sys.stdin.read()
    else:
        print("No message provided.", file=sys.stderr)
        sys.exit(1)

    try:
        send(sid, message)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except (ConnectionRefusedError, FileNotFoundError) as e:
        print(f"Failed to send: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
