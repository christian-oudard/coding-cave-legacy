#!/usr/bin/env python3
"""Guest-side gpg shim: git's gpg.program inside the cave VM.

Signing happens on the host, because that is where the secret key is and the
VM boundary is exactly what stops the key coming here. Everything else (most
importantly verification, which only needs the public keyring on the seed) is
served by the real gpg locally, so `git log --show-signature` still works
without a round trip.

Signing requests go to the broker over the vsock service plane; the guest-side
socat unit re-exposes it on localhost so nothing here knows about vsock. The
broker validates the arguments and pins the key, so this shim is a courier,
not a policy point: it must not be trusted to restrict anything.
"""

import json
import os
import socket
import sys

BROKER = ("127.0.0.1", int(os.environ.get("CAVE_GPG_PORT", "7400")))
REAL_GPG = os.environ.get("CAVE_REAL_GPG", "gpg")
SIGN_FLAGS = {"-bsau", "-b", "--detach-sign", "-s", "--sign"}


def is_signing(argv):
    return bool(SIGN_FLAGS & set(argv))


def main():
    argv = sys.argv[1:]
    if not is_signing(argv):
        # Verification and key listing stay local against the seeded pubring.
        os.execvp(REAL_GPG, [REAL_GPG] + argv)

    payload = sys.stdin.buffer.read()
    try:
        s = socket.create_connection(BROKER, timeout=120)
    except OSError as e:
        sys.stderr.write(f"cave-gpg: signing broker unreachable: {e}\n")
        return 2
    with s:
        header = json.dumps({"argv": argv, "len": len(payload)})
        s.sendall(header.encode() + b"\n" + payload)
        f = s.makefile("rb")
        head = f.readline()
        if not head:
            sys.stderr.write("cave-gpg: broker closed without responding\n")
            return 2
        meta = json.loads(head)
        out = f.read(meta["stdout"])
        err = f.read(meta["stderr"])

    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    # git reads gpg's status protocol from the fd it passed in --status-fd,
    # which is where SIG_CREATED has to land or it reports signing as failed.
    fd = 2
    for a in argv:
        if a.startswith("--status-fd"):
            fd = int(a.split("=", 1)[1]) if "=" in a else 2
    try:
        os.write(fd, err)
    except OSError:
        sys.stderr.buffer.write(err)
    return meta["exit"]


if __name__ == "__main__":
    sys.exit(main())
