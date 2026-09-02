// Command cave-guestd runs inside the guest. One process holds one agent's
// pty, so an agent keeps running with nobody watching and a terminal that
// comes back later gets the output it missed instead of a blank screen.
//
//	cave-guestd session -id name -- claude ...
//	cave-guestd list [-dir path]
//	cave-guestd attach [-dir path] [id]
//
// The session process is started by systemd as cave-session@<id>.service and
// is the only thing that outlives an attach. Nothing multiplexes: `list` reads
// the socket directory and `attach` asks systemd for a session that is not
// there, so no long-lived process holds the ptys and none can take them down.
package main

import (
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"codingcave.dev/cave-vm/internal/agentd"
	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/sessions"
)

const (
	defaultDir = "/run/cave/sessions"
	// Where agents' worktrees live, which only this side knows: an agent sees
	// its own bound over /projects and never this path.
	defaultWorktrees = "/var/lib/coding-cave/agents"
	// The two templates a session runs under. They differ in one thing, which
	// is the user: an ordinary agent's session runs as agent<id>, the root
	// agent's runs as root because rebuilding the system every other agent
	// runs in is what it is for. A unit cannot choose its own User= from its
	// instance name, so the choice is a unit name.
	unitPattern     = "cave-session@%s.service"
	rootUnitPattern = "cave-root-session@%s.service"
	startWait       = 10 * time.Second
)

// unitFor names the template an agent's session runs under.
//
// Read back from the session's own environment file rather than worked out
// from the id, because being the root agent is a property of an agent and not
// a number it holds: the registry hands out ids without reserving one, so
// nothing about "3" says whether agent 3 is root. The env file is where that
// was recorded when the agent was created, and it is the same file the units
// themselves read, so a session cannot be started under one answer and
// stopped under the other.
func unitFor(dir, id string) string {
	if agentIsRoot(dir, id) {
		return fmt.Sprintf(rootUnitPattern, id)
	}
	return fmt.Sprintf(unitPattern, id)
}

// Where the guest keeps homes. The layout is fixed by the guest's own config,
// so these are not options; they are variables only so a test can point them
// at a tree it made, which is the difference between testing this and
// asserting against whatever homes the machine running the test happens to
// have.
var (
	rootHomeDir  = "/root"
	agentHomeDir = "/home"
)

// agentHomes finds every agent's ~/.claude, and whose it is.
//
// Derived from the guest's own layout rather than from a registry, because the
// question outlives any record of it: an agent that has exited still owns the
// transcripts in its home, and those are exactly the ones worth copying out.
// The layout is the same one seed-agent-home@ writes -- /home/agent<N> for an
// ordinary agent, /root for the root one -- so the directory name is the id.
//
// The root agent's id is read back from the session env files, which is where
// it was recorded when it was created. With none of them saying so, the guest
// has no running root agent and "0" is the reserved id it would hold.
func agentHomes(sessionsDir, claudeName string) []sessions.Local {
	var out []sessions.Local
	add := func(agent, home string) {
		dir := filepath.Join(home, claudeName)
		if st, err := os.Stat(dir); err == nil && st.IsDir() {
			out = append(out, sessions.Local{ClaudeDir: dir, Agent: agent})
		}
	}
	add(rootAgentID(sessionsDir), rootHomeDir)
	entries, err := os.ReadDir(agentHomeDir)
	if err != nil {
		return out
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		id, ok := strings.CutPrefix(e.Name(), "agent")
		if !ok || id == "" || strings.ContainsFunc(id, func(r rune) bool { return r < '0' || r > '9' }) {
			continue
		}
		add(id, filepath.Join(agentHomeDir, e.Name()))
	}
	return out
}

// rootAgentID is which id the guest's root agent holds, according to the
// session env files it was started from.
func rootAgentID(dir string) string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return "0"
	}
	for _, e := range entries {
		name, ok := strings.CutSuffix(e.Name(), ".env")
		if !ok {
			continue
		}
		if agentIsRoot(dir, name) {
			return name
		}
	}
	return "0"
}

// agentIsRoot reports whether the agent was created as the guest's root agent.
// A missing or unreadable file reads as "not root", which is the safe way to
// be wrong: an ordinary session that should have been root fails visibly at
// the first thing needing privilege, where the reverse would quietly hand an
// agent the whole guest.
func agentIsRoot(dir, id string) bool {
	b, err := os.ReadFile(filepath.Join(dir, id+".env"))
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(line) == "CAVE_AGENT_ROOT=1" {
			return true
		}
	}
	return false
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	args := os.Args[2:]
	switch os.Args[1] {
	case "session":
		session(args)
	case "list":
		list(args)
	case "attach":
		attach(args)
	case "sessions":
		transcripts(args)
	case "serve":
		serve(args)
	case "start":
		start(args)
	case "env":
		envCmd(args)
	case "holders":
		holders(args)
	case "reclaimable":
		reclaimable(args)
	case "occupied":
		occupied(args)
	case "remove":
		remove(args)
	default:
		usage()
	}
}

// start creates an agent: it records which project and base branch the agent
// was given, then starts the unit pair that checks the project out and runs
// the agent in it.
//
// The environment file is the whole mechanism, and it exists because a
// project is a per-agent fact. It used to be a boot-time constant, which
// meant the VM decided what an agent worked on before any agent existed, and
// got it wrong in two ways at once: the repo was hardcoded and the branch was
// whatever HEAD the shared bare repo froze at when it was created.
func start(args []string) {
	fs := flag.NewFlagSet("start", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	id := fs.String("id", "1", "agent id")
	project := fs.String("project", "", "project slug to check out (required)")
	base := fs.String("base", "", "base branch to work from (required)")
	root := fs.Bool("root", false, "this agent is the guest's root agent (runs as root)")
	fresh := fs.Bool("fresh", false, "start a new conversation instead of continuing the agent's last")
	worktrees := fs.String("worktrees", defaultWorktrees, "where agents' worktrees live")
	fs.Parse(args)
	if *project == "" || *base == "" {
		die(fmt.Errorf("start needs -project and -base"))
	}
	// Both become a path and a branch name in a shell script, and both arrive
	// from the host, so neither may carry anything that changes what that
	// script means.
	for what, v := range map[string]string{"project": *project, "base": *base} {
		if err := validRef(v); err != nil {
			die(fmt.Errorf("%s: %w", what, err))
		}
	}
	if err := os.MkdirAll(*dir, 0o700); err != nil {
		die(err)
	}
	// What this agent was the last time anybody said, which outlives the
	// runtime record. /run is tmpfs, so a VM stop takes the session's env file
	// while everything it describes -- the worktree, the home, the branch on
	// the host -- survives; the agent ends up the only part of itself that
	// does not. Keeping a copy beside the worktree is not the guest deciding
	// what agents exist, which stays the host's call: it is the guest not
	// forgetting what it was told about one it already has.
	//
	// Root-ness is the field that matters, because it is the one a caller can
	// omit. Recreating agent 0 without -root resolved its identity to a user
	// the guest does not declare, and the guest came back running nothing and
	// reporting itself as not answering.
	if !*root && agentWasRoot(*worktrees, *id) {
		fmt.Fprintf(os.Stderr,
			"cave-guestd: agent %s was created as the root agent; keeping that\n", *id)
		*root = true
	}

	env := fmt.Sprintf("CAVE_AGENT_ID=%s\nCAVE_PROJECT=%s\nCAVE_BASE=%s\n", *id, *project, *base)
	// Which kind of agent this is, written down once, here, at the only
	// moment anybody says so. Every unit serving this agent reads it back:
	// which user to run as, which home to seed, and which of the two session
	// templates to start. Recording it rather than deriving it is what lets
	// root be a property of an agent instead of a reserved number.
	if *root {
		env += "CAVE_AGENT_ROOT=1\n"
	}
	// An agent resumes its own last conversation by default, because a restart
	// is not a new agent. This is how to say you want a blank one -- a context
	// that has gone somewhere unhelpful is the usual reason, and the old
	// transcript is still on disk either way.
	if *fresh {
		env += "CAVE_AGENT_FRESH=1\n"
	}
	if err := os.WriteFile(filepath.Join(*dir, *id+".env"), []byte(env), 0o600); err != nil {
		die(err)
	}
	// The durable copy, beside the worktree it describes, so the two are lost
	// together or not at all. `remove` deletes that directory, which is what
	// forgetting an agent should mean.
	if err := os.MkdirAll(filepath.Join(*worktrees, *id), 0o755); err == nil {
		os.WriteFile(agentRecord(*worktrees, *id), []byte(env), 0o600)
	}
	if err := startUnit(*dir, *id); err != nil {
		die(err)
	}
}

// agentRecord is where an agent's identity survives a stop, beside the
// worktree rather than in /run, which is tmpfs.
func agentRecord(worktrees, id string) string {
	return filepath.Join(worktrees, id, "agent.env")
}

// agentWasRoot reports whether this agent was created as the root agent,
// according to the record it left behind. A missing record means no opinion:
// a new agent is whatever it is being asked to be.
func agentWasRoot(worktrees, id string) bool {
	b, err := os.ReadFile(agentRecord(worktrees, id))
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(line) == "CAVE_AGENT_ROOT=1" {
			return true
		}
	}
	return false
}

// remove deletes what an agent leaves behind in the guest: its worktrees and
// the contents of its home.
//
// Here rather than in the host script because it is the guest's layout that
// says what an agent has -- a worktree root, a home per user, the same two
// places seed-agent-home@ and clone-repo@ write to. A host that knew those
// paths would be a second copy of them, and the copy that drifts is the one
// that deletes the wrong directory.
//
// It refuses a running agent rather than stopping one. Stopping is a decision
// with a person behind it: an agent has a terminal somebody may be attached
// to, and taking its worktree out from under it is the one thing a reformat
// exists to do deliberately.
//
// Transcripts are not its problem. They live in the home it deletes, and are
// copied to the host's archive before this runs -- by the caller, which is the
// side that has the archive. That ordering is the host's to get right and is
// why this says nothing about it.
func remove(args []string) {
	fs := flag.NewFlagSet("remove", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	id := fs.String("id", "", "agent to remove (required)")
	root := fs.String("worktrees", defaultWorktrees, "where agents' worktrees live")
	fs.Parse(args)
	if *id == "" {
		die(fmt.Errorf("remove needs -id"))
	}
	if err := validRef(*id); err != nil {
		die(fmt.Errorf("id: %w", err))
	}
	if _, err := os.Stat(agentd.SocketPath(*dir, *id)); err == nil {
		die(fmt.Errorf("agent %s is running; stop it first", *id))
	}

	// The worktrees, which is the space worth reclaiming.
	tree := filepath.Join(*root, *id)
	if err := os.RemoveAll(tree); err != nil {
		die(err)
	}
	// And the home's contents, not the home: the directory belongs to a
	// declared user and is not this program's to remove. Emptying it is what
	// makes the next agent to hold this id start clean rather than inheriting
	// a stranger's settings and half a conversation.
	if home := homeOf(*id); home != "" {
		entries, err := os.ReadDir(home)
		if err == nil {
			for _, e := range entries {
				os.RemoveAll(filepath.Join(home, e.Name()))
			}
		}
	}
	// The session's record of what it was created for. Without this the next
	// `start` for this id reads the old project out of a stale file.
	os.Remove(filepath.Join(*dir, *id+".env"))
	fmt.Printf("removed agent %s: %s and its home\n", *id, tree)
}

// reclaimable lists agents holding disk that nothing is using: stopped, not
// the root agent, and untouched for long enough that somebody is unlikely to
// be coming back to them.
//
// It only reports. What an agent has left behind is visible from here, but
// whether it is safe to delete is not: an agent branch with commits its base
// does not have lives in the shared repo on the host, and that is the copy
// that makes a worktree worth keeping. So the host asks this what exists and
// decides for itself.
//
// Age is the worktree's mtime, which is when the agent last wrote code. A
// session that is merely idle keeps its worktree fresh only if it is working,
// which is the distinction wanted: an agent stopped an hour ago mid-task is
// not the same as one abandoned last week.
func reclaimable(args []string) {
	fs := flag.NewFlagSet("reclaimable", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	root := fs.String("worktrees", defaultWorktrees, "where agents' worktrees live")
	olderThan := fs.Duration("older-than", 24*time.Hour, "ignore agents touched more recently")
	fs.Parse(args)

	entries, err := os.ReadDir(*root)
	if err != nil {
		return // no worktree root is no agents, not a failure
	}
	rootID := rootAgentID(*dir)
	cutoff := time.Now().Add(-*olderThan)
	for _, e := range entries {
		id := e.Name()
		if !e.IsDir() || id == rootID {
			continue
		}
		if _, err := os.Stat(agentd.SocketPath(*dir, id)); err == nil {
			continue // running
		}
		tree := filepath.Join(*root, id)
		st, err := os.Stat(tree)
		if err != nil || st.ModTime().After(cutoff) {
			continue
		}
		fmt.Printf("%s %d %s\n", id, int64(time.Since(st.ModTime()).Hours()), tree)
	}
}

// occupied lists every agent id that still holds state in this guest: a
// worktree, or a home claude has written in. Running or not, recent or not --
// this is "is anything of an agent still here", not "may it be swept".
//
// The host allocates ids by finding one nothing collides with, and it could
// see only two of the three things an id owns: a running session, which it
// asks the guest for, and a pushed branch, which is in the shared repo. The
// worktree and the home are on the guest's own disk and outlive both. So an
// agent that stopped and had its work landed looked free while six megabytes
// of its transcripts and a checked-out tree sat under its id, and the next
// agent handed that id inherited them -- including, because continuing is the
// default, the previous agent's last conversation.
//
// Separate from reclaimable, which answers a different question: that one is
// for sweeping and so excludes the running and the recent, and both of those
// are exactly what allocation must still count as occupied.
func occupied(args []string) {
	fs := flag.NewFlagSet("occupied", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	root := fs.String("worktrees", defaultWorktrees, "where agents' worktrees live")
	fs.Parse(args)

	ids := map[string]bool{}
	// A worktree is a directory named for the id that owns it.
	if entries, err := os.ReadDir(*root); err == nil {
		for _, e := range entries {
			if e.IsDir() {
				ids[e.Name()] = true
			}
		}
	}
	// A home counts even with no worktree beside it: the transcripts are
	// there, and handing them to a new agent is the thing being prevented.
	for _, h := range agentHomes(*dir, ".claude") {
		ids[h.Agent] = true
	}

	out := make([]string, 0, len(ids))
	for id := range ids {
		out = append(out, id)
	}
	sort.Strings(out)
	for _, id := range out {
		fmt.Println(id)
	}
}

// holders lists the agents holding a worktree of a project, and whether each
// is running.
//
// Only the guest can answer it: a worktree is a directory under the worktree
// root, which the host never sees. Unregistering a project the host is still
// serving to an agent would leave that agent with a checkout it can no longer
// clone or fetch, which is a worse state than either having the project or
// not.
func holders(args []string) {
	fs := flag.NewFlagSet("holders", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	root := fs.String("worktrees", defaultWorktrees, "where agents' worktrees live")
	project := fs.String("project", "", "project slug to look for (required)")
	fs.Parse(args)
	if *project == "" {
		die(fmt.Errorf("holders needs -project"))
	}
	if err := validRef(*project); err != nil {
		die(fmt.Errorf("project: %w", err))
	}
	entries, err := os.ReadDir(*root)
	if err != nil {
		return
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		id := e.Name()
		if _, err := os.Stat(filepath.Join(*root, id, "projects", *project)); err != nil {
			continue
		}
		state := "stopped"
		if _, err := os.Stat(agentd.SocketPath(*dir, id)); err == nil {
			state = "running"
		}
		fmt.Printf("%s %s\n", id, state)
	}
}

// homeOf is where an agent's home is, or empty for one that has none. Never
// /root: the root agent's home is the machine's, holding ssh keys and the
// gnupg keyring, and emptying it would take the guest apart.
func homeOf(id string) string {
	home := "/home/agent" + id
	if st, err := os.Stat(home); err == nil && st.IsDir() {
		return home
	}
	return ""
}

// validRef keeps a host-supplied project or branch from being anything but a
// name. The host is the only caller and would not send anything strange, which
// is exactly the assumption worth not making where a string becomes a path.
func validRef(s string) error {
	if s == "" || len(s) > 128 || strings.HasPrefix(s, "-") || strings.HasPrefix(s, "/") {
		return fmt.Errorf("%q is not a usable name", s)
	}
	if strings.Contains(s, "..") {
		return fmt.Errorf("%q contains ..", s)
	}
	for _, r := range s {
		ok := r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' ||
			r == '-' || r == '_' || r == '.' || r == '/'
		if !ok {
			return fmt.Errorf("%q contains %q", s, r)
		}
	}
	return nil
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: cave-guestd session|list|attach|serve")
	fmt.Fprintln(os.Stderr, "       cave-guestd start -id N -project SLUG -base BRANCH")
	fmt.Fprintln(os.Stderr, "       cave-guestd sessions list|read <id>|write <id>")
	fmt.Fprintln(os.Stderr, "       cave-guestd env build [-force]")
	fmt.Fprintln(os.Stderr, "       cave-guestd env seed -home DIR -user NAME")
	fmt.Fprintln(os.Stderr, "       cave-guestd remove -id N")
	fmt.Fprintln(os.Stderr, "       cave-guestd reclaimable [-older-than D]")
	fmt.Fprintln(os.Stderr, "       cave-guestd occupied")
	fmt.Fprintln(os.Stderr, "       cave-guestd holders -project SLUG")
	os.Exit(2)
}

// serve answers the host's control plane on vsock 7001. systemd binds the
// socket (ListenStream=vsock::7001) and passes it in, so the port is listening
// from the moment the socket unit is enabled, whether or not this process has
// started yet, and a caller cannot race the daemon's startup.
//
// The token comes from the seed disk, which is where every other host-provided
// secret arrives, and is read at startup rather than per request: the seed is
// read-only and cannot change under a running guest.
func serve(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	tokenPath := fs.String("token", "/etc/cave-host/control-token", "shared secret from the seed")
	addr := fs.String("addr", "", "listen here instead of on the socket systemd passed")
	// The same three paths the `sessions` subcommand takes, and for the same
	// reasons; see transcripts below.
	claude := fs.String("claude", filepath.Join(homeDir(), ".claude"), "the agent's ~/.claude")
	cwd := fs.String("cwd", "/projects/coding-cave", "the cwd a restored session resumes in")
	shareDir := fs.String("shared-dir", "/cave/sessions", "where every agent's records are readable")
	fs.Parse(args)

	token, err := control.ReadToken(*tokenPath)
	if err != nil {
		die(err)
	}
	ln, err := control.ActivationListener()
	if err != nil {
		die(err)
	}
	if ln == nil {
		if *addr == "" {
			die(fmt.Errorf("no socket from systemd and no -addr; nothing to listen on"))
		}
		if ln, err = net.Listen("tcp", *addr); err != nil {
			die(err)
		}
	}
	g := control.System{
		Sessions: *dir,
		Unit:     func(id string) string { return unitFor(*dir, id) },
		List:     agentd.List,
	}
	// The transcript half, wired here rather than inside the control package,
	// which must not import sessions: the host's implementation of that
	// package's Guest interface is built on the control client, so the two
	// would import each other. Same reason agentd.List is injected above.
	store := transcripts_{
		homes: agentHomes(*dir, ".claude"),
		write: sessions.Local{ClaudeDir: *claude, Cwd: *cwd, SharedDir: *shareDir},
	}
	srv := &http.Server{
		Handler: control.Handler(g, store, token),
		// A control call is a question about local state; anything slower than
		// this is a hung guest, and holding the connection open hides that.
		ReadHeaderTimeout: 10 * time.Second,
	}
	die(srv.Serve(ln))
}

// transcripts_ adapts sessions.Local to the control plane's Sessions, and is
// where the guest's one-home-per-agent layout is turned into a single view of
// every transcript in the guest.
//
// The type conversion is two lines and no logic -- both sides already agree on
// what a record is, and the duplication buys the two packages not importing
// each other. What is not incidental is the loop: an agent's transcripts live
// in that agent's home, so listing one home lists one agent, and the host used
// to see root's and nobody else's.
type transcripts_ struct {
	homes []sessions.Local
	// write is the fallback home, and the cwd and shared directory every
	// write borrows: a home found by scanning knows whose it is and nothing
	// else, while these two are properties of the guest's layout.
	//
	// Only a record that claims no agent lands here. One that names an agent
	// goes to that agent's home or is refused, because a home that is not the
	// writer's is the wrong answer rather than a near one.
	write sessions.Local
}

func (t transcripts_) List() ([]control.SessionRecord, error) {
	var out []control.SessionRecord
	for _, h := range t.homes {
		recs, err := h.List()
		if err != nil {
			// One unreadable home does not hide the rest. A home with no
			// transcripts is the ordinary state of an agent that has not run.
			continue
		}
		for _, r := range recs {
			out = append(out, control.SessionRecord{
				ID: r.ID, Size: r.Size, Project: r.Project, Agent: r.Agent,
			})
		}
	}
	return out, nil
}

// Read finds a record wherever it lives. Ids are uuids, so the first home that
// has one is the one that owns it.
func (t transcripts_) Read(id string) (io.ReadCloser, error) {
	var err error
	for _, h := range t.homes {
		var rc io.ReadCloser
		if rc, err = h.Read(id); err == nil {
			return rc, nil
		}
	}
	return nil, err
}

// Write puts a restored record into the home of the agent it belongs to, so
// that agent's own picker offers it and nobody else's does.
//
// Falling back to the default home when the agent is unknown is deliberate and
// narrow: a shared record has no owner, and a record whose owner is not an
// agent this guest has is better placed somewhere a human can find it than
// dropped. A named agent that does have a home is never diverted.
//
// Ownership matters as much as the path. The daemon is root and the agent is
// not, so a record written here and left alone is one its owner cannot read --
// a restore that appears to work and yields an empty picker.
func (t transcripts_) Write(id string, r io.Reader, shared bool, agent string) error {
	dest := sessions.Resumable
	if shared {
		dest = sessions.Shared
	}
	home := t.write
	if agent != "" && !shared {
		found := false
		for _, h := range t.homes {
			if h.Agent == agent {
				home, found = h, true
				home.Cwd, home.SharedDir = t.write.Cwd, t.write.SharedDir
				break
			}
		}
		// Refused rather than filed somewhere else. Landing it in the default
		// home puts one agent's history in another agent's picker, labelled as
		// though that agent had written it -- the same misattribution the sync
		// side was fixed for, arriving from the other direction. It is also
		// self-concealing: Restore skips any id the guest already has, so the
		// wrong copy is what stops the right one ever being placed.
		//
		// Reachable on the ordinary path. A start restores the whole archive
		// before any agent has been created, so on a fresh or reformatted
		// guest every ordinary agent's records arrive here with no home to go
		// to. They are placed when the agent is created and its home exists,
		// which is where `vmcav agent` asks for them.
		if !found {
			return fmt.Errorf("agent %s has no home in this guest yet, so its records have nowhere to land", agent)
		}
	}
	if err := home.Write(id, r, dest, agent); err != nil {
		return err
	}
	return chownToOwner(home.ClaudeDir)
}

// chownToOwner hands a home's transcripts back to whoever owns the home. The
// directories are created on demand by the write above, as root, which is the
// one thing that makes a restored record invisible to the agent it was
// restored for.
func chownToOwner(claudeDir string) error {
	st, err := os.Stat(filepath.Dir(claudeDir))
	if err != nil {
		return err
	}
	sys, ok := st.Sys().(*syscall.Stat_t)
	if !ok {
		return nil
	}
	uid, gid := int(sys.Uid), int(sys.Gid)
	return filepath.WalkDir(claudeDir, func(p string, _ fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		return os.Lchown(p, uid, gid)
	})
}

func die(err error) {
	fmt.Fprintln(os.Stderr, "cave-guestd:", err)
	os.Exit(1)
}

// session is the whole of a session's lifetime: it runs the agent, serves the
// socket named after it, and exits when the agent does, which is what makes
// the unit's state and the session's state the same fact.
func session(args []string) {
	fs := flag.NewFlagSet("session", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	id := fs.String("id", "1", "name of this session")
	fs.Parse(args)
	argv := fs.Args()
	if len(argv) == 0 {
		die(fmt.Errorf("session needs a command to run, after --"))
	}

	if err := os.MkdirAll(*dir, 0o700); err != nil {
		die(err)
	}
	sock := agentd.SocketPath(*dir, *id)
	// A socket left behind by a killed session would block the bind. Only one
	// session of a given name runs at a time, systemd sees to that, so the
	// file can only be a corpse.
	os.Remove(sock)
	ln, err := net.Listen("unix", sock)
	if err != nil {
		die(err)
	}
	if err := os.Chmod(sock, 0o600); err != nil {
		die(err)
	}

	s, err := agentd.StartSession(*id, argv, nil, "", 0, 0)
	if err != nil {
		os.Remove(sock)
		die(err)
	}
	go agentd.Serve(ln, s)

	// systemd stopping us is systemd stopping the agent; a client detaching is
	// not, and never comes here.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-stop
		s.Close()
	}()

	waitErr := s.Wait()
	ln.Close()
	os.Remove(sock)
	if ee, ok := waitErr.(*exec.ExitError); ok {
		os.Exit(ee.ExitCode())
	}
	if waitErr != nil {
		die(waitErr)
	}
}

func list(args []string) {
	fs := flag.NewFlagSet("list", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	fs.Parse(args)
	lines, err := agentd.List(*dir)
	if err != nil {
		die(err)
	}
	for _, line := range lines {
		fmt.Println(line)
	}
}

func attach(args []string) {
	fs := flag.NewFlagSet("attach", flag.ExitOnError)
	dir := fs.String("dir", defaultDir, "directory of session sockets")
	fs.Parse(args)
	id := "1"
	if fs.NArg() > 0 {
		id = fs.Arg(0)
	}
	sock, err := agentd.EnsureSocket(*dir, id, func() error { return startUnit(*dir, id) }, startWait)
	if err != nil {
		die(err)
	}
	if err := agentd.AttachTo(sock, os.Stdin, os.Stdout); err != nil {
		die(err)
	}
}

// transcripts serves the host's checkpoints: list what claude has written,
// hand over one record, or put one back. Read-only but for `write`, and it
// names nothing the host asked it to name, which is the point of the split:
// the host validates every id and constructs every path, so nothing here can
// be talked into touching a file outside claude's own project directories.
func transcripts(args []string) {
	fs := flag.NewFlagSet("sessions", flag.ExitOnError)
	claude := fs.String("claude", filepath.Join(homeDir(), ".claude"),
		"the agent's ~/.claude")
	// Where a restored record has to land, because claude looks for a session
	// under the directory encoding the cwd it is resumed in. The agent's
	// worktree, matching what cave-agent cds to; when caves become per-agent
	// this comes from the cave's own record instead of a default.
	cwd := fs.String("cwd", "/projects/coding-cave", "the cwd a restored session resumes in")
	// Every agent's records, for reading rather than resuming. Outside
	// ~/.claude so claude never scans it, which is what keeps another agent's
	// session out of this agent's picker while still being readable.
	shareDir := fs.String("shared-dir", "/cave/sessions", "where every agent's records are readable")
	shared := fs.Bool("shared", false, "write into the shared directory rather than the picker")
	agent := fs.String("agent", "", "whose home a written record belongs in")
	fs.Parse(args)

	local := sessions.Local{ClaudeDir: *claude, Cwd: *cwd, SharedDir: *shareDir}
	// Reading covers every agent, writing lands in one. This is the same shape
	// as the control plane's store and for the same reason: a transcript
	// belongs to whichever home holds it, so listing one home lists one agent,
	// while a restore is addressed to the guest and has no agent to aim at.
	all := transcripts_{homes: agentHomes(defaultDir, ".claude"), write: local}
	switch fs.Arg(0) {
	case "list":
		records, err := all.List()
		if err != nil {
			die(err)
		}
		for _, r := range records {
			fmt.Printf("%s %d %s %s\n", r.ID, r.Size, r.Project, r.Agent)
		}
	case "read":
		rc, err := all.Read(fs.Arg(1))
		if err != nil {
			die(err)
		}
		defer rc.Close()
		if _, err := io.Copy(os.Stdout, rc); err != nil {
			die(err)
		}
	case "write":
		if err := all.Write(fs.Arg(1), os.Stdin, *shared, *agent); err != nil {
			die(err)
		}
	default:
		usage()
	}
}

func homeDir() string {
	if h := os.Getenv("HOME"); h != "" {
		return h
	}
	return "/root"
}

// startUnit asks systemd for a session that is not running, which is also how
// an agent that has exited comes back under the same name. Deliberately not a
// fork from here: a session parented by the process that attached would die
// with the ssh connection that carried it.
func startUnit(dir, id string) error {
	cmd := exec.Command("systemctl", "start", unitFor(dir, id))
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
