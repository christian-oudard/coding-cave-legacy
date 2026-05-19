package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
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
		dispatch(cmdShell, os.Args[2:])
	case "env":
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "cav env: missing subcommand (build|metadata)")
			os.Exit(2)
		}
		switch os.Args[2] {
		case "build":
			dispatch(cmdEnvBuild, os.Args[3:])
		case "metadata":
			dispatch(cmdEnvMetadata, os.Args[3:])
		default:
			fmt.Fprintf(os.Stderr, "cav env: unknown subcommand %q\n", os.Args[2])
			os.Exit(2)
		}
	case "-h", "--help", "help":
		usage(os.Stdout)
	default:
		fmt.Fprintf(os.Stderr, "cav: unknown command %q\n", os.Args[1])
		usage(os.Stderr)
		os.Exit(2)
	}
}

func dispatch(fn func([]string) error, args []string) {
	if err := fn(args); err != nil {
		fmt.Fprintln(os.Stderr, "cav:", err)
		os.Exit(1)
	}
}

func usage(w *os.File) {
	fmt.Fprint(w, `usage: cav <command> [flags]

commands:
  shell           Boot the cave-vm guest and attach the current terminal.
  env build       Build the user env from ~/.config/coding-cave/cave.nix.
  env metadata    Show the user env's build inputs.

`)
}

// ============================================================================
// shell
// ============================================================================

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
	skipEnv := fs.Bool("no-env", false, "skip building/using the cave.nix-driven user env")
	keepRuntime := fs.Bool("keep-runtime", false, "leave the per-VM runtime directory after exit (debug)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *runner == "" {
		return errors.New("--runner is required (set CAV_NIXOS_RUNNER or pass --runner)")
	}

	// Build (or reuse) the cave.nix-driven user env, then publish its
	// path so the guest's cave-shell systemd unit can find it. Failures
	// are fatal — booting the VM without the user's tools available
	// is almost never what the user wants. Override with --no-env.
	if !*skipEnv {
		envPath, err := ensureUserEnv()
		if err != nil {
			return fmt.Errorf("user env build failed: %w\n\nrun `cav env build` to iterate, or pass --no-env to launch without it", err)
		}
		if err := publishUserEnv(*homeDir, envPath); err != nil {
			return fmt.Errorf("publish user env path: %w", err)
		}
	}

	// Reap any prior /tmp/cav-shell-* whose virtiofsd PIDs are dead.
	// Defers in this process can't run after SIGKILL / hard crash, so
	// each fresh invocation cleans up after itself + previous runs.
	sweepStaleRuntimeDirs()

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

	c := exec.Command(*scriptBin,
		"--quiet",
		"--flush",
		"--append",
		"--return",
		"--log-out", logPath,
		"--command", *runner,
	)
	c.Dir = runtimeDir
	c.Stdin = os.Stdin
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr

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

// ============================================================================
// env
// ============================================================================

// caveNixPath returns the user's cave.nix path, or empty if it doesn't exist.
func caveNixPath() string {
	home := os.Getenv("HOME")
	if home == "" {
		return ""
	}
	p := filepath.Join(home, ".config", "coding-cave", "cave.nix")
	if _, err := os.Stat(p); err != nil {
		return ""
	}
	return p
}

// userEnvDir is the per-user state dir for the cave-vm user env build.
// It holds the generated wrapper flake, its flake.lock, and a `result`
// symlink to the latest /nix/store/.../cave-user-env.
func userEnvDir() string {
	return filepath.Join(stateDir(), "cave-vm", "user-env")
}

func userEnvResult() string {
	return filepath.Join(userEnvDir(), "result")
}

func cmdEnvBuild(args []string) error {
	fs := flag.NewFlagSet("env build", flag.ContinueOnError)
	force := fs.Bool("force", false, "rebuild even if the result symlink exists")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if !*force {
		if _, err := os.Stat(userEnvResult()); err == nil {
			fmt.Println(userEnvResult())
			return nil
		}
	}
	path, err := buildUserEnv()
	if err != nil {
		return err
	}
	fmt.Println(path)
	return nil
}

func cmdEnvMetadata(args []string) error {
	dir := userEnvDir()
	if _, err := os.Stat(dir); err != nil {
		return fmt.Errorf("no user env built yet — run `cav env build` first")
	}
	nixBin := envOr("CAV_NIX", "nix")
	c := exec.Command(nixBin, "flake", "metadata", "--json", dir)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	return c.Run()
}

// ensureUserEnv returns the path to the current user-env, building it
// if no result symlink exists. Idempotent within a process.
func ensureUserEnv() (string, error) {
	if p, err := os.Readlink(userEnvResult()); err == nil {
		return p, nil
	}
	return buildUserEnv()
}

// buildUserEnv generates (or refreshes) the wrapper flake at userEnvDir,
// invokes `nix build` against it, and writes the `result` symlink.
// Returns the realised store path.
func buildUserEnv() (string, error) {
	dir := userEnvDir()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}

	flakeSource := os.Getenv("CAV_FLAKE_SOURCE")
	if flakeSource == "" {
		return "", errors.New("CAV_FLAKE_SOURCE is unset (cav was not built via flake wrapper)")
	}
	caveNix := caveNixPath()

	// Wrapper flake. We copy the user's cave.nix into the wrapper
	// directory rather than symlink it. Pure flake evaluation
	// forbids reading paths outside the flake's source tree, and a
	// symlink to an absolute /home/... path counts as "outside" even
	// when the symlink itself is inside.
	//
	// Remove any prior entry first; os.WriteFile follows symlinks, so
	// if an earlier cav left a symlink to the user's home here,
	// WriteFile would clobber the symlink target rather than replace
	// the link with a regular file.
	dst := filepath.Join(dir, "cave.nix")
	_ = os.Remove(dst)
	var content []byte
	if caveNix != "" {
		var err error
		content, err = os.ReadFile(caveNix)
		if err != nil {
			return "", fmt.Errorf("read user cave.nix: %w", err)
		}
	} else {
		content = []byte(defaultCaveNix)
	}
	if err := os.WriteFile(dst, content, 0o644); err != nil {
		return "", fmt.Errorf("write wrapper cave.nix: %w", err)
	}

	// Extract the user's declared `inputs` from cave.nix so we can
	// splice them into the wrapper flake and pass them as named args
	// to config. Empty / absent inputs is fine (no extra args).
	userInputs, err := extractUserInputs(dst)
	if err != nil {
		return "", fmt.Errorf("read user inputs from cave.nix: %w", err)
	}

	flakePath := filepath.Join(dir, "flake.nix")
	if err := os.WriteFile(flakePath, []byte(wrapperFlakeNix(flakeSource, userInputs)), 0o644); err != nil {
		return "", err
	}

	nixBin := envOr("CAV_NIX", "nix")
	// `nix build path:DIR` against a local flake. --print-out-paths so
	// we capture the realised /nix/store path. --no-link plus our own
	// symlink below so the `result` always lives at userEnvDir/result.
	c := exec.Command(nixBin, "build", "--no-link", "--print-out-paths",
		"--extra-experimental-features", "nix-command flakes",
		"path:"+dir)
	c.Stderr = os.Stderr
	out, err := c.Output()
	if err != nil {
		return "", fmt.Errorf("nix build user-env: %w", err)
	}
	storePath := strings.TrimSpace(string(out))
	if storePath == "" {
		return "", errors.New("nix build produced no output path")
	}
	// Update the result symlink atomically.
	tmp := filepath.Join(dir, "result.tmp")
	_ = os.Remove(tmp)
	if err := os.Symlink(storePath, tmp); err != nil {
		return "", err
	}
	if err := os.Rename(tmp, userEnvResult()); err != nil {
		return "", err
	}
	return storePath, nil
}

// publishUserEnv writes the user-env store path to a stable location
// inside the cave-home virtio-fs share, so the guest's cave-shell
// systemd unit can read it at boot. The file lives at
// $homeDir/.cache/coding-cave/cave-vm-userenv-path and contains a
// single line: the store path.
func publishUserEnv(homeDir, envPath string) error {
	dir := filepath.Join(homeDir, ".cache", "coding-cave")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "cave-vm-userenv-path"), []byte(envPath+"\n"), 0o644)
}

// wrapperFlakeNix returns the wrapper flake content. The wrapper has
// our flake as a source-only input (not a flake, just files) and
// imports lib/mkCaveEnv.nix directly. Any user-declared `inputs` in
// cave.nix are spliced in as flake inputs and passed through to
// `config` as named args.
func wrapperFlakeNix(caveVmSource string, userInputs map[string]nixValue) string {
	systemNames := map[string]bool{
		"self": true, "nixpkgs": true, "home-manager": true,
		"claude-code": true, "cavevm": true,
	}

	var inputs strings.Builder
	inputs.WriteString(`    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    cavevm = {
      url = "path:` + caveVmSource + `";
      flake = false;
    };
`)
	// Sort for stable flake.lock output across runs.
	names := make([]string, 0, len(userInputs))
	for n := range userInputs {
		if !systemNames[n] {
			names = append(names, n)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		switch v := userInputs[name].value.(type) {
		case string:
			inputs.WriteString(fmt.Sprintf("    %s.url = %s;\n", name, nixStringLit(v)))
		default:
			inputs.WriteString(fmt.Sprintf("    %s = %s;\n", name, toNix(v)))
		}
	}

	return `{
  description = "cave-vm user environment (generated by cav env build)";

  inputs = {
` + inputs.String() + `  };

  outputs = { self, nixpkgs, home-manager, claude-code, cavevm, ... }@allInputs:
    let
      system = "x86_64-linux";
      caveNix = import ./cave.nix;
      userInputs = builtins.removeAttrs allInputs
        [ "cavevm" "claude-code" "home-manager" "nixpkgs" "self" ];
      mkCaveEnv = import "${cavevm}/lib/mkCaveEnv.nix" {
        inherit nixpkgs home-manager system;
      };
    in {
      packages.${system}.default = mkCaveEnv {
        inherit claude-code;
        userModule = { pkgs, ... }:
          caveNix.config (userInputs // { inherit pkgs; });
      };
    };
}
`
}

// nixValue wraps a JSON-decoded value with its origin so we can format
// it back as Nix. (string|map[string]any|[]any|bool|number — same set
// that JSON natively carries.)
type nixValue struct{ value any }

// extractUserInputs evaluates `(import cave.nix).inputs or {}` to get
// the user's declared inputs as a JSON object, then converts each entry
// to a nixValue we can re-emit in the wrapper flake.
func extractUserInputs(caveNixPath string) (map[string]nixValue, error) {
	nixBin := envOr("CAV_NIX", "nix")
	expr := fmt.Sprintf("(import %s).inputs or {}", caveNixPath)
	c := exec.Command(nixBin, "eval", "--json", "--impure", "--expr", expr,
		"--extra-experimental-features", "nix-command flakes")
	out, err := c.Output()
	if err != nil {
		// If cave.nix is malformed or has no `inputs` and the impure
		// eval barfs in some other way, treat as "no inputs". Other
		// errors will surface from the actual nix build.
		return nil, nil
	}
	var raw map[string]any
	if err := json.Unmarshal(out, &raw); err != nil {
		return nil, fmt.Errorf("decode inputs JSON: %w", err)
	}
	result := make(map[string]nixValue, len(raw))
	for k, v := range raw {
		result[k] = nixValue{value: v}
	}
	return result, nil
}

// toNix converts a JSON-decoded value back into a Nix expression
// string. Mirrors cav-rs/src/user_env.rs's to_nix function.
func toNix(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case bool:
		if x {
			return "true"
		}
		return "false"
	case string:
		return nixStringLit(x)
	case float64:
		// json.Unmarshal gives numbers as float64
		if x == float64(int64(x)) {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = toNix(e)
		}
		return "[ " + strings.Join(parts, " ") + " ]"
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, k := range keys {
			parts = append(parts, fmt.Sprintf("%s = %s;", k, toNix(x[k])))
		}
		return "{ " + strings.Join(parts, " ") + " }"
	default:
		return fmt.Sprintf("%v", x)
	}
}

// nixStringLit returns a Nix double-quoted string literal with the
// characters that the Nix lexer cares about escaped: backslash, double
// quote, and the antiquotation opener "${".
func nixStringLit(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '\\':
			b.WriteString(`\\`)
		case c == '"':
			b.WriteString(`\"`)
		case c == '$' && i+1 < len(s) && s[i+1] == '{':
			b.WriteString(`\${`)
			i++ // consumed the '{' too
		default:
			b.WriteByte(c)
		}
	}
	b.WriteByte('"')
	return b.String()
}

const defaultCaveNix = `# Default cave.nix generated by cav. Replace at ~/.config/coding-cave/cave.nix.
{
  config = { pkgs, ... }: {
    home.packages = with pkgs; [ eza ripgrep fzf bat zoxide ];
  };
}
`

// ============================================================================
// helpers
// ============================================================================

func startVirtiofsd(bin, socketPath, sharedDir, logPath string) (*exec.Cmd, error) {
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
		return
	case <-time.After(grace):
		_ = c.Process.Kill()
	}
	// Always wait for the reaper goroutine to finish before returning,
	// or we leak the wait4() and the kernel keeps the child as a
	// zombie until cav itself dies. Adding a final timeout so a hung
	// Wait() can't trap cav forever.
	select {
	case <-done:
	case <-time.After(2 * time.Second):
	}
}

// sweepStaleRuntimeDirs removes /tmp/cav-shell-* directories whose
// recorded PIDs are dead. Called at the start of `cav shell` so prior
// crashed sessions don't accumulate. Cheap (just stat + kill(0)
// probe), no new state file required.
func sweepStaleRuntimeDirs() {
	matches, err := filepath.Glob("/tmp/cav-shell-*")
	if err != nil {
		return
	}
	for _, dir := range matches {
		if runtimeDirAlive(dir) {
			continue
		}
		_ = os.RemoveAll(dir)
	}
}

// runtimeDirAlive returns true if any *.pid file under dir names a
// process that's still alive. virtiofsd writes one of these next to
// each socket it opens, which gives us a cheap liveness probe without
// needing to track our own state.
func runtimeDirAlive(dir string) bool {
	pidFiles, _ := filepath.Glob(filepath.Join(dir, "*.pid"))
	for _, pf := range pidFiles {
		b, err := os.ReadFile(pf)
		if err != nil {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
		if err != nil || pid <= 0 {
			continue
		}
		// signal 0 = existence probe; no signal delivered. ESRCH => dead.
		if err := syscall.Kill(pid, 0); err == nil {
			return true
		}
	}
	return false
}

// prepareOverlayImage creates the per-VM /nix/.rw-store backing image
// (sparse ext4 file) at runtimeDir/name, suppressing mkfs's chatter
// into the session log.
func prepareOverlayImage(runtimeDir, name string, size int64, logPath string) error {
	path := filepath.Join(runtimeDir, name)
	if _, err := os.Stat(path); err == nil {
		return nil
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
