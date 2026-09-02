#!/usr/bin/env bash
# test-cave-git.sh: self-asserting test for the guest's git wrapper
# (guest/cave-git.sh), the thing that makes a commit in the VM reach the host
# without the agent remembering to push.
#
# Runs the real script against throwaway repos, with @REAL_GIT@ substituted
# for the git on PATH, so what is tested is the file that gets installed
# rather than a description of it. Offline and fast: host-cave is a local bare
# repo, which is the same shape as the vsock remote minus the transport.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SRC="$REPO/guest/cave-git.sh"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
# Stated, not inherited. cave-git pushes only the running agent's own
# namespace, and these fixtures work on agent-1/*, so a test run from inside a
# real agent's session used to pick that agent's id out of the environment and
# fail for reasons that had nothing to do with what it was testing.
export CAVE_AGENT_ID=1
fail=0

# The git underneath the wrapper has to be a plain one. In the guest that is
# ${pkgs.git} and the question never arises, but this test also runs inside a
# deployed guest, where `git` on PATH is the wrapper itself -- and then every
# setup command syncs, and the wrapper's own push runs through a second copy
# of the thing under test. So: a shim that turns syncing off for anything
# below this line, while the wrapper being tested is still invoked without it.
real="$work/plain-git"
cat > "$real" <<EOF
#!$(command -v bash)
exec env CAVE_GIT_NO_SYNC=1 $(command -v git) "\$@"
EOF
chmod +x "$real"

# The wrapper, built the way the guest builds it: the same two substitutions
# configuration.nix makes, against what is on PATH here rather than store
# paths.
cavegit="$work/git"
sed -e "1s#.*##" -e "1s#^#\#!$(command -v bash)#" \
    -e "s#@REAL_GIT@#$real#" "$SRC" > "$cavegit"
chmod +x "$cavegit"

ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fail=1; }
check() { # check <label> <want> <got>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want $2, got $3)"; fi
}

# fresh <branch> -> a checkout on <branch> with a bare host-cave, one commit in.
# Nothing is pushed yet, so host-cave has no branches at all until the wrapper
# decides to make one.
fresh() {
  local branch="$1" n=$((++repo_n))
  d="$work/r$n"; bare="$work/o$n.git"
  "$real" init -q --bare "$bare"
  "$real" init -q -b main "$d"
  "$real" -C "$d" remote add host-cave "$bare"
  echo one > "$d/f.txt"
  "$real" -C "$d" add -A
  "$real" -C "$d" commit -q -m first
  "$real" -C "$d" checkout -q -B "$branch"
}
repo_n=0

# remote_tip <bare> <branch> -> the sha host-cave has, or empty
remote_tip() {
  "$real" -C "$1" rev-parse --verify --quiet "refs/heads/$2" || true
}

# --- The point of the whole thing: a commit lands on the host ---
fresh "agent-1/main"
echo two > "$d/f.txt"
"$cavegit" -C "$d" commit -qam second
check "a commit on an agent branch is pushed" \
  "$("$real" -C "$d" rev-parse HEAD)" "$(remote_tip "$bare" agent-1/main)"

# The wrapper must be transparent: same exit status, same output stream, and
# a second commit keeps the branch current rather than pushing only the first.
echo three > "$d/f.txt"
"$cavegit" -C "$d" commit -qam third
check "a later commit keeps host-cave current" \
  "$("$real" -C "$d" rev-parse HEAD)" "$(remote_tip "$bare" agent-1/main)"

# --- A rewrite is a force-push, because a rebase is how this branch moves ---
"$cavegit" -C "$d" reset -q --hard HEAD~1
check "a rewritten history is force-pushed" \
  "$("$real" -C "$d" rev-parse HEAD)" "$(remote_tip "$bare" agent-1/main)"

# --- Everything that must not push ---
fresh "main"
echo two > "$d/f.txt"
"$cavegit" -C "$d" commit -qam second
check "a base branch is never pushed" "" "$(remote_tip "$bare" main)"

fresh "agent-1/worktree-side"
echo two > "$d/f.txt"
"$cavegit" -C "$d" commit -qam second
check "a worktree-* scratch branch is not pushed" \
  "" "$(remote_tip "$bare" agent-1/worktree-side)"

# Another agent's namespace, which the host can no longer refuse: its injected
# id describes the guest rather than the pusher, so this is the check that
# keeps an agent's branches its own. It stops a mistake, not an attacker --
# anything that runs real git gets past it.
fresh "agent-2/main"
echo two > "$d/f.txt"
"$cavegit" -C "$d" commit -qam second 2>/dev/null
check "another agent's namespace is not pushed" "" "$(remote_tip "$bare" agent-2/main)"

fresh "agent-1/main"
"$cavegit" -C "$d" status --porcelain >/dev/null
"$cavegit" -C "$d" log --oneline >/dev/null
check "a read-only command pushes nothing" "" "$(remote_tip "$bare" agent-1/main)"

fresh "agent-1/main"
"$real" -C "$d" checkout -q --detach
echo two > "$d/f.txt"
"$cavegit" -C "$d" commit -qam second
check "a detached HEAD is not pushed" "" "$(remote_tip "$bare" agent-1/main)"

fresh "agent-1/main"
CAVE_GIT_NO_SYNC=1 "$cavegit" -C "$d" commit -q --allow-empty -m second
check "CAVE_GIT_NO_SYNC turns it off" "" "$(remote_tip "$bare" agent-1/main)"

# A repo with no host-cave is not an error to report, it is a repo with nowhere
# to push: the guest runs git over plenty of them.
fresh "agent-1/main"
"$real" -C "$d" remote remove host-cave
if "$cavegit" -C "$d" commit -q --allow-empty -m second 2>"$work/err"; then
  if [ -s "$work/err" ]; then bad "no host-cave stays quiet ($(cat "$work/err"))"
  else ok "no host-cave stays quiet"; fi
else bad "no host-cave made the commit fail"
fi

# The remote is chosen by name, not by being the only one: a repo whose remote
# is somebody else's is not one to publish out of the VM.
fresh "agent-1/main"
"$real" -C "$d" remote rename host-cave origin
"$cavegit" -C "$d" commit -q --allow-empty -m second
check "a remote by another name is not pushed to" "" "$(remote_tip "$bare" agent-1/main)"

# --- Transparency: git's own exit status is what the caller sees ---
fresh "agent-1/main"
"$cavegit" -C "$d" commit -qam "nothing staged" >/dev/null 2>&1
check "a failed git keeps its exit status" "1" "$?"
check "a failed git pushes nothing" "" "$(remote_tip "$bare" agent-1/main)"

"$cavegit" -C "$d" rev-parse --verify HEAD >/dev/null 2>&1
check "a successful git keeps its exit status" "0" "$?"

# --- A push that cannot land is a warning, not a failed commit ---
# host-cave is made unreachable after the branch exists, which is what a stopped
# host bridge looks like from in here.
fresh "agent-1/main"
echo two > "$d/f.txt"
"$cavegit" -C "$d" commit -qam second
rm -rf "$bare"
echo three > "$d/f.txt"
if "$cavegit" -C "$d" commit -qam third >"$work/out" 2>"$work/err"; then
  ok "a failed push does not fail the commit"
else
  bad "a failed push failed the commit"
fi
if grep -q "cave-git" "$work/err"; then ok "a failed push says so"
else bad "a failed push was silent"; fi
# Every word of the complaint belongs on stderr: git's stdout is parsed.
if [ -s "$work/out" ]; then bad "a failed push wrote to stdout ($(cat "$work/out"))"
else ok "a failed push says nothing on stdout"; fi
check "the commit itself was made" "third" \
  "$("$real" -C "$d" log -1 --format=%s)"

# --- The cwd form, not just -C ---
fresh "agent-1/main"
( cd "$d" && echo two > f.txt && "$cavegit" commit -qam second )
check "it works from the working directory too" \
  "$("$real" -C "$d" rev-parse HEAD)" "$(remote_tip "$bare" agent-1/main)"


# --- git's -C, and a subcommand's ---
# `-C` before the subcommand names a directory; after it, it belongs to the
# subcommand and usually means something else entirely. `git commit -C <commit>`
# reuses that commit's message, and taking its value for a directory made the
# wrapper ask every question about a path that does not exist: the tip read
# empty before and after, compared equal, and the commit was never pushed --
# silently, which is the one failure this wrapper exists to prevent.
#
# Run from inside the repository, with no leading -C. With one, the scan finds
# git's own first and the subcommand's never gets looked at -- so a test that
# passes -C <dir> here proves nothing about this, and passed against the old
# scan too.
fresh "agent-1/main"
"$real" -C "$d" commit -q --allow-empty -m "a message worth reusing"
reused=$("$real" -C "$d" rev-parse HEAD)
( cd "$d" && echo three > f.txt && "$real" add -A && "$cavegit" commit -q -C "$reused" )
check "a commit reusing a message is still pushed" \
  "$("$real" -C "$d" rev-parse HEAD)" "$(remote_tip "$bare" agent-1/main)"

# --amend -C HEAD is the same shape and more common.
fresh "agent-1/main"
( cd "$d" && echo four > f.txt && "$real" add -A && "$real" commit -q -m "to be amended" )
( cd "$d" && echo five > f.txt && "$real" add -A && "$cavegit" commit -q --amend -C HEAD )
check "an amend reusing a message is pushed" \
  "$("$real" -C "$d" rev-parse HEAD)" "$(remote_tip "$bare" agent-1/main)"

# And git's own -C still decides which repository is meant: without it the
# wrapper would ask about the directory the test is standing in.
fresh "agent-1/main"
echo six > "$d/f.txt"
"$real" -C "$d" add -A
( cd "$work" && "$cavegit" -C "$d" commit -q -m "from elsewhere" )
check "git's own -C still names the repository" \
  "$("$real" -C "$d" rev-parse HEAD)" "$(remote_tip "$bare" agent-1/main)"
[ "$fail" = 0 ] && echo "PASS" || echo "FAIL"
exit "$fail"
