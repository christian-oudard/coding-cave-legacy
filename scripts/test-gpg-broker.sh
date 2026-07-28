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

if ! echo probe | gpg --batch --no-tty --armor --detach-sign >/dev/null 2>&1; then
  echo "SKIP: no usable signing key in this environment"
  exit 0
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

[ "$fail" = 0 ] && echo PASS || echo FAIL
exit "$fail"
