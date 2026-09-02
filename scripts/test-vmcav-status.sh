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
# An agent that stopped still holds its id: its worktree and its home are on
# the guest's disk and outlive the session. Listing only what was running
# described the guest as emptier than it is, and left `vmcav agent`'s "no free
# agent id" with nothing on screen to account for it.
#
# Everything a status touches is stubbed, because none of it is the subject:
# what is under test is which agents get printed and what is said about them.
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

# A dash is how the guest prints a field it has nothing for, which is what an
# agent whose environment file went with the tmpfs looks like.
ROSTER=$'0 running coding-cave vm-cave\n2 stopped widgets main\n5 stopped - -\n'
roster_out=$(do_status 2>&1 || true)
says "the running agent is listed"        "agent 0: coding-cave @ vm-cave"    "$roster_out"
says "a stopped agent is listed too"      "agent 2: stopped, still holding"   "$roster_out"
says "with what it was working on"        "was working on: widgets @ main"    "$roster_out"
# The session's environment file is on a tmpfs and the agent outlives it, so a
# stopped agent with nothing left to say still has to appear -- it is holding
# an id either way, which is the whole reason for the line.
says "one with no record still appears"   "agent 5: stopped, still holding"   "$roster_out"
says "and how to release the id is named" "vmcav agent remove <id>"           "$roster_out"

# Nothing stopped: no paragraph about ids nobody is holding.
ROSTER=$'0 running coding-cave vm-cave\n'
roster_out=$(do_status 2>&1 || true)
says "the running agent is still listed" "agent 0: coding-cave" "$roster_out"
case "$roster_out" in
  *"still holding"*) echo "FAIL a guest with nothing stopped stays quiet"; fail=1 ;;
  *) echo "ok   a guest with nothing stopped stays quiet" ;;
esac

# --- what a start says to do next ---
# A stop leaves the worktrees and homes behind, so a guest that has run before
# comes back holding agents. "no agents yet" there is the sentence that makes
# somebody create a second agent beside the one holding their conversation.
ROSTER=$'2 stopped widgets main\n5 stopped - -\n'
start_out=$(announce_agents 2>&1 || true)
says "the held agents are named"      "agent 2: widgets @ main" "$start_out"
says "including one with no record"   "agent 5"                 "$start_out"
says "and how to get back into one"   "vmcav attach <id>"       "$start_out"
case "$start_out" in
  *"no agents yet"*) echo "FAIL a guest holding agents does not say it has none"; fail=1 ;;
  *) echo "ok   a guest holding agents does not say it has none" ;;
esac

# A guest that really is empty, and an older one that cannot answer: both say
# what this always said.
ROSTER=""
says "an empty guest still says to start one" "no agents yet" "$(announce_agents 2>&1 || true)"
guest_rpc() { return 1; }
says "and so does one that cannot answer"     "no agents yet" "$(announce_agents 2>&1 || true)"

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

[ "$fail" = 0 ] && echo "test-vmcav-status: ok"
exit "$fail"
