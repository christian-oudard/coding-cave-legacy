package main

import (
	"strings"
	"testing"
)

func stale(was, now string) string {
	return strings.Join(stalenessLines(was, now), "\n")
}

func TestStalenessSaysNothingWhenThereIsNothingToSay(t *testing.T) {
	// Never recorded: a permanent warning about a one-off state is worse than
	// the state.
	if got := stale("", "abc1234def"); got != "" {
		t.Errorf("no record should be silent: %q", got)
	}
	// Same commit, clean tree.
	if got := stale("abc1234def", "abc1234def"); got != "" {
		t.Errorf("up to date should be silent: %q", got)
	}
}

// The case the shell got wrong. `was` and `now` differ as strings while their
// commits are the same, and stripping "-dirty" from both printed the same
// short sha twice with "deploy to catch it up" -- true, and unreadable.
func TestDirtySinceTheDeployIsNotABehindCommit(t *testing.T) {
	got := stale("abc1234def", "abc1234def-dirty")
	if got == "" {
		t.Fatal("uncommitted changes since the build should be reported")
	}
	if strings.Contains(got, "catch it up") {
		t.Errorf("this is not a behind-commit: %q", got)
	}
	if !strings.Contains(got, "uncommitted changes") {
		t.Errorf("should name what differs: %q", got)
	}
	// And it must not read as two different places.
	if strings.Count(got, short("abc1234def")) > 1 {
		t.Errorf("the same commit was printed twice: %q", got)
	}
}

// A guest built from a dirty tree cannot say what it was built from, whatever
// the checkout is at now.
func TestBuiltFromUncommittedChanges(t *testing.T) {
	for _, now := range []string{"abc1234def", "abc1234def-dirty", "9999999fff", ""} {
		got := stale("abc1234def-dirty", now)
		if !strings.Contains(got, "nothing records what those were") {
			t.Errorf("now=%q: %q", now, got)
		}
	}
}

func TestBehindACommit(t *testing.T) {
	got := stale("abc1234def", "9999999fff")
	if !strings.Contains(got, "abc1234") || !strings.Contains(got, "9999999") {
		t.Errorf("both revisions should appear: %q", got)
	}
	if !strings.Contains(got, "catch it up") {
		t.Errorf("want the deploy hint: %q", got)
	}
	// A dirty checkout ahead of the record is still behind by a commit, and
	// the commit is the thing to say.
	if got := stale("abc1234def", "9999999fff-dirty"); !strings.Contains(got, "9999999") {
		t.Errorf("want the checkout's commit: %q", got)
	}
}

// An unreadable checkout is not something to be behind. Saying the guest is
// behind a revision that cannot be named is worse than saying nothing.
func TestAnUnreadableCheckoutIsSilent(t *testing.T) {
	if got := stale("abc1234def", ""); got != "" {
		t.Errorf("want silence, got %q", got)
	}
}
