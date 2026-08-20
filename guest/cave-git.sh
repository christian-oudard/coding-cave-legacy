#!/usr/bin/env bash
# The guest's git: real git, and then a push of whatever it just moved.
#
# `main` installs the same thing in a container cave as ~/bin/git
# (cav-rs/src/bin/git_wrapper.rs). The reason is the same here and stated in
# the guest's own CLAUDE.md: nothing an agent writes is visible outside the VM
# until it is pushed, and the guest disk is cattle, so a commit that sits
# unpushed is one `vmcav image reformat` away from never having existed. Asking
# the agent to remember `git push` is a contract that fails quietly, once, at
# the worst moment.
#
# Installed as the system's `git` rather than earlier on PATH than it: PATH
# order is a thing a subprocess, a unit file or a login shell can each get
# wrong on its own, and there is nothing here that wants an unsynced git.
# Everything below calls @REAL_GIT@ directly, so the wrapper cannot re-enter
# itself however it was invoked.
#
# The rules for whether to push, all of them narrowing:
#   - the command succeeded (a failed git has moved nothing worth sending)
#   - HEAD's tip actually changed, so read-only git stays read-only
#   - HEAD is on a branch, not detached
#   - the branch is agent-<id>/<base>, the only namespace the host's
#     pre-receive accepts. `main` tests this the other way round because a
#     container agent works on the base branch's name and pushes to its own;
#     here clone-repo@ puts the guest on the agent branch itself, so the same
#     rule -- push only what the gate would take -- reads inverted.
#   - no path segment is a worktree-* scratch branch, which is where claude
#     parks its side checkouts. Cave-private by design; `main` skips them too.
#   - there is a host-cave remote to push to
#
# A push that fails is reported and does not change the exit status: the
# command you ran succeeded, and claiming otherwise would make every failed
# push look like a failed commit.
set -uo pipefail

real=@REAL_GIT@

# An escape hatch for the case this wrapper is the thing under test, and for
# anything that manipulates a repo it does not mean to publish.
if [ -n "${CAVE_GIT_NO_SYNC:-}" ]; then
  exec "$real" "$@"
fi

# -C decides which repository the command is about, so every question below
# has to be asked about the same one. Only the first is honoured, which is
# also all git itself documents for the repeated form.
chdir=()
prev=""
for arg in "$@"; do
  if [ "$prev" = "-C" ]; then chdir=(-C "$arg"); break; fi
  prev="$arg"
done

ask() { "$real" "${chdir[@]}" "$@" 2>/dev/null; }
tip() { ask rev-parse --verify --quiet HEAD; }

before=$(tip)

"$real" "$@"
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

after=$(tip)
[ "$before" != "$after" ] || exit 0

branch=$(ask symbolic-ref --quiet --short HEAD) || exit 0
[ -n "$branch" ] || exit 0

case "$branch" in
  agent-*/*) ;;
  *) exit 0 ;;
esac
case "/$branch" in
  */worktree-*) exit 0 ;;
esac

# By name, never "the only remote there is": host-cave is the host's copy,
# behind the approval gate, and it is the one place a push from in here may
# go. A repo with some other remote is a repo this has no business publishing.
ask remote get-url host-cave >/dev/null || exit 0

# --force, because a rebase onto a moved base is the ordinary way this branch
# advances and it is not a fast-forward. It is not a licence to overwrite
# anyone: the host's pre-receive allows a non-fast-forward only over history
# this agent itself pushed, and refuses it otherwise.
if ! out=$("$real" "${chdir[@]}" push --force --quiet host-cave \
            "HEAD:refs/heads/$branch" 2>&1); then
  # All of it on stderr. git's stdout is something callers parse, and a
  # wrapper that mixes its own remarks into it breaks them in ways that look
  # like git having gone mad.
  {
    printf 'cave-git: pushing %s to host-cave failed:\n' "$branch"
    while IFS= read -r line; do printf '  %s\n' "$line"; done <<<"$out"
    printf 'cave-git: the commit is safe here, and only here.\n'
  } >&2
fi
exit 0
