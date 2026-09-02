#!/usr/bin/env bash
# test-vmcav-carry-base.sh: self-asserting test for carrying a human's commits
# into the shared repo, which is what an agent clones and rebases onto.
# Sources vmcav for its functions and never starts a VM.
#
# What it pins down: the shared repo follows the checkout's base branch, but
# only ever forward; a base the human rewrote is refused and refused quietly,
# because that is theirs to resolve rather than something to nag about on
# every status; and an agent branch is never carried, since the shared repo's
# copy is the one the agent pushed and the checkout's is at best a stale
# mirror of it.
set -uo pipefail

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmcav}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
# Run inside a guest, `git` is cave-git, which would try to publish these
# throwaway commits. Stated rather than inherited: a test that behaves
# differently depending on where it runs is testing the wrong thing.
export CAVE_GIT_NO_SYNC=1
fail=0

check() { # <label> <want> <got>
  if [ "$2" = "$3" ]; then printf 'ok   %s\n' "$1"
  else printf 'FAIL %s\n       want: %s\n       got:  %s\n' "$1" "$2" "$3"; fail=1; fi
}

commit() { git -C "$1" -c user.email=t@t -c user.name=t commit -q "${@:2}"; }

git init -q -b main "$work/repo"
commit "$work/repo" --allow-empty -m base

export REPO="$work/repo" STATE_DIR="$work/state"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1090
source "$SRC"
git clone -q --bare "$REPO" "$SHARED"

shared_at() { git -C "$SHARED" rev-parse --short main 2>/dev/null; }

# Nothing new to carry says nothing. This runs on every status, so silence
# when there is no news is most of what makes it usable.
check "an unchanged base is silent" "" "$(carry_base_to_shared 2>&1)"

# The case this exists for: a person and an agent on the same project at once.
# The person commits and leads, and without this the agent rebases onto a base
# stale by everything they did.
was=$(shared_at)
commit "$work/repo" --allow-empty -m "the human's commit"
out=$(carry_base_to_shared 2>&1)
check "a new commit is carried" "$(git -C "$REPO" rev-parse --short main)" "$(shared_at)"
case "$out" in *"shared repo"*) printf 'ok   %s\n' "and it is reported" ;;
  *) printf 'FAIL and it is reported (got %s)\n' "$out"; fail=1 ;; esac
[ "$was" != "$(shared_at)" ] || { printf 'FAIL the shared repo did not move\n'; fail=1; }

# A rewritten base is not a fast-forward. Refused by fetch itself, without a
# leading +, so the unsafe case cannot happen here by accident.
held=$(shared_at)
commit "$work/repo" --amend --allow-empty -m "rewritten"
check "a rewritten base is refused" "$held" "$(shared_at)"
check "and refused quietly" "" "$(carry_base_to_shared 2>&1)"

# An agent branch is the agent's. The shared repo's copy is what it pushed.
git -C "$REPO" checkout -q -b agent-1/main
commit "$work/repo" --allow-empty -m "as if an agent"
check "an agent branch is not carried" "" "$(carry_base_to_shared 2>&1)"
check "and does not appear in the shared repo" "" \
  "$(git -C "$SHARED" rev-parse --verify --quiet refs/heads/agent-1/main || true)"

# A detached HEAD names no branch, so there is nothing to carry and nothing to
# complain about.
git -C "$REPO" checkout -q --detach
check "a detached HEAD is not an error" "" "$(carry_base_to_shared 2>&1)"

[ "$fail" = 0 ] && echo "PASS" || echo "FAIL"
exit "$fail"
