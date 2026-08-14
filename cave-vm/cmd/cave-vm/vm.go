package main

import (
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"codingcave.dev/cave-vm/internal/vmm"
)

// The VM half of the CLI. vmcav still owns everything above the VMM: seed
// staging, the git daemon, ssh and the agent, image creation. What moved here
// is the part that differs per host platform, so that it differs in one place.

type vmFlags struct {
	fs                    *flag.FlagSet
	state                 string
	kernel, initrd, init_ string
	disk, seed            string
	memory, cpus, cid     int
	crinj, git            string
}

func vmFlagSet(name string) *vmFlags {
	v := &vmFlags{fs: flag.NewFlagSet(name, flag.ExitOnError)}
	v.fs.StringVar(&v.state, "state", "", "state directory (required)")
	v.fs.StringVar(&v.kernel, "kernel", "", "kernel image")
	v.fs.StringVar(&v.initrd, "initrd", "", "initrd image")
	v.fs.StringVar(&v.init_, "init", "", "init= target, the guest toplevel's init")
	v.fs.StringVar(&v.disk, "disk", "", "guest root disk")
	v.fs.StringVar(&v.seed, "seed", "", "read-only seed disk")
	v.fs.IntVar(&v.memory, "memory", 4096, "guest memory in MiB")
	v.fs.IntVar(&v.cpus, "cpus", 4, "guest vCPUs")
	v.fs.IntVar(&v.cid, "cid", 3, "guest vsock CID")
	v.fs.StringVar(&v.crinj, "crinj", "", "host address to relay vsock 7200 to")
	v.fs.StringVar(&v.git, "git", "", "host address to relay vsock 7300 to")
	return v
}

func (v *vmFlags) parse(args []string) {
	v.fs.Parse(args)
	if v.state == "" {
		fmt.Fprintln(os.Stderr, "cave-vm: --state is required")
		os.Exit(2)
	}
}

func (v *vmFlags) paths() vmm.Paths { return vmm.NewPaths(v.state) }

func (v *vmFlags) spec(p vmm.Paths) vmm.Spec {
	return vmm.Spec{
		Kernel:   v.kernel,
		Initrd:   v.initrd,
		Init:     v.init_,
		Disks:    []vmm.Disk{{Path: v.disk}, {Path: v.seed, ReadOnly: true}},
		MemoryMB: v.memory,
		CPUs:     v.cpus,
		CID:      uint32(v.cid),
		Console:  filepath.Join(p.State, "console.log"),
		Ports:    vmm.Ports(v.crinj, v.git),
	}
}

func backend() vmm.Backend {
	b, err := vmm.Pick()
	if err != nil {
		fmt.Fprintln(os.Stderr, "cave-vm:", err)
		os.Exit(1)
	}
	return b
}

func planePidFile(p vmm.Paths) string { return filepath.Join(p.State, "plane.pid") }

// vmStart brings up the plane and then the VM, in that order: the guest dials
// out during early boot, so a socket that appears afterwards is a service the
// guest already failed to reach.
func vmStart(args []string) {
	v := vmFlagSet("start")
	v.parse(args)
	b, p := backend(), v.paths()
	spec := v.spec(p)

	if pid, ok := vmm.Running(b, spec, p); ok {
		if pid == 0 {
			fmt.Fprintln(os.Stderr, "cave-vm: this VM is already running under another supervisor")
			os.Exit(1)
		}
		fmt.Fprintf(os.Stderr, "cave-vm: already running (pid %d)\n", pid)
		return
	}
	// Every precondition before the first side effect. Starting the plane and
	// then discovering there is no VMM leaves relays running for a VM that
	// will never boot, and the next start inherits them.
	// --init is deliberately not checked: it is a path in the guest's store,
	// not the host's. On a Linux host the two happen to hold the same paths
	// and the check would pass; on macOS the guest artifacts are built in a
	// Lima VM, so the toplevel exists nowhere on the host and the check
	// rejects a perfectly bootable VM.
	for _, f := range []string{v.kernel, v.initrd, v.disk, v.seed} {
		if _, err := os.Stat(f); err != nil {
			fmt.Fprintf(os.Stderr, "cave-vm: missing boot artifact %s\n", f)
			os.Exit(1)
		}
	}
	if err := b.Check(); err != nil {
		fmt.Fprintln(os.Stderr, "cave-vm:", err)
		os.Exit(1)
	}
	if err := servePlane(v, p); err != nil {
		fmt.Fprintln(os.Stderr, "cave-vm:", err)
		os.Exit(1)
	}
	pid, err := vmm.Start(b, spec, p)
	if err != nil {
		stopPlane(p)
		fmt.Fprintln(os.Stderr, "cave-vm:", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "cave-vm: vm started (%s, pid %d); console: %s\n",
		b.Name(), pid, spec.Console)
}

// servePlane runs the relays in their own process rather than this one, because
// `start` returns and the relays have to outlive it. Re-exec rather than fork:
// the supervisor holds no state the VM depends on, so the plane must be
// restartable on its own, and that is only true if it is a normal command.
func servePlane(v *vmFlags, p vmm.Paths) error {
	stopPlane(p)
	self, err := os.Executable()
	if err != nil {
		return err
	}
	log, err := os.OpenFile(filepath.Join(p.State, "plane.log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer log.Close()
	cmd := exec.Command(self, "serve", "--state", v.state, "--crinj", v.crinj, "--git", v.git)
	cmd.Stdout, cmd.Stderr = log, log
	pid, err := vmm.StartDetached(cmd)
	if err != nil {
		return fmt.Errorf("starting the vsock plane: %w", err)
	}
	if err := os.WriteFile(planePidFile(p), []byte(strconv.Itoa(pid)), 0o644); err != nil {
		return err
	}
	// The sockets have to exist before the VMM is told to dial them.
	time.Sleep(200 * time.Millisecond)
	return nil
}

func stopPlane(p vmm.Paths) {
	raw, err := os.ReadFile(planePidFile(p))
	if err != nil {
		return
	}
	if pid, err := strconv.Atoi(string(raw)); err == nil && pid > 0 {
		syscall.Kill(pid, syscall.SIGTERM)
	}
	os.Remove(planePidFile(p))
}

// vmServe is the host end of every guest-initiated port: one unix socket each,
// relayed to the host service. This is what socat did per port on Linux, and
// what macOS needs done in-process because it has no socat.
func vmServe(args []string) {
	v := vmFlagSet("serve")
	v.parse(args)
	b, p := backend(), v.paths()
	lns, err := vmm.Listen(b, v.spec(p), p)
	if err != nil {
		fmt.Fprintln(os.Stderr, "cave-vm:", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "cave-vm: vsock plane up (%d relays)\n", len(lns))
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	<-sig
	for _, l := range lns {
		l.Close()
	}
}

func vmStop(args []string) {
	v := vmFlagSet("stop")
	v.parse(args)
	b, p := backend(), v.paths()
	err := vmm.Stop(b, v.spec(p), p, 240*time.Second)
	stopPlane(p)
	if err != nil {
		fmt.Fprintln(os.Stderr, "cave-vm:", err)
		os.Exit(1)
	}
	fmt.Fprintln(os.Stderr, "cave-vm: vm stopped")
}

// vmRunning is the `is_running` check, exit status and all, so a shell caller
// gets the same answer this package would give itself.
func vmRunning(args []string) {
	v := vmFlagSet("running")
	v.parse(args)
	p := v.paths()
	pid, ok := vmm.Running(backend(), v.spec(p), p)
	if !ok {
		os.Exit(1)
	}
	// A VM held by a supervisor in another namespace is running, and its pid
	// means nothing here. Print nothing rather than 0, which a shell caller
	// would go on to use as a pid.
	if pid > 0 {
		fmt.Println(pid)
	}
}

// vmSocket prints where a port's unix socket lives, for the services that bind
// their own rather than being relayed to: the gpg signing broker is a
// unix-socket server, so there is no TCP address to forward to.
func vmSocket(args []string) {
	v := vmFlagSet("socket")
	v.parse(args)
	if v.fs.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: cave-vm socket --state DIR <port>")
		os.Exit(2)
	}
	num := mustPort(v.fs.Arg(0))
	b, p := backend(), v.paths()
	for _, port := range vmm.Ports("", "") {
		if port.Num == num {
			fmt.Println(b.Socket(p, port))
			return
		}
	}
	fmt.Fprintf(os.Stderr, "cave-vm: port %d is not in the service plane\n", num)
	os.Exit(1)
}

// vmVsockConnect is an ssh ProxyCommand: it makes the guest's port look like a
// plain stdio pipe. Replaces scripts/vsock-connect.py.
func vmVsockConnect(args []string) {
	v := vmFlagSet("vsock-connect")
	v.parse(args)
	port := uint32(22)
	if v.fs.NArg() > 0 {
		port = mustPort(v.fs.Arg(0))
	}
	conn, err := backend().Dial(v.paths(), port)
	if err != nil {
		fmt.Fprintln(os.Stderr, "cave-vm:", err)
		os.Exit(1)
	}
	defer conn.Close()
	go func() {
		io.Copy(conn, os.Stdin)
		// ssh signals it is done sending by closing stdin; propagate that
		// rather than dropping the whole connection, which the guest would
		// see as a session that vanished mid-command.
		if cw, ok := conn.(interface{ CloseWrite() error }); ok {
			cw.CloseWrite()
		}
	}()
	io.Copy(os.Stdout, conn)
}

func mustPort(s string) uint32 {
	n, err := strconv.ParseUint(s, 10, 32)
	if err != nil {
		fmt.Fprintf(os.Stderr, "cave-vm: bad port %q\n", s)
		os.Exit(2)
	}
	return uint32(n)
}
