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
#
# Only git's own -C, which is the one before the subcommand. A subcommand's
# -C is a different option that happens to share the letter, and `git commit
# -C <commit>` -- reuse that commit's message -- is the common one. Scanning
# the whole argument list took its value for a directory, so every question
# below asked about a path that does not exist: the tip read empty before and
# after, compared equal, and the commit was never pushed. Silently, which is
# the one failure this wrapper exists to prevent. `git commit --amend -C HEAD`
# is the same shape and is more common still.
chdir=()
args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  case "${args[$i]}" in
    -C) chdir=(-C "${args[$((i + 1))]:-}"); break ;;
    # -c takes a value of its own, so skip both and stay aligned.
    -c) i=$((i + 2)) ;;
    # Any other global option. One that takes a separate value
    # (`--git-dir <path>`) ends the scan at that value, which is read as the
    # subcommand and leaves chdir unset -- the safe way to be wrong, since
    # everything below then asks about the working directory.
    -*) i=$((i + 1)) ;;
    # The subcommand. Everything after it belongs to it, not to git.
    *) break ;;
  esac
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

# This agent's own namespace, not merely some agent's.
#
# The host cannot check this. Every agent in the guest pushes through one
# socket, so the id its daemon injects describes the guest rather than the
# pusher, and it stopped trying: it now accepts any well-formed agent
# namespace and refuses base branches, which is what it can actually know.
# The answer is known here, so the check belongs here.
#
# Said plainly, because it is weaker than it looks: this stops an agent
# publishing over another's branch by accident, not on purpose. An agent that
# edited this file, or ran git itself, would get past it. Making it hold
# against that needs cave-guestd to stamp each connection from the connecting
# process's uid -- agents are separate uids now, so the kernel knows the
# answer even when the process lies (docs/vm-cave-todo.md).
#
# Only when the agent id is known: a human running git from `vmcav shell` has
# no CAVE_AGENT_ID, and is not an agent to be held to an agent's namespace.
if [ -n "${CAVE_AGENT_ID:-}" ]; then
  case "$branch" in
    "agent-$CAVE_AGENT_ID/"*) ;;
    *)
      printf 'cave-git: not pushing %s: this is agent %s, so its branches are agent-%s/*\n' \
        "$branch" "$CAVE_AGENT_ID" "$CAVE_AGENT_ID" >&2
      exit 0
      ;;
  esac
fi

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
