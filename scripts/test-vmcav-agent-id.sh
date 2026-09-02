#!/usr/bin/env bash
# test-vmcav-agent-id.sh: self-asserting test for what happens when you create
# an agent -- which id it gets, and what you are told when it will not start.
# Sources vmcav for its functions, builds a throwaway shared repo, stubs the
# calls that would need a running guest, and never starts a VM.
#
# What it pins down: the root agent's id is reserved rather than low, so it
# can never be handed to an ordinary agent by counting; "in use" means either
# half of what an agent is, a session the guest is running or a branch it has
# pushed, because either one is something to collide with; and a failed start
# says which unit failed and why, instead of naming a journal in the guest.
set -uo pipefail

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmcav}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
fail=0

git init -q "$work/repo"
git -C "$work/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base

export REPO="$work/repo" STATE_DIR="$work/state"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1090
source "$SRC"

# The shared bare repo, where agent branches live. vmcav derives SHARED from
# REPO, so this has to land exactly where it looked.
git clone -q --bare "$REPO" "$SHARED"

# The one thing here that needs a guest. Sessions are whatever this says.
GUEST_AGENTS=""
guest_rpc() { [ "$1" = "agents" ] && printf '%s' "$GUEST_AGENTS"; return 0; }

# pushed <id> -- an agent branch in the shared repo, as a push would leave it
pushed() {
  git -C "$SHARED" branch -q "agent-$1/main" 2>/dev/null \
    || git -C "$SHARED" branch -qf "agent-$1/main"
}

check() { # check <label> <want> <got>
  if [ "$2" = "$3" ]; then printf 'ok   %s\n' "$1"
  else printf 'FAIL %s (want %s, got %s)\n' "$1" "$2" "$3"; fail=1; fi
}

check "the root agent's id is reserved" "0" "$ROOT_AGENT_ID"
check "--root gets it" "$ROOT_AGENT_ID" "$(allocate_agent_id 1)"

# Nothing running and nothing pushed: the root id is free, and must still not
# be handed out. This is the case that made the reserved id worth a line of
# code rather than a convention.
check "the first ordinary agent is 1, not 0" "1" "$(allocate_agent_id "")"

GUEST_AGENTS=$'1 running 80 24\n'
check "a running session takes its id out of play" "2" "$(allocate_agent_id "")"

GUEST_AGENTS=$'1 running 80 24\n2 running 80 24\n'
check "two running sessions" "3" "$(allocate_agent_id "")"

# A stopped agent that has pushed still owns its id: its work is sitting in
# the shared repo waiting to be reviewed, under a branch named after it.
GUEST_AGENTS=""
pushed 1
check "a pushed branch takes its id out of play" "2" "$(allocate_agent_id "")"

# The gap is reused: ids are not a sequence anybody has promised to remember.
pushed 3
check "the lowest free id wins, not the next one up" "2" "$(allocate_agent_id "")"

GUEST_AGENTS=$'2 running 80 24\n'
check "sessions and branches are the same question" "4" "$(allocate_agent_id "")"

# The root agent running is not an ordinary agent occupying id 0.
GUEST_AGENTS=$'0 running 80 24\n'
git -C "$SHARED" branch -qD agent-1/main agent-3/main 2>/dev/null || true
check "the root agent does not shift the sequence" "1" "$(allocate_agent_id "")"
check "and can still be asked for by name" "0" "$(allocate_agent_id 1)"

# --- what a start that fails tells you ---
# Stubbed at ssh: which units systemd calls failed is the whole input, so a
# guest would add nothing but minutes.
has() { # has <label> <want-substring-or-empty> <got>
  if [ -z "$2" ]; then
    [ -z "$3" ] && { printf 'ok   %s\n' "$1"; return 0; }
    printf 'FAIL %s (want silence, got %s)\n' "$1" "$3"; fail=1; return 0
  fi
  case "$3" in *"$2"*) printf 'ok   %s\n' "$1"; return 0 ;; esac
  printf 'FAIL %s (want %s, got %s)\n' "$1" "$2" "${3:-<silence>}"; fail=1
}

FAILED_UNITS=""
JOURNAL="clone-repo: /projects/coding-cave/0 is on agent-1/vm-cave"
guest_ssh() {
  case "$1" in
    *"systemctl is-failed"*)
      printf '%s\n' "$FAILED_UNITS" | grep -qx "${1##* }" ;;
    *journalctl*) printf '%s\n' "$JOURNAL" ;;
  esac
}

has "a start that failed elsewhere says nothing" "" \
  "$(explain_session_failure 0 2>&1)"

# The case that sent a human into `journalctl -xe` in the guest: the session
# unit is what systemd names, and the checkout is what actually failed.
FAILED_UNITS="clone-repo@0"
out="$(explain_session_failure 0 2>&1)"
has "the failed dependency is named" "clone-repo@0 failed" "$out"
has "and its journal is quoted" "is on agent-1/vm-cave" "$out"

# The instance, not the template: agent 3's start must not report agent 0's
# stale failure, which sits in systemd for as long as nobody resets it.
has "another agent's failure is not borrowed" "" "$(explain_session_failure 3 2>&1)"

# --- and what a start is told before it tries ---
# A guest older than the checkout is the likeliest reason a session will not
# come up, and it is knowable on the host without asking the guest anything.
is_running() { return 0; }
ATTACHED=""
do_attach() { ATTACHED="${1:-}"; }
record_deployed_from
mkdir -p "$REPO/guest"
echo changed > "$REPO/guest/configuration.nix"
git -C "$REPO" add -A
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m guest
GUEST_AGENTS=""
has "a stale guest is called out before the start" "vmcav deploy" \
  "$(do_agent --root proj main 2>&1)"

# An option nobody recognises is not a project. The guest used to be the one
# to object, having already been told to check out a project called --help.
has "an unknown option is refused here, not in the guest" "unknown option" \
  "$(do_agent --nope proj 2>&1)"

# The id that was created is the id that gets attached to. Left off, this
# attached to cave-guestd's own default of 1 -- and attach starts what it
# cannot find, so `vmcav agent --root` created a second agent nobody asked
# for and left the first one unattended.
do_agent --root proj main >/dev/null 2>&1
check "the root agent attaches to itself" "0" "$ATTACHED"

# An ordinary agent is created rather than refused. It used to be refused,
# because it would have come up root under a number saying it was not; it now
# has a user of its own and a view of /projects of its own, so the number
# means what it says.
ATTACHED=""
has "an ordinary agent gets a low id" "agent 1: proj @ main" \
  "$(do_agent proj main 2>&1)"
# Again, and not in a subshell: do_attach records into ATTACHED, which a
# $(...) would set in the child and lose.
do_agent proj main >/dev/null 2>&1
check "and it is the one attached to" "1" "$ATTACHED"

# The root agent keeps its reserved id, which is what stops counting from
# handing it out: an ordinary agent takes the lowest free number from 1 up,
# and 0 is not in that sequence.
ATTACHED=""
has "the root agent is still asked for by name" "agent 0 (vm root)" \
  "$(do_agent --root proj main 2>&1)"

# With no id, the only one running. Two of them is a guess, and attaching to
# the wrong agent is how you type into a session you did not mean.
GUEST_AGENTS=$'2 running 80 24\n'
check "the only running agent needs no id" "2" "$(the_only_running_agent)"
GUEST_AGENTS=$'2 running 80 24\n3 running 80 24\n'
has "two of them is not guessed at" "more than one agent" \
  "$(the_only_running_agent 2>&1)"

# --- removing an agent ---
# The check that belongs on this side: whether the agent has work nobody has
# landed. The guest can say whether an agent is running; only the host can see
# the shared repo, and an agent branch ahead of its base is the only copy of
# that work outside the worktree about to be deleted.

REMOVED=""
sync_sessions() { :; }
guest_ssh() { case "$1" in *"cave-guestd remove"*) REMOVED="$1" ;; esac; }

# An agent whose work has all landed: nothing to lose, so it goes. Run
# directly rather than in $(...), which would set REMOVED in a child.
REMOVED=""
do_agent_remove 7 >/dev/null 2>&1
check "a clean agent is removed" "yes" \
  "$(case "$REMOVED" in *"-id 7"*) echo yes ;; *) echo no ;; esac)"

# An agent with commits its base does not have. A fetch into the bare repo is
# how a push arrives here, and how approve moves refs.
git -C "$work/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "unlanded work"
base=$(git -C "$work/repo" symbolic-ref --short HEAD)
git -C "$SHARED" fetch -q "$work/repo" "HEAD:refs/heads/agent-9/$base"

REMOVED=""
# `|| true` because refusing is the expected result and set -e came with vmcav.
out=$(do_agent_remove 9 2>&1 || true)
has "unlanded work stops a removal" "vmcav approve 9" "$out"
has "and the commits are named" "unlanded work" "$out"
check "and nothing was removed" "" "$REMOVED"
check "and the branch is still there" "yes" \
  "$(git -C "$SHARED" rev-parse --verify --quiet refs/heads/agent-9/$base >/dev/null && echo yes || echo no)"

# Said out loud, it goes anyway.
REMOVED=""
do_agent_remove 9 1 >/dev/null 2>&1
check "--force removes it regardless" "yes" \
  "$(case "$REMOVED" in *"-id 9"*) echo yes ;; *) echo no ;; esac)"
check "and the branch goes with it" "no" \
  "$(git -C "$SHARED" rev-parse --verify --quiet refs/heads/agent-9/$base >/dev/null && echo yes || echo no)"

has "a non-id is refused" "not an agent id" "$(do_agent_remove nine 2>&1 || true)"

[ "$fail" = 0 ] && echo "PASS" || echo "FAIL"
exit "$fail"
