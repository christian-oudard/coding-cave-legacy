package vmm

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"time"
)

// vfkit is the macOS backend. It is the only macOS VMM process covering Cloud
// Hypervisor's full feature set, and it ships a pre-signed binary carrying
// com.apple.security.virtualization, which a nix-built one would not have.
//
// The binary is usually not on PATH: the release is downloaded and unpacked,
// because re-signing a repackaged copy is the problem the pre-signed release
// exists to avoid. VFKIT overrides where it lives.
type vfkit struct{}

func (*vfkit) Name() string { return "vfkit" }

func (*vfkit) Binary() string {
	if p := os.Getenv("VFKIT"); p != "" {
		return p
	}
	return "vfkit"
}

func (v *vfkit) Check() error {
	bin := v.Binary()
	if _, err := exec.LookPath(bin); err != nil {
		return fmt.Errorf("vfkit not found at %q: %w\n"+
			"  install the pre-signed release and set VFKIT to it", bin, err)
	}
	// No /dev/kvm equivalent to check: virtualization is an entitlement on
	// the binary, not a device the caller needs access to.
	return nil
}

func (v *vfkit) Args(s Spec, p Paths) []string {
	// The kernel command line must be enclosed in literal double quotes, per
	// vfkit's own docs; they are part of the value, not shell quoting.
	// console=hvc0, because vfkit's console is virtio-serial rather than an
	// emulated 8250.
	boot := fmt.Sprintf("linux,kernel=%s,initrd=%s,cmdline=%q",
		s.Kernel, s.Initrd, fmt.Sprintf("console=hvc0 root=/dev/vda rw init=%s", s.Init))
	args := []string{
		"--cpus", fmt.Sprintf("%d", s.CPUs),
		"--memory", fmt.Sprintf("%d", s.MemoryMB),
		"--bootloader", boot,
	}
	for _, d := range s.Disks {
		// virtio-blk has no readonly argument, unlike usb-mass-storage and
		// nbd. Harmless here: the guest mounts the seed disk ro itself, so
		// enforcement is guest-side on both platforms and only the belt is
		// missing, not the braces.
		args = append(args, "--device", "virtio-blk,path="+d.Path)
	}
	// One --device per port, which is the whole reason the port table is
	// fixed at boot. There is still only one virtio-vsock device in the VM
	// however many of these are passed.
	for _, port := range s.Ports {
		dev := fmt.Sprintf("virtio-vsock,port=%d,socketURL=%s", port.Num, v.Socket(p, port))
		if port.Dir == FromHost {
			// vfkit's flag names the host's role: the host connects to the
			// guest, so vfkit listens on the unix socket. The default is
			// `listen`, meaning the host listens and vfkit dials in.
			dev += ",connect"
		}
		args = append(args, "--device", dev)
	}
	return append(args,
		"--device", "virtio-serial,logFilePath="+s.Console,
		"--restful-uri", "unix://"+p.API(),
	)
}

// Shutdown asks for an ACPI power-down over the REST API. HardStop exists and
// is not what we want: it is the equivalent of pulling the cord, with the
// guest's filesystem still mounted.
func (v *vfkit) Shutdown(p Paths) error {
	c := &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", p.API())
			},
		},
	}
	// The host in the URL is ignored; the transport always dials the socket.
	resp, err := c.Post("http://vfkit/vm/state", "application/json",
		bytes.NewReader([]byte(`{"state":"Stop"}`)))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	// Any 2xx. vfkit's own docs say this returns 200; v0.6.4 actually returns
	// 202, which is the more accurate answer anyway, since the guest has only
	// been asked to power down at this point and has not done it yet.
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("vfkit: POST /vm/state returned %s", resp.Status)
	}
	return nil
}

// Socket is one path per port in both directions, since vfkit has no
// multiplexed socket to share.
func (v *vfkit) Socket(p Paths, port Port) string {
	return fmt.Sprintf("%s_%d", p.Vsock(), port.Num)
}

// Dial is a plain connection to the port's own socket: no handshake, because
// the port was already selected when the VM was launched.
func (v *vfkit) Dial(p Paths, port uint32) (net.Conn, error) {
	return net.Dial("unix", fmt.Sprintf("%s_%d", p.Vsock(), port))
}
