#!/usr/bin/env python3
"""ssh ProxyCommand for reaching the guest over Cloud Hypervisor's vsock.

CHV's hybrid vsock exposes host-initiated connections on one unix socket: you
connect, send "CONNECT <port>", read back a single "OK <cid>" line, and from
then on the stream is the guest's TCP port.

This lives in its own file rather than a `python3 - <<HEREDOC` inside vmcav
for a load-bearing reason: with `python3 -`, the heredoc *is* stdin, so the
relay reads EOF immediately and ssh's traffic never reaches the socket.

Becomes `cave-vm vsock-connect` once the Go CLI exists.
"""

import os
import socket
import sys
import threading


def pump(read, write, done=None):
    try:
        while True:
            d = read()
            if not d:
                break
            write(d)
    except OSError:
        pass
    finally:
        if done:
            try:
                done()
            except OSError:
                pass


def main():
    path, port = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 22
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    s.sendall(b"CONNECT %d\n" % port)
    # One byte at a time: anything over-read here would be guest payload.
    buf = b""
    while not buf.endswith(b"\n"):
        ch = s.recv(1)
        if not ch:
            sys.exit("vsock: connection closed during handshake")
        buf += ch
    if not buf.startswith(b"OK"):
        sys.exit("vsock: handshake failed: %r" % buf)

    # Two blocking pumps. ssh needs both directions live for the whole
    # session, so the reader runs in a thread and the writer owns the main
    # thread; a selector over mixed socket and file objects is where this
    # went wrong before.
    threading.Thread(
        target=pump,
        args=(lambda: os.read(sys.stdin.fileno(), 65536), s.sendall),
        kwargs={"done": lambda: s.shutdown(socket.SHUT_WR)},
        daemon=True,
    ).start()

    def out(d):
        sys.stdout.buffer.write(d)
        sys.stdout.buffer.flush()

    pump(lambda: s.recv(65536), out)


if __name__ == "__main__":
    main()
