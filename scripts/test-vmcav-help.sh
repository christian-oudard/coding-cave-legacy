#!/usr/bin/env bash
# test-vmcav-help.sh: self-asserting test for `vmcav <command> --help`. Runs
# the script rather than sourcing it, because the dispatch is what is under
# test; no command reached here starts anything.
#
# There is one description of a command, in the usage table, and help_for cuts
# an entry out of it. So what can go wrong is the cutting: an entry that stops
# short of its continuation lines, or runs on into the next command's.
set -uo pipefail

VMCAV="${VMCAV:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmcav}"
fail=0

has() { # has <label> <want-substring> <got>
  case "$3" in *"$2"*) printf 'ok   %s\n' "$1"; return 0 ;; esac
  printf 'FAIL %s (want %s, got %s)\n' "$1" "$2" "${3:-<silence>}"; fail=1
}
hasnt() { # hasnt <label> <unwanted-substring> <got>
  case "$3" in *"$2"*) printf 'FAIL %s (found %s)\n' "$1" "$2"; fail=1; return 0 ;; esac
  printf 'ok   %s\n' "$1"
}

# The case that sent someone into the guest: --help was read as the argument
# after the command, so `vmcav agent --help` set out to create an agent for a
# project called --help.
out=$(bash "$VMCAV" agent --help 2>&1)
has "the command's own line" "agent [--root]" "$out"
has "and its continuation lines" "root in the guest" "$out"
has "and its subcommands" "agent stop" "$out"
hasnt "and nothing after it" "shell" "$out"

# `vmcav help <command>` is the same answer asked the other way round.
has "help <command> agrees" "$out" "$(bash "$VMCAV" help agent 2>&1)"

# An entry whose lines are not adjacent in the table is still one entry.
has "image is both of its lines" "image reformat" "$(bash "$VMCAV" image --help 2>&1)"

# The whole table, when nothing is named.
has "bare help is the table" "usage: vmcav" "$(bash "$VMCAV" help 2>&1)"
has "and --help is too" "usage: vmcav" "$(bash "$VMCAV" --help 2>&1)"

# A command nobody has heard of falls back to the table, on stderr, refusing.
out=$(bash "$VMCAV" nope --help 2>&1); rc=$?
has "an unknown command shows the table" "usage: vmcav" "$out"
[ "$rc" = 2 ] || { echo "FAIL an unknown command exits 0"; fail=1; }

# Every command in the table can be asked about: a line here with no entry
# means the table and the dispatch have drifted apart.
while read -r cmd; do
  [ -n "$(bash "$VMCAV" "$cmd" --help 2>&1)" ] \
    || { echo "FAIL '$cmd' is in the table but has no entry"; fail=1; }
done < <(bash "$VMCAV" help 2>&1 | sed -n 's/^  \([a-z][a-z-]*\).*/\1/p' | sort -u)
echo "ok   every command in the table answers --help"

[ "$fail" = 0 ] && echo "PASS" || echo "FAIL"
exit "$fail"
