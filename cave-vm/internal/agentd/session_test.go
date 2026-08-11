package agentd

import (
	"strings"
	"testing"
	"time"
)

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestSessionRunsOnAPtyAndBuffersOutput(t *testing.T) {
	s, err := StartSession("1", []string{"/bin/sh", "-c", "echo marker; sleep 30"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	waitFor(t, "output in the ring", func() bool {
		return strings.Contains(string(s.ring.Snapshot()), "marker")
	})
}

// The point of the daemon: output produced while nobody was attached is
// still there for the client that arrives afterwards.
func TestAttachReplaysOutputProducedWhileDetached(t *testing.T) {
	s, err := StartSession("1", []string{"/bin/sh", "-c", "echo before-attach; sleep 30"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	waitFor(t, "output", func() bool {
		return strings.Contains(string(s.ring.Snapshot()), "before-attach")
	})
	replay, _, detach := s.Attach()
	defer detach()
	if !strings.Contains(string(replay), "before-attach") {
		t.Fatalf("replay lost earlier output: %q", replay)
	}
}

func TestTwoClientsSeeTheSameStream(t *testing.T) {
	// Pure shell rather than /bin/cat: the build sandbox has /bin/sh and
	// nothing else, so anything on PATH is a test that fails for its own
	// reasons rather than the code's.
	s, err := StartSession("1", []string{"/bin/sh", "-c", "while read l; do echo \"$l\"; done"},
		nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	_, a, detachA := s.Attach()
	defer detachA()
	_, b, detachB := s.Attach()
	defer detachB()

	if _, err := s.Write([]byte("ping\n")); err != nil {
		t.Fatal(err)
	}
	for i, ch := range []<-chan []byte{a, b} {
		select {
		case chunk := <-ch:
			if !strings.Contains(string(chunk), "ping") {
				t.Fatalf("client %d got %q", i, chunk)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("client %d saw nothing", i)
		}
	}
}

// Detaching must leave the agent alone: that is the difference between
// closing a window and killing the work.
func TestDetachDoesNotStopTheAgent(t *testing.T) {
	s, err := StartSession("1", []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	_, _, detach := s.Attach()
	detach()
	if !s.Running() {
		t.Fatal("agent stopped when its client detached")
	}
}

func TestResizeIsVisibleToTheProcess(t *testing.T) {
	s, err := StartSession("1", []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err := s.Resize(31, 101); err != nil {
		t.Fatal(err)
	}
	ws, err := s.Size()
	if err != nil {
		t.Fatal(err)
	}
	if ws.Rows != 31 || ws.Cols != 101 {
		t.Fatalf("size is %dx%d, want 31x101", ws.Rows, ws.Cols)
	}
}

func TestSessionReportsExit(t *testing.T) {
	s, err := StartSession("1", []string{"/bin/sh", "-c", "exit 0"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the session to notice its process exited", func() bool { return !s.Running() })
}
