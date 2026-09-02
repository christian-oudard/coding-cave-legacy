package main

import (
	"strings"
	"testing"
)

func plan(action string, s EnvState) EnvPlan { return envPlan(action, s) }

// The one that matters. The shell kept the ssh exit status in a variable it
// never read, so an unreachable guest was indistinguishable from a guest with
// no spec. An empty capture is caught by the "nothing to pull" guard; a
// capture that is not empty and is not a spec is not, and that is what
// happened on the host this was reported from -- `env pull` called the local
// cave.nix different from a guest it had not reached, and offered --force.
func TestNothingIsDecidedAgainstAGuestThatHasNotAnswered(t *testing.T) {
	for _, action := range []string{"pull", "push"} {
		for _, force := range []bool{false, true} {
			p := plan(action, EnvState{HostHas: true, Reachable: false, Force: force})
			if p.OK {
				t.Errorf("%s force=%v: should refuse", action, force)
			}
			if p.Do != "" {
				t.Errorf("%s force=%v: would %q against an unanswered guest", action, force, p.Do)
			}
			if !strings.Contains(strings.Join(p.Lines, "\n"), "not answering") {
				t.Errorf("%s: should say why: %q", action, p.Lines)
			}
		}
	}
	// Force is the case worth naming: it means "I know they differ and want
	// that one", which cannot apply when there is no answer.
	p := plan("pull", EnvState{HostHas: true, Reachable: false, Force: true})
	if !strings.Contains(strings.Join(p.Lines, "\n"), "--force cannot help") {
		t.Errorf("want the force case addressed: %q", p.Lines)
	}
	// show still works, and says what it does not know rather than guessing.
	s := plan("show", EnvState{HostHas: true, Reachable: false})
	if !s.OK || !strings.Contains(strings.Join(s.Lines, "\n"), "unknown") {
		t.Errorf("show against an unreachable guest: %+v", s)
	}
}

func TestPullAndPushRefuseToOverwriteWithoutForce(t *testing.T) {
	both := EnvState{HostHas: true, Reachable: true, GuestHas: true}
	if p := plan("pull", both); p.OK || p.Do != "" {
		t.Errorf("pull over a different spec needs --force: %+v", p)
	}
	if p := plan("push", both); p.OK || p.Do != "" {
		t.Errorf("push over a different spec needs --force: %+v", p)
	}
	forced := both
	forced.Force = true
	if p := plan("pull", forced); !p.OK || p.Do != "write-host" {
		t.Errorf("forced pull should write this side: %+v", p)
	}
	if p := plan("push", forced); !p.OK || p.Do != "write-guest" {
		t.Errorf("forced push should write the guest: %+v", p)
	}
}

// Identical is not an overwrite, so it must not ask for --force.
func TestSameIsANoOp(t *testing.T) {
	same := EnvState{HostHas: true, Reachable: true, GuestHas: true, Same: true}
	for _, action := range []string{"pull", "push"} {
		p := plan(action, same)
		if !p.OK || p.Do != "" {
			t.Errorf("%s of an identical spec: %+v", action, p)
		}
		if !strings.Contains(strings.Join(p.Lines, "\n"), "nothing to") {
			t.Errorf("%s: %q", action, p.Lines)
		}
	}
}

// Nothing to take, and nothing to send, are different sentences.
func TestNothingToWorkWith(t *testing.T) {
	if p := plan("pull", EnvState{Reachable: true, HostHas: true}); p.OK ||
		!strings.Contains(strings.Join(p.Lines, "\n"), "guest has no spec") {
		t.Errorf("%+v", p)
	}
	if p := plan("push", EnvState{Reachable: true, GuestHas: true}); p.OK ||
		!strings.Contains(strings.Join(p.Lines, "\n"), "no spec here") {
		t.Errorf("%+v", p)
	}
	// A first spec going either way needs no force: there is nothing to lose.
	if p := plan("pull", EnvState{Reachable: true, GuestHas: true}); !p.OK || p.Do != "write-host" {
		t.Errorf("a first pull should just land: %+v", p)
	}
	if p := plan("push", EnvState{Reachable: true, HostHas: true}); !p.OK || p.Do != "write-guest" {
		t.Errorf("a first push should just land: %+v", p)
	}
}
