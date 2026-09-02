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
# Read by `cave-vm projects --guards` now, where the four states are a table
# in internal/projects: ours, another tool's, none at all, and a registration
# whose project is not there -- which this shell reported as a missing guard,
# because it resolved the dead link to an empty path and looked for a hook
# under it.
#
# What stays here is that installing puts our own guard in place, which is
# this side's job and is checked above.

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
UNREACHABLE=""
guest_ssh() {
  # A guest that does not answer is not a guest that answered "nobody": those
  # were the same empty string, and empty meant nobody held the project.
  [ -n "$UNREACHABLE" ] && return 255
  case "$1" in *"cave-guestd holders"*) printf '%s' "$HELD" ;; esac
  return 0
}

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

# A guest that is running and will not answer. Unregistering then could take a
# worktree out from under an agent that has one, so it is refused -- and the
# project stays registered, which is the half that matters.
UNREACHABLE=1
out=$(do_remove_project beta 2>&1 || true)
has "an unreachable guest stops the unregister" "did not answer" "$out"
has "and says how to proceed deliberately" "stop the vm first" "$out"
check "and the project is still registered" "yes" \
  "$([ -L "$REPOS/beta.git" ] && echo yes || echo no)"
UNREACHABLE=""

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

# --- two directories with the same basename ---
# A slug is a basename, so ~/work/docs and ~/other/docs are both `docs`. The
# link was written with `ln -sfn`, so the second registration took the name
# from the first without a word -- and the name is what the guest resolves
# git://127.0.0.1/docs.git to, so an agent given "docs" would carry on
# against a repository nobody had told it about.
mkdir -p "$work/one/docs" "$work/two/docs"
for d in one/docs two/docs; do
  git init -q "$work/$d"
  git -C "$work/$d" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
done
register_project "$work/one/docs" >/dev/null 2>&1
first=$(readlink "$REPOS/docs.git")

out=$(register_project "$work/two/docs" 2>&1 || true)
has "a name already taken is refused" "already registered, and to a different project" "$out"
has "and both projects are named" "$work/one/docs" "$out"
has "and the way out is offered" "--as" "$out"
check "and the first project keeps the name" "$first" "$(readlink "$REPOS/docs.git")"

# Nothing is created for a project that was refused: the check happens before
# the shared repo and the hooks, so a project you were told to rename is not
# left with half a registration inside it.
check "and nothing was created in the refused project" "no" \
  "$([ -d "$work/two/docs/.coding-cave" ] && echo yes || echo no)"

# --as gives it one of its own, and then both are served.
register_project "$work/two/docs" other-docs >/dev/null 2>&1
check "--as registers it under another name" "yes" \
  "$([ -L "$REPOS/other-docs.git" ] && echo yes || echo no)"

# Registering the same project again is not a collision with itself: `start`
# does it on every run.
out=$(register_project "$work/one/docs" 2>&1 || true)
has "re-registering the same project is fine" "docs" "$out"

# --- registering a project while the vm is up ---
# The daemon's whitelist is an argument list fixed when it started, so a new
# registration is invisible to it. The advice used to be to stop and start the
# VM, which reaches the daemon by taking down every agent session with it.
DAEMON_STOPS=0
DAEMON_STARTS=0
stop_git_daemon() { DAEMON_STOPS=$((DAEMON_STOPS + 1)); }
start_git_daemon() { DAEMON_STARTS=$((DAEMON_STARTS + 1)); }
pgrep() { return 1; }   # nothing left running once it has been stopped
is_running() { return 0; }

git init -q "$work/gamma"
git -C "$work/gamma" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
# Not $( ): the assertions are about side effects on this shell.
do_init "$work/gamma" >"$work/init.out" 2>&1 || true
out=$(cat "$work/init.out")
has "registering says the guest can reach it" "the guest can reach it now" "$out"
check "and the daemon was restarted" "1" "$DAEMON_STARTS"
check "after being stopped" "1" "$DAEMON_STOPS"
case "$out" in
  *"vmcav stop"*) echo "FAIL registering no longer asks for a vm restart"; fail=1 ;;
  *) echo "ok   registering no longer asks for a vm restart" ;;
esac

# A daemon that will not stop is not quietly assumed to have: the old
# whitelist would still be serving, and the new project just as invisible.
DAEMON_STARTS=0
pgrep() { return 0; }   # it never goes away
# The production cap is five seconds of polling, which is patience this test
# has no reason to buy.
GIT_DAEMON_STOP_TRIES=2
do_init "$work/gamma" >"$work/init2.out" 2>&1 || true
check "a daemon that will not stop is not restarted over" "0" "$DAEMON_STARTS"
has "and the fallback is named" "vmcav stop && vmcav start" "$(cat "$work/init2.out")"
unset -f pgrep is_running stop_git_daemon start_git_daemon

# --- a project whose directory moved ---
# The listing and the daemon's whitelist are both `cave-vm projects` now, and
# their cases -- a dangling registration still saying where it pointed, and
# being kept out of what the daemon is served -- are a table in
# internal/projects. They were here as assertions over shell that read the
# same directory twice and disagreed about it.

exit "$fail"
