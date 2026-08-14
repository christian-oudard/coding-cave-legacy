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
	"net"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"

	"codingcave.dev/cave-vm/internal/agentd"
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
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: cave-guestd session|list|attach")
	os.Exit(2)
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

// startUnit asks systemd for a session that is not running, which is also how
// an agent that has exited comes back under the same name. Deliberately not a
// fork from here: a session parented by the process that attached would die
// with the ssh connection that carried it.
func startUnit(id string) error {
	cmd := exec.Command("systemctl", "start", fmt.Sprintf(unitPattern, id))
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
