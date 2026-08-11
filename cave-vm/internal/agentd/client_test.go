package agentd

import (
	"io"
	"os"
	"strings"
	"sync"
	"testing"
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

func TestListReturnsOneLinePerAgent(t *testing.T) {
	reg, sock := serveTest(t)
	for _, id := range []string{"2", "1"} {
		if _, err := reg.Start(id, []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80); err != nil {
			t.Fatal(err)
		}
	}
	got, err := List(sock)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != "1 running 24 80" || got[1] != "2 running 24 80" {
		t.Fatalf("got %q", got)
	}
}

func TestListWithoutADaemonSaysSo(t *testing.T) {
	_, err := List("/nonexistent/agentd.sock")
	if err == nil || !strings.Contains(err.Error(), "cave-guestd") {
		t.Fatalf("got %v", err)
	}
}

// The round trip the user actually performs: type at the agent, see the
// answer, then detach and leave it running.
func TestAttachRelaysTypingAndDetachesOnTheDetachKey(t *testing.T) {
	reg, sock := serveTest(t)
	s, err := reg.Start("1", []string{"/bin/sh", "-c",
		"while read l; do echo \"got $l\"; done"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}

	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	defer inW.Close()
	var seen collector
	go seen.drain(outR)

	done := make(chan error, 1)
	go func() { done <- AttachTo(sock, "1", inR, outW) }()

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
}

// When the agent exits the client comes back on its own, without waiting for
// the user to press anything.
func TestAttachReturnsWhenTheAgentExits(t *testing.T) {
	reg, sock := serveTest(t)
	if _, err := reg.Start("1", []string{"/bin/sh", "-c", "echo bye; sleep 0.2"}, nil, "/", 24, 80); err != nil {
		t.Fatal(err)
	}
	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	defer inW.Close()
	var seen collector
	go seen.drain(outR)

	done := make(chan error, 1)
	go func() { done <- AttachTo(sock, "1", inR, outW) }()
	if err := <-done; err != nil {
		t.Fatalf("got %v", err)
	}
	if !strings.Contains(seen.String(), "bye") {
		t.Fatalf("lost the agent's output: %q", seen.String())
	}
}
