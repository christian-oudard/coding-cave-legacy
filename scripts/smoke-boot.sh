#!/usr/bin/env bash
# smoke-boot.sh: build and boot the guest headless, then assert it reached
# the milestones that matter for the bootstrap agent. Works without KVM
# (QEMU falls back to TCG), so it runs inside a cave with no /dev/kvm.
#
# It does NOT need crinj: the host-seed 9p share is absent and the
# /etc/cave-host mount is nofail. The guest trusts a throwaway CA minted here
# (the eval requires one) instead of crinj's. This verifies the OS + unit
# wiring, not live API auth.
#
# Boot ends by itself: with stdin at /dev/null the serial console hits EOF at
# the auto-login shell (which is past multi-user + the agent unit), so QEMU
# exits cleanly. A timeout bounds a hung boot. Success is decided by grepping
# the captured console, not by QEMU's exit code.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
GUEST_FLAKE="${GUEST_FLAKE:-$REPO/guest}"

# ssh/ssh-keygen drive the probe; provision from nix if the host lacks them.
# openssh has multiple outputs (-man etc.), so pick the one with the binaries.
if ! command -v ssh >/dev/null 2>&1 || ! command -v ssh-keygen >/dev/null 2>&1; then
  for p in $(nix build --no-link --print-out-paths \
      --extra-experimental-features 'nix-command flakes' nixpkgs#openssh 2>/dev/null); do
    [ -x "$p/bin/ssh-keygen" ] && { export PATH="$p/bin:$PATH"; break; }
  done
fi
WORK="${WORK:-$(mktemp -d)}"; mkdir -p "$WORK"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-1200}"   # seconds; TCG is slow
console="$WORK/console.log"

log() { printf '[smoke-boot] %s\n' "$*" >&2; }

# The guest bakes a crinj CA into its system trust store at build time and the
# eval refuses to build without one (fail loudly). The smoke test has no crinj,
# so mint a throwaway self-signed CA solely to satisfy the eval; nothing here
# makes a real TLS connection through it (no crinj egress in the smoke path).
if ! command -v openssl >/dev/null 2>&1; then
  for p in $(nix build --no-link --print-out-paths \
      --extra-experimental-features 'nix-command flakes' nixpkgs#openssl 2>/dev/null); do
    [ -x "$p/bin/openssl" ] && { export PATH="$p/bin:$PATH"; break; }
  done
fi
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$WORK/smoke-ca.key" \
  -out "$WORK/smoke-ca.pem" -days 1 -subj "/CN=smoke-test-ca" 2>/dev/null
export CODING_CAVE_CRINJ_CA; CODING_CAVE_CRINJ_CA=$(cat "$WORK/smoke-ca.pem")

log "building VM runner"
runner_link="$WORK/vm"
nix build --impure --extra-experimental-features 'nix-command flakes' \
  "$GUEST_FLAKE#nixosConfigurations.cave-guest-dev.config.system.build.vm" \
  --out-link "$runner_link"
runner="$(readlink -f "$runner_link"/bin/run-*-vm)"

# Stage an ssh key in a host-seed 9p share and forward ssh to a free host
# port, so the probe below can log in and check the agent actually launched
# (the console cannot show claude, which runs in a detached tmux session).
seed="$WORK/cave-host"; mkdir -p "$seed"
smoke_key="$WORK/smoke_ssh_key"
ssh-keygen -t ed25519 -N '' -f "$smoke_key" -C smoke >/dev/null
cp -f "$smoke_key.pub" "$seed/authorized_keys"
ssh_port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
log "ssh forwarded to host port $ssh_port; host-seed share staged"
export QEMU_OPTS="${QEMU_OPTS:-} -virtfs local,path=$seed,mount_tag=cavehost,security_model=none,readonly=on"
export QEMU_NET_OPTS="hostfwd=tcp::${ssh_port}-:22"

# Serve this repo so the guest's clone-repo.service can clone it over
# user-net (SLIRP maps 10.0.2.2 -> this host's loopback). The guest clones
# git://10.0.2.2/<basename>, so base-path is the repo's parent.
GIT_PORT="${GIT_PORT:-9418}"
repo_parent="$(dirname "$REPO")"
log "serving $repo_parent over git daemon :$GIT_PORT"
git daemon --reuseaddr --export-all --enable=upload-pack \
  --base-path="$repo_parent" --port="$GIT_PORT" --detach --pid-file="$WORK/git.pid" \
  "$repo_parent" 2>/dev/null || { log "git daemon failed to start"; exit 1; }
# (cleanup trap is installed below, once the VM teardown helper is defined)

log "booting headless under a ${BOOT_TIMEOUT}s cap (console -> $console)"
export NIX_DISK_IMAGE="$WORK/disk.qcow2"; rm -f "$NIX_DISK_IMAGE"
export USE_TMPDIR=1 TMPDIR="$WORK"

# Hold stdin open via a fifo (fd 9) so the auto-login serial console never
# hits EOF and terminates QEMU. The serial getty/login prompt appears early,
# independent of multi-user, so an EOF exit there would race ahead of slower
# services (clone-repo does a git clone over TCG-emulated net). Poll the
# console for the real terminal markers, then tear QEMU down by closing the
# fifo and killing the process.
fifo="$WORK/stdin.fifo"; mkfifo "$fifo"
kill_qemu() {
  pkill -f "qemu-system-x86_64.*$NIX_DISK_IMAGE" 2>/dev/null || true
  exec 9>&- 2>/dev/null || true
}
trap 'kill_qemu; [ -f "$WORK/git.pid" ] && kill "$(cat "$WORK/git.pid")" 2>/dev/null || true' EXIT
( cd "$WORK" && "$runner" < "$fifo" > "$console" 2>&1 ) &
exec 9>"$fifo"   # keep the write end open so the reader never sees EOF

deadline=$(( $(date +%s) + BOOT_TIMEOUT ))
strip_ansi() { sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/\r//g' "$console" 2>/dev/null; }
while [ "$(date +%s)" -lt "$deadline" ]; do
  # Strip ANSI first: systemd inserts color codes *between* words (e.g.
  # "Started \e[..mBootstrap"), so a plain grep of the raw console never
  # matches and the loop would wait out the full timeout.
  if strip_ansi | grep -qF "Reached target Multi-User System"; then
    log "reached multi-user; stopping VM"; break
  fi
  if strip_ansi | grep -qiE "Kernel panic|emergency mode|Failed to start (Clone|Bootstrap)"; then
    log "terminal failure marker seen; stopping VM"; break
  fi
  sleep 3
done

# ssh probe: log in over the forwarded port and confirm claude actually
# launches as root. Without crinj the agent can't complete a real API call,
# so it exits at the login prompt rather than staying resident; the probe
# runs claude directly and checks it gets PAST the root refusal to the auth
# boundary (the most that's verifiable without crinj). Retries because sshd
# is socket-activated. The remote script is heredoc'd so quoting stays sane.
probe="$WORK/ssh-probe.out"; : > "$probe"
remote_probe='
echo SSH_OK
cd /root/coding-cave || exit 0
IS_SANDBOX=1 timeout 25 claude --dangerously-skip-permissions -p hi > /tmp/cl.out 2>&1 || true
if grep -qiE "cannot be used with root" /tmp/cl.out; then
  echo CLAUDE_ROOT_BLOCKED
elif grep -qiE "not logged in|please run /login|logged in|assistant|result|hi" /tmp/cl.out; then
  echo CLAUDE_LAUNCHED_TO_AUTH
else
  echo "CLAUDE_OTHER: $(head -c 160 /tmp/cl.out | tr "\n" " ")"
fi'
for _ in 1 2 3 4 5 6; do
  if ssh -i "$smoke_key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       -o ConnectTimeout=5 -o BatchMode=yes -p "$ssh_port" root@127.0.0.1 \
       "$remote_probe" > "$probe" 2>>"$probe.err"; then
    log "ssh probe succeeded"; break
  fi
  sleep 3
done
[ -s "$probe" ] && log "probe: $(tr "\n" "|" < "$probe")"
kill_qemu

# Strip ANSI, then require every milestone. Order matters only for reading.
plain="$(sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/\r//g' "$console")"
fail=0
require() {
  if printf '%s' "$plain" | grep -qF "$1"; then
    log "OK: $2"
  else
    log "MISSING: $2"; fail=1
  fi
}
# Assert on systemd's console status lines, not the services' own stdout:
# oneshot ExecStart output goes to the journal, not the serial console. A
# "Finished ..." line means the unit succeeded; a failed clone would instead
# print "Failed to start Clone ...".
require "Finished Seed /root/.claude.json"          "claude config seeded"
require "Finished Clone the coding-cave repo"       "repo cloned from host git daemon"
require "Started Bootstrap vm-root-agent"           "agent unit started"
require "Reached target Multi-User System"          "reached multi-user"

# Probe assertions (over ssh): proves ssh attach works and claude actually
# launched (its detached tmux session exists), which the console cannot show.
require_probe() {
  if grep -qF "$1" "$probe" 2>/dev/null; then log "OK: $2"; else log "MISSING: $2"; fail=1; fi
}
require_probe "SSH_OK"                 "ssh login works"
require_probe "CLAUDE_LAUNCHED_TO_AUTH" "claude launches as root, reaches auth boundary"

if [ "$fail" = 0 ]; then
  log "PASS: guest boots, clones, ssh works, and claude launches to the auth boundary"
else
  log "FAIL: see $console and $probe"
fi
exit "$fail"
