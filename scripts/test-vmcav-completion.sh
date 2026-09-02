#!/usr/bin/env bash
# test-vmcav-completion.sh: self-asserting test for the completion scripts and
# the values they ask for. Sources vmcav, never starts a VM.
#
# What it pins down: a completion script is code in another shell that nothing
# else parses, so the only way it is known to be valid is by asking that shell;
# and the dynamic half must answer with the guest down, because a shell that
# prints an error where a list belongs is worse than one that offers nothing.
set -uo pipefail

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmcav}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
fail=0

git init -q "$work/repo"
git -C "$work/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
git -C "$work/repo" branch -q vm-cave
git -C "$work/repo" branch -q agent-3/main
export REPO="$work/repo" STATE_DIR="$work/state"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1090
source "$SRC"

has() { case "$3" in *"$2"*) echo "ok   $1"; return 0 ;; esac
        echo "FAIL $1 (wanted '$2', got: ${3:-<silence>})"; fail=1; }
lacks() { case "$3" in *"$2"*) echo "FAIL $1 (should not offer '$2')"; fail=1; return 0 ;; esac
          echo "ok   $1"; }

# --- the scripts are valid in the shell they are for ---
# The whole risk here: this is code in another language, emitted from a
# heredoc, that nothing on the way out ever parses.
do_completion bash > "$work/c.bash"
if bash -n "$work/c.bash" 2>"$work/err"; then echo "ok   the bash script parses as bash"
else echo "FAIL the bash script does not parse: $(cat "$work/err")"; fail=1; fi

do_completion zsh > "$work/c.zsh"
if command -v zsh >/dev/null 2>&1; then
  if zsh -n "$work/c.zsh" 2>"$work/err"; then echo "ok   the zsh script parses as zsh"
  else echo "FAIL the zsh script does not parse: $(cat "$work/err")"; fail=1; fi
else
  echo "skip the zsh script parses as zsh (no zsh here)"
fi
has "the zsh script declares what it completes" "#compdef vmcav" "$(cat "$work/c.zsh")"
has "the bash script registers itself" "complete -F _vmcav vmcav" "$(cat "$work/c.bash")"

# A shell nobody wrote a script for is refused with the two ways to install
# the ones that exist, rather than emitting nothing and exiting quietly.
out=$(do_completion fish 2>&1 || true)
has "an unsupported shell is refused" "usage: vmcav completion" "$out"
has "and says how to install bash's" "bashrc" "$out"
has "and zsh's" "fpath" "$out"

# --- the values the scripts ask for ---
has "commands include the ones a person types most" "status" "$(do_complete commands)"
has "and the newer ones are not forgotten" "env" "$(do_complete commands)"

# Every command in the help table should be offerable. The table is the
# contract; a completion that knows fewer commands than the help does is a
# second list to keep in step, which is the thing this design avoids.
table=$(usage 2>&1 | sed -n 's/^  \([a-z-]\{2,\}\).*/\1/p' | sort -u)
# The extraction has to find something, or this compares two empty lists and
# calls it agreement. It did exactly that when it named a function that does
# not exist.
if [ -z "$table" ]; then
  echo "FAIL the help table could not be read, so this test proves nothing"; fail=1
else
  missing=""
  # Space-separated, because the match below tests for a whole word and the
  # list arrives one per line.
  offered=$(do_complete commands | tr '\n' ' ')
  while read -r cmd; do
    [ -n "$cmd" ] || continue
    case " $offered " in *" $cmd "*) ;; *) missing="$missing $cmd" ;; esac
  done <<EOF
$table
EOF
  if [ -z "$missing" ]; then echo "ok   every command in the help table is offered"
  else echo "FAIL these are in the help but not completed:$missing"; fail=1; fi
fi

# Projects come from the registry, which is the served directory itself.
mkdir -p "$REPOS"
ln -sfn "$work/somewhere.git" "$REPOS/alpha.git"
ln -sfn "$work/elsewhere.git" "$REPOS/beta.git"
has "registered projects are offered" "alpha" "$(do_complete projects)"
has "all of them" "beta" "$(do_complete projects)"

# Bases are branches; an agent's branch is not one, and offering it would be
# offering the mistake `approve` exists to prevent.
has "base branches are offered" "vm-cave" "$(do_complete bases)"
lacks "an agent branch is not a base" "agent-3/main" "$(do_complete bases)"

# --- with no guest ---
# A completion runs on a keystroke. Asking a VM that is not there must be
# instant and silent, not an error where a list belongs.
is_running() { return 1; }
out=$(do_complete agents 2>&1)
if [ -z "$out" ]; then echo "ok   agents are empty rather than an error with no vm"
else echo "FAIL agents printed something with no vm: $out"; fail=1; fi
rc=0; do_complete agents >/dev/null 2>&1 || rc=$?
[ "$rc" = 0 ] && echo "ok   and it still succeeds" \
  || { echo "FAIL asking for agents failed with rc=$rc"; fail=1; }

# An unknown kind is silence and success, for the same reason.
rc=0; out=$(do_complete nonsense 2>&1) || rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] && echo "ok   an unknown kind is silent" \
  || { echo "FAIL unknown kind: rc=$rc out=$out"; fail=1; }

[ "$fail" = 0 ] && echo "test-vmcav-completion: ok"
exit "$fail"
