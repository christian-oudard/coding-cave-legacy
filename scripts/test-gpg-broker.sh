#!/usr/bin/env bash
# test-gpg-broker.sh: self-asserting test for the guest signing path.
#
# Exercises shim -> broker -> real gpg without a VM. In production the two
# halves are separated by vsock (guest TCP 7400 -> vsock 7400 -> host unix
# socket); here a socat bridge stands in for that hop, so everything except
# the vsock transport is the real code path.
#
# Needs a usable signing key. Inside a container cave that is the host's key
# via the forwarded gpg-agent, which is the same arrangement the broker itself
# relies on. Skips rather than fails where no key is available.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BROKER="$REPO/scripts/gpg-broker.py"
SHIM="$REPO/guest/cave-gpg.py"
PORT="${PORT:-17400}"
work="$(mktemp -d)"
sock="$work/broker.sock"
fail=0

cleanup() {
  [ -n "${bpid:-}" ] && kill "$bpid" 2>/dev/null
  [ -n "${spid:-}" ] && kill "$spid" 2>/dev/null
  rm -rf "$work"
}
trap cleanup EXIT

ok()   { echo "ok   $*"; }
bad()  { echo "FAIL $*"; fail=1; }

# What the broker will and will not put the user's key over. Ahead of the key
# check, because it needs no key: the payload rules are the half of this that
# can be exercised anywhere, including in a guest, which is where the key
# deliberately is not.
#
# The argv rules stop the guest signing *as* somebody else. These stop it
# signing *anything* else -- the payload used to reach gpg unexamined, so a
# valid signature from the user's key could be had over a release artifact, a
# mail message, a tag.
have_python=1
command -v python3 >/dev/null 2>&1 || {
  have_python=""
  echo "SKIP: python3 is not on PATH, so the payload rules cannot be read back"
}

payload_says() { # <label> <want: sign|refuse> <python bytes expr>
  [ -n "$have_python" ] || return 0
  local got
  got=$(python3 - "$BROKER" <<PY 2>/dev/null
import importlib.util, sys
spec = importlib.util.spec_from_file_location("b", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print("refuse" if m.validate_payload($3) else "sign")
PY
)
  [ "$got" = "$2" ] && ok "$1" || bad "$1 (want $2, got ${got:-<error>})"
}

commit='b"tree "+b"a"*40+b"\nauthor t <t@t> 1 +0000\ncommitter t <t@t> 1 +0000\n\nmsg\n"'
merge='b"tree "+b"a"*40+b"\nparent "+b"b"*40+b"\nparent "+b"c"*40+b"\nauthor t <t@t> 1 +0000\ncommitter t <t@t> 1 +0000\n\nmsg\n"'
tag='b"object "+b"c"*40+b"\ntype commit\ntag v1\ntagger t <t@t> 1 +0000\n\nrelease\n"'

payload_says "a commit is signed"                sign   "$commit"
payload_says "a merge commit is signed"          sign   "$merge"
payload_says "a tag is not"                      refuse "$tag"
payload_says "a mail message is not"             refuse 'b"From: a@b\nSubject: pay this\n\nregards\n"'
payload_says "an arbitrary artifact is not"      refuse 'bytes(range(256))'
payload_says "an empty payload is not"           refuse 'b""'
payload_says "a commit with no message is not"   refuse 'b"tree "+b"a"*40+b"\nauthor t <t@t> 1 +0000\ncommitter t <t@t> 1 +0000\n"'

if ! echo probe | gpg --batch --no-tty --armor --detach-sign >/dev/null 2>&1; then
  echo "SKIP: the rest needs a signing key, which is not in this environment"
  exit "$fail"
fi

python3 "$BROKER" "$sock" >"$work/broker.log" 2>&1 &
bpid=$!
socat "TCP-LISTEN:$PORT,bind=127.0.0.1,fork,reuseaddr" "UNIX-CONNECT:$sock" &
spid=$!
for _ in $(seq 50); do [ -S "$sock" ] && break; sleep 0.1; done
[ -S "$sock" ] || { echo "FAIL: broker never bound $sock"; exit 1; }

export CAVE_GPG_PORT="$PORT"
# The broker pins the key host-side, so this is only what git would pass; it
# still has to be a plausible key id, since the allowlist rejects bare words
# that are neither a hex id nor an address.
keyid=$(gpg --list-secret-keys --keyid-format=long 2>/dev/null \
  | awk '/^sec/{print $2}' | head -1 | cut -d/ -f2)

# 1. A signing request comes back as a signature the local gpg accepts.
echo "hello cave" > "$work/payload"
python3 "$SHIM" --status-fd=2 -bsau "$keyid" \
  < "$work/payload" > "$work/sig.asc" 2> "$work/status.txt"
if gpg --verify "$work/sig.asc" "$work/payload" >/dev/null 2>&1; then
  ok "broker returns a verifiable signature"
else
  bad "signature did not verify"; sed -n 1,5p "$work/status.txt"
fi

# 2. git's status protocol survives the hop. Without SIG_CREATED reaching the
#    status fd, git reports signing as failed even with a good signature.
grep -q '\[GNUPG:\] SIG_CREATED' "$work/status.txt" \
  && ok "SIG_CREATED reaches the status fd" \
  || bad "no SIG_CREATED in status output"

# 3. The real thing: git signs a commit through the shim.
export GNUPGHOME="${GNUPGHOME:-$HOME/.gnupg}"
gitdir="$work/repo"; mkdir -p "$gitdir"
git -C "$gitdir" init -q
git -C "$gitdir" config user.name t
git -C "$gitdir" config user.email t@t
git -C "$gitdir" config gpg.program "$SHIM"
git -C "$gitdir" config commit.gpgsign true
git -C "$gitdir" config user.signingkey "$(gpg --list-secret-keys --keyid-format=long 2>/dev/null \
  | awk '/^sec/{print $2}' | head -1 | cut -d/ -f2)"
if git -C "$gitdir" commit -q --allow-empty -m signed 2>"$work/commit.err"; then
  ok "git commit -S succeeds through the shim"
  git -C "$gitdir" log --format=%G? -1 | grep -qE '^[GUN]$' \
    && ok "commit carries a signature" \
    || bad "commit has no signature"
else
  bad "git commit through the shim failed"; sed -n 1,5p "$work/commit.err"
fi

# 4. The allowlist is what makes this safe: the guest must not be able to ask
#    for key material by smuggling arguments past the shim.
printf '{"argv": ["--export-secret-keys"]}\n' \
  | socat - "UNIX-CONNECT:$sock" 2>/dev/null | grep -q 'disallowed' \
  && ok "broker rejects --export-secret-keys" \
  || bad "broker did NOT reject --export-secret-keys"

printf '{"argv": ["--verify", "; rm -rf /"]}\n' \
  | socat - "UNIX-CONNECT:$sock" 2>/dev/null | grep -qE 'disallowed|not a signing' \
  && ok "broker rejects shell-ish operands" \
  || bad "broker did NOT reject a shell-ish operand"

# The broker rebuilds every request as --detach-sign, so a request that asks
# to verify must be refused rather than answered with a signature.
printf '{"argv": ["--verify", "--detach-sign"]}\n' \
  | socat - "UNIX-CONNECT:$sock" 2>/dev/null | grep -q 'disallowed' \
  && ok "broker rejects --verify" \
  || bad "broker signed a request that asked to verify"

[ "$fail" = 0 ] && echo PASS || echo FAIL
exit "$fail"
