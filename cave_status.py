#!/usr/bin/env python3
"""Update the current session's metadata via the PTY relay.

Usage:
    cave-status "working on comms improvements"
    cave-status --clear
    cave-status            # show current status

Sends a meta_update frame to the local session's PTY relay socket,
which updates the sidecar JSON without injecting into the PTY.
"""
import json
import os
import socket
import sys
from pathlib import Path


IPC_DIR = Path.home() / "ipc"


def _sock_path():
    sid = os.environ.get("CODING_CAVE_SESSION_ID")
    if not sid:
        return None
    return str(IPC_DIR / f"{sid}.sock")


def _meta_path():
    sid = os.environ.get("CODING_CAVE_SESSION_ID")
    if not sid:
        return None
    return str(IPC_DIR / f"{sid}.json")


def _send_meta_update(sock_path, fields):
    """Send a meta_update frame to the relay."""
    frame = json.dumps({"type": "meta_update", "fields": fields})
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall(frame.encode())
    s.shutdown(socket.SHUT_WR)
    s.close()


def _show_status():
    """Print current session's metadata."""
    path = _meta_path()
    if not path or not Path(path).exists():
        print("No session metadata found.", file=sys.stderr)
        return
    meta = json.loads(Path(path).read_text())
    task = meta.get("task", "")
    idx = meta.get("index", "?")
    proj = meta.get("project", "?")
    print(f"Session #{idx} ({proj})")
    if task:
        print(f"  Status: {task}")
    else:
        print("  Status: (none)")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        _show_status()
        return

    sock = _sock_path()
    if not sock:
        print("Error: CODING_CAVE_SESSION_ID not set.", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--clear":
        _send_meta_update(sock, {"task": ""})
        print("Status cleared.")
    else:
        task = " ".join(sys.argv[1:])
        _send_meta_update(sock, {"task": task})
        print(f"Status set: {task}")


if __name__ == "__main__":
    main()
