package agentd

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// collector drains a pipe into a buffer the test can poll, so the client is
// never blocked on a reader that is not there.
type collector struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (c *collector) drain(r io.Reader) {
	buf := make([]byte, 4096)
	for {
		n, err := r.Read(buf)
		c.mu.Lock()
		c.buf.Write(buf[:n])
		c.mu.Unlock()
		if err != nil {
			return
		}
	}
}

func (c *collector) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.String()
}

// Listing is a directory read plus one question each, so two sessions that
// know nothing about each other still list together.
func TestListReturnsOneLinePerSession(t *testing.T) {
	dir := sessionDir(t)
	for _, id := range []string{"2", "1"} {
		serveIn(t, dir, id, "/bin/sh", "-c", "sleep 30")
	}
	got, err := List(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != "1 running 24 80" || got[1] != "2 running 24 80" {
		t.Fatalf("got %q", got)
	}
}

// What a killed session leaves behind is a file, not a session.
func TestListSkipsASocketNobodyIsListeningOn(t *testing.T) {
	dir := sessionDir(t)
	if err := os.WriteFile(filepath.Join(dir, "9.sock"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	serveIn(t, dir, "1", "/bin/sh", "-c", "sleep 30")
	got, err := List(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != "1 running 24 80" {
		t.Fatalf("got %q", got)
	}
}

func TestListOfNothingIsEmpty(t *testing.T) {
	got, err := List(sessionDir(t))
	if err != nil || len(got) != 0 {
		t.Fatalf("got %q, %v", got, err)
	}
}

// A session that is already listening is never started twice: attaching to a
// live agent must not restart the work it is doing.
func TestEnsureSocketLeavesALiveSessionAlone(t *testing.T) {
	dir := sessionDir(t)
	serveIn(t, dir, "1", "/bin/sh", "-c", "sleep 30")
	started := false
	sock, err := EnsureSocket(dir, "1", func() error { started = true; return nil }, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if started {
		t.Fatal("started a session that was already running")
	}
	if sock != SocketPath(dir, "1") {
		t.Fatalf("got %q", sock)
	}
}

// Asking for an agent that is not there is what starts it, which is how an
// agent that exited comes back under the same name.
func TestEnsureSocketStartsAMissingSession(t *testing.T) {
	dir := sessionDir(t)
	sock, err := EnsureSocket(dir, "1", func() error {
		serveIn(t, dir, "1", "/bin/sh", "-c", "sleep 30")
		return nil
	}, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Info(sock); err != nil {
		t.Fatal(err)
	}
}

func TestEnsureSocketGivesUpWhenNothingComesUp(t *testing.T) {
	dir := sessionDir(t)
	_, err := EnsureSocket(dir, "1", func() error { return nil }, 100*time.Millisecond)
	if err == nil || !strings.Contains(err.Error(), "never listened") {
		t.Fatalf("got %v", err)
	}
}

// The round trip the user actually performs: type at the agent, see the
// answer, then detach and leave it running.
func TestAttachRelaysTypingAndDetachesOnTheDetachKey(t *testing.T) {
	s, sock := serveTest(t, "/bin/sh", "-c", "while read l; do echo \"got $l\"; done")

	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	defer inW.Close()
	var seen collector
	go seen.drain(outR)

	done := make(chan error, 1)
	go func() { done <- AttachTo(sock, inR, outW) }()

	waitFor(t, "the client to attach", func() bool {
		s.mu.Lock()
		defer s.mu.Unlock()
		return len(s.clients) == 1
	})
	if _, err := inW.Write([]byte("ping\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the echo to come back", func() bool { return strings.Contains(seen.String(), "got ping") })

	if _, err := inW.Write([]byte{DetachKey}); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatalf("detach returned %v", err)
	}
	waitFor(t, "the detach notice", func() bool { return strings.Contains(seen.String(), "[detached]") })
	if !s.Running() {
		t.Fatal("detaching killed the agent")
	}
	// The TUI hides the cursor and never gets to put it back, so the client
	// has to, or you land in a shell you cannot see yourself typing in.
	if !strings.Contains(seen.String(), "\x1b[?25h") {
		t.Fatal("detach left the terminal without showing the cursor")
	}
}

// When the agent exits the client comes back on its own, without waiting for
// the user to press anything.
func TestAttachReturnsWhenTheAgentExits(t *testing.T) {
	_, sock := serveTest(t, "/bin/sh", "-c", "echo bye; sleep 0.2")
	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	defer inW.Close()
	var seen collector
	go seen.drain(outR)

	done := make(chan error, 1)
	go func() { done <- AttachTo(sock, inR, outW) }()
	if err := <-done; err != nil {
		t.Fatalf("got %v", err)
	}
	if !strings.Contains(seen.String(), "bye") {
		t.Fatalf("lost the agent's output: %q", seen.String())
	}
}
