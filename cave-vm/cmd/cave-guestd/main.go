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
	"syscall"
	"time"

	"codingcave.dev/cave-vm/internal/agentd"
	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/sessions"
)

const (
	defaultDir  = "/run/cave/sessions"
	unitPattern = "cave-session@%s.service"
	startWait   = 10 * time.Second
)

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
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: cave-guestd session|list|attach|serve")
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
		Unit:     unitPattern,
		List:     agentd.List,
	}
	srv := &http.Server{
		Handler: control.Handler(g, token),
		// A control call is a question about local state; anything slower than
		// this is a hung guest, and holding the connection open hides that.
		ReadHeaderTimeout: 10 * time.Second,
	}
	die(srv.Serve(ln))
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
	sock, err := agentd.EnsureSocket(*dir, id, func() error { return startUnit(id) }, startWait)
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
	cwd := fs.String("cwd", "/root/coding-cave", "the cwd a restored session resumes in")
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
func startUnit(id string) error {
	cmd := exec.Command("systemctl", "start", fmt.Sprintf(unitPattern, id))
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
