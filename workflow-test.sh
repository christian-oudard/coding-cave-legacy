#!/usr/bin/env bash
# Full workflow test for cav git commands.
# Works inside a cave by pre-creating the host-cave-shared.git stub
# that cav init requires (normally a bind-mount from parent; here we
# create it as a bare clone of the test repo so alternates work).
set -uo pipefail

# Use the Rust cav build directly so sub-caves get the Rust binaries,
# not the Python cav that launched this cave session.
CAV_RESULT=/projects/coding-cave/result
if [ -x "$CAV_RESULT/bin/cav" ]; then
    export PATH="$CAV_RESULT/bin:$PATH"
fi

PASS=0
FAIL=0

check() {
    local name="$1" got="$2" pat="$3"
    if echo "$got" | grep -qF "$pat"; then
        echo "PASS: $name"
        ((PASS++)) || true
    else
        echo "FAIL: $name"
        echo "  expected to contain: $pat"
        printf '  got: %s\n' "$(echo "$got" | head -4)"
        ((FAIL++)) || true
    fi
}

check_absent() {
    local name="$1" got="$2" pat="$3"
    if echo "$got" | grep -qF "$pat"; then
        echo "FAIL: $name (should NOT contain '$pat')"
        printf '  got: %s\n' "$(echo "$got" | head -4)"
        ((FAIL++)) || true
    else
        echo "PASS: $name"
        ((PASS++)) || true
    fi
}

check_rc() {
    local name="$1" rc="$2" expected="$3"
    if [ "$rc" -eq "$expected" ]; then
        echo "PASS: $name (exit $rc)"
        ((PASS++)) || true
    else
        echo "FAIL: $name (expected exit $expected, got $rc)"
        ((FAIL++)) || true
    fi
}

echo "=== Setup test repo ==="
TESTDIR=$(mktemp -d /tmp/cav-wf-XXXXXX)
cd "$TESTDIR"
git init -b main -q
git config user.email "test@test.com"
git config user.name "Test User"
echo "# Workflow Test" > README.md
git add README.md
git commit -q -m "initial commit"

# Inside a cave, cav init requires .coding-cave/host-cave-shared.git to
# be a bind-mount from the parent. Stub it as a bare clone of this repo.
mkdir -p .coding-cave
git clone --bare -q . .coding-cave/host-cave-shared.git
echo "(host-cave-shared.git stub created)"

echo ""
echo "=== cav init ==="
out=$(cav init . 2>&1); rc=$?
check_rc "cav init exit" $rc 0
check "cav init output" "$out" "initialized"

echo ""
echo "=== cav status (no agents) ==="
out=$(cav status 2>&1); rc=$?
check_rc "cav status exit" $rc 0
check "status empty" "$out" "No running agents"

echo ""
echo "=== cav agent: make first commit ==="
cav agent --prompt "git commit --allow-empty -m 'feat: add login page'"
check_rc "cav agent exit" $? 0

echo ""
echo "=== cav status (agent has 1 commit) ==="
out=$(cav status 2>&1); rc=$?
check_rc "cav status exit" $rc 0
check "status shows agent" "$out" "@"
check "status shows commit" "$out" "feat: add login page"
# Capture agent ID for later
AGENT_ID=$(echo "$out" | grep -oP '(?<=@)\d+' | head -1)

echo ""
echo "=== cav fetch (git fetch cave) ==="
out=$(git fetch cave 2>&1); rc=$?
check_rc "cav fetch exit" $rc 0
echo "  fetch output: $out"

echo ""
echo "=== cav approve -y ==="
out=$(cav approve -y 2>&1); rc=$?
check_rc "cav approve exit" $rc 0
check "approve output" "$out" "Approved"

log=$(git log --oneline -1 2>&1)
check "main fast-forwarded" "$log" "feat: add login page"

echo ""
echo "=== cav status (no unmerged after approve) ==="
out=$(cav status 2>&1); rc=$?
check_rc "cav status exit" $rc 0
check_absent "no unmerged commits" "$out" "feat: add login page"

echo ""
echo "=== cav push (host main -> cave remote) ==="
out=$(env -u CODING_CAVE_VERSION cav push 2>&1); rc=$?
check_rc "cav push exit" $rc 0
echo "  push output: $out"
if echo "$out" | grep -qF "Already up to date"; then
    check "cav push result" "$out" "Already up to date"
else
    check "cav push result" "$out" "Pushed"
fi

echo ""
echo "=== second agent commit (for reject test) ==="
cav agent --prompt "git commit --allow-empty -m 'feat: add logout page'"
check_rc "second agent exit" $? 0

out=$(cav status 2>&1)
check "status shows second commit" "$out" "feat: add logout page"

git fetch cave 2>&1 > /dev/null || true
out=$(cav approve -y 2>&1); rc=$?
check_rc "second approve exit" $rc 0
check "second approve output" "$out" "Approved"

log=$(git log --oneline -1 2>&1)
check "main at second commit" "$log" "feat: add logout page"

echo ""
echo "=== cav reject -y ==="
out=$(cav reject -y 2>&1); rc=$?
check_rc "cav reject exit" $rc 0
check "reject output" "$out" "Rejected"

log=$(git log --oneline -1 2>&1)
check "main reverted after reject" "$log" "feat: add login page"

echo ""
echo "=== cav checkout (retarget agent to feature branch) ==="
git checkout -q -b feature/test
git checkout -q main
out=$(env -u CODING_CAVE_VERSION cav checkout feature/test "$AGENT_ID" 2>&1); rc=$?
check_rc "cav checkout exit" $rc 0
check "checkout output" "$out" "Retargeted"

echo ""
echo "=== cav remove-agent ==="
aid=$(cav status 2>&1 | grep -oP '(?<=@)\d+' | head -1)
[ -z "$aid" ] && aid="${AGENT_ID:-1}"
out=$(cav remove-agent "$aid" 2>&1); rc=$?
check_rc "cav remove-agent exit" $rc 0
check "remove-agent output" "$out" "Removed"

out=$(cav status 2>&1)
check "status empty after remove" "$out" "No running agents"

echo ""
echo "=== Cleanup ==="
cd /tmp
rm -rf "$TESTDIR"

echo ""
echo "=============================="
echo "Results: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && echo "All tests passed!" || exit 1
