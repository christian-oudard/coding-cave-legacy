package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

func main() {
	if len(os.Args) < 2 {
		usage(os.Stderr)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "shell":
		if err := cmdShell(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, "cav:", err)
			os.Exit(1)
		}
	case "-h", "--help", "help":
		usage(os.Stdout)
	default:
		fmt.Fprintf(os.Stderr, "cav: unknown command %q\n", os.Args[1])
		usage(os.Stderr)
		os.Exit(2)
	}
}

func usage(w *os.File) {
	fmt.Fprint(w, `usage: cav <command> [flags]

commands:
  shell    Boot the cave-vm guest and attach the current terminal.

`)
}

// Each share corresponds to a virtio-fs declaration in the NixOS guest
// config (lib/nixos.nix). The socket basename must match what the
// microvm runner expects (cave-vm-virtiofs-<tag>.sock).
type share struct {
	tag       string
	sharedDir string
}

func cmdShell(args []string) error {
	fs := flag.NewFlagSet("shell", flag.ContinueOnError)
	runner := fs.String("runner", os.Getenv("CAV_NIXOS_RUNNER"), "path to microvm-run script")
	virtiofsd := fs.String("virtiofsd", envOr("CAV_VIRTIOFSD", "virtiofsd"), "virtiofsd binary")
	scriptBin := fs.String("script", envOr("CAV_SCRIPT", "script"), "util-linux script(1) binary")
	storeDir := fs.String("store", "/nix/store", "host path shared into the guest as /nix/.ro-store")
	homeDir := fs.String("home", envOr("HOME", "/home/cave-coder"), "host path shared into the guest as /home/cave-coder")
	keepRuntime := fs.Bool("keep-runtime", false, "leave the per-VM runtime directory after exit (debug)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *runner == "" {
		return errors.New("--runner is required (set CAV_NIXOS_RUNNER or pass --runner)")
	}

	runtimeDir, err := os.MkdirTemp("", "cav-shell-*")
	if err != nil {
		return fmt.Errorf("create runtime dir: %w", err)
	}
	if !*keepRuntime {
		defer os.RemoveAll(runtimeDir)
	}

	logPath, err := openLogFile()
	if err != nil {
		return fmt.Errorf("prepare log file: %w", err)
	}

	// Pre-create the per-VM nix-store overlay so microvm-run skips the
	// noisy `chattr + truncate + mkfs.ext4` block in its shell prologue.
	// Path/size match what lib/nixos.nix declares for the volume.
	if err := prepareOverlayImage(runtimeDir, "nix-store-overlay.img", 2048*1024*1024, logPath); err != nil {
		return failWithLog(logPath, fmt.Errorf("prepare overlay image: %w", err))
	}

	shares := []share{
		{tag: "ro-store", sharedDir: *storeDir},
		{tag: "cave-home", sharedDir: *homeDir},
	}

	var vfsds []*exec.Cmd
	defer func() {
		for _, c := range vfsds {
			terminateChild(c, 3*time.Second)
		}
	}()

	for _, sh := range shares {
		sock := filepath.Join(runtimeDir, "cave-vm-virtiofs-"+sh.tag+".sock")
		c, err := startVirtiofsd(*virtiofsd, sock, sh.sharedDir, logPath)
		if err != nil {
			return failWithLog(logPath, fmt.Errorf("start virtiofsd[%s]: %w", sh.tag, err))
		}
		vfsds = append(vfsds, c)
		if err := waitForSocket(sock, 5*time.Second); err != nil {
			return failWithLog(logPath, fmt.Errorf("virtiofsd[%s] socket never appeared: %w", sh.tag, err))
		}
	}

	// Wrap microvm-run in util-linux script(1). CHV's --serial tty opens
	// /dev/tty directly, so we have to allocate a pty wrapper *outside*
	// CHV to capture the kernel+systemd serial output. script(1) does
	// exactly that and writes both directions of the transcript to a
	// log file while letting the user keep an interactive session.
	// util-linux 2.42's script(1) rejects `--log-out FILE` together
	// with a positional output FILE ("unexpected number of arguments"),
	// so we pass only --log-out.
	c := exec.Command(*scriptBin,
		"--quiet",
		"--flush",
		"--append",
		"--return", // exit with the wrapped command's status code
		"--log-out", logPath,
		"--command", *runner,
	)
	c.Dir = runtimeDir
	c.Stdin = os.Stdin
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	// No Setpgid: script(1) needs to share our controlling terminal so
	// /dev/tty inside the pty subprocess resolves to the right pty.

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(sigCh)

	if err := c.Start(); err != nil {
		return failWithLog(logPath, fmt.Errorf("start script wrapper: %w", err))
	}

	done := make(chan error, 1)
	go func() { done <- c.Wait() }()

	select {
	case err := <-done:
		if err != nil {
			var ee *exec.ExitError
			if errors.As(err, &ee) {
				_ = failWithLog(logPath, fmt.Errorf("VM exited with status %d", ee.ExitCode()))
				os.Exit(ee.ExitCode())
			}
			return failWithLog(logPath, err)
		}
	case sig := <-sigCh:
		_ = c.Process.Signal(sig)
		<-done
	}
	return nil
}

func startVirtiofsd(bin, socketPath, sharedDir, logPath string) (*exec.Cmd, error) {
	// virtiofsd's clap is strict about which flags accept space vs `=`;
	// stick to the `=` form to avoid that minefield.
	args := []string{
		"--socket-path=" + socketPath,
		"--shared-dir=" + sharedDir,
		"--thread-pool-size=8",
		"--cache=auto",
		"--inode-file-handles=prefer",
		"--posix-acl",
		"--xattr",
	}
	c := exec.Command(bin, args...)
	// Append virtiofsd output to the same log as the VM serial. Direct
	// process output (not pty), so the file is shared with script(1)'s
	// log target; both writers append-and-flush.
	logF, err := os.OpenFile(logPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	c.Stdout = logF
	c.Stderr = logF
	c.SysProcAttr = &syscall.SysProcAttr{
		Pdeathsig: syscall.SIGTERM,
		Setpgid:   true,
	}
	if err := c.Start(); err != nil {
		_ = logF.Close()
		return nil, err
	}
	return c, nil
}

func waitForSocket(path string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return nil
		}
		time.Sleep(20 * time.Millisecond)
	}
	return fmt.Errorf("timeout waiting for %s", path)
}

func terminateChild(c *exec.Cmd, grace time.Duration) {
	if c == nil || c.Process == nil {
		return
	}
	_ = c.Process.Signal(syscall.SIGTERM)
	done := make(chan struct{})
	go func() { _, _ = c.Process.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(grace):
		_ = c.Process.Kill()
	}
}

// prepareOverlayImage creates the per-VM /nix/.rw-store backing image
// (sparse ext4 file) at runtimeDir/name, suppressing mkfs's chatter
// into the session log. If the file already exists it's left alone.
func prepareOverlayImage(runtimeDir, name string, size int64, logPath string) error {
	path := filepath.Join(runtimeDir, name)
	if _, err := os.Stat(path); err == nil {
		return nil // already exists
	}
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	if err := f.Truncate(size); err != nil {
		_ = f.Close()
		return err
	}
	_ = f.Close()

	mkfs, err := exec.LookPath("mkfs.ext4")
	if err != nil {
		return fmt.Errorf("mkfs.ext4 not found on PATH: %w", err)
	}
	logF, err := os.OpenFile(logPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer logF.Close()
	c := exec.Command(mkfs, "-q", "-F", path)
	c.Stdout = logF
	c.Stderr = logF
	return c.Run()
}

// openLogFile prepares the location of the per-session log file and
// truncates it so each `cav shell` invocation starts fresh.
func openLogFile() (string, error) {
	dir := filepath.Join(stateDir(), "cave-vm")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(dir, "last.log")
	f, err := os.Create(path)
	if err != nil {
		return "", err
	}
	_ = f.Close()
	return path, nil
}

// stateDir returns the coding-cave state directory, following the
// XDG_STATE_HOME convention and falling back to ~/.local/share if not
// set (which matches the existing bwrap-cave layout).
func stateDir() string {
	if d := os.Getenv("XDG_STATE_HOME"); d != "" {
		return filepath.Join(d, "coding-cave")
	}
	home := os.Getenv("HOME")
	if home == "" {
		home = "/tmp"
	}
	return filepath.Join(home, ".local", "share", "coding-cave")
}

// failWithLog prints a hint about the log file alongside the error so
// the user knows where to look post-mortem. Returns the original
// error so callers can keep using it.
func failWithLog(logPath string, err error) error {
	fmt.Fprintf(os.Stderr, "cav: %v\ncav: VM serial log saved at %s\n", err, logPath)
	return err
}

func envOr(k, fallback string) string {
	if v, ok := os.LookupEnv(k); ok && v != "" {
		return v
	}
	return fallback
}
