package agentd

import (
	"bufio"
	"net"
	"os"
	"strings"
	"testing"
	"time"
)

// sessionDir is not t.TempDir(): that puts the test's name in the path, and a
// unix socket address is capped at 108 bytes, which a long test name under a
// build sandbox's temp dir blows straight through.
func sessionDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "agentd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

// serveIn starts a session called id in dir and serves it, the way the session
// process does.
func serveIn(t *testing.T, dir, id string, argv ...string) (*Session, string) {
	t.Helper()
	sock := SocketPath(dir, id)
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	s, err := StartSession(id, argv, nil, "/", 24, 80)
	if err != nil {
		t.Fatal(err)
	}
	go Serve(ln, s)
	t.Cleanup(func() {
		ln.Close()
		s.Close()
	})
	return s, sock
}

func serveTest(t *testing.T, argv ...string) (*Session, string) {
	t.Helper()
	return serveIn(t, sessionDir(t), "1", argv...)
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

// readUntil gives the session a bounded chance to produce what we asked for
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

func TestInfoReportsTheAgent(t *testing.T) {
	_, sock := serveTest(t, "/bin/sh", "-c", "sleep 30")
	conn := dial(t, sock, "INFO")
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
	s, sock := serveTest(t, "/bin/sh", "-c",
		"echo before-attach; while read l; do echo \"got $l\"; done")
	waitFor(t, "startup output", func() bool {
		return strings.Contains(string(s.ring.Snapshot()), "before-attach")
	})

	conn := dial(t, sock, "ATTACH 24 80")
	readUntil(t, conn, "before-attach")
	if err := WriteFrame(conn, FrameData, []byte("ping\n")); err != nil {
		t.Fatal(err)
	}
	readUntil(t, conn, "got ping")
}

// The client's size is authoritative on attach, so a terminal that reconnects
// narrower does not get a screen painted for the old one.
func TestAttachAppliesTheClientSize(t *testing.T) {
	s, sock := serveTest(t, "/bin/sh", "-c", "sleep 30")
	dial(t, sock, "ATTACH 50 132")
	waitFor(t, "the resize to land", func() bool {
		ws, err := s.Size()
		return err == nil && ws.Rows == 50 && ws.Cols == 132
	})
}

func TestResizeFrameIsApplied(t *testing.T) {
	s, sock := serveTest(t, "/bin/sh", "-c", "sleep 30")
	conn := dial(t, sock, "ATTACH 24 80")
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
	s, sock := serveTest(t, "/bin/sh", "-c", "sleep 30")
	conn := dial(t, sock, "ATTACH 24 80")
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

func TestUnknownRequestIsAnError(t *testing.T) {
	_, sock := serveTest(t, "/bin/sh", "-c", "sleep 30")
	conn := dial(t, sock, "WHAT")
	if got := readUntil(t, conn, "error"); !strings.Contains(got, "unknown request") {
		t.Fatalf("got %q", got)
	}
}
