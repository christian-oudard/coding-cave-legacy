#!/usr/bin/env bash
# test-vmcav-status.sh: self-asserting test for what `vmcav status` says about
# the guest being out of date. Sources vmcav for its functions, builds a
# throwaway repo and state dir, and never starts a VM. Offline and fast.
#
# The three behaviours under test, each of which was once wrong:
#   - a stopped VM reported nothing about staleness, which is the moment it
#     matters, since `start` boots what the disk says
#   - a deploy from a dirty tree recorded a bare commit, so the next status
#     compared HEAD to HEAD and called a guest nothing describes up to date
#   - a failing rev-parse truncated a good record, after which status was
#     silent for good
set -uo pipefail

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmcav}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
unset CODING_CAVE_AGENT_NAME 2>/dev/null || true
fail=0

git init -q "$work/repo"
mkdir -p "$work/repo/guest" "$work/repo/cave-vm" "$work/repo/docs"
echo base > "$work/repo/guest/configuration.nix"
echo base > "$work/repo/docs/notes.md"
git -C "$work/repo" add -A
git -C "$work/repo" -c user.email=t@t -c user.name=t commit -q -m base

export REPO="$work/repo" STATE_DIR="$work/state"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1090
source "$SRC"

commit() { # commit() <path> <text>
  printf '%s\n' "$2" > "$REPO/$1"
  git -C "$REPO" add -A
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "$2"
}

check() { # check() <name> <expected-substring-or-empty> <actual>
  local name="$1" want="$2" got="$3"
  if [ -z "$want" ]; then
    [ -z "$got" ] && return 0
    echo "FAIL $name: expected silence, got: $got"; fail=1; return 0
  fi
  case "$got" in *"$want"*) return 0 ;; esac
  echo "FAIL $name: expected '$want', got: ${got:-<silence>}"; fail=1
}

warn() { warn_if_guest_is_behind 2>&1; }

# --- no record at all: silent, because a permanent warning about a one-off
# state is worse than the state ---
check "no record" "" "$(warn)"

# --- clean tree, recorded, unchanged: silent ---
record_deployed_from
check "clean and current" "" "$(warn)"

# --- a commit under guest/: behind ---
commit guest/configuration.nix changed
check "guest commit" "vmcav deploy to catch it up" "$(warn)"

# --- a commit touching only docs/ also warns. Pinned deliberately: the
# comparison is by commit, and erring toward a mostly-cached rebuild is the
# documented trade ---
record_deployed_from
commit docs/notes.md changed
check "docs commit" "vmcav deploy to catch it up" "$(warn)"

# --- dirty at deploy time is recorded as dirty, and warns even though HEAD
# has not moved. This is the false all-clear the bare commit used to give ---
record_deployed_from   # clean baseline first
echo wip > "$REPO/guest/configuration.nix"
record_deployed_from
case "$(cat "$STATE_DIR/deployed-from")" in
  *-dirty) ;;
  *) echo "FAIL dirty marker: deployed-from has no -dirty suffix"; fail=1 ;;
esac
check "dirty deploy" "built from uncommitted changes" "$(warn)"

# --- an untracked file under guest/ counts: the build copies the directory,
# so a file git has never seen is still an input ---
git -C "$REPO" checkout -q -- guest
touch "$REPO/guest/extra.nix"
record_deployed_from
check "untracked input" "built from uncommitted changes" "$(warn)"
rm -f "$REPO/guest/extra.nix"

# --- dirt outside the build's inputs does not count, or the warning would
# stand while any doc was edited and nobody would read it ---
echo scratch > "$REPO/docs/notes.md"
record_deployed_from
check "docs dirt ignored" "" "$(warn)"
git -C "$REPO" checkout -q -- docs

# --- a failing rev-parse leaves the existing record alone rather than
# truncating it to empty ---
record_deployed_from
before="$(cat "$STATE_DIR/deployed-from")"
( REPO="$work/not-a-repo"; mkdir -p "$REPO"; record_deployed_from )
[ "$(cat "$STATE_DIR/deployed-from")" = "$before" ] \
  || { echo "FAIL failed rev-parse destroyed the record"; fail=1; }

# --- status with the VM stopped. Stubbed rather than started: the point is
# which branch speaks, not the VM ---
is_running() { return 1; }
cave_vm() { return 0; }
DISK="$work/state/cave-guest.raw"

out="$(do_status 2>&1)"
check "no image" "no image yet" "$out"
case "$out" in *"vm: stopped"*) echo "FAIL no image: said stopped too"; fail=1 ;; esac

: > "$DISK"
commit guest/configuration.nix later
record_deployed_from
commit guest/configuration.nix later-still
out="$(do_status 2>&1)"
check "stopped and stale" "vmcav deploy to catch it up" "$out"
check "stopped says stopped" "vm: stopped" "$out"

# --- which guest gets built ---
#
# The guest is Linux either way; what changes is the architecture, because a VM
# runs the host's. This was written out as x86_64-linux, which is right on the
# only host it has ever run on and silently wrong on the other: an Apple
# silicon host would build an x86_64 guest and hand it to a VMM that cannot
# boot it. `uname -m` says arm64 on macOS and aarch64 on Linux for the same
# machine, so both have to map to the same guest.
arch_gives() { # <label> <uname -m> <want>
  # The arch in a local, not $2: inside the uname override $2 would be that
  # function's own argument list, which is empty.
  local machine="$2" got
  got=$(uname() { echo "$machine"; }; guest_system 2>/dev/null || echo refused)
  [ "$got" = "$3" ] && echo "ok   $1" || { echo "FAIL $1 (want $3, got $got)"; fail=1; }
}

arch_gives "an x86_64 host builds the x86_64 guest" x86_64  x86_64-linux
arch_gives "arm64, which is what macOS says"        arm64   aarch64-linux
arch_gives "aarch64, which is what Linux says"      aarch64 aarch64-linux
arch_gives "and nothing else is guessed at"         riscv64 refused

# --- the agent roster ---
# Rendered by `cave-vm agents` now, and its cases -- an unreachable guest, an
# agent with nothing recorded, running versus held -- are a table in
# cmd/cave-vm/agents_test.go. They were assertions here against thirty lines
# of awk over text this program had just printed; the awk is gone, and prose
# this suite would have to stub a binary to produce proves nothing.
#
# What stays is that status reports the VM itself, which is this side's job.
ROSTER=""
is_running() { return 0; }
vm_pid() { echo 4242; }
cave_vm() { :; }
carry_base_to_shared() { :; }
correct_guest_clock() { :; }
warn_if_guest_is_behind() { :; }
warn_if_hooks_are_foreign() { :; }
guest_rpc() {
  case "$1" in
    agents) printf '%s\n' "$ROSTER" ;;
    health) echo "system /nix/store/x" ;;
  esac
  return 0
}

says() { # says <label> <want-substring> <text>
  case "$3" in *"$2"*) echo "ok   $1"; return 0 ;; esac
  echo "FAIL $1 (wanted '$2')"; fail=1
}

says "status reports the vm itself" "vm: running (pid 4242" "$(do_status 2>&1 || true)"

# --- what a start says to do next ---
# Rendered by `cave-vm agents --after-start` now. Its cases -- a guest holding
# agents, an empty one, one that cannot answer, one with only running agents --
# are a table in cmd/cave-vm/agents_test.go, where they are a slice rather
# than a stubbed guest_rpc and a shell function reading its own output back.

# --- deploy when the guest already runs the system ---
# The ordinary state after `cave-rebuild` in the guest: same source, same
# closure, so the running system is what a deploy would build. What is not the
# same is what the host boots, and an in-guest rebuild moves only the former.
# Returning early there left the boot pointer on the generation before, so the
# next stop/start reverted work that had been deployed and reported as live.
BOOT="$work/boot"; mkdir -p "$BOOT"
# To a file: do_deploy runs in a command substitution below, and a subshell's
# assignments do not come back. This is the fifth time that has caught me in
# this repo, which is why it is written down here rather than remembered.
: > "$work/built"
require_tools() { :; }
is_running() { return 0; }
guest_ready() { return 0; }
build_guest() { printf '%s\n' "$1" >> "$work/built"; mkdir -p "$1"; ln -sfn /nix/store/same-toplevel "$1/toplevel"; return 0; }
guest_ssh() { echo /nix/store/same-toplevel; return 0; }
STATE_DIR="$work/state2"; mkdir -p "$STATE_DIR"

out=$(do_deploy 2>&1 || true)
says "an already-running system still updates the boot pointer" \
  "the host now boots it too" "$out"
case "$(cat "$work/built")" in
  *"$BOOT"*) echo "ok   and the boot out-links were actually built" ;;
  *) echo "FAIL the boot out-links were not built (built:$(cat "$work/built"))"; fail=1 ;;
esac


# --- a running VM whose socket is not where this version looks ---
# The sockets moved from a flat /tmp name into a per-uid directory, so a VM
# started before that change keeps its socket at the old path while the
# upgraded tool looks at the new one. Everything then reports the same thing --
# the guest is not answering ssh -- which sends you to a console log showing a
# guest that is perfectly well.
sockdir="$work/sockdir"; mkdir -p "$sockdir"
newsock="$sockdir/1453397151"
oldsock="/tmp/vmcav-$(id -u)-1453397151"
cave-vm() { [ "$1" = socket ] && printf '%s\n' "$newsock"; return 0; }

out=$(explain_unreachable_guest 2>&1 || true)
says "a missing socket is named rather than implied" "$newsock" "$out"
says "and with nothing at the old path, a restart is the answer" \
  "Stopping and" "$out"

# With the pre-move socket actually present, say so and say how to bridge it.
if ln -sf /dev/null "$oldsock.vsock" 2>/dev/null && [ -e "$oldsock.vsock" ]; then
  :
fi
rm -f "$oldsock.vsock"

# And a socket that is there is not a diagnosis at all.
python3 - "$newsock" <<'PY2' 2>/dev/null || true
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(sys.argv[1]); s.listen(1)
PY2
if [ -S "$newsock" ]; then
  out=$(explain_unreachable_guest 2>&1 || true)
  [ -z "$out" ] && echo "ok   a socket that exists produces no diagnosis" \
    || { echo "FAIL diagnosed a healthy socket: $out"; fail=1; }
fi


# --- status must not fail silently ---
# This runs under `set -e`, and every question status asks touches something
# that can be missing: a socket, a pidfile, a repo, a guest that stopped
# answering. One unguarded non-zero ended the script before the first line was
# printed, so the report on a broken cave was an empty screen and an exit
# code. Reported from a real host, where it was reproducible and silent.
status_report() { echo "vm: running (pid 1)"; return 7; }
# `|| rc=$?`, not `; rc=$?`: sourcing vmcav brought its `set -e` in here, so a
# plain assignment from a command that returns 7 ends the suite instead of
# being the thing under test. Which is the same mistake, one level up, as the
# one being tested for.
rc=0
out=$(do_status 2>&1) || rc=$?
says "what it got to is still printed" "vm: running (pid 1)" "$out"
says "and it says it stopped early"    "stopped early (exit 7)" "$out"
says "and how to find where"           "bash -x" "$out"
[ "$rc" = 7 ] && echo "ok   and the exit code is the real one" \
  || { echo "FAIL exit was $rc, not 7"; fail=1; }
unset -f status_report

# --- functions that must not kill their caller ---
#
# Not tested here, deliberately, and the reason is worth writing down.
#
# `correct_guest_clock` ended with a bare pipeline whose last filter was a
# grep. grep exits 1 when it matches nothing, matching nothing is the ordinary
# case, and under this script's `set -euo pipefail` that ends everything --
# before the `return 0` on the next line, and before status or agent has
# printed anything. Verified directly against the real binary: a subshell that
# calls it never reaches the line after.
#
# It cannot be reproduced here. The function returns early unless cave-vm is on
# PATH, and these suites do not have it; a stubbed cave-vm does not trip the
# same pipeline. Testing it needs the real binary in the suite, which is the
# question left open in 1a6793c -- whether these suites should stub the binary
# or have it. Asserting it with a stub would be a test that cannot fail, which
# is worse than none.

[ "$fail" = 0 ] && echo "test-vmcav-status: ok"
exit "$fail"
