#!/usr/bin/env bash
# test-vmcav-env.sh: self-asserting test for carrying the cave environment's
# spec across the boundary. Sources vmcav, stubs the one call that needs a
# guest, and never starts a VM.
#
# What it pins down: neither side is the author, so neither direction may
# quietly overwrite the other's work; and the two ways of having nothing --
# no spec here, no spec there -- are different situations with different
# answers.
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

# Both sides are files here. The guest's is a file the stub reads and writes,
# which is what it is on the other side of the ssh too.
ENV_SPEC="$work/host-cave.nix"
# Under a directory that does not exist yet, which is the state of a guest
# where neither `cave-env edit` nor a build has ever run -- the guest you
# would be pushing a first spec to.
GUEST_FILE="$work/guest-env/cave-nix"
BUILD_FAILS=""
BUILT=0

is_running() { return 0; }
guest_ssh() {
  case "$1" in
    "cat $GUEST_ENV_SPEC"*)
      [ -s "$GUEST_FILE" ] || return 1
      cat "$GUEST_FILE" ;;
    *"cat > $GUEST_ENV_SPEC"*)
      # Faithful to what a shell does with the redirect: it fails unless the
      # parent directory is there, and the command is what has to create it.
      case "$1" in *"install -d"*) mkdir -p "$(dirname "$GUEST_FILE")" ;; esac
      [ -d "$(dirname "$GUEST_FILE")" ] || return 1
      cat > "$GUEST_FILE" ;;
    "cave-env build"*)
      BUILT=$((BUILT + 1))
      [ -n "$BUILD_FAILS" ] && return 1
      return 0 ;;
    *) return 0 ;;
  esac
}

has() { # has <label> <want-substring> <text>
  case "$3" in *"$2"*) echo "ok   $1"; return 0 ;; esac
  echo "FAIL $1 (wanted '$2', got: ${3:-<silence>})"; fail=1
}
check() { # check <label> <want> <got>
  if [ "$2" = "$3" ]; then echo "ok   $1"
  else echo "FAIL $1 (want '$2', got '$3')"; fail=1; fi
}

host_has() { printf '%s\n' "$1" > "$ENV_SPEC"; }
guest_has() { mkdir -p "$(dirname "$GUEST_FILE")"; printf '%s\n' "$1" > "$GUEST_FILE"; }
# Both gone, and the guest's directory with it: a guest that has never built
# an environment does not have one.
nobody_has() { rm -rf "$ENV_SPEC" "$(dirname "$GUEST_FILE")"; }

# --- show ---
nobody_has
has "with no spec anywhere, the defaults are named" "neither side has one" \
  "$(do_env show 2>&1 || true)"

host_has '{ a = 1; }'; guest_has '{ a = 1; }'
has "identical specs are reported as agreeing" "they agree" \
  "$(do_env show 2>&1 || true)"

guest_has '{ a = 2; }'
has "differing specs say which way to resolve it" "vmcav env pull" \
  "$(do_env show 2>&1 || true)"

# --- pull ---
# The direction that matters most today: the guest half of phase 9 can write a
# spec and nothing could ever get it out, so it was one reformat from gone.
nobody_has
has "pulling nothing is refused" "no /var/lib/coding-cave/env/cave.nix to pull" \
  "$(do_env pull 2>&1 || true)"

guest_has '{ from = "guest"; }'
do_env pull >/dev/null 2>&1 || true
check "a spec with nothing to overwrite is pulled" '{ from = "guest"; }' \
  "$(cat "$ENV_SPEC" 2>/dev/null)"

# Same on both sides is not an overwrite, so it must not ask for --force.
has "pulling an identical spec is a no-op" "nothing to pull" \
  "$(do_env pull 2>&1 || true)"

host_has '{ from = "host"; }'; guest_has '{ from = "guest"; }'
out=$(do_env pull 2>&1 || true)
has "pulling over different work is refused" "would be overwritten" "$out"
has "and the other direction is offered" "vmcav env push" "$out"
check "and nothing was written" '{ from = "host"; }' "$(cat "$ENV_SPEC")"

do_env pull --force >/dev/null 2>&1 || true
check "--force takes the guest's" '{ from = "guest"; }' "$(cat "$ENV_SPEC")"

# --- push ---
nobody_has
has "pushing nothing is refused" "no $ENV_SPEC to push" \
  "$(do_env push 2>&1 || true)"

host_has '{ from = "host"; }'
BUILT=0
do_env push >/dev/null 2>&1 || true
check "a spec with nothing to overwrite is pushed" '{ from = "host"; }' \
  "$(cat "$GUEST_FILE" 2>/dev/null)"
check "and it is built rather than left for later" "1" "$BUILT"

guest_has '{ from = "guest"; }'
out=$(do_env push 2>&1 || true)
has "pushing over different work is refused" "would be overwritten" "$out"
check "and nothing crossed" '{ from = "guest"; }' "$(cat "$GUEST_FILE")"

# A spec that lands but does not build is worth hearing about now rather than
# at the next session start, and the file stays so it can be fixed in place.
BUILD_FAILS=1
out=$(do_env push --force 2>&1 || true)
has "a spec that will not build says so" "did not build" "$out"
check "and it is still in the guest to fix" '{ from = "host"; }' "$(cat "$GUEST_FILE")"

[ "$fail" = 0 ] && echo "test-vmcav-env: ok"
exit "$fail"
