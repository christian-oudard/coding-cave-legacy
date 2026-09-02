package main

import (
	"io"
	"net"
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"codingcave.dev/cave-vm/internal/agentd"
	"codingcave.dev/cave-vm/internal/sessions"
)

// homesUnder points the guest's home layout at a tree the test made, so that
// what these assert is the code and not the machine the test runs on -- which,
// for this project, is a guest with real agent homes in exactly these places.
func homesUnder(t *testing.T, root string) (rootHome, agentHomes string) {
	t.Helper()
	rootHome, agentHomes = filepath.Join(root, "root"), filepath.Join(root, "home")
	if err := os.MkdirAll(agentHomes, 0o755); err != nil {
		t.Fatal(err)
	}
	savedRoot, savedAgents := rootHomeDir, agentHomeDir
	rootHomeDir, agentHomeDir = rootHome, agentHomes
	t.Cleanup(func() { rootHomeDir, agentHomeDir = savedRoot, savedAgents })
	return rootHome, agentHomes
}

// The guest daemon decides two things nothing else can: which unit an agent's
// session runs under, and whose home a restored transcript lands in. Both are
// answers about identity, and both were written when an agent stopped being
// "the guest" and became a uid with a home of its own. Neither had a test.

// envFile writes the session env file the guest records an agent's kind in.
func envFile(t *testing.T, dir, id, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, id+".env"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestAgentIsRoot(t *testing.T) {
	dir := t.TempDir()
	envFile(t, dir, "0", "CAVE_AGENT_ID=0\nCAVE_AGENT_ROOT=1\n")
	envFile(t, dir, "3", "CAVE_AGENT_ID=3\n")
	// Trailing whitespace is what a shell heredoc leaves behind, and the
	// answer must not depend on it.
	envFile(t, dir, "4", "  CAVE_AGENT_ROOT=1  \n")
	// A value that is not 1 is not root: the file records a flag, and
	// substring matching here would make CAVE_AGENT_ROOT=0 mean root.
	envFile(t, dir, "5", "CAVE_AGENT_ROOT=0\n")

	for _, tc := range []struct {
		id   string
		want bool
	}{
		{"0", true},
		{"3", false},
		{"4", true},
		{"5", false},
		// A missing file reads as "not root", which is the safe way to be
		// wrong: the reverse hands an unknown agent the whole guest.
		{"nosuch", false},
	} {
		if got := agentIsRoot(dir, tc.id); got != tc.want {
			t.Errorf("agentIsRoot(%q) = %v, want %v", tc.id, got, tc.want)
		}
	}
}

func TestUnitForFollowsTheEnvFileNotTheID(t *testing.T) {
	dir := t.TempDir()
	// Deliberately not agent 0: the registry hands out ids without reserving
	// one, so nothing about a number says whether it is the root agent. If
	// unitFor ever went back to deciding from the id, this is what catches it.
	envFile(t, dir, "7", "CAVE_AGENT_ROOT=1\n")
	envFile(t, dir, "0", "CAVE_AGENT_ID=0\n")

	if got, want := unitFor(dir, "7"), "cave-root-session@7.service"; got != want {
		t.Errorf("unitFor(root agent 7) = %q, want %q", got, want)
	}
	if got, want := unitFor(dir, "0"), "cave-session@0.service"; got != want {
		t.Errorf("unitFor(ordinary agent 0) = %q, want %q", got, want)
	}
}

func TestRootAgentID(t *testing.T) {
	dir := t.TempDir()
	envFile(t, dir, "2", "CAVE_AGENT_ID=2\n")
	envFile(t, dir, "5", "CAVE_AGENT_ROOT=1\n")
	if got, want := rootAgentID(dir), "5"; got != want {
		t.Errorf("rootAgentID = %q, want %q", got, want)
	}

	// With nobody claiming it, the answer is the reserved id rather than an
	// error: the guest may simply have no root agent running.
	empty := t.TempDir()
	if got, want := rootAgentID(empty), "0"; got != want {
		t.Errorf("rootAgentID(no agents) = %q, want %q", got, want)
	}
}

// An id is occupied while anything of the agent is still on disk, which is
// what stops the host handing it to somebody else. The two halves are counted
// separately because they are lost separately: `approve` deletes the branch,
// a stop ends the session, and neither touches the worktree or the home.
func TestOccupiedCountsWorktreesAndHomes(t *testing.T) {
	root := t.TempDir()
	_, homes := homesUnder(t, root)

	trees := filepath.Join(root, "agents")
	for _, id := range []string{"1", "4"} {
		if err := os.MkdirAll(filepath.Join(trees, id), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	// A file in the worktree root is not an agent.
	if err := os.WriteFile(filepath.Join(trees, "notes"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	// Agent 6 has a home and no worktree: the transcripts alone are enough to
	// make handing its id to somebody else the wrong thing to do.
	if err := os.MkdirAll(filepath.Join(homes, "agent6", ".claude"), 0o700); err != nil {
		t.Fatal(err)
	}

	out := captureStdout(t, func() {
		occupied([]string{"-worktrees", trees, "-dir", t.TempDir()})
	})
	if got, want := out, "1\n4\n6\n"; got != want {
		t.Errorf("occupied = %q, want %q", got, want)
	}
}

// What a stopped agent was created for comes from the record beside its
// worktree, not from /run. /run is a tmpfs, so a stop takes the file while
// leaving everything it describes -- which is the whole reason the durable
// copy exists, and the reason a restarted guest can still say what agent 2
// was working on.
func TestHeldAgentsPreferTheDurableRecord(t *testing.T) {
	root := t.TempDir()
	homesUnder(t, root)
	trees, sessions := filepath.Join(root, "agents"), t.TempDir()

	for _, id := range []string{"2", "3"} {
		if err := os.MkdirAll(filepath.Join(trees, id), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	// Agent 2 has both, and they disagree: the durable one wins.
	if err := os.WriteFile(agentRecord(trees, "2"),
		[]byte("CAVE_PROJECT=widgets\nCAVE_BASE=main\nCAVE_AGENT_ROOT=1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sessions, "2.env"),
		[]byte("CAVE_PROJECT=stale\nCAVE_BASE=stale\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Agent 3 predates the durable record and has only the tmpfs copy, which
	// is still better than nothing while the guest has not restarted.
	if err := os.WriteFile(filepath.Join(sessions, "3.env"),
		[]byte("CAVE_PROJECT=older\nCAVE_BASE=vm-cave\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	got := heldAgents(sessions, trees)
	if len(got) != 2 {
		t.Fatalf("got %+v", got)
	}
	if got[0].Project != "widgets" || got[0].Base != "main" || !got[0].Root {
		t.Errorf("agent 2 should come from its durable record: %+v", got[0])
	}
	if got[1].Project != "older" || got[1].Base != "vm-cave" {
		t.Errorf("agent 3 should fall back to /run: %+v", got[1])
	}
}

// An agent with neither record is still held. It has a worktree, so it has an
// id, and that is what the answer is for -- saying nothing about what it was
// doing is not the same as leaving it out.
func TestHeldAgentsWithNoRecordAtAll(t *testing.T) {
	root := t.TempDir()
	homesUnder(t, root)
	trees := filepath.Join(root, "agents")
	if err := os.MkdirAll(filepath.Join(trees, "4"), 0o755); err != nil {
		t.Fatal(err)
	}
	got := heldAgents(t.TempDir(), trees)
	if len(got) != 1 || got[0].ID != "4" || got[0].Project != "" {
		t.Fatalf("got %+v", got)
	}
}

// After a restart the agent is still here and its description of itself is
// not, because /run is a tmpfs. Putting it back is what lets the unit load its
// EnvironmentFile at all -- and, before that, what lets unitFor tell the root
// agent from an ordinary one, since it decides by reading the same file.
func TestEnsureRuntimeEnvRestoresFromTheDurableRecord(t *testing.T) {
	run, trees := t.TempDir(), t.TempDir()
	if err := os.MkdirAll(filepath.Join(trees, "0"), 0o755); err != nil {
		t.Fatal(err)
	}
	body := "CAVE_AGENT_ID=0\nCAVE_PROJECT=coding-cave\nCAVE_BASE=vm-cave\nCAVE_AGENT_ROOT=1\n"
	if err := os.WriteFile(agentRecord(trees, "0"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	// The state a restart leaves: nothing in /run at all.
	if agentIsRoot(run, "0") {
		t.Fatal("the runtime file should be missing to begin with")
	}
	if err := ensureRuntimeEnv(run, trees, "0"); err != nil {
		t.Fatal(err)
	}
	if got, want := unitFor(run, "0"), "cave-root-session@0.service"; got != want {
		t.Errorf("after restoring the record, unitFor = %q, want %q", got, want)
	}
	// Owner-only: it is the same file `start` writes, and it names what an
	// agent is.
	st, err := os.Stat(filepath.Join(run, "0.env"))
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != 0o600 {
		t.Errorf("restored env file is %v, want 0600", st.Mode().Perm())
	}
}

// An existing runtime file is left alone. It is the live one, and a restart is
// the only thing this is for.
func TestEnsureRuntimeEnvLeavesALiveFileAlone(t *testing.T) {
	run, trees := t.TempDir(), t.TempDir()
	if err := os.MkdirAll(filepath.Join(trees, "3"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(agentRecord(trees, "3"), []byte("CAVE_PROJECT=old\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(run, "3.env"), []byte("CAVE_PROJECT=live\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := ensureRuntimeEnv(run, trees, "3"); err != nil {
		t.Fatal(err)
	}
	if got := readEnv(filepath.Join(run, "3.env"))["CAVE_PROJECT"]; got != "live" {
		t.Errorf("the live file was overwritten with %q", got)
	}
}

// With no record at all there is nothing to put back, and saying so is the
// point: systemd's version of this is a sentence about a unit failing to load
// a file, which reads like a broken unit rather than a missing fact.
func TestEnsureRuntimeEnvWithNothingToRestore(t *testing.T) {
	err := ensureRuntimeEnv(t.TempDir(), t.TempDir(), "5")
	if err == nil {
		t.Fatal("starting an agent with no record should not be attempted")
	}
	for _, want := range []string{"agent 5", "vmcav agent"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal should mention %q: %v", want, err)
		}
	}
}

// The reverse, and the reason the host can hand an id out again at all: an
// agent whose state has been removed stops holding its id.
func TestOccupiedIsEmptyWhenNothingIsHeld(t *testing.T) {
	root := t.TempDir()
	homesUnder(t, root)
	out := captureStdout(t, func() {
		occupied([]string{"-worktrees", filepath.Join(root, "agents"), "-dir", t.TempDir()})
	})
	if out != "" {
		t.Errorf("occupied = %q, want nothing held", out)
	}
}

// captureStdout runs f with os.Stdout replaced, and returns what it printed.
func captureStdout(t *testing.T, f func()) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	saved := os.Stdout
	os.Stdout = w
	done := make(chan string, 1)
	go func() {
		var b strings.Builder
		io.Copy(&b, r)
		done <- b.String()
	}()
	f()
	w.Close()
	os.Stdout = saved
	return <-done
}

// homeAt builds one agent's home the way seed-agent-home@ lays it out.
func homeAt(t *testing.T, root, agent string) sessions.Local {
	t.Helper()
	claude := filepath.Join(root, "home-"+agent, ".claude")
	if err := os.MkdirAll(claude, 0o700); err != nil {
		t.Fatal(err)
	}
	return sessions.Local{ClaudeDir: claude, Agent: agent}
}

const testID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

// A restored transcript must land in the home of the agent that wrote it. The
// archive is the only thing that still knows -- the guest lost the record,
// which is why it is being restored -- so the id rides in from the host's
// metadata and this is where it is honoured.
func TestWriteRoutesToTheOwningAgentsHome(t *testing.T) {
	root := t.TempDir()
	rootHome := homeAt(t, root, "0")
	rootHome.Cwd = "/projects/coding-cave"
	rootHome.SharedDir = filepath.Join(root, "shared")

	agent3 := homeAt(t, root, "3")
	tr := transcripts_{homes: []sessions.Local{rootHome, agent3}, write: rootHome}

	if err := tr.Write(testID, strings.NewReader("{}\n"), false, "3"); err != nil {
		t.Fatal(err)
	}

	landed := filepath.Join(agent3.ClaudeDir, "projects",
		sessions.EncodeProject(rootHome.Cwd), testID+".jsonl")
	if _, err := os.Stat(landed); err != nil {
		t.Errorf("agent 3's record did not land in agent 3's home: %v", err)
	}
	// And nowhere else. A copy in root's home would be offered in root's
	// picker as though root had written it.
	stray := filepath.Join(rootHome.ClaudeDir, "projects",
		sessions.EncodeProject(rootHome.Cwd), testID+".jsonl")
	if _, err := os.Stat(stray); err == nil {
		t.Error("the record was also written into the root agent's home")
	}
}

// The worktree path is the same string for every agent -- each session has its
// own mount namespace and its own tree bound over /projects/<slug> -- so the
// cwd that decides the project directory comes from the writing side rather
// than from the home being written to, which has none of its own.
func TestWriteBorrowsCwdForATargetHome(t *testing.T) {
	root := t.TempDir()
	rootHome := homeAt(t, root, "0")
	rootHome.Cwd = "/projects/coding-cave"
	agent3 := homeAt(t, root, "3") // no Cwd, as agentHomes reports it

	tr := transcripts_{homes: []sessions.Local{rootHome, agent3}, write: rootHome}
	if err := tr.Write(testID, strings.NewReader("{}\n"), false, "3"); err != nil {
		t.Fatalf("write to a home with no cwd of its own: %v", err)
	}
}

// Shared is not addressed to an agent: it is the read-only view of every
// agent's records, so it lands in the one shared directory whoever it belongs
// to.
func TestWriteSharedIgnoresTheAgent(t *testing.T) {
	root := t.TempDir()
	rootHome := homeAt(t, root, "0")
	rootHome.Cwd = "/projects/coding-cave"
	rootHome.SharedDir = filepath.Join(root, "shared")
	agent3 := homeAt(t, root, "3")

	tr := transcripts_{homes: []sessions.Local{rootHome, agent3}, write: rootHome}
	if err := tr.Write(testID, strings.NewReader("{}\n"), true, "3"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(rootHome.SharedDir, testID+".jsonl")); err != nil {
		t.Errorf("shared record did not land in the shared directory: %v", err)
	}
	// Not in agent 3's home: the shared view exists so that reading another
	// agent's session does not put it in your own picker.
	if _, err := os.Stat(filepath.Join(agent3.ClaudeDir, "projects")); err == nil {
		t.Error("a shared record was written into an agent's home")
	}
}

// A record whose agent has no home is refused, not filed somewhere else. This
// is the ordinary path on a fresh guest: a start restores the whole archive
// before any agent exists, so without the refusal every ordinary agent's
// history lands in the root agent's picker as though root had written it.
func TestWriteForAnAgentWithNoHomeIsRefused(t *testing.T) {
	root := t.TempDir()
	rootHome := homeAt(t, root, "0")
	rootHome.Cwd = "/projects/coding-cave"
	tr := transcripts_{homes: []sessions.Local{rootHome}, write: rootHome}

	err := tr.Write(testID, strings.NewReader("{}\n"), false, "9")
	if err == nil {
		t.Fatal("writing a record for an agent with no home succeeded")
	}
	if !strings.Contains(err.Error(), "9") {
		t.Errorf("the refusal does not name the agent: %v", err)
	}
	landed := filepath.Join(rootHome.ClaudeDir, "projects",
		sessions.EncodeProject(rootHome.Cwd), testID+".jsonl")
	if _, err := os.Stat(landed); err == nil {
		t.Error("the record was filed in the root agent's home anyway")
	}
}

// A record that claims no agent still lands. Older archives have no metadata
// saying who wrote what, and a record with nothing to contradict is not the
// same as one filed against the wrong agent.
func TestWriteWithNoAgentUsesTheFallbackHome(t *testing.T) {
	root := t.TempDir()
	rootHome := homeAt(t, root, "0")
	rootHome.Cwd = "/projects/coding-cave"
	tr := transcripts_{homes: []sessions.Local{rootHome}, write: rootHome}

	if err := tr.Write(testID, strings.NewReader("{}\n"), false, ""); err != nil {
		t.Fatal(err)
	}
	landed := filepath.Join(rootHome.ClaudeDir, "projects",
		sessions.EncodeProject(rootHome.Cwd), testID+".jsonl")
	if _, err := os.Stat(landed); err != nil {
		t.Errorf("an unattributed record did not land: %v", err)
	}
}

// /run/cave/sessions is group-writable by every agent, because an ordinary
// session runs as agent<id> and creates its own socket there. The sticky bit
// stops an agent removing somebody else's entry; it does not stop one
// creating an entry for a name nobody has taken. A socket planted under
// another agent's id would be found by `attach`, and attach hands over a
// terminal -- the human's.
func TestAttachRefusesASocketOwnedByTheWrongUser(t *testing.T) {
	dir := t.TempDir()
	envFile(t, dir, "0", "CAVE_AGENT_ROOT=1\n")
	envFile(t, dir, "6", "CAVE_AGENT_ID=6\n")

	// A socket this test made: owned by whoever runs the test, which for the
	// root agent has to be root.
	sock := agentd.SocketPath(dir, "0")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Skipf("cannot bind a unix socket here: %v", err)
	}
	defer ln.Close()

	st, err := os.Stat(sock)
	if err != nil {
		t.Fatal(err)
	}
	mine := st.Sys().(*syscall.Stat_t).Uid

	err = sessionIsTheAgents(dir, "0")
	if mine == 0 {
		if err != nil {
			t.Errorf("the root agent's own socket was refused: %v", err)
		}
	} else if err == nil {
		t.Error("a socket not owned by root was accepted for the root agent")
	}

	// Nothing there at all is not a problem: attach starts what it cannot
	// find, and the unit makes its own socket.
	if err := sessionIsTheAgents(dir, "3"); err != nil {
		t.Errorf("a missing socket should not be refused: %v", err)
	}

	// An id with no user cannot have a rightful owner, so there is nothing to
	// compare against and the answer is no rather than a guess.
	if err := os.Rename(sock, agentd.SocketPath(dir, "6")); err != nil {
		t.Fatal(err)
	}
	if _, err := user.Lookup("agent6"); err != nil {
		if err := sessionIsTheAgents(dir, "6"); err == nil {
			t.Error("a socket for an id with no user was accepted")
		}
	}
}
