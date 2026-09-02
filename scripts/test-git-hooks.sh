#!/usr/bin/env bash
# test-git-hooks.sh: self-asserting test for the pre-receive branch guard.
# Builds a throwaway bare repo, feeds the hook ref updates as git would on
# stdin, and checks accept (exit 0) vs reject (exit 1) for each case. Offline
# and fast. The hook reads CODING_CAVE_AGENT_ID from the environment, exactly
# as the daemon injects it.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HOOK="$REPO/scripts/git-hooks/pre-receive"
POST="$REPO/scripts/git-hooks/post-receive"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
# Neutralize a cav git-wrapper if this runs inside a cave: without an agent
# name it won't try to auto-push the throwaway repo's branches.
unset CODING_CAVE_AGENT_NAME 2>/dev/null || true
zero=0000000000000000000000000000000000000000
fail=0

# Build commits: A -> B -> C linear, plus D branching off A (diverges from B).
git init -q "$work/src"; cd "$work/src"
git -c user.email=t@t -c user.name=t commit --allow-empty -q -m A; A=$(git rev-parse HEAD)
git -c user.email=t@t -c user.name=t commit --allow-empty -q -m B; B=$(git rev-parse HEAD)
git -c user.email=t@t -c user.name=t commit --allow-empty -q -m C; C=$(git rev-parse HEAD)
git checkout -q -b side "$A"
git -c user.email=t@t -c user.name=t commit --allow-empty -q -m D; D=$(git rev-parse HEAD)
cd "$work"; git clone -q --bare src bare.git; cd bare.git
git update-ref refs/heads/main "$A"
git update-ref refs/heads/agent-0/main "$B"
git update-ref refs/cav/agent-tip/agent-0/main "$B"

# run <agent-id> <old> <new> <ref> -> echoes hook exit code
run() {
  local id="$1"; shift
  printf '%s %s %s\n' "$1" "$2" "$3" | CODING_CAVE_AGENT_ID="$id" bash "$HOOK" >/dev/null 2>&1
  echo $?
}
# expect <want-exit> <label> <agent-id> <old> <new> <ref>
expect() {
  local want="$1" label="$2"; shift 2
  local got; got=$(run "$@")
  if [ "$got" = "$want" ]; then
    printf 'ok   %s\n' "$label"
  else
    printf 'FAIL %s (want exit %s, got %s)\n' "$label" "$want" "$got"; fail=1
  fi
}

# --- Agent 0 ---
expect 0 "agent ff-update own branch"        0 "$A" "$C" refs/heads/agent-0/main
expect 0 "agent create own branch"           0 "$zero" "$B" refs/heads/agent-0/feature
expect 0 "agent delete own branch"           0 "$B" "$zero" refs/heads/agent-0/main
expect 0 "agent rewrite own pushed history"  0 "$B" "$D" refs/heads/agent-0/main   # tip==old==B
expect 1 "agent force past user commits"      0 "$C" "$D" refs/heads/agent-0/main   # tip=B != old=C
# Allowed, and deliberately so. The injected id describes the guest, not the
# pusher: every agent in a VM pushes through one socket and the id is fixed
# before any agent exists, so requiring it to match the branch rejected every
# agent but one and stranded their work. The host checks what it can know --
# a well-formed agent namespace, and never a base branch -- and cave-git
# checks the agent's own namespace on the side that knows which agent it is.
expect 0 "agent push other agent namespace"   0 "$zero" "$B" refs/heads/agent-1/main
expect 1 "agent push a namespace that is not one" 0 "$zero" "$B" refs/heads/agent-x/main
expect 1 "agent push base branch"             0 "$A" "$C" refs/heads/main
expect 1 "agent push tag"                     0 "$zero" "$A" refs/tags/v1
expect 1 "agent push HEAD branch"             0 "$A" "$B" refs/heads/agent-0/HEAD

# --- User (no id): unrestricted ---
expect 0 "user push base branch"             "" "$A" "$C" refs/heads/main
expect 0 "user delete base branch"           "" "$A" "$zero" refs/heads/main
expect 0 "user push tag"                     "" "$zero" "$A" refs/tags/v1
expect 1 "user push HEAD branch still bad"   "" "$A" "$B" refs/heads/agent-0/HEAD

# --- post-receive maintains the agent-tip high-water ref ---
printf '%s %s %s\n' "$B" "$C" refs/heads/agent-0/main | CODING_CAVE_AGENT_ID=0 bash "$POST" >/dev/null 2>&1
tip=$(git rev-parse --verify --quiet refs/cav/agent-tip/agent-0/main || true)
if [ "$tip" = "$C" ]; then printf 'ok   post-receive advances agent-tip\n'
else printf 'FAIL post-receive advances agent-tip (want %s, got %s)\n' "$C" "$tip"; fail=1; fi
printf '%s %s %s\n' "$C" "$zero" refs/heads/agent-0/main | CODING_CAVE_AGENT_ID=0 bash "$POST" >/dev/null 2>&1
if git rev-parse --verify --quiet refs/cav/agent-tip/agent-0/main >/dev/null; then
  printf 'FAIL post-receive removes agent-tip on delete\n'; fail=1
else printf 'ok   post-receive removes agent-tip on delete\n'; fi
# A user push (no id) must not move the tip.
git update-ref refs/cav/agent-tip/agent-0/main "$B"
printf '%s %s %s\n' "$B" "$C" refs/heads/agent-0/main | bash "$POST" >/dev/null 2>&1
if [ "$(git rev-parse refs/cav/agent-tip/agent-0/main)" = "$B" ]; then
  printf 'ok   post-receive ignores user pushes\n'
else printf 'FAIL post-receive moved tip for a user push\n'; fail=1; fi

# --- a push from the guest with no id ---
# "No id" means the user, who may move a base branch: that is how approve
# lands work. So an id that goes missing does not weaken the gate, it removes
# it, in the direction of letting the guest move base branches. The daemon
# marks its own pushes, and an unattributable one is refused.
from_guest() { # from_guest <id> <old> <new> <ref> -> exit code
  local id="$1"; shift
  printf '%s %s %s\n' "$1" "$2" "$3" \
    | CODING_CAVE_AGENT_ID="$id" CODING_CAVE_FROM_GUEST=1 bash "$HOOK" >/dev/null 2>&1
  echo $?
}
got=$(from_guest "" "$A" "$C" refs/heads/main)
if [ "$got" = 1 ]; then printf 'ok   a guest push with no id cannot move a base branch\n'
else printf 'FAIL a guest push with no id moved main (exit %s)\n' "$got"; fail=1; fi

# Not even its own namespace: once it is unattributable there is nothing to
# check it against.
got=$(from_guest "" "$B" "$C" refs/heads/agent-0/main)
if [ "$got" = 1 ]; then printf 'ok   nor push to an agent namespace\n'
else printf 'FAIL an unattributable guest push was accepted (exit %s)\n' "$got"; fail=1; fi

# The marker alone changes nothing for a push that does say who it is.
got=$(from_guest 0 "$A" "$C" refs/heads/agent-0/main)
if [ "$got" = 0 ]; then printf 'ok   a marked push with an id is unaffected\n'
else printf 'FAIL a normal agent push was refused (exit %s)\n' "$got"; fail=1; fi

# And the human, whose push carries neither, still moves base branches: that
# is the approve flow, and refusing it would be refusing the whole point.
got=$(run "" "$A" "$C" refs/heads/main)
if [ "$got" = 0 ]; then printf 'ok   the user can still move a base branch\n'
else printf 'FAIL the user was refused (exit %s)\n' "$got"; fail=1; fi

[ "$fail" = 0 ] && echo "PASS" || echo "FAIL"
exit "$fail"
