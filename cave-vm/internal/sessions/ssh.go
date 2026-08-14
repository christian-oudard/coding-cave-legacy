package sessions

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os/exec"
	"strconv"
	"strings"
)

// SSH is the guest reached over the ssh path a deploy already uses. It runs
// one-shot commands rather than talking to a daemon, so it answers whether or
// not a session is live, and there is no lifetime to manage.
//
// Deliberately not rsync or tar over the directory: a batch copier works by
// letting the sender describe the filesystem, which would let a compromised
// guest choose the names that land on the host. Here the host asks for one id
// at a time and constructs both paths itself.
type SSH struct {
	// Config is an ssh_config naming the guest, which vmcav already writes.
	Config string
	// Host is the alias inside it.
	Host string
	// Bin is the guest-side binary, on the guest's PATH.
	Bin string
}

func (s SSH) command(args ...string) *exec.Cmd {
	argv := []string{"-T", "-F", s.Config, s.Host, s.bin(), "sessions"}
	argv = append(argv, args...)
	return exec.Command("ssh", argv...)
}

func (s SSH) bin() string {
	if s.Bin == "" {
		return "cave-guestd"
	}
	return s.Bin
}

func (s SSH) List() ([]Record, error) {
	cmd := s.command("list")
	var errb bytes.Buffer
	cmd.Stderr = &errb
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("sessions: listing in the guest: %w: %s",
			err, strings.TrimSpace(errb.String()))
	}
	return parseList(bytes.NewReader(out))
}

// parseList reads "<id> <size>" lines. Anything malformed is dropped rather
// than failing the checkpoint: the guest's output is input, not instruction.
func parseList(r io.Reader) ([]Record, error) {
	var out []Record
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) != 2 || !ValidID(fields[0]) {
			continue
		}
		size, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil {
			continue
		}
		out = append(out, Record{ID: fields[0], Size: size})
	}
	return out, sc.Err()
}

// Read streams one record. Closing waits for ssh, so a fetch that died partway
// reports it there rather than passing off a truncated copy as complete.
func (s SSH) Read(id string) (io.ReadCloser, error) {
	if !ValidID(id) {
		return nil, fmt.Errorf("sessions: %q is not a session id", id)
	}
	cmd := s.command("read", id)
	var errb bytes.Buffer
	cmd.Stderr = &errb
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return &cmdReader{cmd: cmd, out: stdout, errb: &errb}, nil
}

func (s SSH) Write(id string, r io.Reader) error {
	if !ValidID(id) {
		return fmt.Errorf("sessions: %q is not a session id", id)
	}
	cmd := s.command("write", id)
	cmd.Stdin = r
	var errb bytes.Buffer
	cmd.Stderr = &errb
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("sessions: restoring %s: %w: %s",
			id, err, strings.TrimSpace(errb.String()))
	}
	return nil
}

type cmdReader struct {
	cmd  *exec.Cmd
	out  io.ReadCloser
	errb *bytes.Buffer
}

func (c *cmdReader) Read(p []byte) (int, error) { return c.out.Read(p) }

func (c *cmdReader) Close() error {
	c.out.Close()
	if err := c.cmd.Wait(); err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(c.errb.String()))
	}
	return nil
}
