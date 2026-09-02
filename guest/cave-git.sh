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

# git's own options, the ones before the subcommand. They decide which
# repository the command is about, so every question below has to be asked
# with the same ones or it is asking about a different repository -- and a
# wrapper that cannot see the tip move concludes nothing happened and pushes
# nothing, silently, which is the one failure it exists to prevent.
#
# All of them are carried rather than the few that obviously name a
# repository. -C, --git-dir and --work-tree each select one, and there is no
# reason for this to hold its own list of which options matter when passing
# them all asks exactly the question the command answered.
#
# The scan stops at the subcommand, and that is the other half. After it the
# same spellings belong to the subcommand and usually mean something else:
# `git commit -C <commit>` reuses that commit's message, and `git commit
# --amend -C HEAD` is more common still. Reading the whole argument list took
# those for a directory.
#
# Both halves were bugs, and both were found by running them rather than
# reasoned about: a commit reusing a message, and a commit made through
# --git-dir, each stayed in the VM.
globals=()
args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  case "${args[$i]}" in
    # The two that take a separate value. Carried whole, so the scan stays
    # aligned and the value is never read as the subcommand.
    -C|-c) globals+=("${args[$i]}" "${args[$((i + 1))]:-}"); i=$((i + 2)) ;;
    # Everything else git accepts before a subcommand, including the
    # --option=value spellings of the two above.
    -*) globals+=("${args[$i]}"); i=$((i + 1)) ;;
    # The subcommand. Globals end here: after it the same spellings belong to
    # the subcommand and usually mean something else entirely.
    *) break ;;
  esac
done

ask() { "$real" "${globals[@]}" "$@" 2>/dev/null; }
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
if ! out=$("$real" "${globals[@]}" push --force --quiet host-cave \
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
