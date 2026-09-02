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

# The third thing an agent is, and the only one the host cannot see for
# itself: a worktree and a home on the guest's disk, which outlive both the
# session and the branch. Stubbed like the sessions above.
OCCUPIED=""
OCCUPIED_FAILS=""
occupied_agent_ids() {
  [ -n "$OCCUPIED_FAILS" ] && return 1
  printf '%s' "$OCCUPIED"
  return 0
}

# pushed <id> -- an agent branch in the shared repo, as a push would leave it
pushed() {
  git -C "$SHARED" branch -q "agent-$1/main" 2>/dev/null \
    || git -C "$SHARED" branch -qf "agent-$1/main"
}

check() { # check <label> <want> <got>
  if [ "$2" = "$3" ]; then printf 'ok   %s\n' "$1"
  else printf 'FAIL %s (want %s, got %s)\n' "$1" "$2" "$3"; fail=1; fi
}

has() { # has <label> <want-substring-or-empty> <got>
  if [ -z "$2" ]; then
    [ -z "$3" ] && { printf 'ok   %s\n' "$1"; return 0; }
    printf 'FAIL %s (want silence, got %s)\n' "$1" "$3"; fail=1; return 0
  fi
  case "$3" in *"$2"*) printf 'ok   %s\n' "$1"; return 0 ;; esac
  printf 'FAIL %s (want %s, got %s)\n' "$1" "$2" "${3:-<silence>}"; fail=1
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

# --- state on the guest's disk holds an id too ---
# The case this missed for real. An agent stopped, its work landed, its branch
# deleted by approve: nothing the host can see says the id is taken, and the
# guest still has six megabytes of that agent's transcripts and a checked-out
# worktree under it. Handing the id on runs the new agent as the same unix
# user in the same home -- and since continuing is the default, it resumes a
# conversation it never had.
GUEST_AGENTS=""
OCCUPIED=$'1\n'
check "a leftover home or worktree takes its id out of play" "2" "$(allocate_agent_id "")"

# Both halves, so the lowest free id is found around them rather than after
# them.
OCCUPIED=$'1\n2\n4\n'
check "occupied ids are skipped, not counted past" "3" "$(allocate_agent_id "")"

# And the id does come back: this is what makes `vmcav agent remove` and
# `vmcav gc` the way one is released, rather than a number nobody reuses.
OCCUPIED=""
check "removing the state releases the id" "1" "$(allocate_agent_id "")"

# A guest that cannot answer must not read as "nothing is held", which is the
# same empty string a guest holding nothing returns.
# Tested rather than run bare: sourcing vmcav brought its `set -e` in here,
# so an unguarded call that returns non-zero ends the suite instead of failing
# one case.
OCCUPIED_FAILS=1
rc=0
out=$(allocate_agent_id "" 2>&1) || rc=$?
OCCUPIED_FAILS=""
check "a guest that cannot answer stops the allocation" "1" "$rc"
case "$out" in
  *"already has a home"*) printf 'ok   %s\n' "and says what it was protecting against" ;;
  *) printf 'FAIL %s (got %s)\n' "and says what it was protecting against" "$out"; fail=1 ;;
esac

# --- naming a conversation ---
# An agent continues its last by default. The archive holds every session this
# cave has had, and choosing among them used to be possible only from inside,
# in claude's own picker.
RESTORED=""
is_running() { return 0; }
restore_one_session() { RESTORED="$1"; }
do_attach() { :; }
# To a file, not a variable: do_agent calls this inside a command
# substitution, and a subshell's assignments do not come back.
guest_ssh() { printf '%s\n' "$*" > "$work/started"; return 0; }
# Deliberately not stubbing warn_if_guest_is_behind or correct_guest_clock:
# a later case asserts on what the first of them prints, and a stub left
# behind here made that case fail for a reason that had nothing to do with it.
GUEST_AGENTS=""
OCCUPIED=""

rm -f "$work/started"
do_agent --root --resume 3f2504e0-4f89-11d3-9a0c-0305e82c3301 proj main >/dev/null 2>&1 || true
case "$(cat "$work/started" 2>/dev/null)" in
  *"-session 3f2504e0-4f89-11d3-9a0c-0305e82c3301"*)
    echo "ok   the session id is passed to the guest" ;;
  *) echo "FAIL the session id did not reach the guest (got: $(cat "$work/started" 2>/dev/null))"; fail=1 ;;
esac
check "and it is restored before the start, not after" \
  "3f2504e0-4f89-11d3-9a0c-0305e82c3301" "$RESTORED"

# A typo is answered by the command you typed, rather than by claude some
# seconds later inside a session you then have to leave.
out=$(do_agent --root --resume not-a-session proj main 2>&1 || true)
has "a bad session id is refused here" "is not a session id" "$out"
has "and says where ids come from" "vmcav sessions list" "$out"

# The two flags ask for opposite things.
out=$(do_agent --root --fresh --resume 3f2504e0-4f89-11d3-9a0c-0305e82c3301 proj main 2>&1 || true)
has "--fresh and --resume together are refused" "opposite things" "$out"

# --- what a start that fails tells you ---
# Stubbed at ssh: which units systemd calls failed is the whole input, so a
# guest would add nothing but minutes.
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

# --- a vm that is not running ---
# Refusing here was telling somebody to type the command they would have typed
# anyway: nothing about creating an agent is ambiguous with the VM down.
STARTED=0
STARTED_ARG=""
do_start() { STARTED=$((STARTED + 1)); STARTED_ARG="${1:-}"; is_running() { return 0; }; }
is_running() { return 1; }
# Not `out=$(do_agent ...)`: a command substitution is a subshell, and the
# whole point here is the side effect it has on this one.
do_agent --root proj main >"$work/agent.out" 2>&1 || true
out=$(cat "$work/agent.out")
check "a stopped vm is started rather than refused" "1" "$STARTED"
has "and it says so, because booting was not what was asked for" \
  "starting it first" "$out"
check "start is told it is on the way to an agent" "--for-agent" "${STARTED_ARG:-}"

# A start that fails stops the agent, rather than pressing on into a guest
# that is not there.
STARTED=0
is_running() { return 1; }
do_start() { STARTED=$((STARTED + 1)); return 1; }
rc=0
do_agent --root proj main >/dev/null 2>&1 || rc=$?
check "an agent is not created when the start failed" "1" "$rc"
is_running() { return 0; }

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

# --- an agent that could not publish is not started ---
# A guard belonging to another tool refuses every agent but the one the guest
# is stamped as. Committing is what publishes, so such an agent would work and
# then find out at its first push, with the work in a VM a reformat deletes.

install -d "$SHARED/hooks"
printf '#!/bin/sh\nexec some-other-tool "$@"\n' > "$SHARED/hooks/pre-receive"
GUEST_AGENTS=""
has "an ordinary agent is refused under a foreign guard" "vmcav init" \
  "$(do_agent proj main 2>&1 || true)"
# The root agent holds the id the guard was stamped with, so it still works.
has "the root agent is not" "" \
  "$(do_agent --root proj main 2>&1 | grep 'vmcav init' || true)"

printf '#!/bin/sh\nexec cave-vm-git-pre-receive "$@"\n' > "$SHARED/hooks/pre-receive"
has "and it is started once the guard is ours" "agent 1: proj @ main" \
  "$(do_agent proj main 2>&1 || true)"

# --- gc ---
# A sweep can only do what naming each agent would have done, so the unlanded
# work check has to hold here too: the reason to reclaim disk is never a
# reason to drop the only copy of somebody's commits.

RECLAIMABLE=""
REMOVED=""
GC_UNREACHABLE=""
guest_ssh() {
  # A guest that does not answer is not a guest with nothing to reclaim.
  [ -n "$GC_UNREACHABLE" ] && return 255
  case "$1" in
    *"cave-guestd reclaimable"*) printf '%s' "$RECLAIMABLE" ;;
    *"cave-guestd remove"*) REMOVED="$REMOVED ${1##*-id }" ;;
  esac
  return 0
}

has "nothing to reclaim says so" "nothing to reclaim" "$(do_gc 1 2>&1 || true)"

# "nothing to reclaim" is a statement about the guest. A guest that did not
# answer has not made it, and saying it anyway is what sends somebody looking
# somewhere else for the disk they expected back.
GC_UNREACHABLE=1
out=$(do_gc 1 2>&1 || true)
has "an unreachable guest is not an empty one" "not answering" "$out"
case "$out" in
  *"nothing to reclaim"*) echo "FAIL a silent guest was called empty"; fail=1 ;;
  *) echo "ok   and is not called empty" ;;
esac
GC_UNREACHABLE=""

RECLAIMABLE=$'4 100 /var/lib/coding-cave/agents/4\n5 200 /var/lib/coding-cave/agents/5\n'
REMOVED=""
out=$(do_gc 1 2>&1 || true)
has "candidates are listed with their age" "last touched 100 hours ago" "$out"
# Directly, not in $(...), which would set REMOVED in a child and lose it.
REMOVED=""
do_gc 1 >/dev/null 2>&1 || true
check "and both are removed" " 4 5" "$REMOVED"

# One of them is holding work its base does not have.
git -C "$work/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "gc unlanded"
gcbase=$(git -C "$work/repo" symbolic-ref --short HEAD)
git -C "$SHARED" fetch -q "$work/repo" "HEAD:refs/heads/agent-5/$gcbase"
out=$(do_gc 1 2>&1 || true)
has "and says which" "agent 5 kept" "$out"
REMOVED=""
do_gc 1 >/dev/null 2>&1 || true
check "the one with work is kept" " 4" "$REMOVED"

[ "$fail" = 0 ] && echo "PASS" || echo "FAIL"
exit "$fail"
