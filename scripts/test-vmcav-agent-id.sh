#!/usr/bin/env bash
# test-vmcav-agent-id.sh: self-asserting test for which id a new agent gets.
# Sources vmcav for its functions, builds a throwaway shared repo, stubs the
# one call that would need a running guest, and never starts a VM.
#
# What it pins down: the root agent's id is reserved rather than low, so it
# can never be handed to an ordinary agent by counting; and "in use" means
# either half of what an agent is, a session the guest is running or a branch
# it has pushed, because either one is something to collide with.
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
check "--vm-root gets it" "$ROOT_AGENT_ID" "$(allocate_agent_id 1)"

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

[ "$fail" = 0 ] && echo "PASS" || echo "FAIL"
exit "$fail"
