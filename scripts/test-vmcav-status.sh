#!/usr/bin/env bash
# test-vmcav-status.sh: self-asserting test for what `vmcav status` says about
# the guest being out of date. Sources vmcav for its functions, builds a
# throwaway repo and state dir, and never starts a VM. Offline and fast.
#
# The three behaviours under test, each of which was once wrong:
#   - a stopped VM reported nothing about staleness, which is the moment it
#     matters, since `start` boots what the disk says
#   - a deploy from a dirty tree recorded a bare commit, so the next status
#     compared HEAD to HEAD and called a guest nothing describes up to date
#   - a failing rev-parse truncated a good record, after which status was
#     silent for good
set -uo pipefail

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vmcav}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
unset CODING_CAVE_AGENT_NAME 2>/dev/null || true
fail=0

git init -q "$work/repo"
mkdir -p "$work/repo/guest" "$work/repo/cave-vm" "$work/repo/docs"
echo base > "$work/repo/guest/configuration.nix"
echo base > "$work/repo/docs/notes.md"
git -C "$work/repo" add -A
git -C "$work/repo" -c user.email=t@t -c user.name=t commit -q -m base

export REPO="$work/repo" STATE_DIR="$work/state"
mkdir -p "$STATE_DIR"
# shellcheck disable=SC1090
source "$SRC"

commit() { # commit() <path> <text>
  printf '%s\n' "$2" > "$REPO/$1"
  git -C "$REPO" add -A
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "$2"
}

check() { # check() <name> <expected-substring-or-empty> <actual>
  local name="$1" want="$2" got="$3"
  if [ -z "$want" ]; then
    [ -z "$got" ] && return 0
    echo "FAIL $name: expected silence, got: $got"; fail=1; return 0
  fi
  case "$got" in *"$want"*) return 0 ;; esac
  echo "FAIL $name: expected '$want', got: ${got:-<silence>}"; fail=1
}

warn() { warn_if_guest_is_behind 2>&1; }

# --- no record at all: silent, because a permanent warning about a one-off
# state is worse than the state ---
check "no record" "" "$(warn)"

# --- clean tree, recorded, unchanged: silent ---
record_deployed_from
check "clean and current" "" "$(warn)"

# --- a commit under guest/: behind ---
commit guest/configuration.nix changed
check "guest commit" "vmcav deploy to catch it up" "$(warn)"

# --- a commit touching only docs/ also warns. Pinned deliberately: the
# comparison is by commit, and erring toward a mostly-cached rebuild is the
# documented trade ---
record_deployed_from
commit docs/notes.md changed
check "docs commit" "vmcav deploy to catch it up" "$(warn)"

# --- dirty at deploy time is recorded as dirty, and warns even though HEAD
# has not moved. This is the false all-clear the bare commit used to give ---
record_deployed_from   # clean baseline first
echo wip > "$REPO/guest/configuration.nix"
record_deployed_from
case "$(cat "$STATE_DIR/deployed-from")" in
  *-dirty) ;;
  *) echo "FAIL dirty marker: deployed-from has no -dirty suffix"; fail=1 ;;
esac
check "dirty deploy" "built from uncommitted changes" "$(warn)"

# --- an untracked file under guest/ counts: the build copies the directory,
# so a file git has never seen is still an input ---
git -C "$REPO" checkout -q -- guest
touch "$REPO/guest/extra.nix"
record_deployed_from
check "untracked input" "built from uncommitted changes" "$(warn)"
rm -f "$REPO/guest/extra.nix"

# --- dirt outside the build's inputs does not count, or the warning would
# stand while any doc was edited and nobody would read it ---
echo scratch > "$REPO/docs/notes.md"
record_deployed_from
check "docs dirt ignored" "" "$(warn)"
git -C "$REPO" checkout -q -- docs

# --- a failing rev-parse leaves the existing record alone rather than
# truncating it to empty ---
record_deployed_from
before="$(cat "$STATE_DIR/deployed-from")"
( REPO="$work/not-a-repo"; mkdir -p "$REPO"; record_deployed_from )
[ "$(cat "$STATE_DIR/deployed-from")" = "$before" ] \
  || { echo "FAIL failed rev-parse destroyed the record"; fail=1; }

# --- status with the VM stopped. Stubbed rather than started: the point is
# which branch speaks, not the VM ---
is_running() { return 1; }
cave_vm() { return 0; }
DISK="$work/state/cave-guest.raw"

out="$(do_status 2>&1)"
check "no image" "no image yet" "$out"
case "$out" in *"vm: stopped"*) echo "FAIL no image: said stopped too"; fail=1 ;; esac

: > "$DISK"
commit guest/configuration.nix later
record_deployed_from
commit guest/configuration.nix later-still
out="$(do_status 2>&1)"
check "stopped and stale" "vmcav deploy to catch it up" "$out"
check "stopped says stopped" "vm: stopped" "$out"

[ "$fail" = 0 ] && echo "test-vmcav-status: ok"
exit "$fail"
