package registry

import (
	"path/filepath"
	"testing"
)

// newTestReg returns a registry with an injected clock and liveness so tests
// run offline and deterministically. *clock is the current time; *alive
// controls whether recorded pids look alive.
func newTestReg(t *testing.T) (*Registry, *float64, *bool) {
	t.Helper()
	clock := 1_000_000.0
	alive := true
	r, err := New(filepath.Join(t.TempDir(), "agents.json"))
	if err != nil {
		t.Fatal(err)
	}
	r.Now = func() float64 { return clock }
	r.Alive = func(int) bool { return alive }
	return r, &clock, &alive
}

func mustAcquire(t *testing.T, r *Registry, project string) string {
	t.Helper()
	id, err := r.Acquire(project)
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	return id
}

func TestAcquireLowestFree(t *testing.T) {
	r, _, _ := newTestReg(t)
	if id := mustAcquire(t, r, "proj"); id != "1" {
		t.Fatalf("first id = %q, want 1", id)
	}
	if id := mustAcquire(t, r, "proj"); id != "2" {
		t.Fatalf("second id = %q, want 2 (agent 1 still alive)", id)
	}
	if id := mustAcquire(t, r, "proj"); id != "3" {
		t.Fatalf("third id = %q, want 3", id)
	}
}

func TestReclaimAfter24h(t *testing.T) {
	r, clock, alive := newTestReg(t)
	mustAcquire(t, r, "old-proj") // id 1

	// Agent 1 dies and is marked exited at the current time.
	*alive = false
	if running, _ := r.IsRunning("1"); running {
		t.Fatal("agent 1 should be dead")
	}

	// Just under 24h: the slot is not yet reclaimable, so a new agent gets 2.
	*clock += ReclaimableAfterSecs - 1
	if id := mustAcquire(t, r, "new-proj"); id != "2" {
		t.Fatalf("id = %q, want 2 (slot 1 not yet reclaimable)", id)
	}
}

func TestReclaimReusesSlotAndWipes(t *testing.T) {
	r, clock, alive := newTestReg(t)
	mustAcquire(t, r, "old-proj") // id 1

	*alive = false
	r.IsRunning("1") // marks agent 1 exited at now

	var wiped []string
	r.Wipe = func(project, id string) { wiped = append(wiped, project+"/"+id) }

	// Past 24h: slot 1 is reclaimable, so the next agent reuses it and the
	// old occupant's refs are wiped with its project slug.
	*clock += ReclaimableAfterSecs + 1
	*alive = true
	if id := mustAcquire(t, r, "new-proj"); id != "1" {
		t.Fatalf("id = %q, want reused 1", id)
	}
	if len(wiped) != 1 || wiped[0] != "old-proj/1" {
		t.Fatalf("wipe calls = %v, want [old-proj/1]", wiped)
	}
}

func TestSweepMarksDeadPid(t *testing.T) {
	r, _, alive := newTestReg(t)
	mustAcquire(t, r, "proj") // id 1 alive
	*alive = false
	// A fresh Acquire sweeps dead pids first; agent 1 becomes exited, but its
	// exit is "now" so it is not yet reclaimable -> new agent gets 2.
	if id := mustAcquire(t, r, "proj"); id != "2" {
		t.Fatalf("id = %q, want 2", id)
	}
	if running, _ := r.IsRunning("1"); running {
		t.Fatal("agent 1 should have been swept to exited")
	}
}

func TestContinueByID(t *testing.T) {
	r, _, alive := newTestReg(t)
	mustAcquire(t, r, "proj") // id 1

	if _, err := r.Continue("proj", "9"); err == nil {
		t.Fatal("Continue on unknown id should error")
	}
	if _, err := r.Continue("proj", "1"); err == nil {
		t.Fatal("Continue on a running agent should error")
	}

	*alive = false
	r.IsRunning("1") // exit it
	*alive = true
	id, err := r.Continue("proj", "1")
	if err != nil || id != "1" {
		t.Fatalf("Continue(1) = %q, %v; want 1, nil", id, err)
	}
	if running, _ := r.IsRunning("1"); !running {
		t.Fatal("agent 1 should be running again after Continue")
	}
}

func TestContinueLatestForProject(t *testing.T) {
	r, clock, alive := newTestReg(t)
	mustAcquire(t, r, "A") // 1
	mustAcquire(t, r, "B") // 2

	// Exit both; agent 2 (project B) exits later.
	*alive = false
	r.IsRunning("1")
	*clock += 10
	r.IsRunning("2")
	*alive = true

	if id, _ := r.Continue("B", ""); id != "2" {
		t.Fatalf("Continue latest for B = %q, want 2", id)
	}
	// No exited agents for an unknown project.
	if id, _ := r.Continue("C", ""); id != "" {
		t.Fatalf("Continue for C = %q, want empty", id)
	}
}

func TestSessions(t *testing.T) {
	r, _, _ := newTestReg(t)
	mustAcquire(t, r, "proj") // 1
	mustAcquire(t, r, "proj") // 2

	if err := r.SetSession("1", "sX"); err != nil {
		t.Fatal(err)
	}
	if s, _ := r.Session("1"); s != "sX" {
		t.Fatalf("Session(1) = %q, want sX", s)
	}
	if id, _ := r.AgentBySession("sX"); id != "1" {
		t.Fatalf("AgentBySession(sX) = %q, want 1", id)
	}

	// Reassigning the session to agent 2 clears it from agent 1.
	if err := r.SetSession("2", "sX"); err != nil {
		t.Fatal(err)
	}
	if s, _ := r.Session("1"); s != "" {
		t.Fatalf("Session(1) after move = %q, want empty", s)
	}
	if id, _ := r.AgentBySession("sX"); id != "2" {
		t.Fatalf("AgentBySession(sX) = %q, want 2", id)
	}
}

func TestPersistsAcrossInstances(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agents.json")

	r1, _ := New(path)
	r1.Alive = func(int) bool { return true }
	if id := mustAcquire(t, r1, "proj"); id != "1" {
		t.Fatalf("id = %q, want 1", id)
	}

	// A second Registry over the same file sees agent 1 as in use.
	r2, _ := New(path)
	r2.Alive = func(int) bool { return true }
	if id := mustAcquire(t, r2, "proj"); id != "2" {
		t.Fatalf("id = %q, want 2 across instances", id)
	}
}
