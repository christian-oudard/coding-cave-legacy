package agentd

import (
	"bufio"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func serveTest(t *testing.T) (*Registry, string) {
	t.Helper()
	reg := NewRegistry()
	// Not t.TempDir(): it puts the test's name in the path, and a unix socket
	// address is capped at 108 bytes, which a long test name under a build
	// sandbox's temp dir blows straight through.
	dir, err := os.MkdirTemp("", "agentd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	sock := filepath.Join(dir, "s.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	go Serve(ln, reg)
	t.Cleanup(func() {
		ln.Close()
		reg.CloseAll()
	})
	return reg, sock
}

func dial(t *testing.T, sock, req string) net.Conn {
	t.Helper()
	conn, err := net.Dial("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write([]byte(req + "\n")); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

// readUntil gives the daemon a bounded chance to produce what we asked for
// rather than failing on the first partial read.
func readUntil(t *testing.T, conn net.Conn, want string) string {
	t.Helper()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	var got strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := conn.Read(buf)
		got.Write(buf[:n])
		if strings.Contains(got.String(), want) {
			return got.String()
		}
		if err != nil {
			t.Fatalf("waiting for %q, got %q: %v", want, got.String(), err)
		}
	}
}

func TestListReportsRunningAgents(t *testing.T) {
	reg, sock := serveTest(t)
	if _, err := reg.Start("1", []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80); err != nil {
		t.Fatal(err)
	}
	conn := dial(t, sock, "LIST")
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	line, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(line) != "1 running 24 80" {
		t.Fatalf("got %q", line)
	}
}

func TestAttachReplaysThenStreams(t *testing.T) {
	reg, sock := serveTest(t)
	if _, err := reg.Start("1", []string{"/bin/sh", "-c",
		"echo before-attach; while read l; do echo \"got $l\"; done"}, nil, "/", 24, 80); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "startup output", func() bool {
		s, _ := reg.Get("1")
		return strings.Contains(string(s.ring.Snapshot()), "before-attach")
	})

	conn := dial(t, sock, "ATTACH 1 24 80")
	readUntil(t, conn, "before-attach")
	if err := WriteFrame(conn, FrameData, []byte("ping\n")); err != nil {
		t.Fatal(err)
	}
	readUntil(t, conn, "got ping")
}

// The client's size is authoritative on attach, so a terminal that reconnects
// narrower does not get a screen painted for the old one.
func TestAttachAppliesTheClientSize(t *testing.T) {
	reg, sock := serveTest(t)
	s, err := reg.Start("1", []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	dial(t, sock, "ATTACH 1 50 132")
	waitFor(t, "the resize to land", func() bool {
		ws, err := s.Size()
		return err == nil && ws.Rows == 50 && ws.Cols == 132
	})
}

func TestResizeFrameIsApplied(t *testing.T) {
	reg, sock := serveTest(t)
	s, err := reg.Start("1", []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	conn := dial(t, sock, "ATTACH 1 24 80")
	if err := WriteResize(conn, 60, 200); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the resize to land", func() bool {
		ws, err := s.Size()
		return err == nil && ws.Rows == 60 && ws.Cols == 200
	})
}

// Hanging up a client is closing a window, not killing the work.
func TestClosingTheConnectionLeavesTheAgentRunning(t *testing.T) {
	reg, sock := serveTest(t)
	s, err := reg.Start("1", []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	conn := dial(t, sock, "ATTACH 1 24 80")
	conn.Close()
	waitFor(t, "the server to drop the client", func() bool {
		s.mu.Lock()
		defer s.mu.Unlock()
		return len(s.clients) == 0
	})
	if !s.Running() {
		t.Fatal("agent died with its client")
	}
}

// Asking for the agent gets you the agent: if the last one exited, attaching
// starts a fresh one rather than dropping the user at an error.
func TestAttachRestartsAnExitedAgent(t *testing.T) {
	reg, sock := serveTest(t)
	s, err := reg.Start("1", []string{"/bin/sh", "-c", "echo up; exit 0"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the agent to exit", func() bool { return !s.Running() })

	conn := dial(t, sock, "ATTACH 1 24 80")
	readUntil(t, conn, "up")
	if again, _ := reg.Get("1"); again == s {
		t.Fatal("attached to the dead session instead of a fresh one")
	}
}

func TestAttachToUnknownAgentReportsAnError(t *testing.T) {
	_, sock := serveTest(t)
	conn := dial(t, sock, "ATTACH nope 24 80")
	if got := readUntil(t, conn, "error"); !strings.Contains(got, "no agent") {
		t.Fatalf("got %q", got)
	}
}

func TestStartingADuplicateIdFails(t *testing.T) {
	reg, _ := serveTest(t)
	argv := []string{"/bin/sh", "-c", "sleep 30"}
	if _, err := reg.Start("1", argv, nil, "/", 24, 80); err != nil {
		t.Fatal(err)
	}
	if _, err := reg.Start("1", argv, nil, "/", 24, 80); err == nil {
		t.Fatal("started two agents under one id")
	}
}

// An id is reusable once its agent is gone, so a crashed agent does not
// permanently burn its name.
func TestAnExitedAgentsIdCanBeReused(t *testing.T) {
	reg, _ := serveTest(t)
	s, err := reg.Start("1", []string{"/bin/sh", "-c", "exit 0"}, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the agent to exit", func() bool { return !s.Running() })
	if _, err := reg.Start("1", []string{"/bin/sh", "-c", "sleep 30"}, nil, "/", 24, 80); err != nil {
		t.Fatal(err)
	}
}
