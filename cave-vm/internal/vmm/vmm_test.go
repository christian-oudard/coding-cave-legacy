package vmm

import (
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func testPaths(t *testing.T) Paths {
	t.Helper()
	dir := t.TempDir()
	// Not NewPaths: it deliberately puts sockets in /tmp to stay under the
	// 108-byte sun_path limit, and a test should not litter there.
	return Paths{State: dir, Sock: filepath.Join(dir, "s")}
}

func argsString(a []string) string { return strings.Join(a, " ") }

func spec(p Paths) Spec {
	return Spec{
		Kernel:   "/boot/kernel",
		Initrd:   "/boot/initrd",
		Init:     "/nix/store/abc-system/init",
		Disks:    []Disk{{Path: "/s/guest.img"}, {Path: "/s/seed.raw", ReadOnly: true}},
		MemoryMB: 4096,
		CPUs:     4,
		CID:      3,
		Console:  filepath.Join(p.State, "console.log"),
		Ports:    Ports(Services{Crinj: "127.0.0.1:10255", Git: "127.0.0.1:9418"}),
	}
}

// The port table is fixed at boot because vfkit needs one device per port at
// launch. If a port is ever added, it is added here, not allocated at runtime.
func TestPortsAreFixedAndDirected(t *testing.T) {
	ports := Ports(Services{Crinj: "crinj:1", Git: "git:2"})
	want := map[uint32]Dir{
		22: FromHost, 7001: FromHost,
		7100: FromGuest, 7200: FromGuest, 7300: FromGuest, 7400: FromGuest,
	}
	if len(ports) != len(want) {
		t.Fatalf("port count = %d, want %d", len(ports), len(want))
	}
	for _, p := range ports {
		dir, ok := want[p.Num]
		if !ok {
			t.Errorf("unexpected port %d", p.Num)
			continue
		}
		if p.Dir != dir {
			t.Errorf("port %d direction = %v, want %v", p.Num, p.Dir, dir)
		}
	}
	// The gpg broker binds its own socket, so it has no relay target; a
	// relay for it would race the broker for the same path.
	for _, p := range ports {
		if p.Num == 7400 && p.Relay != "" {
			t.Errorf("port 7400 should have no relay, got %q", p.Relay)
		}
	}
}

// Under vfkit the port table is the launch argv, so which ports exist must not
// depend on which services happen to be configured. The launcher builds the
// table with nothing wired up and the plane process builds it with everything;
// if those disagreed, the guest would dial a device that was never created.
func TestThePortTableDoesNotDependOnWhatIsWired(t *testing.T) {
	bare := Ports(Services{})
	wired := Ports(Services{Crinj: "c:1", Git: "g:2", Nix: func(net.Listener) {}})
	if len(bare) != len(wired) {
		t.Fatalf("port count %d unwired vs %d wired", len(bare), len(wired))
	}
	for i := range bare {
		if bare[i].Num != wired[i].Num || bare[i].Dir != wired[i].Dir {
			t.Errorf("port %d differs: %v vs %v", i, bare[i], wired[i])
		}
	}
}

// The nix binary cache is answered in this process rather than forwarded to a
// TCP listener, which is what keeps the host's store reachable from the guest
// and from nothing else on the host.
func TestListenServesAPortInProcess(t *testing.T) {
	p := testPaths(t)
	s := spec(p)
	served := make(chan net.Listener, 1)
	s.Ports = []Port{{Num: 7100, Dir: FromGuest, Name: "nix",
		Serve: func(ln net.Listener) { served <- ln }}}
	lns, err := Listen(&chv{}, s, p)
	if err != nil {
		t.Fatal(err)
	}
	defer lns[0].Close()
	if len(lns) != 1 {
		t.Fatalf("listened on %d sockets, want 1", len(lns))
	}
	select {
	case ln := <-served:
		if ln.Addr().String() != p.Vsock()+"_7100" {
			t.Errorf("served %s, want %s", ln.Addr(), p.Vsock()+"_7100")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the in-process server was never handed its listener")
	}
}

func TestCHVArgs(t *testing.T) {
	p := testPaths(t)
	got := argsString((&chv{}).Args(spec(p), p))
	for _, want := range []string{
		"--cmdline console=ttyS0 root=/dev/vda rw init=/nix/store/abc-system/init",
		"path=/s/guest.img,image_type=raw path=/s/seed.raw,image_type=raw,readonly=on",
		"--vsock cid=3,socket=" + p.Vsock(),
		"--api-socket path=" + p.API(),
		"--serial file=" + filepath.Join(p.State, "console.log"),
	} {
		if !strings.Contains(got, want) {
			t.Errorf("chv args missing %q\ngot: %s", want, got)
		}
	}
	// One vsock socket for the whole plane, not one per port.
	if n := strings.Count(got, "--vsock"); n != 1 {
		t.Errorf("chv should pass exactly one --vsock, got %d", n)
	}
}

func TestVfkitArgs(t *testing.T) {
	p := testPaths(t)
	got := argsString((&vfkit{}).Args(spec(p), p))
	for _, want := range []string{
		`--bootloader linux,kernel=/boot/kernel,initrd=/boot/initrd,cmdline="console=hvc0 root=/dev/vda rw init=/nix/store/abc-system/init"`,
		"--device virtio-blk,path=/s/guest.img",
		"--device virtio-blk,path=/s/seed.raw",
		"--restful-uri unix://" + p.API(),
		"--device virtio-serial,logFilePath=" + filepath.Join(p.State, "console.log"),
	} {
		if !strings.Contains(got, want) {
			t.Errorf("vfkit args missing %q\ngot: %s", want, got)
		}
	}
	// The direction flag is the whole delta between the two backends, and
	// getting it backwards fails silently: the socket exists either way and
	// nothing ever connects.
	if !strings.Contains(got, fmt.Sprintf("virtio-vsock,port=22,socketURL=%s_22,connect", p.Vsock())) {
		t.Errorf("host-initiated port 22 needs ,connect\ngot: %s", got)
	}
	for _, guestInit := range []uint32{7200, 7300, 7400} {
		dev := fmt.Sprintf("virtio-vsock,port=%d,socketURL=%s_%d", guestInit, p.Vsock(), guestInit)
		if !strings.Contains(got, "--device "+dev+" ") && !strings.HasSuffix(got, dev) {
			t.Errorf("guest-initiated port %d must be passed without ,connect\ngot: %s", guestInit, got)
		}
	}
}

// Both backends agree on where a guest-initiated socket lives, so the gpg
// broker and the relays need no per-platform knowledge. They differ only on
// host-initiated ports, where CHV multiplexes onto one socket.
func TestSocketPaths(t *testing.T) {
	p := testPaths(t)
	ssh := Port{Num: 22, Dir: FromHost}
	git := Port{Num: 7300, Dir: FromGuest}

	if got, want := (&chv{}).Socket(p, ssh), p.Vsock(); got != want {
		t.Errorf("chv host-initiated socket = %q, want %q", got, want)
	}
	if got, want := (&vfkit{}).Socket(p, ssh), p.Vsock()+"_22"; got != want {
		t.Errorf("vfkit host-initiated socket = %q, want %q", got, want)
	}
	for _, b := range []Backend{&chv{}, &vfkit{}} {
		if got, want := b.Socket(p, git), p.Vsock()+"_7300"; got != want {
			t.Errorf("%s guest-initiated socket = %q, want %q", b.Name(), got, want)
		}
	}
}

func TestCHVDialHandshake(t *testing.T) {
	p := testPaths(t)
	ln, err := net.Listen("unix", p.Vsock())
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()

	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		defer c.Close()
		buf := make([]byte, 64)
		n, _ := c.Read(buf)
		if string(buf[:n]) != "CONNECT 22\n" {
			c.Write([]byte("ERR\n"))
			return
		}
		c.Write([]byte("OK 3\nbanner"))
	}()

	conn, err := (&chv{}).Dial(p, 22)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	// The handshake must consume the OK line and not a byte more: an
	// over-read here eats the start of the guest's traffic, which shows up
	// as ssh hanging with no error anywhere.
	got, err := io.ReadAll(conn)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "banner" {
		t.Errorf("after handshake read %q, want %q", got, "banner")
	}
}

func TestCHVDialRejectsBadHandshake(t *testing.T) {
	p := testPaths(t)
	ln, err := net.Listen("unix", p.Vsock())
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		c.Write([]byte("ERR no such port\n"))
		c.Close()
	}()
	if _, err := (&chv{}).Dial(p, 7999); err == nil {
		t.Fatal("expected an error for a refused port")
	}
}

// The relay replaces a socat process per port. Half-close is the part socat got
// right for free: git's client stops sending and keeps reading, so a relay that
// tears down both directions at once truncates the fetch.
func TestRelayForwardsAndHalfCloses(t *testing.T) {
	backend, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer backend.Close()
	go func() {
		c, err := backend.Accept()
		if err != nil {
			return
		}
		defer c.Close()
		body, _ := io.ReadAll(c) // only returns once the client half-closes
		c.Write([]byte("got:" + string(body)))
	}()

	p := testPaths(t)
	s := spec(p)
	s.Ports = []Port{{Num: 7300, Dir: FromGuest, Name: "git", Relay: backend.Addr().String()}}
	lns, err := Listen(&chv{}, s, p)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		for _, l := range lns {
			l.Close()
		}
	}()

	conn, err := net.Dial("unix", p.Vsock()+"_7300")
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Write([]byte("want")); err != nil {
		t.Fatal(err)
	}
	conn.(*net.UnixConn).CloseWrite()
	got, err := io.ReadAll(conn)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "got:want" {
		t.Errorf("relayed %q, want %q", got, "got:want")
	}
}

// A port with no relay target must be left alone, or the launcher would take
// the socket the gpg broker is about to bind.
func TestListenSkipsUnrelayedPorts(t *testing.T) {
	p := testPaths(t)
	s := spec(p)
	s.Ports = []Port{{Num: 7400, Dir: FromGuest, Name: "gpg"}}
	lns, err := Listen(&chv{}, s, p)
	if err != nil {
		t.Fatal(err)
	}
	if len(lns) != 0 {
		t.Fatalf("listened on %d sockets, want 0", len(lns))
	}
	if _, err := os.Stat(p.Vsock() + "_7400"); err == nil {
		t.Error("gpg socket was created by the launcher")
	}
}

// Launching must not disturb a socket someone else already bound. The gpg
// broker is started before the VM on purpose, so that the guest cannot boot
// into a signing channel that is not there yet.
func TestStartClearsOnlyItsOwnSockets(t *testing.T) {
	for _, b := range []Backend{&chv{}, &vfkit{}} {
		t.Run(b.Name(), func(t *testing.T) {
			p := testPaths(t)
			s := spec(p)
			var gpg, ssh Port
			for _, port := range s.Ports {
				switch port.Num {
				case 7400:
					gpg = port
				case 22:
					ssh = port
				}
			}
			// A live listener, as the gpg broker would have.
			ln, err := net.Listen("unix", b.Socket(p, gpg))
			if err != nil {
				t.Fatal(err)
			}
			defer ln.Close()
			// A stale file, as a previous run would have left.
			if err := os.WriteFile(b.Socket(p, ssh), nil, 0o644); err != nil {
				t.Fatal(err)
			}

			clearOwnedSockets(b, s, p)

			if _, err := os.Stat(b.Socket(p, gpg)); err != nil {
				t.Errorf("the gpg broker's socket was removed: %v", err)
			}
			if _, err := os.Stat(b.Socket(p, ssh)); err == nil {
				t.Error("a stale host-initiated socket survived; the VMM will fail to bind it")
			}
		})
	}
}

// One cave VM per machine, enforced with a filesystem lock rather than a
// pidfile, because caves share a home and pids do not cross namespaces.
func TestLockRefusesASecondSupervisor(t *testing.T) {
	p := testPaths(t)
	first, err := Lock(p)
	if err != nil {
		t.Fatal(err)
	}
	if !Locked(p) {
		t.Error("a held VM does not report as locked")
	}
	if _, err := Lock(p); err == nil {
		t.Fatal("a second supervisor was allowed to take the same VM")
	}
	// Releasing has to actually release, or a crashed supervisor would wedge
	// the VM until someone deleted a file by hand.
	first.Close()
	if Locked(p) {
		t.Error("the lock outlived the process holding it")
	}
	second, err := Lock(p)
	if err != nil {
		t.Fatalf("could not retake a released VM: %v", err)
	}
	second.Close()
}

// The lock has to survive the process that took it, or it would be released
// the moment `start` returns and the next start would sail past it. flock
// belongs to the open file description, so a child inherits the same lock.
func TestLockIsHeldByTheChildNotTheStarter(t *testing.T) {
	p := testPaths(t)
	lock, err := Lock(p)
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("sleep", "30")
	cmd.ExtraFiles = []*os.File{lock}
	pid, err := StartDetached(cmd)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Kill(pid, syscall.SIGKILL)

	// The starter drops its copy, exactly as Start does when it returns.
	lock.Close()
	if !Locked(p) {
		t.Fatal("the lock was released when the starter closed its copy")
	}
	syscall.Kill(pid, syscall.SIGKILL)
	deadline := time.Now().Add(5 * time.Second)
	for Locked(p) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if Locked(p) {
		t.Error("the lock was not released when the child died")
	}
}

// A VM held by a supervisor in another namespace reads as running with no pid.
// Stop must refuse it: every kill would be kill(0), which signals the caller's
// own process group, and the cleanup would delete the live VM's sockets.
func TestStopRefusesAVMItDoesNotOwn(t *testing.T) {
	p := testPaths(t)
	s := spec(p)
	lock, err := Lock(p)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	// A pidfile naming something unsignallable, as a foreign namespace's
	// would be.
	if err := os.WriteFile(p.PidFile(), []byte("999999999"), 0o644); err != nil {
		t.Fatal(err)
	}
	sock := (&chv{}).Socket(p, Port{Num: 7300, Dir: FromGuest})
	if err := os.WriteFile(sock, nil, 0o644); err != nil {
		t.Fatal(err)
	}

	pid, ok := Running(&chv{}, s, p)
	if !ok || pid != 0 {
		t.Fatalf("Running = (%d, %v), want (0, true) for a foreign VM", pid, ok)
	}
	if err := Stop(&chv{}, s, p, time.Second); err == nil {
		t.Error("Stop acted on a VM belonging to another supervisor")
	}
	if _, err := os.Stat(sock); err != nil {
		t.Error("Stop deleted a live VM's socket")
	}
}

// deadBackend is a VMM that exits the moment it is launched, which is what a
// bad argv or an unavailable device produces in practice.
type deadBackend struct{ chv }

func (*deadBackend) Binary() string            { return "false" }
func (*deadBackend) Check() error              { return nil }
func (*deadBackend) Args(Spec, Paths) []string { return nil }

// Start holds the lock while it runs, and flock conflicts between open file
// descriptions rather than processes, so a liveness check that probes the lock
// sees Start's own and calls a launch that already failed a success. The
// failure then surfaces much later, as a guest that never finishes booting.
func TestStartReportsAVMMThatDiesImmediately(t *testing.T) {
	p := testPaths(t)
	s := spec(p)
	disk := filepath.Join(p.State, "guest.raw")
	if err := os.WriteFile(disk, make([]byte, 4096), 0o644); err != nil {
		t.Fatal(err)
	}
	s.Disks = []Disk{{Path: disk}}
	s.Console = filepath.Join(p.State, "console.log")

	_, err := Start(&deadBackend{}, s, p)
	if err == nil {
		t.Fatal("Start reported success for a VMM that exited immediately")
	}
	if !strings.Contains(err.Error(), "exited immediately") {
		t.Errorf("unhelpful error for a dead VMM: %v", err)
	}
	// And it must not leave the VM looking alive to the next caller.
	if _, ok := Running(&deadBackend{}, s, p); ok {
		t.Error("a VM that never started reports as running")
	}
}

// Cloud Hypervisor locks the guest disk with fcntl(F_SETLK), and on Linux
// flock and POSIX record locks are invisible to each other. A probe using the
// wrong one succeeds against a disk a live VM is holding, so this check would
// pass while doing nothing. The lock has to be taken by another process:
// POSIX record locks are per-process, so a lock taken here would not conflict
// with itself.
func TestDiskInUseSeesAPosixRecordLock(t *testing.T) {
	disk := filepath.Join(t.TempDir(), "guest.raw")
	if err := os.WriteFile(disk, make([]byte, 4096), 0o644); err != nil {
		t.Fatal(err)
	}
	if diskInUse(disk) {
		t.Fatal("an unused disk reported as in use")
	}

	if os.Getenv("VMM_TEST_HOLD_DISK") != "" {
		holdDiskForever(os.Getenv("VMM_TEST_HOLD_DISK"))
		return
	}
	helper := exec.Command(os.Args[0], "-test.run=TestDiskInUseSeesAPosixRecordLock")
	helper.Env = append(os.Environ(), "VMM_TEST_HOLD_DISK="+disk)
	out, err := helper.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := helper.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { helper.Process.Kill(); helper.Wait() }()

	// The helper says "held" once the lock is actually taken.
	buf := make([]byte, 5)
	if _, err := io.ReadFull(out, buf); err != nil {
		t.Fatalf("helper never reported holding the lock: %v", err)
	}
	if !diskInUse(disk) {
		t.Error("a disk locked by another process reported as free")
	}

	helper.Process.Kill()
	helper.Wait()
	deadline := time.Now().Add(5 * time.Second)
	for diskInUse(disk) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if diskInUse(disk) {
		t.Error("the disk stayed locked after the holder died")
	}
}

func holdDiskForever(path string) {
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return
	}
	lk := syscall.Flock_t{Type: syscall.F_WRLCK, Whence: io.SeekStart}
	if syscall.FcntlFlock(f.Fd(), syscall.F_SETLK, &lk) != nil {
		return
	}
	os.Stdout.WriteString("held\n")
	select {}
}

// os.Process.Release sets Pid to -1, so a pid read after it is not just wrong
// but actively dangerous: -1 is kill(2)'s "every process you can signal". A
// pidfile holding it turns an ordinary shutdown into a self-inflicted logout.
func TestStartDetachedReturnsTheRealPid(t *testing.T) {
	cmd := exec.Command("sleep", "30")
	pid, err := StartDetached(cmd)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Kill(pid, syscall.SIGKILL)

	if pid <= 0 {
		t.Fatalf("StartDetached returned pid %d", pid)
	}
	if _, name := procStatus(pid); !strings.HasPrefix("sleep", name) {
		t.Errorf("pid %d is %q, not the process we started", pid, name)
	}
	// And it must actually be signallable, which is the whole point.
	if err := syscall.Kill(pid, syscall.SIGTERM); err != nil {
		t.Errorf("cannot signal the returned pid: %v", err)
	}
}

// A dead VMM whose parent has not reaped it is still a pid that answers
// kill(0), and it keeps its name. Treating that as a running VM sends the
// caller to a vsock socket with nothing behind it, which is the confusing
// failure the pid check exists to prevent in the first place.
func TestRunningIgnoresZombies(t *testing.T) {
	cmd := exec.Command("sh", "-c", "exit 0")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	pid := cmd.Process.Pid
	// Deliberately not Wait()ed, so it stays defunct for the test.
	defer cmd.Wait()

	deadline := time.Now().Add(5 * time.Second)
	for {
		if state, _ := procStatus(pid); strings.HasPrefix(state, "Z") {
			break
		}
		if time.Now().After(deadline) {
			t.Skip("could not produce a zombie on this host")
		}
		time.Sleep(10 * time.Millisecond)
	}

	p := testPaths(t)
	if err := os.WriteFile(p.PidFile(), []byte(strconv.Itoa(pid)), 0o644); err != nil {
		t.Fatal(err)
	}
	if syscall.Kill(pid, 0) != nil {
		t.Fatal("precondition: a zombie should still answer kill(0)")
	}
	// An empty Spec, so nothing but the pid check can answer: this is about
	// the zombie, not about the lock or the disk.
	if _, ok := Running(&zombieBackend{}, Spec{}, p); ok {
		t.Error("a defunct process was reported as a running VM")
	}
}

// zombieBackend claims the name the test's dead process actually has, so the
// test cannot pass merely because the names failed to match.
type zombieBackend struct{ chv }

func (*zombieBackend) Binary() string { return "sh" }

// Linux reports comm truncated to 15 characters, macOS reports the full path.
func TestIsBinary(t *testing.T) {
	cases := []struct {
		name, bin string
		want      bool
	}{
		{"cloud-hyperviso", "cloud-hypervisor", true},
		{"cloud-hypervisor", "cloud-hypervisor", true},
		{"/Users/m1/vfkit/bin/vfkit", "/Users/m1/vfkit/bin/vfkit", true},
		{"vfkit", "/Users/m1/vfkit/bin/vfkit", true},
		{"sshd", "cloud-hypervisor", false},
		{"", "cloud-hypervisor", false},
	}
	for _, c := range cases {
		if got := isBinary(c.name, c.bin); got != c.want {
			t.Errorf("isBinary(%q, %q) = %v, want %v", c.name, c.bin, got, c.want)
		}
	}
}

func TestConsoleHalted(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "console.log")

	os.WriteFile(path, []byte("[   1.0] booting\n"), 0o644)
	if consoleHalted(path) {
		t.Error("a booting console reported as halted")
	}
	// Real consoles are full of escape sequences, and the marker arrives
	// wrapped in them.
	os.WriteFile(path, []byte("\x1b[0;32m[  OK  ]\x1b[0m Reached target System Power Off\n"), 0o644)
	if !consoleHalted(path) {
		t.Error("power-off marker not seen through ANSI escapes")
	}
	if consoleHalted(filepath.Join(dir, "absent.log")) {
		t.Error("a missing console reported as halted")
	}
}

func TestRunScriptIsReproducible(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "run-vm.sh")
	argv := []string{"cloud-hypervisor", "--cmdline", "console=ttyS0 init=/nix/store/abc/init"}
	if err := writeRunScript(path, argv); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)
	// `vmcav status` recovers the booted system by matching init= in here,
	// so the path has to survive quoting intact.
	if !strings.Contains(got, "init=/nix/store/abc/init") {
		t.Errorf("run script lost the init path:\n%s", got)
	}
	if !strings.Contains(got, "'console=ttyS0 init=/nix/store/abc/init'") {
		t.Errorf("argument with spaces was not quoted:\n%s", got)
	}
	// A flag and its value on separate lines is harder to read than the argv
	// it came from, which defeats the point of writing the file at all.
	if !strings.Contains(got, "--cmdline 'console=ttyS0 init=/nix/store/abc/init'") {
		t.Errorf("flag was split from its value:\n%s", got)
	}
	if fi, _ := os.Stat(path); fi.Mode().Perm()&0o100 == 0 {
		t.Error("run script is not executable")
	}
}

// The sockets sit in a directory nobody else may enter.
//
// They used to sit directly in /tmp under a predictable name, taking whatever
// permissions their creator chose -- the VMM's API socket, through which a VM
// can be controlled, and the vsock bridges, through which another local user
// could reach crinj's egress or push through the git daemon with the agent's
// injected identity. The mode goes on the directory because the sockets are
// created by the VMM, and the per-port ones later still, so there is no single
// moment when they all exist to be chmodded.
func TestSocketDirectoryIsOwnerOnly(t *testing.T) {
	p := Paths{Sock: filepath.Join(t.TempDir(), "sockets", "1234")}
	if err := p.EnsureDir(); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(p.Dir())
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o700 {
		t.Fatalf("socket directory is %v, want 0700", fi.Mode().Perm())
	}
}

// A directory that already exists is tightened rather than trusted, since it
// is on a path anybody can create first.
func TestSocketDirectoryIsTightenedIfItExists(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "sockets")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	p := Paths{Sock: filepath.Join(dir, "1234")}
	if err := p.EnsureDir(); err != nil {
		t.Fatal(err)
	}
	fi, _ := os.Stat(dir)
	if fi.Mode().Perm() != 0o700 {
		t.Fatalf("an existing directory was left at %v", fi.Mode().Perm())
	}
}

// And the socket path is inside it, which is the whole point: a Paths whose
// socket sat beside the directory would be covered by nothing.
func TestSocketPathIsInsideTheDirectory(t *testing.T) {
	p := NewPaths(t.TempDir())
	if filepath.Dir(p.Sock) != p.Dir() {
		t.Fatalf("socket %q is not in %q", p.Sock, p.Dir())
	}
	for _, s := range []string{p.API(), p.Vsock()} {
		if filepath.Dir(s) != p.Dir() {
			t.Fatalf("%q is not in %q", s, p.Dir())
		}
	}
}
