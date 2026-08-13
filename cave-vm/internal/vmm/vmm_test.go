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
		Ports:    Ports("127.0.0.1:10255", "127.0.0.1:9418"),
	}
}

// The port table is fixed at boot because vfkit needs one device per port at
// launch. If a port is ever added, it is added here, not allocated at runtime.
func TestPortsAreFixedAndDirected(t *testing.T) {
	ports := Ports("crinj:1", "git:2")
	want := map[uint32]Dir{22: FromHost, 7200: FromGuest, 7300: FromGuest, 7400: FromGuest}
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
	if _, ok := Running(&zombieBackend{}, p); ok {
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
