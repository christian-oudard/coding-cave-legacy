// Command cave-guestd runs inside the guest. It owns each agent's pty, so an
// agent keeps running with nobody watching and a terminal that comes back
// later gets the output it missed instead of a blank screen.
//
//	cave-guestd serve [-socket path] [-id name] -- claude ...
//	cave-guestd list [-socket path]
//	cave-guestd attach [-socket path] [id]
package main

import (
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	"codingcave.dev/cave-vm/internal/agentd"
)

const defaultSocket = "/run/cave-agentd.sock"

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	args := os.Args[2:]
	switch os.Args[1] {
	case "serve":
		serve(args)
	case "list":
		list(args)
	case "attach":
		attach(args)
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: cave-guestd serve|list|attach")
	os.Exit(2)
}

func die(err error) {
	fmt.Fprintln(os.Stderr, "cave-guestd:", err)
	os.Exit(1)
}

func serve(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	sock := fs.String("socket", defaultSocket, "unix socket to listen on")
	id := fs.String("id", "1", "name of the agent to start")
	fs.Parse(args)
	argv := fs.Args()
	if len(argv) == 0 {
		die(fmt.Errorf("serve needs a command to run, after --"))
	}

	// A socket left behind by a killed daemon would block the bind, and the
	// unit that starts us is the only thing that ever owns this path.
	os.Remove(*sock)
	ln, err := net.Listen("unix", *sock)
	if err != nil {
		die(err)
	}
	if err := os.Chmod(*sock, 0o600); err != nil {
		die(err)
	}

	reg := agentd.NewRegistry()
	if _, err := reg.Start(*id, argv, nil, "", 0, 0); err != nil {
		die(err)
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-stop
		ln.Close()
		reg.CloseAll()
		os.Exit(0)
	}()

	if err := agentd.Serve(ln, reg); err != nil {
		die(err)
	}
}

func list(args []string) {
	fs := flag.NewFlagSet("list", flag.ExitOnError)
	sock := fs.String("socket", defaultSocket, "unix socket to connect to")
	fs.Parse(args)
	lines, err := agentd.List(*sock)
	if err != nil {
		die(err)
	}
	for _, line := range lines {
		fmt.Println(line)
	}
}

func attach(args []string) {
	fs := flag.NewFlagSet("attach", flag.ExitOnError)
	sock := fs.String("socket", defaultSocket, "unix socket to connect to")
	fs.Parse(args)
	id := "1"
	if fs.NArg() > 0 {
		id = fs.Arg(0)
	}
	if err := agentd.AttachTo(*sock, id, os.Stdin, os.Stdout); err != nil {
		die(err)
	}
}
