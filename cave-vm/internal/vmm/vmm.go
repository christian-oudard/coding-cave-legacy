// Package vmm launches the cave VM and owns the vsock service plane.
//
// These are one thing, not two. The design doc's layout splits them into
// internal/chv and internal/plane, which was written when Cloud Hypervisor was
// the only backend: CHV multiplexes every host-initiated port over one socket
// selected by a "CONNECT <port>" handshake, so the plane genuinely could dial a
// port the VMM knew nothing about. vfkit takes one --device virtio-vsock per
// port, with the port number, the socket path and the direction all fixed in
// the launch argv. A separate plane package would therefore have to be handed
// its entire configuration by the VMM package, and the VMM package could not
// build its argv without the port map. That is one package wearing two names.
//
// The constraint this leaves, which binds on Linux too even though CHV would
// permit more: no feature may allocate a host-initiated vsock port after boot.
// Such a feature would work on Linux and be discovered unportable much later.
package vmm

import (
	"fmt"
	"hash/crc32"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Dir is which side opens a connection on a vsock port. It is not decoration:
// it decides who creates the unix socket, and under vfkit it is a launch-time
// argument that cannot be changed while the VM runs.
type Dir int

const (
	// FromGuest: the guest dials out, the host listens on the unix socket.
	FromGuest Dir = iota
	// FromHost: the host dials in, the guest is listening on the vsock port.
	FromHost
)

// Port is one entry in the service plane. Relay is the host TCP address to
// forward to, and is only meaningful FromGuest. An empty Relay means something
// outside this process binds the socket itself, which is how the gpg signing
// broker works: it is a unix-socket server rather than a TCP service, so there
// is nothing to relay to.
type Port struct {
	Num   uint32
	Dir   Dir
	Name  string
	Relay string
}

// Ports is the whole service plane, fixed at boot. 7100 (nix binary cache) and
// 7001 (control RPC) are in the design and absent here because neither exists
// yet; adding one is a new entry plus a reboot, not a runtime allocation.
func Ports(crinj, git string) []Port {
	return []Port{
		{Num: 22, Dir: FromHost, Name: "ssh"},
		{Num: 7200, Dir: FromGuest, Name: "crinj", Relay: crinj},
		{Num: 7300, Dir: FromGuest, Name: "git", Relay: git},
		{Num: 7400, Dir: FromGuest, Name: "gpg"},
	}
}

type Disk struct {
	Path     string
	ReadOnly bool
}

// Spec is the VM to launch. The kernel command line is not a field: the console
// device differs per backend (ttyS0 under CHV's 8250 serial, hvc0 under
// vfkit's virtio-serial), so each backend composes its own and a caller that
// supplied one would be supplying a portability bug.
type Spec struct {
	Kernel   string
	Initrd   string
	Init     string // init= target, the NixOS toplevel's init
	Disks    []Disk // Disks[0] is root; the guest boots root=/dev/vda
	MemoryMB int
	CPUs     int
	CID      uint32
	Console  string // guest console log
	Ports    []Port
}

// Paths locates the runtime files. Sockets do not live under the state dir:
// AF_UNIX sun_path is 108 bytes and a state dir nested a few levels deep spends
// most of that before the socket is named, so they hang off a short /tmp base
// keyed by a hash of the state dir. Everything that needs a socket path derives
// it from here, so the launcher and the gpg broker cannot disagree about where
// a socket is.
type Paths struct {
	State string
	Sock  string
}

func NewPaths(state string) Paths {
	abs, err := filepath.Abs(state)
	if err != nil {
		abs = state
	}
	sum := crc32.ChecksumIEEE([]byte(abs))
	return Paths{
		State: abs,
		Sock:  fmt.Sprintf("/tmp/vmcav-%d-%d", os.Getuid(), sum),
	}
}

func (p Paths) PidFile() string   { return filepath.Join(p.State, "vm.pid") }
func (p Paths) LockFile() string  { return filepath.Join(p.State, "vm.lock") }
func (p Paths) RunScript() string { return filepath.Join(p.State, "run-vm.sh") }
func (p Paths) API() string       { return p.Sock + ".api" }
func (p Paths) Vsock() string     { return p.Sock + ".vsock" }

// Backend is the per-VMM delta, and it is deliberately small: everything below
// this interface in the file is shared by both platforms.
type Backend interface {
	Name() string
	// Binary is the VMM executable, used both to launch it and to confirm a
	// live pid is still ours rather than a recycled pid number.
	Binary() string
	// Check reports whether this host can run the backend at all.
	Check() error
	Args(Spec, Paths) []string
	// Shutdown asks the guest to power down. It returns without waiting;
	// Stop owns the waiting, because how long a clean shutdown takes is a
	// property of the guest, not of the VMM.
	Shutdown(Paths) error
	// Socket is where a port's unix socket lives. Under CHV every
	// host-initiated port shares one socket, so this is not injective there.
	Socket(Paths, Port) string
	// Dial opens a connection to a port the guest is listening on.
	Dial(Paths, uint32) (net.Conn, error)
}

// Pick returns the backend for this host. The guest cannot tell the difference:
// it speaks plain AF_VSOCK either way.
func Pick() (Backend, error) {
	switch runtime.GOOS {
	case "linux":
		return &chv{}, nil
	case "darwin":
		return &vfkit{}, nil
	}
	return nil, fmt.Errorf("no VMM backend for %s", runtime.GOOS)
}

// One cave VM per machine. The state dir is the VM's identity, and it is
// reachable from more than one place: caves share a home, so the same
// directory is visible to the host and to every cave on it.
//
// A pidfile cannot enforce this. Pids are per namespace, so a supervisor in a
// cave asks kill(pid, 0) about a host pid, is told nothing is there, and
// starts a second VMM on a disk that is already open. The first symptom is the
// VMM's own disk lock, by which point the pidfile has already been overwritten
// and the running VM can no longer be stopped by the supervisor that owns it.
//
// flock lives in the filesystem rather than in a process table, so it answers
// the question that is actually being asked: is this state dir in use, by
// anyone, anywhere on this machine.
func Lock(p Paths) (*os.File, error) {
	if err := os.MkdirAll(p.State, 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(p.LockFile(), os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, fmt.Errorf("another supervisor is already running this VM (%s).\n"+
			"  One cave VM per machine: the state dir is shared, so this may be a\n"+
			"  cave other than the one you are in, or the host itself", p.State)
	}
	return f, nil
}

// diskInUse reports whether some process already has the guest disk open for
// writing. This is a second, independent check, and it is not redundant: the
// lock above is only seen by supervisors that take it, so it cannot see a VM
// started by an older vmcav, or by anything that is not this binary.
//
// It has to be a POSIX record lock rather than flock. Cloud Hypervisor takes
// its disk lock with fcntl(F_SETLK), and on Linux the two kinds do not see
// each other at all: an flock probe of a disk CHV is holding succeeds, which
// would make this check quietly useless. Verified by probing a live VM's disk
// both ways.
//
// The probe takes the lock and drops it immediately, so it never contends with
// the VMM that is about to start.
func diskInUse(path string) bool {
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return false
	}
	defer f.Close()
	lk := syscall.Flock_t{Type: syscall.F_WRLCK, Whence: io.SeekStart}
	if syscall.FcntlFlock(f.Fd(), syscall.F_SETLK, &lk) != nil {
		return true
	}
	lk.Type = syscall.F_UNLCK
	syscall.FcntlFlock(f.Fd(), syscall.F_SETLK, &lk)
	return false
}

// Locked reports whether anyone holds the VM, without taking it. Probing costs
// a lock and an unlock, which is racy by nature; it is for reporting, and Lock
// is what start relies on.
func Locked(p Paths) bool {
	f, err := os.OpenFile(p.LockFile(), os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return false
	}
	defer f.Close()
	if syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		return true
	}
	syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	return false
}

// Start launches the VM detached and returns once the VMM is up. It does not
// wait for the guest to boot: that is the caller's business, and the caller has
// better ways to ask (ssh, a health endpoint) than this package does.
//
// Detached is a requirement rather than a convenience. The supervisor must be
// killable and restartable with the VM and every agent session surviving, so
// the VMM gets its own session and the only thing tying the two together is the
// pidfile plus the control socket.
func Start(b Backend, s Spec, p Paths) (int, error) {
	if pid, ok := Running(b, s, p); ok {
		// Ours, already up: nothing to do. Someone else's is not a success,
		// and saying so with a pid of 0 would hand the caller a number that
		// every kill reads as its own process group.
		if pid == 0 {
			return 0, fmt.Errorf("this VM is already running under another supervisor")
		}
		return pid, nil
	}
	if err := b.Check(); err != nil {
		return 0, err
	}
	// Taken before anything is written, so a refused start changes nothing.
	lock, err := Lock(p)
	if err != nil {
		return 0, err
	}
	// The VMM inherits the lock and holds it for exactly as long as it runs.
	// flock belongs to the open file description, not to a process, so this
	// copy can close while the VMM's keeps the lock held, and the lock is
	// released by the kernel when the VMM exits however it exits.
	defer lock.Close()
	// Still before any side effect: the VMM would discover this itself and
	// die, but only after the pidfile had been overwritten, which is what
	// makes the VM that is actually running unstoppable by its own supervisor.
	if len(s.Disks) > 0 && diskInUse(s.Disks[0].Path) {
		return 0, fmt.Errorf("the guest disk is already open by a running VM (%s).\n"+
			"  One cave VM per machine, and this one was not started from here", s.Disks[0].Path)
	}
	clearOwnedSockets(b, s, p)

	argv := append([]string{b.Binary()}, b.Args(s, p)...)
	// The exact invocation on disk, which is what makes a failed boot
	// reproducible by hand. `vmcav status` also reads the booted toplevel
	// back out of it, so this is load-bearing rather than a courtesy.
	if err := writeRunScript(p.RunScript(), argv); err != nil {
		return 0, err
	}

	log, err := os.OpenFile(filepath.Join(p.State, b.Name()+".log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return 0, err
	}
	defer log.Close()

	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdout, cmd.Stderr = log, log
	cmd.ExtraFiles = []*os.File{lock}
	pid, err := StartDetached(cmd)
	if err != nil {
		return 0, fmt.Errorf("starting %s: %w", b.Name(), err)
	}
	if err := os.WriteFile(p.PidFile(), []byte(strconv.Itoa(pid)), 0o644); err != nil {
		return 0, err
	}
	// A VMM that fails on its argv dies in well under a second, and reporting
	// success for it would push the failure into whatever ran next.
	//
	// ourVMM, not Running: we are holding the lock, so Running would see it,
	// call that a live VM, and report a launch that already failed as a
	// success.
	time.Sleep(300 * time.Millisecond)
	if _, ok := ourVMM(b, p); !ok {
		return 0, fmt.Errorf("%s exited immediately; see %s and %s",
			b.Name(), filepath.Join(p.State, b.Name()+".log"), s.Console)
	}
	return pid, nil
}

// clearOwnedSockets removes only what the VMM itself binds, where a leftover
// file makes the launch fail. The guest-initiated sockets belong to whoever
// listens on them: Listen clears its own before binding, and the gpg broker
// binds its own before the VM starts, so removing those here would delete a
// live socket and leave the guest with a signing channel that goes nowhere.
func clearOwnedSockets(b Backend, s Spec, p Paths) {
	for _, port := range s.Ports {
		if port.Dir == FromHost {
			os.Remove(b.Socket(p, port))
		}
	}
	os.Remove(p.API())
}

// StartDetached runs cmd in a session of its own and returns its pid. Both the
// VMM and the vsock plane outlive the process that launched them, which is what
// makes the supervisor restartable.
//
// The pid is read before Release and not after: Release sets Process.Pid to -1
// to mark the handle spent, and -1 is the argument kill(2) reads as "every
// process you are allowed to signal".
func StartDetached(cmd *exec.Cmd) (int, error) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	pid := cmd.Process.Pid
	// Release, or the child becomes a zombie the moment this process exits
	// without reaping it.
	cmd.Process.Release()
	return pid, nil
}

// writeRunScript records the exact invocation, which is what makes a failed
// boot reproducible by hand. Flags are kept on the same line as their value,
// because a file where `--kernel` and its path are two lines apart is worse to
// read than the argv it came from.
func writeRunScript(path string, argv []string) error {
	var sb strings.Builder
	sb.WriteString("#!/bin/sh\n# Written by cave-vm; the exact VMM invocation.\nexec " + shellQuote(argv[0]))
	for i := 1; i < len(argv); i++ {
		line := shellQuote(argv[i])
		for strings.HasPrefix(argv[i], "-") && i+1 < len(argv) && !strings.HasPrefix(argv[i+1], "-") {
			i++
			line += " " + shellQuote(argv[i])
		}
		sb.WriteString(" \\\n  " + line)
	}
	sb.WriteString("\n")
	return os.WriteFile(path, []byte(sb.String()), 0o755)
}

func shellQuote(s string) string {
	if s != "" && !strings.ContainsAny(s, " \t\n\"'$`\\*?[]{}();&|<>#~") {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

// Running reports whether a VM is up and, when it is ours to act on, its pid.
// A pid of 0 with true means a VM is running under a supervisor this process
// cannot see: the lock is held, but the pidfile names a process in another
// namespace. Callers must not signal a pid of 0, which the kernel reads as the
// whole process group.
// Callers that already hold the lock must not use this: flock conflicts
// between open file descriptions rather than processes, so a second probe from
// inside Start sees Start's own lock and reports a VM that may have died.
func Running(b Backend, s Spec, p Paths) (int, bool) {
	if pid, ours := ourVMM(b, p); ours {
		return pid, true
	}
	// No process of ours, but either of these settles it. The lock catches
	// another cave-vm; the disk catches a VM this binary did not start, which
	// is what a status that only knew about the lock would call "stopped"
	// while a VM was plainly running.
	if Locked(p) {
		return 0, true
	}
	if len(s.Disks) > 0 && diskInUse(s.Disks[0].Path) {
		return 0, true
	}
	return 0, false
}

// ourVMM reports the pid only when this process could actually signal it. A
// live pid is not enough on its own: once the VMM exits the pidfile outlives it
// and the kernel eventually hands that number to something else, which then
// reads as a running VM until the first vsock connection fails in a confusing
// way. So confirm the process is still ours.
func ourVMM(b Backend, p Paths) (int, bool) {
	raw, err := os.ReadFile(p.PidFile())
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 {
		return 0, false
	}
	if syscall.Kill(pid, 0) != nil {
		return 0, false
	}
	state, name := procStatus(pid)
	// A zombie still answers kill(0) and still carries its old name: the pid
	// is occupied, but the VM behind it is gone. Reporting that as running
	// sends the caller off to a vsock socket nothing is listening on.
	if strings.HasPrefix(state, "Z") {
		return 0, false
	}
	return pid, isBinary(name, b.Binary())
}

// procStatus asks ps rather than reading /proc, which does not exist on macOS.
func procStatus(pid int) (state, name string) {
	out, err := exec.Command("ps", "-p", strconv.Itoa(pid), "-o", "state=", "-o", "comm=").Output()
	if err != nil {
		return "", ""
	}
	fields := strings.Fields(string(out))
	if len(fields) < 2 {
		return "", ""
	}
	return fields[0], fields[1]
}

// isBinary compares a process name against an executable path. Linux's comm is
// the basename truncated to 15 characters ("cloud-hyperviso"), macOS reports
// the full path, so compare basenames by prefix in that order.
func isBinary(name, bin string) bool {
	name, bin = filepath.Base(strings.TrimSpace(name)), filepath.Base(bin)
	return name != "" && strings.HasPrefix(bin, name)
}

// poweredOff matches the guest's own account of reaching power-off. Watching
// the console matters because the VMM process can outlive the guest halting;
// once these appear the disk is unmounted and killing the VMM is safe.
var poweredOff = regexp.MustCompile(`reboot: Power down|Reached target System Power Off`)
var ansi = regexp.MustCompile(`\x1b\[[0-9;?]*[a-zA-Z]`)

func consoleHalted(path string) bool {
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	if len(b) > 1<<16 {
		b = b[len(b)-(1<<16):]
	}
	return poweredOff.Match(ansi.ReplaceAll(b, nil))
}

// Stop powers the guest down and does not return until the VM is gone. The
// timeout is generous because the guest is unmounting a real filesystem, and
// the failure mode of being impatient here is a disk that needs fsck.
func Stop(b Backend, s Spec, p Paths, timeout time.Duration) error {
	pid, ok := Running(b, s, p)
	if !ok {
		cleanup(b, s, p)
		return nil
	}
	// Someone else's VM. Refusing is not caution, it is correctness twice
	// over: every kill below would be kill(0), which signals our own process
	// group rather than the VM, and cleanup would delete the sockets the live
	// VM is still serving.
	if pid == 0 {
		return fmt.Errorf("this VM is running under another supervisor and cannot be\n" +
			"  stopped from here; stop it where it was started")
	}
	if err := b.Shutdown(p); err != nil {
		fmt.Fprintf(os.Stderr, "cave-vm: power-down request failed: %v\n", err)
	}
	deadline := time.Now().Add(timeout)
	clean := false
	for time.Now().Before(deadline) {
		if syscall.Kill(pid, 0) != nil || consoleHalted(s.Console) {
			clean = true
			break
		}
		time.Sleep(time.Second)
	}
	if !clean {
		fmt.Fprintln(os.Stderr, "cave-vm: power-down did not complete; forcing")
	}
	if syscall.Kill(pid, 0) == nil {
		syscall.Kill(pid, syscall.SIGTERM)
		time.Sleep(2 * time.Second)
		syscall.Kill(pid, syscall.SIGKILL)
	}
	cleanup(b, s, p)
	if !clean {
		return fmt.Errorf("guest did not power down cleanly")
	}
	return nil
}

func cleanup(b Backend, s Spec, p Paths) {
	os.Remove(p.PidFile())
	os.Remove(p.API())
	for _, port := range s.Ports {
		os.Remove(b.Socket(p, port))
	}
	os.Remove(p.Vsock())
}

// Listen creates the host end of every guest-initiated port. The VMM dials
// these when the guest connects, so they have to exist before the guest tries.
// Under CHV this was a socat process per port; in-process is the same thing
// without the dependency, and macOS has no socat at all.
func Listen(b Backend, s Spec, p Paths) ([]net.Listener, error) {
	var lns []net.Listener
	for _, port := range s.Ports {
		if port.Dir != FromGuest || port.Relay == "" {
			continue
		}
		path := b.Socket(p, port)
		os.Remove(path)
		ln, err := net.Listen("unix", path)
		if err != nil {
			for _, l := range lns {
				l.Close()
			}
			return nil, fmt.Errorf("listening for %s on %s: %w", port.Name, path, err)
		}
		lns = append(lns, ln)
		go relay(ln, port.Relay)
	}
	return lns, nil
}

func relay(ln net.Listener, target string) {
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		go func() {
			defer c.Close()
			u, err := net.Dial("tcp", target)
			if err != nil {
				return
			}
			defer u.Close()
			done := make(chan struct{})
			go func() { pipe(u, c); close(done) }()
			pipe(c, u)
			<-done
		}()
	}
}

// pipe copies one direction and then half-closes, which is not fussiness: git's
// wire protocol has the client stop sending and keep reading, and a relay that
// tears the whole connection down at that point truncates the fetch.
func pipe(dst, src net.Conn) {
	io.Copy(dst, src)
	if cw, ok := dst.(interface{ CloseWrite() error }); ok {
		cw.CloseWrite()
	}
}
