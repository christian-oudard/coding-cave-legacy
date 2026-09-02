#!/usr/bin/env bash
# test-vmcav-cp.sh: self-asserting test for `vmcav cp`'s addressing. Sources
# vmcav, stubs scp and the guest, and copies nothing.
#
# The transport is scp over the same ssh config as `vmcav shell`, and was
# already known to work. What is under test is the half that was missing:
# turning `cave:notes.md` into the path that file actually has in the guest,
# which is a layout nobody should have to hold in their head and which the
# agent itself cannot name, since inside its namespace the same tree is
# /projects/<slug>.
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

is_running() { return 0; }
RUNNING=""
guest_rpc() { [ "$1" = agents ] && printf '%s' "$RUNNING"; return 0; }
# To a file: do_cp's caller runs it in a command substitution in these tests,
# and a subshell's assignments do not come back.
scp() { printf '%s\n' "$*" > "$work/scp"; return 0; }
scp_args() { cat "$work/scp" 2>/dev/null; }

has() { case "$3" in *"$2"*) echo "ok   $1"; return 0 ;; esac
        echo "FAIL $1 (wanted '$2', got: ${3:-<nothing>})"; fail=1; }
lacks() { case "$3" in *"$2"*) echo "FAIL $1 (should not contain '$2')"; fail=1; return 0 ;; esac
          echo "ok   $1"; }

RUNNING=$'2 running proj main\n'

# --- out of the worktree ---
rm -f "$work/scp"; do_cp cave:notes.md . >/dev/null 2>&1 || true
has "a bare cave: path is the agent's worktree" \
  "cave-guest:/var/lib/coding-cave/agents/2/projects/proj/notes.md" "$(scp_args)"
has "and the local side is left alone" " ." "$(scp_args)"

# --- the home, which is not the worktree ---
rm -f "$work/scp"; do_cp 'cave:~/.claude/settings.json' . >/dev/null 2>&1 || true
has "~ is the agent's home" "cave-guest:/home/agent2/.claude/settings.json" "$(scp_args)"
lacks "and not a directory called ~ in the worktree" "projects/proj/~" "$(scp_args)"

# An absolute path is the guest's, as written: it is how you reach anything
# that is not the agent's own two directories.
rm -f "$work/scp"; do_cp cave:/etc/os-release . >/dev/null 2>&1 || true
has "an absolute cave: path is passed through" "cave-guest:/etc/os-release" "$(scp_args)"

# --- inward ---
rm -f "$work/scp"; do_cp "$work/data.csv" cave: >/dev/null 2>&1 || true
has "an empty cave: is the worktree itself" \
  "cave-guest:/var/lib/coding-cave/agents/2/projects/proj" "$(scp_args)"

# --- which agent ---
# The same rule as every other command: one running agent needs no name.
RUNNING=$'2 running proj main\n5 running proj main\n'
out=$(do_cp cave:x . 2>&1 || true)
has "two agents is not guessed at" "more than one agent is running" "$out"

rm -f "$work/scp"; do_cp cave@5:x . >/dev/null 2>&1 || true
has "naming one picks it" "agents/5/projects/proj/x" "$(scp_args)"

# The root agent's home is /root, not /home/agent0: it is root.
rm -f "$work/scp"; do_cp "cave@$ROOT_AGENT_ID:~/x" . >/dev/null 2>&1 || true
has "the root agent's home is /root" "cave-guest:/root/x" "$(scp_args)"

# Two different agents in one command would make every argument ambiguous.
out=$(do_cp cave@2:a cave@5:b 2>&1 || true)
has "two agents in one command is refused" "one command, one agent" "$out"

out=$(do_cp cave@x:a . 2>&1 || true)
has "a non-numeric agent is refused" "does not name an agent" "$out"

# Neither side in the cave is plain cp, and saying so beats copying two local
# files through an ssh transport for no reason.
out=$(do_cp a b 2>&1 || true)
has "a copy with no cave: side is refused" "this is just cp" "$out"

out=$(do_cp cave:only-one 2>&1 || true)
has "one argument is refused" "usage: vmcav cp" "$out"

# An agent whose project the guest cannot name -- a dash is how it prints a
# field it has nothing for. A relative path then has nothing to be relative
# to, but the agent is still reachable by a path that needs no project.
RUNNING=$'7 running - -\n'
out=$(do_cp cave@7:rel . 2>&1 || true)
has "a relative path with no project is refused" "nothing to be relative to" "$out"
rm -f "$work/scp"; do_cp cave@7:/etc/os-release . >/dev/null 2>&1 || true
has "but an absolute one still reaches it" "cave-guest:/etc/os-release" "$(scp_args)"

[ "$fail" = 0 ] && echo "test-vmcav-cp: ok"
exit "$fail"
