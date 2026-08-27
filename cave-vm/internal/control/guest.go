package control

import (
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// System is the real guest, answering out of systemd, /proc and the clock.
// Everything it does is a shell-out or a syscall, which is why Guest is an
// interface: none of this runs in a test.
type System struct {
	// Sessions is where cave-session sockets live; the directory is the
	// roster, since a session's socket exists exactly while it is running.
	Sessions string
	// Unit names the systemd unit an agent's session runs under. A function
	// rather than a template string, because there is more than one template
	// and which one applies is a fact about the agent rather than about its
	// id. Injected for the same reason List is: resolving it means reading
	// the session's environment file, which is the daemon's business.
	Unit func(id string) string
	// List reports the running sessions, injected so the daemon can pass
	// agentd.List without this package importing it.
	List func(dir string) ([]string, error)
}

func (s System) Health() (Health, error) {
	system, err := os.Readlink("/run/current-system")
	if err != nil {
		return Health{}, err
	}
	var info syscall.Sysinfo_t
	if err := syscall.Sysinfo(&info); err != nil {
		return Health{}, err
	}
	return Health{
		System: system,
		Uptime: int(info.Uptime),
		Now:    time.Now().UnixNano(),
	}, nil
}

// Agents reads the socket directory rather than asking systemd, because a unit
// can be active while the agent behind it is gone; a socket exists only for as
// long as the session process holds it.
func (s System) Agents() ([]Agent, error) {
	lines, err := s.List(s.Sessions)
	if err != nil {
		return nil, err
	}
	out := make([]Agent, 0, len(lines))
	for _, line := range lines {
		f := strings.Fields(line)
		if len(f) == 0 {
			continue
		}
		a := Agent{ID: f[0], State: "running"}
		if len(f) > 1 {
			a.State = f[1]
			a.Info = strings.Join(f[2:], " ")
		}
		out = append(out, a)
	}
	return out, nil
}

// StopAgent stops the unit, which stops the agent: the session process and the
// unit are the same lifetime by design.
func (s System) StopAgent(id string) error {
	if err := validID(id); err != nil {
		return err
	}
	unit := s.Unit(id)
	out, err := exec.Command("systemctl", "stop", unit).CombinedOutput()
	if err != nil {
		return fmt.Errorf("stopping %s: %v: %s", unit, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// validID keeps a caller-supplied id out of a unit name. The host is the only
// client and would not send anything strange, which is exactly the assumption
// worth not making at the point where a string becomes an argument.
func validID(id string) error {
	if id == "" || len(id) > 32 {
		return fmt.Errorf("bad agent id %q", id)
	}
	for _, r := range id {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r == '-' || r == '_') {
			return fmt.Errorf("bad agent id %q", id)
		}
	}
	return nil
}

// SetTime steps the clock. A step rather than a slew: the guest is arriving
// from a host suspend with a clock that may be hours out, and there is nothing
// here for a slew to protect. The RTC is not touched, because the guest does
// not own one that survives anything.
func (s System) SetTime(t time.Time) error {
	tv := syscall.NsecToTimeval(t.UnixNano())
	// settimeofday, via the raw syscall: package syscall has no wrapper for
	// it on linux/amd64, and this is the one call in the daemon that needs one.
	if _, _, errno := syscall.Syscall(syscall.SYS_SETTIMEOFDAY,
		uintptr(unsafe.Pointer(&tv)), 0, 0); errno != 0 {
		return fmt.Errorf("settimeofday: %w", errno)
	}
	return nil
}

// ActivationListener returns the listener systemd passed in, which is how the
// control port gets bound before anything that depends on it runs. systemd
// binds AF_VSOCK itself with ListenStream=vsock::<port>, so nothing here has to
// know what a sockaddr_vm looks like, and the ordering that a socat bridge only
// hopes for is a guarantee: the socket exists from the moment the unit is
// enabled, whether or not the daemon behind it has started.
//
// Returns nil with no error when there is no activation, so a developer can
// still run the daemon by hand against a plain address.
func ActivationListener() (net.Listener, error) {
	if os.Getenv("LISTEN_PID") != strconv.Itoa(os.Getpid()) {
		return nil, nil
	}
	n, err := strconv.Atoi(os.Getenv("LISTEN_FDS"))
	if err != nil || n < 1 {
		return nil, nil
	}
	// fd 3 is the first passed socket, by protocol. Only one is ever passed
	// here, and a second would be a configuration error rather than something
	// to serve.
	if n > 1 {
		return nil, fmt.Errorf("systemd passed %d sockets, expected 1", n)
	}
	const firstFD = 3
	syscall.CloseOnExec(firstFD)
	// Not net.FileListener: it calls getsockname to work out what kind of
	// listener to build, and the net package knows only AF_INET, AF_INET6 and
	// AF_UNIX, so an AF_VSOCK socket fails there with "address family not
	// supported by protocol". systemd has already bound it correctly; all that
	// is missing is something to accept on it.
	return &fdListener{fd: firstFD}, nil
}

// fdListener accepts on a socket someone else bound, whatever its address
// family. It exists for AF_VSOCK, which the net package cannot represent.
type fdListener struct {
	fd int
}

func (l *fdListener) Accept() (net.Conn, error) {
	for {
		// The raw syscall, not syscall.Accept4: that wrapper converts the peer
		// address it accepted, and the conversion knows nothing about
		// AF_VSOCK, so it discards a perfectly good connection with
		// EAFNOSUPPORT. Passing NULL for the address asks the kernel not to
		// report one, which is what we would have thrown away anyway.
		//
		// SOCK_NONBLOCK so os.NewFile registers the connection with the
		// runtime poller: that is what makes reads, writes and the deadlines
		// http.Server sets behave like a socket rather than like a file.
		nfd, _, errno := syscall.Syscall6(syscall.SYS_ACCEPT4, uintptr(l.fd),
			0, 0, uintptr(syscall.SOCK_CLOEXEC|syscall.SOCK_NONBLOCK), 0, 0)
		if errno != 0 {
			// Transient by definition: an interrupted accept, or a caller that
			// went away between its connection landing and being accepted, is
			// not a reason to end the listener.
			if errno == syscall.EINTR || errno == syscall.ECONNABORTED {
				continue
			}
			return nil, fmt.Errorf("accept on vsock: %w", errno)
		}
		return &fdConn{File: os.NewFile(nfd, "vsock")}, nil
	}
}

// Close does not close the descriptor: systemd owns it, and the socket unit
// outlives this process by design, which is the whole point of activation.
func (l *fdListener) Close() error   { return nil }
func (l *fdListener) Addr() net.Addr { return vsockAddr{} }

// fdConn is a net.Conn over an accepted descriptor. os.File already provides
// reads, writes and deadlines; what is missing is the addressing, which
// nothing on this connection uses for anything but a log line.
type fdConn struct {
	*os.File
}

func (c *fdConn) LocalAddr() net.Addr  { return vsockAddr{} }
func (c *fdConn) RemoteAddr() net.Addr { return vsockAddr{} }

type vsockAddr struct{}

func (vsockAddr) Network() string { return "vsock" }
func (vsockAddr) String() string  { return "host" }
