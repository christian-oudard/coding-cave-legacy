#!/usr/bin/env python3
"""Inter-session messaging for coding-cave.

Usage:
    cave-send <target> <message>      send a message
    echo "message" | cave-send <target>
    cave-send --all-project <message> send to all sessions on same project
    cave-send --list                  list active sessions
    cave-send hook                    Claude Code hook (reads queue, delivers)

Target can be a session index (e.g. 4) or a full session ID.

Messages are written to ~/ipc/messages/<target-sid>.jsonl. The hook
subcommand, registered on Stop/UserPromptSubmit/PostToolUse, reads
and delivers them.
"""
import fcntl
import json
import os
import sys
import time
from pathlib import Path


IPC_DIR = Path.home() / "ipc"
MSG_DIR = IPC_DIR / "messages"


# --- send ---

def _sender_tag():
    """Build sender tag from environment."""
    idx = os.environ.get("CODING_CAVE_SESSION_INDEX", "")
    proj = os.environ.get("CODING_CAVE_PROJECT_PATH", "")
    proj = os.path.basename(proj) if proj else ""
    if not idx:
        return "user:"
    if proj:
        return f"{proj}#{idx}:"
    return f"#{idx}:"


def send(target_sid, message):
    """Send a message by writing to the target's queue file."""
    my_sid = os.environ.get("CODING_CAVE_SESSION_ID")
    if my_sid == target_sid:
        raise ValueError("Cannot send to yourself.")
    MSG_DIR.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "sender": _sender_tag(),
        "message": message,
        "ts": time.time(),
    })
    path = MSG_DIR / f"{target_sid}.jsonl"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (entry + "\n").encode())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- hook ---

def _read_and_clear(path):
    """Atomically read and clear the message queue file."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return []
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = b""
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            data += chunk
        if not data:
            return []
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    messages = []
    for line in data.decode().splitlines():
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return messages


def _format_messages(messages):
    """Format messages for injection into Claude's context."""
    parts = []
    for msg in messages:
        sender = msg.get("sender", "unknown")
        text = msg.get("message", "")
        parts.append(f"{sender} {text}")
    return "\n".join(parts)


def hook():
    """Claude Code hook entry point. Reads stdin for hook event JSON."""
    sid = os.environ.get("CODING_CAVE_SESSION_ID", "")
    if not sid:
        sys.exit(0)
    path = str(MSG_DIR / f"{sid}.jsonl")

    messages = _read_and_clear(path)
    if not messages:
        sys.exit(0)

    formatted = _format_messages(messages)
    hook_input = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    event = hook_input.get("hook_event_name", "")

    if event == "Stop":
        # Block the stop so Claude processes the messages immediately.
        json.dump({"decision": "block", "reason": formatted}, sys.stdout)
        sys.exit(0)
    else:
        # PostToolUse exit 2: stderr shown to user, tool already ran.
        # UserPromptSubmit exit 1: stderr shown to user, prompt proceeds.
        # (exit 2 would block the prompt, exit 0 is invisible to user)
        print(formatted, file=sys.stderr)
        sys.exit(2 if event == "PostToolUse" else 1)


# --- session listing ---

def resolve_index(index):
    """Resolve a session index (e.g. '4' or '#4') to (sid, sock_path).

    When multiple sessions share the same index (stale + fresh),
    returns the newest one by start time.
    """
    index = str(index).lstrip("#")
    best_sid, best_started = None, 0
    for sid, meta in list_sessions():
        if meta.get("index") == index:
            started = meta.get("started", 0)
            if started >= best_started:
                best_sid, best_started = sid, started
    if best_sid:
        return best_sid, str(IPC_DIR / f"{best_sid}.sock")
    return None, None


def list_sessions():
    """Return list of (sid, metadata_dict) for sessions with active sockets.

    Stale sockets (connection refused) are cleaned up automatically.
    """
    if not IPC_DIR.is_dir():
        return []
    sessions = []
    for sock_path in IPC_DIR.glob("*.sock"):
        sid = sock_path.stem
        # Probe the socket to check if the relay is alive
        import socket as _socket
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.connect(str(sock_path))
            s.close()
        except (ConnectionRefusedError, OSError):
            # Stale socket, clean up
            sock_path.unlink(missing_ok=True)
            sock_path.with_suffix(".json").unlink(missing_ok=True)
            continue
        meta_path = sock_path.with_suffix(".json")
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


# --- main ---

def main():
    if len(sys.argv) < 2:
        print("Usage: cave-send <target> <message>", file=sys.stderr)
        print("       cave-send hook", file=sys.stderr)
        print("       cave-send --list", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "hook":
        hook()
        return

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
        for sid, meta in list_sessions():
            if sid == my_sid:
                continue
            if meta.get("project") != my_project:
                continue
            try:
                send(sid, message)
            except (ValueError, OSError):
                pass
        return

    target = sys.argv[1]
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
    except OSError as e:
        print(f"Failed to send: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
