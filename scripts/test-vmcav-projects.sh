#!/usr/bin/env bash
# test-vmcav-projects.sh: self-asserting test for the project registry -- what
# `vmcav init` registers, what the git daemon is then told to serve, and what
# happens to a directory that is not a project. Sources vmcav for its
# functions and never starts a VM or a daemon.
#
# What it pins down: the served directory *is* the registry, so registering is
# creating a link and listing is reading them back, with no second list to
# fall out of step; the project vmcav was run from needs no ceremony, because
# it is the one that can be inferred; and a project keeps its own shared repo
# inside itself, so registering a second one does not move the first.
set -uo pipefail

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmcav}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
fail=0

check() { # <description> <expected> <actual>
  if [ "$2" = "$3" ]; then
    printf 'ok   %s\n' "$1"
  else
    printf 'FAIL %s\n       want: %s\n       got:  %s\n' "$1" "$2" "$3"
    fail=1
  fi
}

for p in alpha beta; do
  git init -q "$work/$p"
  git -C "$work/$p" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
done

export REPO="$work/alpha" STATE_DIR="$work/state"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1090
source "$SRC"

# --- the project vmcav was run from ---------------------------------------

slug=$(register_project "$REPO")
check "registering returns the slug" "alpha" "$slug"
check "a link is what registration is" "yes" \
  "$([ -L "$REPOS/alpha.git" ] && echo yes || echo no)"
check "the link points into the project" "$work/alpha/.coding-cave/cave-shared.git" \
  "$(readlink -f "$REPOS/alpha.git")"
check "the shared repo is a bare repo" "true" \
  "$(git -C "$work/alpha/.coding-cave/cave-shared.git" rev-parse --is-bare-repository)"
check "the branch guard is installed" "yes" \
  "$([ -x "$work/alpha/.coding-cave/cave-shared.git/hooks/pre-receive" ] && echo yes || echo no)"

# --- a second project, named rather than inferred -------------------------

check "registering another returns its slug" "beta" "$(register_project "$work/beta")"
check "both are served" "alpha beta" "$(registered_projects | sort | tr '\n' ' ' | sed 's/ $//')"
check "the second has its own shared repo" "yes" \
  "$([ -d "$work/beta/.coding-cave/cave-shared.git" ] && echo yes || echo no)"
check "registering the second left the first alone" \
  "$work/alpha/.coding-cave/cave-shared.git" "$(readlink -f "$REPOS/alpha.git")"

# --- idempotence ----------------------------------------------------------
#
# Running init twice is not an error and does not accumulate anything: the
# link is the registration, so writing it again writes the same link.

register_project "$work/beta" >/dev/null
check "re-registering does not duplicate" "alpha beta" \
  "$(registered_projects | sort | tr '\n' ' ' | sed 's/ $//')"

# --- the branch guard is kept current -------------------------------------
#
# Both halves have been wrong in turn. Installing only when absent meant a fix
# to the guard never reached a repo that already had one; refusing to touch a
# hook this did not install meant the one repo in use kept `cav`'s guard, which
# rejects every agent but one, and no amount of fixing the guard reached it.

hook="$work/alpha/.coding-cave/cave-shared.git/hooks/pre-receive"
printf '#!/bin/sh\n# stale shim\nexec cave-vm-git-pre-receive "$@"\n' > "$hook"
register_project "$REPO" >/dev/null
check "our own hook is rewritten, not skipped" "gone" \
  "$(grep -q 'stale shim' "$hook" && echo "still there" || echo gone)"

printf '#!/bin/sh\nexec some-other-tool "$@"\n' > "$hook"
register_project "$REPO" >/dev/null
check "a foreign hook is replaced" "yes" \
  "$(grep -q cave-vm-git-pre-receive "$hook" && echo yes || echo no)"
check "and the old one is kept beside it" "yes" \
  "$([ -e "$hook.replaced-by-vmcav" ] && echo yes || echo no)"
check "the kept copy is the hook that was there" "yes" \
  "$(grep -q some-other-tool "$hook.replaced-by-vmcav" && echo yes || echo no)"

# --- and status says when it is not ours ----------------------------------
#
# The check that would have caught the four rounds above. A foreign guard
# refuses pushes in a way that reads like the guard working, so nothing about
# the symptom points at the cause; status is where a host-side fact that is
# quietly wrong has to surface.

printf '#!/bin/sh\nexec some-other-tool "$@"\n' > "$hook"
out=$(warn_if_hooks_are_foreign 2>&1)
check "status names a foreign guard" "yes" \
  "$(printf '%s' "$out" | grep -q "installed by another tool" && echo yes || echo no)"
check "and says how to take it over" "yes" \
  "$(printf '%s' "$out" | grep -q "vmcav init" && echo yes || echo no)"

register_project "$REPO" >/dev/null
check "and says nothing once it is ours" "" "$(warn_if_hooks_are_foreign 2>&1)"

rm -f "$hook"
out=$(warn_if_hooks_are_foreign 2>&1)
check "a missing guard is reported too" "yes" \
  "$(printf '%s' "$out" | grep -q "no branch guard" && echo yes || echo no)"
register_project "$REPO" >/dev/null

# --- what is not a project ------------------------------------------------
#
# Refused rather than registered empty. A link to a directory with no shared
# repo would be served, and the guest would clone nothing and fail at the
# checkout instead of here, where the reason is known.

mkdir -p "$work/notarepo"
# `set -e` comes with vmcav when this file sources it, so a non-zero return
# has to be caught rather than merely observed afterwards.
rc=0; register_project "$work/notarepo" >/dev/null 2>&1 || rc=$?
check "a non-checkout is refused" "1" "$rc"
check "and is not left in the registry" "alpha beta" \
  "$(registered_projects | sort | tr '\n' ' ' | sed 's/ $//')"

# --- a checkout reached by a subdirectory ---------------------------------
#
# The slug comes from the toplevel, not from wherever you happened to stand,
# so `vmcav init` in a subdirectory registers the project rather than
# inventing one named after the subdirectory.

mkdir -p "$work/alpha/sub/deeper"
check "a subdirectory resolves to its project" "alpha" \
  "$(register_project "$work/alpha/sub/deeper")"

# --- unregistering a project ---
#
# Unregistering, not deleting. The link goes; the project's own shared repo,
# which holds every agent branch pushed to it, stays. Removing a project must
# not be a way to lose work.

HELD=""
is_running() { return 0; }
guest_ssh() { case "$1" in *"cave-guestd holders"*) printf '%s' "$HELD" ;; esac; }

has() { # <label> <want-substring> <got>
  case "$3" in *"$2"*) printf 'ok   %s\n' "$1"; return 0 ;; esac
  printf 'FAIL %s (want %s, got %s)\n' "$1" "$2" "${3:-<silence>}"; fail=1
}

has "an unregistered project is refused" "not registered" \
  "$(do_remove_project never-registered 2>&1 || true)"
has "and a missing name is refused" "needs a project name" \
  "$(do_remove_project 2>&1 || true)"

# An agent still holding a worktree of it. Refused, and the agents are named,
# because the next step is removing them one at a time.
HELD=$'2 stopped\n5 running\n'
out=$(do_remove_project beta 2>&1 || true)
has "a held project is refused" "agents still have a worktree" "$out"
has "and the holders are named" "agent 5 (running)" "$out"
has "and it points at the check that decides" "vmcav agent remove" "$out"
check "and it is still registered" "yes" \
  "$([ -L "$REPOS/beta.git" ] && echo yes || echo no)"

# Nobody holding it: the link goes and nothing else does.
HELD=""
betarepo=$(readlink -f "$REPOS/beta.git")
out=$(do_remove_project beta 2>&1 || true)
has "an unheld project is unregistered" "unregistered" "$out"
check "the link is gone" "no" \
  "$([ -L "$REPOS/beta.git" ] && echo yes || echo no)"
check "and it is no longer served" "alpha" \
  "$(registered_projects | sort | tr '\n' ' ' | sed 's/ $//')"
check "but its repo is untouched" "true" \
  "$(git -C "$betarepo" rev-parse --is-bare-repository 2>/dev/null)"

# --- a project whose directory moved ---
# The registration is a symlink into the project's own checkout, so moving or
# deleting that checkout leaves the link pointing at nothing. What made this
# worth reporting is how it failed: the slug still listed, with an empty
# second column, and the guest told only that the repository does not exist.
moved=$(readlink "$REPOS/alpha.git")
mv "${moved%/.coding-cave/*}" "$work/alpha-moved"

out=$(do_projects 2>&1 || true)
has "a moved project is marked missing" "(missing)" "$out"
has "and where it used to be is still named" "$moved" "$out"
has "and re-registering it is offered" "vmcav init <dir>" "$out"

# And it is kept out of what the daemon is told to serve. A dead path there
# answers the guest with "repository not found", which reads exactly like
# never having registered it at all.
served=$(git_daemon_serve_args 2>/dev/null || true)
case "$served" in
  *alpha*) echo "FAIL a moved project is still handed to the daemon"; fail=1 ;;
  *) echo "ok   a moved project is not handed to the daemon" ;;
esac

exit "$fail"
