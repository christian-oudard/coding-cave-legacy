package main

import (
	"strings"
	"testing"

	"codingcave.dev/cave-vm/internal/control"
)

func render(agents []control.Agent, reachable bool) string {
	var b strings.Builder
	renderAgents(&b, agents, reachable)
	return b.String()
}

// The bug this was moved for. In the shell the roster arrived as text and was
// captured with `$(guest_rpc agents || true)`, which makes "the guest did not
// answer" and "the guest has nothing" the same empty string. A cave with a
// live agent reported "none running" to the agent reading it, and invited a
// second one alongside.
func TestUnreachableGuestIsNotAnEmptyOne(t *testing.T) {
	got := render(nil, false)
	if !strings.Contains(got, "unknown") {
		t.Errorf("an unreachable guest should say so: %q", got)
	}
	if strings.Contains(got, "none running") {
		t.Errorf("an unreachable guest was called empty: %q", got)
	}
	// And the reverse is still said plainly, since it is a different fact.
	if empty := render(nil, true); !strings.Contains(empty, "none running") {
		t.Errorf("a guest that answered nothing should say none: %q", empty)
	}
}

// The other shell bug: the list of running agents was `grep ' running '`,
// which needs a space on both sides, so an agent with no project and no base
// printed as "9 running" and fell out of the count entirely.
func TestAnAgentWithNothingRecordedIsStillCounted(t *testing.T) {
	got := render([]control.Agent{{ID: "9", State: "running"}}, true)
	if !strings.Contains(got, "agents: 1") {
		t.Errorf("an agent with no project was not counted: %q", got)
	}
	if !strings.Contains(got, "agent 9") {
		t.Errorf("an agent with no project was not listed: %q", got)
	}
}

func TestRunningAndStoppedAreDifferentThings(t *testing.T) {
	got := render([]control.Agent{
		{ID: "0", State: "running", Project: "coding-cave", Base: "vm-cave"},
		{ID: "2", State: "stopped", Project: "widgets", Base: "main"},
		// A dash is how the wire form spells a field it has nothing for.
		{ID: "5", State: "stopped", Project: "-", Base: "-"},
	}, true)

	for _, want := range []string{
		"agents: 1",
		"agent 0: coding-cave @ vm-cave (pushes land on agent-0/vm-cave)",
		"agent 2: stopped, still holding its worktree and home",
		"was working on: widgets @ main",
		// Listed even with nothing to say about it: holding an id is the point
		// of the line, and its env file went with the tmpfs.
		"agent 5: stopped, still holding its worktree and home",
		"these hold their ids",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in:\n%s", want, got)
		}
	}
	// A stopped agent with nothing recorded must not claim a project called "-".
	if strings.Contains(got, "was working on: - @ -") {
		t.Errorf("a dash was printed as a project name:\n%s", got)
	}
	// One running agent is one, not three.
	if strings.Contains(got, "agents: 3") {
		t.Errorf("stopped agents were counted as running:\n%s", got)
	}
}

// Nothing at all is the ordinary state of a fresh guest, and it should not
// mention ids nobody is holding.
func TestNothingHeldSaysNothingAboutIds(t *testing.T) {
	got := render(nil, true)
	if strings.Contains(got, "hold their ids") {
		t.Errorf("a guest holding nothing talked about ids: %q", got)
	}
}
