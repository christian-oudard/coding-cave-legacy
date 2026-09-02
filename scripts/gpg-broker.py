#!/usr/bin/env python3
"""Host-side gpg signing broker for the cave guest.

The guest must sign its commits (unsigned commits were a disaster for
container caves, which is why they forward the gpg agent socket). That
forwarding cannot cross the VM boundary: a unix socket is not bind-mountable
into a VM, and re-exposing gpg-agent on a TCP port would hand signing to every
process on the host, which is strictly worse than the 0600 socket it replaces.

So the agent socket is never exposed. This broker accepts a signing *request*
over one enumerated vsock port and runs gpg itself, on the host, where the
secret key and pinentry already live. The guest gets one operation and no
argv: `--export-secret-keys` is not reachable because the guest never supplies
the arguments that would ask for it.

Wire protocol, one request per connection:

    -> {"argv": [...], "len": N}\n  followed by exactly N bytes to sign
    <- {"exit": int, "stdout": int, "stderr": int}\n  then stdout, then stderr

argv is validated against ALLOWED below and otherwise rejected. stdout carries
the armored signature and stderr the real gpg status lines, so git sees byte
for byte what a local gpg would have produced, including SIG_CREATED.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading

# Only the shapes git uses to make a detached OpenPGP signature. Anything that
# could export, decrypt, edit, or delete key material is absent by
# construction: this is an allowlist, not a denylist, so a gpg flag we have
# never heard of is rejected rather than passed through.
#
# The verification flags git uses (--verify, --keyid-format,
# --enable-progress-filter) are deliberately absent. Verification never comes
# here: the guest runs it locally against the seeded public keyring, and it
# needs no secret. Allowing them was worse than useless, because the
# invocation below is rebuilt as --detach-sign whatever arrives, so a request
# that said --verify would have been answered with a signature.
ALLOWED = {
    "--status-fd", "--detach-sign", "--armor", "--local-user", "--sign",
    "-b", "-s", "-a", "-u", "-bsau", "--batch", "--no-tty", "--textmode",
}
KEYISH = re.compile(r"\A[0-9A-Fa-f]{8,40}\Z|\A[^\s<>|;&$`]+@[^\s<>|;&$`]+\Z")

# The most this will read for one request. A commit object is a few hundred
# bytes; one with many parents and a long message is still kilobytes, so a
# mebibyte is three orders of magnitude of headroom.
#
# The cap is not about commit sizes. `len` is a number the guest chooses and
# the payload was read before anything about it was checked, so the guest
# decided how much the host allocated. Measured against this broker: a
# declared 256 MiB took it from 14 MB resident to 166 MB and was still
# climbing. Everything that makes this channel narrow -- one operation, an
# argv allowlist, a pinned key -- was on the far side of that read.
MAX_PAYLOAD = 1 << 20

# Nothing in one request should take long. Without this a connection could
# open, say nothing, and hold a thread for as long as it liked: a cost the
# guest sets and the host pays. Overridable so a test does not have to wait
# out the real one.
TIMEOUT = float(os.environ.get("CAVE_GPG_BROKER_TIMEOUT", "30"))


# What a git commit object looks like on its way to a signer: the headers git
# writes, in the order it writes them, before the blank line that starts the
# message. Sha lengths cover sha1 and sha256 repositories.
COMMIT_OBJECT = re.compile(
    rb"\Atree [0-9a-f]{40,64}\n"
    rb"(?:parent [0-9a-f]{40,64}\n)*"
    rb"author [^<>\n]*<[^<>\n]*> \d+ [+-]\d{4}\n"
    rb"committer [^<>\n]*<[^<>\n]*> \d+ [+-]\d{4}\n"
)


def validate_payload(payload):
    """Reject anything that is not a git commit object.

    The argv checks above stop the guest signing *as* somebody else or
    exporting key material. They say nothing about what gets signed, and the
    payload was handed to gpg unexamined -- so the guest could obtain a valid
    detached signature from the user's key over any bytes at all: a signed
    tag, a release artifact, a mail message.

    Part of this is inherent and stays: an agent that signs its own commits
    has to be trusted with its own commits, and nothing here can tell a
    commit worth making from one that is not. What is not inherent is that it
    could sign things which are not commits, and that is what this closes.

    Deliberately structural, and not the check the red-team note proposed.
    That note suggested requiring the commit's tree and parents to exist in
    the served repository, which cannot work: git signs a commit object before
    it writes it, and pushes only afterwards, so at signing time the tree
    exists in the guest's repository and nowhere else. Requiring it here would
    reject every legitimate commit. Verified by capturing what git hands a
    signer.

    Tags are refused along with everything else, which costs nothing: the
    pre-receive guard refuses to accept a tag from an agent anyway, so a
    signed one could never be published.
    """
    if not payload:
        return "nothing to sign"
    if not COMMIT_OBJECT.match(payload):
        return "payload is not a git commit object"
    if b"\n\n" not in payload:
        return "commit object has no message"
    return None


def validate(argv):
    """Reject anything that is not a recognised signing invocation."""
    for a in argv:
        if a.startswith("-"):
            # --status-fd=2 and --local-user=KEY arrive glued together.
            flag = a.split("=", 1)[0]
            if flag not in ALLOWED:
                return f"disallowed argument: {a}"
        elif not (a.isdigit() or KEYISH.match(a)):
            # Bare words are only ever a key id or a status-fd number.
            return f"disallowed operand: {a}"
    if not ({"-bsau", "-b", "--detach-sign", "-s", "--sign"} & set(argv)):
        return "not a signing request"
    return None


def handle(conn, gpg, keyid):
    with conn:
        # Applies to every read below, so a client that stops talking costs a
        # thread for this long rather than for as long as it likes.
        conn.settimeout(TIMEOUT)
        try:
            return serve(conn, gpg, keyid)
        except (socket.timeout, TimeoutError) as e:
            sys.stderr.write(f"[gpg-broker] dropped a stalled connection: {e}\n")
        except OSError as e:
            sys.stderr.write(f"[gpg-broker] connection failed: {e}\n")


def serve(conn, gpg, keyid):
        f = conn.makefile("rb")
        header = f.readline()
        if not header:
            return
        try:
            req = json.loads(header)
            argv, length = req["argv"], req.get("len", 0)
        except Exception as e:
            return respond(conn, 2, b"", f"bad request: {e}\n".encode())
        bad = validate(argv)
        if bad:
            sys.stderr.write(f"[gpg-broker] rejected: {bad}\n")
            return respond(conn, 2, b"", f"gpg-broker: {bad}\n".encode())
        # Before the read, not after it: this is the check that decides how
        # much of the host's memory the guest is allowed to name. A length
        # that is not a plain number in range is refused rather than coerced,
        # since anything else here is a client that is not the shim.
        if not isinstance(length, int) or isinstance(length, bool) \
                or not 0 <= length <= MAX_PAYLOAD:
            sys.stderr.write(f"[gpg-broker] rejected: length {length!r}\n")
            return respond(conn, 2, b"",
                           f"gpg-broker: {length!r} is not a signable length\n".encode())
        # Read exactly the declared length. Relying on EOF would need the
        # client to half-close, and that does not survive the socat/vsock hop:
        # the shutdown propagates as a full close and the reply is lost.
        payload = f.read(length) if length else b""
        bad = validate_payload(payload)
        if bad:
            sys.stderr.write(f"[gpg-broker] rejected: {bad}\n")
            return respond(conn, 2, b"", f"gpg-broker: {bad}\n".encode())

        # Rebuild the invocation host-side. The guest's argv is used only as a
        # validated hint; the key is pinned here so a guest cannot sign as
        # someone else even with an otherwise-legal request.
        cmd = [gpg, "--batch", "--no-tty", "--armor", "--detach-sign",
               "--status-fd=2"]
        if keyid:
            cmd += ["--local-user", keyid]
        p = subprocess.run(cmd, input=payload, capture_output=True)
        respond(conn, p.returncode, p.stdout, p.stderr)


def respond(conn, code, out, err):
    head = json.dumps({"exit": code, "stdout": len(out), "stderr": len(err)})
    conn.sendall(head.encode() + b"\n" + out + err)


def main():
    path = sys.argv[1]
    gpg = "gpg"
    keyid = os.environ.get("CAVE_SIGNING_KEY", "")
    if os.path.exists(path):
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o600)
    s.listen(8)
    sys.stderr.write(f"[gpg-broker] listening on {path} (key {keyid or 'default'})\n")
    while True:
        conn, _ = s.accept()
        threading.Thread(target=handle, args=(conn, gpg, keyid), daemon=True).start()


if __name__ == "__main__":
    main()
