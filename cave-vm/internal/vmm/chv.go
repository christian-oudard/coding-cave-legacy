package vmm

import (
	"fmt"
	"net"
	"os"
	"os/exec"
	"strings"
)

// chv is the Linux backend: Cloud Hypervisor, chosen partly because nested KVM
// keeps the subcave workflow working.
type chv struct{}

func (*chv) Name() string   { return "cloud-hypervisor" }
func (*chv) Binary() string { return "cloud-hypervisor" }

func (c *chv) Check() error {
	for _, t := range []string{"cloud-hypervisor", "ch-remote"} {
		if _, err := exec.LookPath(t); err != nil {
			return fmt.Errorf("%s not found; enter the dev shell (nix develop)", t)
		}
	}
	// Cloud Hypervisor is KVM-only with no software fallback, so an
	// unavailable /dev/kvm is a launch that cannot succeed rather than one
	// that runs slowly.
	f, err := os.OpenFile("/dev/kvm", os.O_RDWR, 0)
	if err != nil {
		return fmt.Errorf("/dev/kvm not available: %w\n"+
			"  Cloud Hypervisor has no software fallback; relaunch the cave with --device /dev/kvm", err)
	}
	f.Close()
	return nil
}

func (c *chv) Args(s Spec, p Paths) []string {
	// console=ttyS0: virtio_console is a module, so hvc0 is silent through
	// early boot and a kernel panic before initrd would leave no trace.
	args := []string{
		"--kernel", s.Kernel,
		"--initramfs", s.Initrd,
		"--cmdline", fmt.Sprintf("console=ttyS0 root=/dev/vda rw init=%s", s.Init),
		"--disk",
	}
	for _, d := range s.Disks {
		spec := "path=" + d.Path + ",image_type=raw"
		if d.ReadOnly {
			spec += ",readonly=on"
		}
		args = append(args, spec)
	}
	return append(args,
		"--memory", fmt.Sprintf("size=%dM", s.MemoryMB),
		"--cpus", fmt.Sprintf("boot=%d", s.CPUs),
		// One socket for the whole plane: guest-initiated port N appears as
		// <socket>_N, host-initiated goes to <socket> with a handshake.
		"--vsock", fmt.Sprintf("cid=%d,socket=%s", s.CID, p.Vsock()),
		"--api-socket", "path="+p.API(),
		"--console", "off",
		"--serial", "file="+s.Console,
	)
}

func (c *chv) Shutdown(p Paths) error {
	return exec.Command("ch-remote", "--api-socket", p.API(), "power-button").Run()
}

func (c *chv) Socket(p Paths, port Port) string {
	if port.Dir == FromHost {
		return p.Vsock()
	}
	return fmt.Sprintf("%s_%d", p.Vsock(), port.Num)
}

// Dial performs CHV's hybrid-vsock handshake: connect to the one socket, send
// "CONNECT <port>", read back a single "OK <cid>" line, and from then on the
// stream is the guest's port.
func (c *chv) Dial(p Paths, port uint32) (net.Conn, error) {
	conn, err := net.Dial("unix", p.Vsock())
	if err != nil {
		return nil, err
	}
	if _, err := fmt.Fprintf(conn, "CONNECT %d\n", port); err != nil {
		conn.Close()
		return nil, err
	}
	// A byte at a time. Anything over-read here would be guest payload, and
	// a buffered reader would swallow the first chunk of the session.
	var line []byte
	buf := make([]byte, 1)
	for {
		if _, err := conn.Read(buf); err != nil {
			conn.Close()
			return nil, fmt.Errorf("vsock: connection closed during handshake")
		}
		if buf[0] == '\n' {
			break
		}
		line = append(line, buf[0])
	}
	if !strings.HasPrefix(string(line), "OK") {
		conn.Close()
		return nil, fmt.Errorf("vsock: handshake failed: %q", line)
	}
	return conn, nil
}
