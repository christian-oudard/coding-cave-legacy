// Package userenv builds the cave user environment inside the guest and puts
// it into an agent's home.
//
// This is phase 9 of docs/vm-cave-design.md, guest side. One environment is
// specified by one file -- cave.nix -- and it is what every agent gets, which
// is why the build lives here rather than on the host: an aarch64-darwin host
// cannot build an aarch64-linux environment at all, so a host-side build is
// not portable, and once the guest builds on macOS there is no reason for
// Linux to differ. What crosses the boundary is a handful of small text files
// rather than a closure.
//
// Three steps, deliberately separable because they fail differently:
//
//	Prepare      generate the wrapper flake from the spec (cheap, pure IO)
//	Build        nix build it, and move `result` only if that succeeded
//	Materialize  copy the built tree into one agent's home
//
// Build failing must leave the previous result in place, because the
// alternative is that a typo in cave.nix takes every agent's environment away.
// Materialize is per-agent and runs from seed-agent-home@, so a home seeded
// before the spec changed catches up at that agent's next session start.
package userenv

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

// Inputs are the three flake sources the environment is built from, as store
// paths taken out of the guest system's own closure (guest/flake.nix passes
// them in as caveEnvInputs).
//
// Store paths rather than github: refs, and that is the whole trick: every one
// of them is already in the guest store, because the running system depends on
// it. So `cave-env build` fetches nothing, needs no proxy, and cannot produce
// an environment built against a different nixpkgs than the guest is running.
// A second nixpkgs would mean a second glibc and a second claude in one VM,
// with PATH order deciding which an agent got.
//
// `main` reaches the same place from the other end: it bakes path:${nixpkgs}
// into cav at cav-build time so a cave's env build is offline too.
type Inputs struct {
	Nixpkgs     string `json:"nixpkgs"`
	HomeManager string `json:"home-manager"`
	ClaudeCode  string `json:"claude-code"`
}

// Env is one guest's user environment: where the spec lives, and what to build
// it against.
type Env struct {
	// Dir holds the spec and the build. cave.nix and its sibling .nix files
	// are the input; flake.nix, flake.lock and result are this package's.
	Dir string
	// CaveEnv is the store path of the guest's env flake (guest/env),
	// installed by the system config. Its version is the deployed generation,
	// which is why there is no source hash here: `main` needs caveSourceHash
	// to notice edits to its cave modules, and a store path already is one.
	CaveEnv string
	Inputs  Inputs
}

// systemInputs are the names the wrapper flake defines itself. A user's
// cave.nix may not redefine them: nixpkgs and home-manager decide what the
// environment is built against, and letting a spec repoint them is the
// second-glibc problem by another route.
var systemInputs = map[string]bool{
	"self": true, "nixpkgs": true, "home-manager": true,
	"claude-code": true, "cave-env": true,
}

func (e Env) caveNix() string   { return filepath.Join(e.Dir, "cave.nix") }
func (e Env) flakeNix() string  { return filepath.Join(e.Dir, "flake.nix") }
func (e Env) Result() string    { return filepath.Join(e.Dir, "result") }
func (e Env) flakeLock() string { return filepath.Join(e.Dir, "flake.lock") }

// Prepare writes the wrapper flake for the current spec and returns its text.
//
// Not free: reading a spec's own flake inputs takes a `nix eval`, which is a
// good fraction of a second. That is why Stamp does not depend on this -- the
// common case is a session starting against an environment nobody has touched,
// and it should cost a hash of a few small files rather than an evaluation.
func (e Env) Prepare() (string, error) {
	if err := os.MkdirAll(e.Dir, 0o755); err != nil {
		return "", err
	}
	_, err := os.Stat(e.caveNix())
	hasSpec := err == nil

	var userInputs map[string]any
	if hasSpec {
		if userInputs, err = e.readUserInputs(); err != nil {
			return "", err
		}
	}
	generated := e.GenerateFlake(hasSpec, userInputs)
	return generated, os.WriteFile(e.flakeNix(), []byte(generated), 0o644)
}

// readUserInputs evaluates `(import cave.nix).inputs or {}`, which is how a
// spec asks for a flake input of its own (a dotfiles repo, a plugin source).
//
// A failure here is reported rather than swallowed. `main` returns an empty
// set on a bad eval, which silently drops every input the user asked for and
// then fails much later with an unbound variable in their own module.
func (e Env) readUserInputs() (map[string]any, error) {
	cmd := exec.Command("nix", "eval", "--json", "--impure", "--expr",
		fmt.Sprintf("(import %s).inputs or {}", e.caveNix()))
	out, err := cmd.Output()
	if err != nil {
		var stderr string
		if ee, ok := err.(*exec.ExitError); ok {
			stderr = strings.TrimSpace(string(ee.Stderr))
		}
		return nil, fmt.Errorf("reading inputs from cave.nix: %v\n%s", err, stderr)
	}
	var inputs map[string]any
	if err := json.Unmarshal(out, &inputs); err != nil {
		return nil, fmt.Errorf("cave.nix inputs are not an attrset: %w", err)
	}
	return inputs, nil
}

// GenerateFlake renders the wrapper flake. Exported for the tests, which are
// the only place its output is worth reading as text.
func (e Env) GenerateFlake(hasSpec bool, userInputs map[string]any) string {
	var b strings.Builder
	b.WriteString("# Generated by cave-guestd. Edits are overwritten;\n")
	b.WriteString("# the file to edit is cave.nix beside this one.\n{\n")
	fmt.Fprintf(&b, "  inputs.nixpkgs.url = %s;\n", nixString("path:"+e.Inputs.Nixpkgs))
	// Written out one by one rather than ranged over a map, because map
	// iteration order is random and this file is compared against itself to
	// decide whether to rebuild. A flake that differed only in the order of
	// its inputs would rebuild every time.
	fmt.Fprintf(&b, "  inputs.home-manager = {\n    url = %s;\n"+
		"    inputs.nixpkgs.follows = \"nixpkgs\";\n  };\n",
		nixString("path:"+e.Inputs.HomeManager))
	fmt.Fprintf(&b, "  inputs.claude-code = {\n    url = %s;\n"+
		"    inputs.nixpkgs.follows = \"nixpkgs\";\n  };\n",
		nixString("path:"+e.Inputs.ClaudeCode))
	fmt.Fprintf(&b, "  inputs.cave-env.url = %s;\n", nixString("path:"+e.CaveEnv))

	names := make([]string, 0, len(userInputs))
	for name := range userInputs {
		if !systemInputs[name] {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		if url, ok := userInputs[name].(string); ok {
			fmt.Fprintf(&b, "  inputs.%s.url = %s;\n", name, nixString(url))
		} else {
			fmt.Fprintf(&b, "  inputs.%s = %s;\n", name, toNix(userInputs[name]))
		}
	}

	b.WriteString("\n  outputs = { nixpkgs, claude-code, home-manager, cave-env, ... }@inputs:\n")
	if hasSpec {
		b.WriteString("    let\n")
		b.WriteString("      userCfg = import ./cave.nix;\n")
		// The spec's own inputs, and only those: it asked for them by name and
		// should not have to skip past the four this file defines.
		b.WriteString("      userInputs = builtins.removeAttrs inputs\n")
		b.WriteString("        " + nixStringList(sortedKeys(systemInputs)) + ";\n")
		b.WriteString("    in cave-env.lib.mkCaveEnv { inherit nixpkgs home-manager; } {\n")
		b.WriteString("      inherit claude-code;\n")
		b.WriteString("      userModule = { pkgs, ... }:\n")
		b.WriteString("        userCfg.config (userInputs // { inherit pkgs; });\n")
		b.WriteString("    };\n")
	} else {
		// No spec is a supported state, not an error: a guest that has never
		// been given a cave.nix still gets the cave defaults, which is most of
		// what an agent needs.
		b.WriteString("    cave-env.lib.mkCaveEnv { inherit nixpkgs home-manager; } {\n")
		b.WriteString("      inherit claude-code;\n")
		b.WriteString("      userModule = { ... }: { };\n")
		b.WriteString("    };\n")
	}
	b.WriteString("}\n")
	return b.String()
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func nixStringList(items []string) string {
	quoted := make([]string, len(items))
	for i, s := range items {
		quoted[i] = nixString(s)
	}
	return "[ " + strings.Join(quoted, " ") + " ]"
}

// nixString quotes a Go string as a Nix string literal. `${` is escaped
// because Nix would otherwise read it as antiquotation and try to splice.
func nixString(s string) string {
	r := strings.NewReplacer(`\`, `\\`, `"`, `\"`, `${`, `\${`)
	return `"` + r.Replace(s) + `"`
}

// toNix renders a decoded JSON value as a Nix expression, which is how a
// user's input spec survives the round trip through `nix eval --json`.
func toNix(v any) string {
	switch t := v.(type) {
	case string:
		return nixString(t)
	case bool:
		if t {
			return "true"
		}
		return "false"
	case float64:
		return fmt.Sprintf("%g", t)
	case nil:
		return "null"
	case []any:
		items := make([]string, len(t))
		for i, e := range t {
			items[i] = toNix(e)
		}
		return "[ " + strings.Join(items, " ") + " ]"
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		items := make([]string, len(keys))
		for i, k := range keys {
			items[i] = fmt.Sprintf("%s = %s", k, toNix(t[k]))
		}
		return "{ " + strings.Join(items, "; ") + "; }"
	default:
		return nixString(fmt.Sprint(t))
	}
}

// Build evaluates and builds the wrapper flake, then points `result` at the
// output. Returns the store path.
//
// The result symlink is moved only on success, so a spec that does not
// evaluate leaves every agent with the environment they already had. That is
// the same contract `main` has, and it is why this reports the failure rather
// than returning it as a fatal: the caller (a session about to start) should
// carry on with the old environment rather than refuse to start the agent.
func (e Env) Build() (string, error) {
	// --out-link, not --no-link plus a symlink of our own. The difference is
	// that nix registers an out-link as an indirect GC root, and a hand-made
	// symlink in /var/lib is not a root at all. Without it a
	// `nix-collect-garbage` deletes the built environment out from under every
	// home that has one: the materialized files survive, because they are real
	// copies, but ~/.cave-profile is a link into the store and would dangle,
	// so every package the environment provided vanishes from PATH with
	// nothing to say why.
	cmd := exec.Command("nix", "build", "--out-link", e.Result(), "--print-out-paths")
	cmd.Dir = e.Dir
	// stderr inherited so a slow first build shows nix's own progress rather
	// than going quiet for minutes.
	cmd.Stderr = os.Stderr
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("nix build: %w", err)
	}
	path := strings.TrimSpace(string(out))
	if i := strings.IndexByte(path, '\n'); i >= 0 {
		path = path[:i]
	}
	if path == "" {
		return "", fmt.Errorf("nix build printed no output path")
	}
	return path, nil
}

// stampFile records what the current `result` was built from.
func (e Env) stampFile() string { return filepath.Join(e.Dir, ".stamp") }

// Stamp hashes everything that decides the build output: the four store paths
// this environment is built against, and the content of cave.nix and each
// sibling .nix file.
//
// Deliberately computed from the spec rather than from the generated flake,
// even though the flake is the thing handed to nix. The flake is a pure
// function of these same inputs, so it adds nothing to the hash -- and
// deriving the stamp from it would mean running `nix eval` before being
// allowed to decide that no evaluation was needed.
//
// The spec files are hashed individually because the flake only *names*
// cave.nix: two entirely different specs generate byte-identical flakes.
// Missing that is the failure that looks like the tool ignoring your edit.
func (e Env) Stamp() (string, error) {
	h := sha256.New()
	for _, part := range []string{
		e.CaveEnv, e.Inputs.Nixpkgs, e.Inputs.HomeManager, e.Inputs.ClaudeCode,
	} {
		fmt.Fprintf(h, "%s\x00", part)
	}
	names, err := e.specFiles()
	if err != nil {
		return "", err
	}
	for _, name := range names {
		content, err := os.ReadFile(filepath.Join(e.Dir, name))
		if err != nil {
			return "", err
		}
		// Length-prefixed, so that renaming a.nix to ab.nix cannot collide
		// with an edit that moved a byte across the boundary.
		fmt.Fprintf(h, "%s\x00%d\x00%s\x00", name, len(content), content)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// specFiles lists the .nix files that are the spec: cave.nix and its siblings,
// but never the two this package generates.
func (e Env) specFiles() ([]string, error) {
	entries, err := os.ReadDir(e.Dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var names []string
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".nix") || name == "flake.nix" {
			continue
		}
		names = append(names, name)
	}
	sort.Strings(names)
	return names, nil
}

// Changed reports whether a build is needed. A missing result always is: the
// stamp could match a build whose output has since been garbage collected.
func (e Env) Changed(stamp string) bool {
	if _, err := os.Lstat(e.Result()); err != nil {
		return true
	}
	recorded, err := os.ReadFile(e.stampFile())
	if err != nil {
		return true
	}
	return strings.TrimSpace(string(recorded)) != stamp
}

// WriteStamp records a stamp, and is called only after a build succeeds. A
// stamp written before would claim a failed build as the current one and skip
// the retry.
func (e Env) WriteStamp(stamp string) error {
	return os.WriteFile(e.stampFile(), []byte(stamp+"\n"), 0o644)
}
