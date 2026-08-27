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
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"codingcave.dev/cave-vm/internal/agentd"
	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/sessions"
)

const (
	defaultDir = "/run/cave/sessions"
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
	env := fmt.Sprintf("CAVE_AGENT_ID=%s\nCAVE_PROJECT=%s\nCAVE_BASE=%s\n", *id, *project, *base)
	// Which kind of agent this is, written down once, here, at the only
	// moment anybody says so. Every unit serving this agent reads it back:
	// which user to run as, which home to seed, and which of the two session
	// templates to start. Recording it rather than deriving it is what lets
	// root be a property of an agent instead of a reserved number.
	if *root {
		env += "CAVE_AGENT_ROOT=1\n"
	}
	if err := os.WriteFile(filepath.Join(*dir, *id+".env"), []byte(env), 0o600); err != nil {
		die(err)
	}
	if err := startUnit(*dir, *id); err != nil {
		die(err)
	}
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
	store := transcripts_{sessions.Local{ClaudeDir: *claude, Cwd: *cwd, SharedDir: *shareDir}}
	srv := &http.Server{
		Handler: control.Handler(g, store, token),
		// A control call is a question about local state; anything slower than
		// this is a hung guest, and holding the connection open hides that.
		ReadHeaderTimeout: 10 * time.Second,
	}
	die(srv.Serve(ln))
}

// transcripts_ adapts sessions.Local to the control plane's Sessions, which is
// two type conversions and no logic: both sides already agree on what a record
// is, and the duplication buys the two packages not importing each other.
type transcripts_ struct{ local sessions.Local }

func (t transcripts_) List() ([]control.SessionRecord, error) {
	recs, err := t.local.List()
	if err != nil {
		return nil, err
	}
	out := make([]control.SessionRecord, 0, len(recs))
	for _, r := range recs {
		out = append(out, control.SessionRecord{ID: r.ID, Size: r.Size, Project: r.Project})
	}
	return out, nil
}

func (t transcripts_) Read(id string) (io.ReadCloser, error) { return t.local.Read(id) }

func (t transcripts_) Write(id string, r io.Reader, shared bool) error {
	dest := sessions.Resumable
	if shared {
		dest = sessions.Shared
	}
	return t.local.Write(id, r, dest)
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
	fs.Parse(args)

	local := sessions.Local{ClaudeDir: *claude, Cwd: *cwd, SharedDir: *shareDir}
	switch fs.Arg(0) {
	case "list":
		records, err := local.List()
		if err != nil {
			die(err)
		}
		for _, r := range records {
			fmt.Printf("%s %d %s\n", r.ID, r.Size, r.Project)
		}
	case "read":
		rc, err := local.Read(fs.Arg(1))
		if err != nil {
			die(err)
		}
		defer rc.Close()
		if _, err := io.Copy(os.Stdout, rc); err != nil {
			die(err)
		}
	case "write":
		dest := sessions.Resumable
		if *shared {
			dest = sessions.Shared
		}
		if err := local.Write(fs.Arg(1), os.Stdin, dest); err != nil {
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
