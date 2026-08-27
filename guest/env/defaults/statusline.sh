#!/usr/bin/env bash
# Coding Cave default statusline for the VM guest.
#
# Deliberately not `main`'s, which is `exec cav-host agent-tag`: that asks the
# host, over the comms channel, for a byline the host already knows. There is
# no cav-host in here and the comms plane does not exist yet, so this answers
# from what the guest itself knows.
#
# Three facts, which are the three an agent in here can get wrong: which agent
# it is, which model is answering, and which branch its worktree is on. The
# branch matters more here than on a host, because committing is what
# publishes and the branch is what decides whether the push is allowed.
#
# Segments beyond these are the user's to compose by overriding
# statusLine.command in cave.nix.

set -u

input=$(cat)

model="?"
dir="$PWD"

# One jq pass for both fields: a statusline is re-rendered often enough that a
# second process per render is worth not spending.
#
# IFS is set to a tab alone, and that is not a detail. With the default IFS the
# split would also happen on spaces, so every model whose name has one in it --
# "Opus 5", "Claude Haiku 4.5" -- would put half of itself in $dir and leave
# the directory empty. The failure is silent and looks like a missing model.
if command -v jq >/dev/null 2>&1; then
  fields=$(printf '%s' "$input" | jq -r '
    [(.model.display_name // "?"), (.workspace.current_dir // "")] | @tsv
  ' 2>/dev/null) || fields=""
  if [ -n "$fields" ]; then
    IFS=$'\t' read -r model dir <<<"$fields"
    [ -n "$dir" ] || dir="$PWD"
  fi
fi

# git from PATH is the cave wrapper, which pushes after any command that moves
# a branch. symbolic-ref moves nothing, so this is safe -- but it is worth
# knowing that the statusline runs the wrapper, because anything added to this
# script that does move a branch would publish on every render.
branch=$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null) || branch=""
[ -n "$branch" ] || branch="detached"

printf 'agent %s · %s · %s' "${CAVE_AGENT_ID:-?}" "$model" "$branch"
