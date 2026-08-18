package control

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeGuest stands in for systemd and the clock.
type fakeGuest struct {
	agents  []Agent
	stopped []string
	set     time.Time
	fail    error
}

func (f *fakeGuest) Health() (Health, error) {
	return Health{System: "/nix/store/abc-system", Uptime: 42}, f.fail
}
func (f *fakeGuest) Agents() ([]Agent, error)  { return f.agents, f.fail }
func (f *fakeGuest) StopAgent(id string) error { f.stopped = append(f.stopped, id); return f.fail }
func (f *fakeGuest) SetTime(t time.Time) error { f.set = t; return f.fail }

// serve wires a real client to a real handler over a pipe, so every test
// crosses the actual HTTP boundary rather than calling the interface directly.
// The dialer is the only thing a different transport would replace.
func serve(t *testing.T, g Guest, serverTok, clientTok string) *Client {
	t.Helper()
	ln := newPipeListener()
	srv := &http.Server{Handler: Handler(g, serverTok)}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close(); ln.Close() })
	return NewClient(func(ctx context.Context) (net.Conn, error) {
		return ln.dial()
	}, clientTok)
}

func TestHealth(t *testing.T) {
	c := serve(t, &fakeGuest{}, "tok", "tok")
	h, err := c.Health()
	if err != nil {
		t.Fatal(err)
	}
	if h.System != "/nix/store/abc-system" || h.Uptime != 42 {
		t.Fatalf("got %+v", h)
	}
}

func TestAgents(t *testing.T) {
	g := &fakeGuest{agents: []Agent{{ID: "1", State: "running", Info: "claude"}}}
	c := serve(t, g, "tok", "tok")
	got, err := c.Agents()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].ID != "1" || got[0].State != "running" {
		t.Fatalf("got %+v", got)
	}
}

// No agents must decode as an empty list, not a null: a caller ranging over
// the result should see nothing rather than crash, and the wire shape should
// not change with the number of agents.
func TestAgentsEmpty(t *testing.T) {
	c := serve(t, &fakeGuest{}, "tok", "tok")
	got, err := c.Agents()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Fatalf("got %+v", got)
	}
}

func TestStopAgent(t *testing.T) {
	g := &fakeGuest{}
	c := serve(t, g, "tok", "tok")
	if err := c.StopAgent("1"); err != nil {
		t.Fatal(err)
	}
	if len(g.stopped) != 1 || g.stopped[0] != "1" {
		t.Fatalf("stopped %v", g.stopped)
	}
}

func TestSetTime(t *testing.T) {
	g := &fakeGuest{}
	c := serve(t, g, "tok", "tok")
	want := time.Unix(1750000000, 123)
	if err := c.SetTime(want); err != nil {
		t.Fatal(err)
	}
	if !g.set.Equal(want) {
		t.Fatalf("guest clock set to %v, want %v", g.set, want)
	}
}

// A clock of zero or before the epoch is a bug on the calling side, and
// applying it would break TLS in exactly the way this endpoint exists to fix.
func TestSetTimeRejectsNonPositive(t *testing.T) {
	g := &fakeGuest{}
	c := serve(t, g, "tok", "tok")
	if err := c.SetTime(time.Unix(0, 0)); err == nil {
		t.Fatal("expected an error for a zero clock")
	}
	if !g.set.IsZero() {
		t.Fatalf("guest clock was set anyway: %v", g.set)
	}
}

func TestWrongTokenIsRejected(t *testing.T) {
	g := &fakeGuest{}
	c := serve(t, g, "server-token", "client-token")
	if _, err := c.Health(); err == nil {
		t.Fatal("expected an error for a bad token")
	}
	if err := c.StopAgent("1"); err == nil {
		t.Fatal("expected an error for a bad token")
	}
	if len(g.stopped) != 0 {
		t.Fatalf("an unauthenticated call reached the guest: %v", g.stopped)
	}
}

// Authentication happens before routing, so an unauthenticated caller cannot
// learn which paths exist by comparing 404 against 401.
func TestUnknownPathIsUnauthorizedWithoutToken(t *testing.T) {
	c := serve(t, &fakeGuest{}, "tok", "wrong")
	err := c.do("GET", "/does-not-exist", nil, nil)
	if err == nil {
		t.Fatal("expected an error")
	}
	if !contains(err.Error(), "401") {
		t.Fatalf("want 401 for an unknown path without a token, got %v", err)
	}
}

func TestGuestErrorsReachTheCaller(t *testing.T) {
	c := serve(t, &fakeGuest{fail: errors.New("systemd said no")}, "tok", "tok")
	_, err := c.Health()
	if err == nil || !contains(err.Error(), "systemd said no") {
		t.Fatalf("want the guest's own message, got %v", err)
	}
}

func TestEnsureTokenIsStableAndPrivate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control-token")
	first, err := EnsureToken(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(first) < 32 {
		t.Fatalf("token is too short to be worth having: %q", first)
	}
	second, err := EnsureToken(path)
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatal("EnsureToken minted a second token over an existing one")
	}
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("token file is %v, want 0600", fi.Mode().Perm())
	}
	// Read back the way the guest does, through a trailing newline.
	got, err := ReadToken(path)
	if err != nil || got != first {
		t.Fatalf("ReadToken gave %q, %v", got, err)
	}
}

func TestReadTokenRejectsEmpty(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty")
	if err := os.WriteFile(path, []byte("  \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadToken(path); err == nil {
		t.Fatal("an empty token file must not authenticate anything")
	}
}

// The host is the only client, so a strange id can only come from a bug. That
// is exactly the point at which a string becomes part of a unit name.
func TestValidID(t *testing.T) {
	for _, ok := range []string{"1", "root", "agent-2", "a_b"} {
		if err := validID(ok); err != nil {
			t.Errorf("validID(%q) = %v, want nil", ok, err)
		}
	}
	for _, bad := range []string{"", "1 2", "../../etc", "a.service", "x;reboot",
		strings.Repeat("a", 33)} {
		if err := validID(bad); err == nil {
			t.Errorf("validID(%q) = nil, want an error", bad)
		}
	}
}

// The roster is the socket directory, and its lines are whitespace-separated
// with a trailing free-text field that may contain spaces.
func TestSystemAgentsParsesListOutput(t *testing.T) {
	s := System{
		Sessions: "/run/cave/sessions",
		List: func(dir string) ([]string, error) {
			if dir != "/run/cave/sessions" {
				t.Errorf("listed %q", dir)
			}
			return []string{"1 running claude --dangerously-skip-permissions", "2 exited"}, nil
		},
	}
	got, err := s.Agents()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("got %d agents: %+v", len(got), got)
	}
	if got[0].ID != "1" || got[0].State != "running" ||
		got[0].Info != "claude --dangerously-skip-permissions" {
		t.Errorf("first agent: %+v", got[0])
	}
	if got[1].ID != "2" || got[1].State != "exited" || got[1].Info != "" {
		t.Errorf("second agent: %+v", got[1])
	}
}

func contains(s, sub string) bool { return strings.Contains(s, sub) }

// pipeListener is a net.Listener over net.Pipe, which is what lets the test
// exercise the real client and the real handler with no socket anywhere.
type pipeListener struct {
	conns  chan net.Conn
	closed chan struct{}
}

func newPipeListener() *pipeListener {
	return &pipeListener{conns: make(chan net.Conn), closed: make(chan struct{})}
}

func (l *pipeListener) dial() (net.Conn, error) {
	client, server := net.Pipe()
	select {
	case l.conns <- server:
		return client, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

func (l *pipeListener) Accept() (net.Conn, error) {
	select {
	case c := <-l.conns:
		return c, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

func (l *pipeListener) Close() error {
	select {
	case <-l.closed:
	default:
		close(l.closed)
	}
	return nil
}

func (l *pipeListener) Addr() net.Addr { return pipeAddr{} }

type pipeAddr struct{}

func (pipeAddr) Network() string { return "pipe" }
func (pipeAddr) String() string  { return "cave-guest" }
