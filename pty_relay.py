#!/usr/bin/env python3
"""PTY relay with Unix domain socket for message injection.

Sits between the terminal and a child process, forwarding all I/O
transparently. Accepts connections on a Unix domain socket; messages
received are injected into the child's PTY as if typed by the user.

Usage:
    pty-relay --sock /path/to/sock -- command [args...]

Protocol:
    Client connects to the UDS, sends the message as raw bytes,
    then closes (or shutdown(SHUT_WR)). The relay appends CR and
    writes to the child's PTY master.
"""
import argparse
import errno
import fcntl
import json
import os
import select
import signal
import socket
import struct
import sys
import termios
import tty


def _set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))


def _get_winsize(fd):
    s = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", s)
    return rows, cols


def _sender_tag():
    """Build sender tag from this relay's session identity."""
    idx = os.environ.get("CODING_CAVE_SESSION_INDEX", "")
    proj = os.environ.get("CODING_CAVE_PROJECT_PATH", "")
    proj = os.path.basename(proj) if proj else ""
    if proj and idx:
        return f"{proj}#{idx}:"
    if idx:
        return f"#{idx}:"
    return "unknown:"


def _is_local_peer(conn):
    """Check if the peer is in the same PID namespace via SO_PEERCRED."""
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("iII"))
        pid, _, _ = struct.unpack("iII", creds)
        return pid > 0
    except OSError:
        return False


def _forward_message(target_sid, tagged_message):
    """Forward a tagged message to the target session's socket."""
    ipc_dir = os.path.join(os.environ.get("HOME", os.path.expanduser("~")), "ipc")
    target_sock = os.path.join(ipc_dir, f"{target_sid}.sock")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(target_sock)
        s.sendall(tagged_message.encode())
        s.shutdown(socket.SHUT_WR)
        s.close()
    except OSError:
        pass


def _handle_message(buf, master_fd, conn=None):
    """Process an incoming message.

    FWD messages from local peers (same PID namespace) are tagged with
    verified sender identity and forwarded to the target session.
    All other messages are injected into the PTY.
    """
    raw = bytes(buf)
    if raw.startswith(b"FWD ") and conn is not None and _is_local_peer(conn):
        # Forward request from a local process (same session)
        newline = raw.find(b"\n", 4)
        if newline == -1:
            return
        target_sid = raw[4:newline].decode()
        message = raw[newline + 1:].decode()
        tagged = f"{_sender_tag()} {message}"
        _forward_message(target_sid, tagged)
    else:
        os.write(master_fd, raw + b"\r")


def relay(child_argv, sock_path):
    """Run child in a PTY, forward I/O, accept UDS message injection.

    Returns the child's exit code.
    """
    master_fd, slave_fd = os.openpty()
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    stdin_is_tty = os.isatty(stdin_fd)

    if stdin_is_tty:
        try:
            rows, cols = _get_winsize(stdin_fd)
            _set_winsize(slave_fd, rows, cols)
        except OSError:
            pass

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvp(child_argv[0], child_argv)
        os._exit(127)

    os.close(slave_fd)

    # UDS listener
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(sock_path), exist_ok=True)
    sock.bind(sock_path)
    sock.listen(5)
    sock.setblocking(False)

    old_attrs = None
    if stdin_is_tty:
        old_attrs = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    def on_winch(_sig, _frame):
        try:
            rows, cols = _get_winsize(stdin_fd)
            _set_winsize(master_fd, rows, cols)
            os.kill(pid, signal.SIGWINCH)
        except OSError:
            pass

    prev_winch = signal.signal(signal.SIGWINCH, on_winch)

    # UDS client tracking: fd -> (socket_obj, buffer)
    clients = {}
    exit_code = 1

    try:
        while True:
            rfds = [stdin_fd, master_fd, sock.fileno()]
            rfds.extend(clients.keys())
            try:
                readable, _, _ = select.select(rfds, [], [], 0.5)
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                raise

            for fd in readable:
                if fd == stdin_fd:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        data = b""
                    if data:
                        os.write(master_fd, data)

                elif fd == master_fd:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        data = b""
                    if data:
                        os.write(stdout_fd, data)

                elif fd == sock.fileno():
                    try:
                        conn, _ = sock.accept()
                        conn.setblocking(False)
                        clients[conn.fileno()] = (conn, bytearray())
                    except OSError:
                        pass

                elif fd in clients:
                    conn, buf = clients[fd]
                    try:
                        data = conn.recv(4096)
                    except OSError:
                        data = b""
                    if data:
                        buf.extend(data)
                    else:
                        # Client closed. Process accumulated message.
                        if buf:
                            _handle_message(buf, master_fd, conn)
                        conn.close()
                        del clients[fd]

            # Reap child (non-blocking)
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if wpid != 0:
                if os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    exit_code = 128 + os.WTERMSIG(status)
                # Drain remaining PTY output
                while True:
                    try:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if not r:
                            break
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        os.write(stdout_fd, data)
                    except OSError:
                        break
                break

    finally:
        signal.signal(signal.SIGWINCH, prev_winch)
        if old_attrs is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
            except termios.error:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        for conn, _ in clients.values():
            conn.close()
        sock.close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

    return exit_code


def _meta_path(sock_path):
    """Sidecar metadata file path: same stem, .json extension."""
    base = sock_path.rsplit(".", 1)[0] if "." in sock_path else sock_path
    return base + ".json"


def _write_meta(sock_path):
    """Write session metadata from environment variables."""
    import json, time
    meta = {
        "sid": os.environ.get("CODING_CAVE_SESSION_ID", ""),
        "index": os.environ.get("CODING_CAVE_SESSION_INDEX", ""),
        "project": os.path.basename(
            os.environ.get("CODING_CAVE_PROJECT_PATH", "")),
        "started": time.time(),
    }
    path = _meta_path(sock_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(meta, f)


def _remove_meta(sock_path):
    try:
        os.unlink(_meta_path(sock_path))
    except FileNotFoundError:
        pass


def _default_sock_path():
    """Derive socket path from CODING_CAVE_SESSION_ID."""
    sid = os.environ.get("CODING_CAVE_SESSION_ID")
    if sid:
        return os.path.join(os.path.expanduser("~"), "ipc", f"{sid}.sock")
    return None


def main():
    parser = argparse.ArgumentParser(description="PTY relay with UDS injection")
    parser.add_argument("--sock", default=None,
                        help="Unix socket path (default: ~/ipc/$CODING_CAVE_SESSION_ID.sock)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("no command specified")

    sock_path = args.sock or _default_sock_path()
    if not sock_path:
        parser.error("--sock required (CODING_CAVE_SESSION_ID not set)")

    _write_meta(sock_path)
    try:
        sys.exit(relay(cmd, sock_path))
    finally:
        _remove_meta(sock_path)


if __name__ == "__main__":
    main()
