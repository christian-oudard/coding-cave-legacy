package main

import (
	"strings"
	"testing"
)

func renderSys(booted, current string, reachable bool) string {
	var b strings.Builder
	renderSystem(&b, booted, current, reachable)
	return b.String()
}

func TestSystemSaysNothingTwiceWhenTheyAgree(t *testing.T) {
	got := renderSys("/nix/store/x-sys", "/nix/store/x-sys", true)
	if got != "system: /nix/store/x-sys\n" {
		t.Errorf("agreement should be one line: %q", got)
	}
}

// The case worth seeing: `cave-rebuild` in the guest moves the running system
// and leaves what the host boots alone, so a stop and start comes back to the
// first of these.
func TestSystemShowsBothWhenTheyDiffer(t *testing.T) {
	got := renderSys("/nix/store/old", "/nix/store/new", true)
	for _, want := range []string{"booted: /nix/store/old", "current: /nix/store/new"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in %q", want, got)
		}
	}
}

// An unreachable guest has not told us its system; "unreachable" is the
// honest value, and the booted one is still known from this side.
func TestSystemWithAnUnreachableGuest(t *testing.T) {
	got := renderSys("/nix/store/old", "", false)
	if !strings.Contains(got, "booted: /nix/store/old") || !strings.Contains(got, "current: unreachable") {
		t.Errorf("got %q", got)
	}
	// Neither side known: the shell printed "system: " with nothing after it,
	// which reads as a system called nothing.
	blank := renderSys("", "", false)
	if strings.Contains(blank, "system: \n") {
		t.Errorf("an empty system was printed as a value: %q", blank)
	}
	if !strings.Contains(blank, "unknown") {
		t.Errorf("want an explicit unknown: %q", blank)
	}
}
