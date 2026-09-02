package control

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const sessionID = "0f9a1c2e-3b4d-4f5a-8b6c-7d8e9f0a1b2c"

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
	srv := &http.Server{Handler: Handler(g, nil, serverTok)}
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

// An agent is reported as what it was created for, which the guest reads back
// out of the session's own environment file. Without it a human sees a number
// and the shape of a terminal, which identifies nothing once there is more
// than one agent.
func TestSystemAgentsSayWhatAnAgentWasCreatedFor(t *testing.T) {
	dir := t.TempDir()
	write := func(id, body string) {
		if err := os.WriteFile(filepath.Join(dir, id+".env"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("0", "CAVE_AGENT_ID=0\nCAVE_PROJECT=coding-cave\nCAVE_BASE=vm-cave\nCAVE_AGENT_ROOT=1\n")
	write("3", "CAVE_AGENT_ID=3\nCAVE_PROJECT=widgets\nCAVE_BASE=main\n")

	s := System{Sessions: dir, List: func(string) ([]string, error) {
		// An agent with no env file at all: it stays a running agent rather
		// than disappearing from the list or failing it.
		return []string{"0 running 80 24", "3 running 80 24", "9 running 80 24"}, nil
	}}
	got, err := s.Agents()
	if err != nil || len(got) != 3 {
		t.Fatalf("got %+v, %v", got, err)
	}
	if got[0].Project != "coding-cave" || got[0].Base != "vm-cave" || !got[0].Root {
		t.Fatalf("root agent: %+v", got[0])
	}
	if got[1].Project != "widgets" || got[1].Base != "main" || got[1].Root {
		t.Fatalf("ordinary agent: %+v", got[1])
	}
	if got[2].Project != "" || got[2].State != "running" {
		t.Fatalf("agent with no record: %+v", got[2])
	}
}

// An agent that stopped is still on the roster, because its worktree and its
// home are still here and those are what hold its id. Reporting only the
// running ones described the guest as emptier than it is, and left "no free
// agent id" with nothing on screen to account for it.
func TestSystemAgentsIncludeStoppedOnesThatStillHoldAnID(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "2.env"),
		[]byte("CAVE_PROJECT=widgets\nCAVE_BASE=main\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	s := System{
		Sessions: dir,
		List:     func(string) ([]string, error) { return []string{"0 running 80 24"}, nil },
		// Agent 0 is running and also holds state; it must not be listed
		// twice, once as itself and once as its own corpse.
		Held: func() []string { return []string{"0", "2", "5"} },
	}
	got, err := s.Agents()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("want the running one and the two stopped, got %+v", got)
	}
	if got[0].ID != "0" || got[0].State != "running" {
		t.Fatalf("running agent: %+v", got[0])
	}
	// The env file outlives the session on this guest only until the tmpfs
	// goes, so a stopped agent says what it can and stays listed either way.
	if got[1].ID != "2" || got[1].State != "stopped" || got[1].Project != "widgets" {
		t.Fatalf("stopped agent with a record: %+v", got[1])
	}
	if got[2].ID != "5" || got[2].State != "stopped" || got[2].Project != "" {
		t.Fatalf("stopped agent with no record: %+v", got[2])
	}
}

// A Guest that cannot answer the question still works. Held is optional, so
// nothing that builds a System without it starts reporting an empty roster of
// stopped agents as though it had checked.
func TestSystemAgentsWithoutHeld(t *testing.T) {
	s := System{
		Sessions: t.TempDir(),
		List:     func(string) ([]string, error) { return []string{"0 running 80 24"}, nil },
	}
	got, err := s.Agents()
	if err != nil || len(got) != 1 || got[0].State != "running" {
		t.Fatalf("got %+v, %v", got, err)
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

// fakeSessions stands in for the guest's ~/.claude.
type fakeSessions struct {
	records map[string]string
	wrote   map[string]string
	shared  map[string]bool
	agents  map[string]string
	fail    error
}

func (f *fakeSessions) List() ([]SessionRecord, error) {
	if f.fail != nil {
		return nil, f.fail
	}
	var out []SessionRecord
	for id, body := range f.records {
		out = append(out, SessionRecord{ID: id, Size: int64(len(body)), Project: "-root-coding-cave"})
	}
	return out, nil
}

func (f *fakeSessions) Read(id string) (io.ReadCloser, error) {
	body, ok := f.records[id]
	if !ok {
		return nil, errors.New("no such record")
	}
	return io.NopCloser(strings.NewReader(body)), nil
}

func (f *fakeSessions) Write(id string, r io.Reader, shared bool, agent string) error {
	b, err := io.ReadAll(r)
	if err != nil {
		return err
	}
	if f.wrote == nil {
		f.wrote, f.shared = map[string]string{}, map[string]bool{}
	}
	if f.agents == nil {
		f.agents = map[string]string{}
	}
	f.wrote[id], f.shared[id], f.agents[id] = string(b), shared, agent
	return nil
}

func serveSessions(t *testing.T, s Sessions) *Client {
	t.Helper()
	ln := newPipeListener()
	srv := &http.Server{Handler: Handler(&fakeGuest{}, s, "tok")}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close(); ln.Close() })
	return NewClient(func(ctx context.Context) (net.Conn, error) { return ln.dial() }, "tok")
}

func TestListSessions(t *testing.T) {
	c := serveSessions(t, &fakeSessions{records: map[string]string{sessionID: "a\nb\n"}})
	got, err := c.ListSessions()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].ID != sessionID || got[0].Size != 4 {
		t.Fatalf("got %+v", got)
	}
}

// A transcript is the one thing here that grows without bound, so it must
// stream rather than arrive as a decoded field.
func TestReadSessionStreamsTheRecord(t *testing.T) {
	body := strings.Repeat("{\"role\":\"user\"}\n", 5000)
	c := serveSessions(t, &fakeSessions{records: map[string]string{sessionID: body}})
	rc, err := c.ReadSession(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	got, err := io.ReadAll(rc)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != body {
		t.Fatalf("got %d bytes, want %d", len(got), len(body))
	}
}

func TestReadSessionThatIsNotThere(t *testing.T) {
	c := serveSessions(t, &fakeSessions{})
	if _, err := c.ReadSession(sessionID); err == nil {
		t.Fatal("expected an error for a record the guest does not have")
	}
}

// Whose home a restored record lands in. A restore puts an agent's history
// back where its own picker will find it, so the agent has to survive the trip
// across the wire; without it every record lands in one home and shows up in a
// picker it does not belong to while missing from the one it does.
func TestWriteSessionCarriesTheAgent(t *testing.T) {
	f := &fakeSessions{}
	c := serveSessions(t, f)
	if err := c.WriteSession(sessionID, strings.NewReader("history\n"), false, "3"); err != nil {
		t.Fatal(err)
	}
	if f.agents[sessionID] != "3" {
		t.Fatalf("record was restored for agent %q, want 3", f.agents[sessionID])
	}
}

// Which directory a restored record lands in is the whole difference between
// a session an agent can resume and one it can only read.
func TestWriteSessionCarriesTheDestination(t *testing.T) {
	for _, shared := range []bool{false, true} {
		f := &fakeSessions{}
		c := serveSessions(t, f)
		if err := c.WriteSession(sessionID, strings.NewReader("history\n"), shared, ""); err != nil {
			t.Fatal(err)
		}
		if f.wrote[sessionID] != "history\n" {
			t.Fatalf("shared=%v: body was %q", shared, f.wrote[sessionID])
		}
		if f.shared[sessionID] != shared {
			t.Fatalf("shared=%v: guest saw %v", shared, f.shared[sessionID])
		}
	}
}

// A guest with no transcript routes wired up must 404 rather than panic on a
// nil interface, which is what a handler built without them would do.
func TestSessionRoutesAbsentWhenNotWired(t *testing.T) {
	ln := newPipeListener()
	srv := &http.Server{Handler: Handler(&fakeGuest{}, nil, "tok")}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close(); ln.Close() })
	c := NewClient(func(ctx context.Context) (net.Conn, error) { return ln.dial() }, "tok")
	if _, err := c.ListSessions(); err == nil || !contains(err.Error(), "404") {
		t.Fatalf("want a 404, got %v", err)
	}
}

func TestSessionsNeedTheTokenToo(t *testing.T) {
	ln := newPipeListener()
	f := &fakeSessions{records: map[string]string{sessionID: "secret\n"}}
	srv := &http.Server{Handler: Handler(&fakeGuest{}, f, "server-token")}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close(); ln.Close() })
	c := NewClient(func(ctx context.Context) (net.Conn, error) { return ln.dial() }, "wrong")
	if _, err := c.ReadSession(sessionID); err == nil {
		t.Fatal("a transcript was readable without the token")
	}
	if err := c.WriteSession(sessionID, strings.NewReader("x"), false, ""); err == nil {
		t.Fatal("a transcript was writable without the token")
	}
	if len(f.wrote) != 0 {
		t.Fatalf("an unauthenticated write reached the guest: %v", f.wrote)
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
