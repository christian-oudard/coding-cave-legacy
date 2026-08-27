package userenv

import (
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"testing"
)

func testEnv(t *testing.T) Env {
	t.Helper()
	return Env{
		Dir:     t.TempDir(),
		CaveEnv: "/nix/store/aaa-cave-env",
		Inputs: Inputs{
			Nixpkgs:     "/nix/store/bbb-nixpkgs",
			HomeManager: "/nix/store/ccc-home-manager",
			ClaudeCode:  "/nix/store/ddd-claude-code",
		},
	}
}

func TestGenerateFlakePinsEveryInputByStorePath(t *testing.T) {
	got := testEnv(t).GenerateFlake(false, nil)
	for _, want := range []string{
		`inputs.nixpkgs.url = "path:/nix/store/bbb-nixpkgs"`,
		`url = "path:/nix/store/ccc-home-manager"`,
		`url = "path:/nix/store/ddd-claude-code"`,
		`inputs.cave-env.url = "path:/nix/store/aaa-cave-env"`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("generated flake missing %s\n%s", want, got)
		}
	}
	// Nothing may reach the network: an env build has to work with the proxy
	// down, because it runs before an agent that has not logged in yet.
	if strings.Contains(got, "github:") {
		t.Errorf("generated flake names a github ref:\n%s", got)
	}
}

func TestGenerateFlakeWithoutSpecStillBuildsDefaults(t *testing.T) {
	got := testEnv(t).GenerateFlake(false, nil)
	if !strings.Contains(got, "userModule = { ... }: { };") {
		t.Errorf("no-spec flake should use an empty user module:\n%s", got)
	}
	if strings.Contains(got, "import ./cave.nix") {
		t.Errorf("no-spec flake must not import a cave.nix that is not there:\n%s", got)
	}
}

func TestGenerateFlakeWithSpecImportsIt(t *testing.T) {
	got := testEnv(t).GenerateFlake(true, nil)
	if !strings.Contains(got, "userCfg = import ./cave.nix;") {
		t.Errorf("spec flake should import cave.nix:\n%s", got)
	}
}

func TestGenerateFlakeCarriesUserInputs(t *testing.T) {
	got := testEnv(t).GenerateFlake(true, map[string]any{
		"dotfiles": "github:someone/dotfiles",
		"fancy":    map[string]any{"url": "github:a/b", "flake": false},
	})
	if !strings.Contains(got, `inputs.dotfiles.url = "github:someone/dotfiles"`) {
		t.Errorf("string input not carried:\n%s", got)
	}
	if !strings.Contains(got, `inputs.fancy = { flake = false; url = "github:a/b"; }`) {
		t.Errorf("attrset input not carried:\n%s", got)
	}
}

// A spec that names nixpkgs would decide what the environment is built
// against, which is the guest system's call and not the spec's.
func TestGenerateFlakeIgnoresSystemInputNames(t *testing.T) {
	got := testEnv(t).GenerateFlake(true, map[string]any{
		"nixpkgs":      "github:NixOS/nixpkgs/nixos-24.05",
		"home-manager": "github:evil/hm",
		"cave-env":     "github:evil/env",
		"mine":         "github:me/mine",
	})
	if strings.Contains(got, "nixos-24.05") || strings.Contains(got, "evil") {
		t.Errorf("a system input was overridable:\n%s", got)
	}
	if !strings.Contains(got, `inputs.mine.url = "github:me/mine"`) {
		t.Errorf("user input dropped along with the system ones:\n%s", got)
	}
}

// The flake text is half the rebuild stamp, so an unstable rendering would
// rebuild the environment on every session start.
func TestGenerateFlakeIsDeterministic(t *testing.T) {
	e := testEnv(t)
	inputs := map[string]any{"a": "github:a/a", "b": "github:b/b", "c": "github:c/c"}
	first := e.GenerateFlake(true, inputs)
	for range 20 {
		if got := e.GenerateFlake(true, inputs); got != first {
			t.Fatalf("flake rendering is not stable:\n%s\n---\n%s", first, got)
		}
	}
}

func TestNixStringEscapesAntiquotation(t *testing.T) {
	if got := nixString("a${b}c"); got != `"a\${b}c"` {
		t.Errorf(`nixString("a${b}c") = %s`, got)
	}
	if got := nixString(`say "hi"\`); got != `"say \"hi\"\\"` {
		t.Errorf("nixString did not escape quotes and backslashes: %s", got)
	}
}

func TestStampChangesWithSpecContent(t *testing.T) {
	e := testEnv(t)
	spec := filepath.Join(e.Dir, "cave.nix")

	write := func(content string) string {
		t.Helper()
		if err := os.WriteFile(spec, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		s, err := e.Stamp()
		if err != nil {
			t.Fatal(err)
		}
		return s
	}

	first := write("{ config = { ... }: { }; }")
	again := write("{ config = { ... }: { }; }")
	if first != again {
		t.Error("stamp is not stable for identical content")
	}
	edited := write("{ config = { ... }: { home.packages = []; }; }")
	if edited == first {
		t.Error("editing cave.nix did not change the stamp")
	}
}

// Two specs generate byte-identical flakes -- the flake only names cave.nix --
// so a stamp that ignored sibling content would skip a real edit.
func TestStampChangesWithSiblingContent(t *testing.T) {
	e := testEnv(t)
	if err := os.WriteFile(filepath.Join(e.Dir, "cave.nix"), []byte("{ }"), 0o644); err != nil {
		t.Fatal(err)
	}
	sibling := filepath.Join(e.Dir, "extra.nix")
	if err := os.WriteFile(sibling, []byte("{ a = 1; }"), 0o644); err != nil {
		t.Fatal(err)
	}
	before, err := e.Stamp()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(sibling, []byte("{ a = 2; }"), 0o644); err != nil {
		t.Fatal(err)
	}
	after, err := e.Stamp()
	if err != nil {
		t.Fatal(err)
	}
	if before == after {
		t.Error("editing a sibling .nix file did not change the stamp")
	}
}

// A deploy that moves nixpkgs, home-manager, claude-code or the cave modules
// has to rebuild every environment: the old result was built against paths the
// guest no longer runs.
func TestStampChangesWithGuestInputs(t *testing.T) {
	e := testEnv(t)
	if err := os.WriteFile(filepath.Join(e.Dir, "cave.nix"), []byte("{ }"), 0o644); err != nil {
		t.Fatal(err)
	}
	before, err := e.Stamp()
	if err != nil {
		t.Fatal(err)
	}
	for _, moved := range []Env{
		{Dir: e.Dir, CaveEnv: "/nix/store/zzz-cave-env", Inputs: e.Inputs},
		{Dir: e.Dir, CaveEnv: e.CaveEnv, Inputs: Inputs{
			Nixpkgs:     "/nix/store/zzz-nixpkgs",
			HomeManager: e.Inputs.HomeManager,
			ClaudeCode:  e.Inputs.ClaudeCode,
		}},
		{Dir: e.Dir, CaveEnv: e.CaveEnv, Inputs: Inputs{
			Nixpkgs:     e.Inputs.Nixpkgs,
			HomeManager: e.Inputs.HomeManager,
			ClaudeCode:  "/nix/store/zzz-claude-code",
		}},
	} {
		after, err := moved.Stamp()
		if err != nil {
			t.Fatal(err)
		}
		if after == before {
			t.Errorf("a moved input left the stamp unchanged: %+v", moved)
		}
	}
}

// The generated flake is not the spec, so regenerating it must not read as a
// spec change. Otherwise every deploy rebuilds every environment.
func TestStampIgnoresGeneratedFlake(t *testing.T) {
	e := testEnv(t)
	flake := e.GenerateFlake(true, nil)
	if err := os.WriteFile(filepath.Join(e.Dir, "cave.nix"), []byte("{ }"), 0o644); err != nil {
		t.Fatal(err)
	}
	before, err := e.Stamp()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(e.flakeNix(), []byte(flake), 0o644); err != nil {
		t.Fatal(err)
	}
	after, err := e.Stamp()
	if err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Error("writing the generated flake changed the stamp")
	}
}

func TestChangedNeedsBuildWhenResultIsMissing(t *testing.T) {
	e := testEnv(t)
	if err := e.WriteStamp("abc"); err != nil {
		t.Fatal(err)
	}
	// A stamp can outlive the store path it names, which is what a garbage
	// collection leaves behind.
	if !e.Changed("abc") {
		t.Error("a matching stamp with no result should still build")
	}
	if err := os.Symlink("/nix/store/whatever", e.Result()); err != nil {
		t.Fatal(err)
	}
	if e.Changed("abc") {
		t.Error("a matching stamp with a result should not rebuild")
	}
	if !e.Changed("def") {
		t.Error("a different stamp should rebuild")
	}
}

func currentUserName(t *testing.T) string {
	t.Helper()
	u, err := user.Current()
	if err != nil {
		t.Skip("no current user")
	}
	return u.Username
}

// buildResult fakes what mkCaveEnv produces: a files/ tree and a home-path
// symlink, both read-only as nix leaves a store path.
func buildResult(t *testing.T, files map[string]string) string {
	t.Helper()
	result := t.TempDir()
	for rel, content := range files {
		path := filepath.Join(result, "files", rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o444); err != nil {
			t.Fatal(err)
		}
	}
	target := filepath.Join(result, "profile")
	if err := os.MkdirAll(filepath.Join(target, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(result, "home-path")); err != nil {
		t.Fatal(err)
	}
	return result
}

func TestMaterializeWritesRealWritableFiles(t *testing.T) {
	result := buildResult(t, map[string]string{".claude/settings.json": `{"model":"opus"}`})
	home := t.TempDir()
	if err := Materialize(result, home, currentUserName(t)); err != nil {
		t.Fatal(err)
	}
	settings := filepath.Join(home, ".claude/settings.json")
	info, err := os.Lstat(settings)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		t.Error("settings.json is a symlink; claude could not write to it")
	}
	// The reason the tree is materialized at all: claude edits this file.
	if info.Mode().Perm()&0o200 == 0 {
		t.Errorf("settings.json is not writable (mode %v)", info.Mode().Perm())
	}
	if err := os.WriteFile(settings, []byte(`{"model":"sonnet"}`), 0o600); err != nil {
		t.Errorf("claude could not edit its own settings: %v", err)
	}
}

func TestMaterializeLinksProfileThroughToTheBuildEnv(t *testing.T) {
	result := buildResult(t, map[string]string{".claude/settings.json": "{}"})
	home := t.TempDir()
	if err := Materialize(result, home, currentUserName(t)); err != nil {
		t.Fatal(err)
	}
	target, err := os.Readlink(filepath.Join(home, profileLink))
	if err != nil {
		t.Fatal(err)
	}
	// Not a link to result/home-path: the spec directory is root's, and an
	// agent that cannot read it would get a link it cannot follow.
	if strings.Contains(target, "home-path") {
		t.Errorf("profile link points at the spec dir rather than the store: %s", target)
	}
	if _, err := os.Stat(filepath.Join(home, profileLink, "bin")); err != nil {
		t.Errorf("profile link does not resolve: %v", err)
	}
}

func TestMaterializeRemovesWhatTheSpecNoLongerDeclares(t *testing.T) {
	home := t.TempDir()
	first := buildResult(t, map[string]string{
		".claude/settings.json":       "{}",
		".claude/skills/old/SKILL.md": "gone next time",
	})
	if err := Materialize(first, home, currentUserName(t)); err != nil {
		t.Fatal(err)
	}
	second := buildResult(t, map[string]string{".claude/settings.json": "{}"})
	if err := Materialize(second, home, currentUserName(t)); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(home, ".claude/skills/old/SKILL.md")); !os.IsNotExist(err) {
		t.Error("a skill removed from the spec is still in the home")
	}
	// The emptied directories go too, or claude lists a skill directory with
	// nothing in it.
	if _, err := os.Stat(filepath.Join(home, ".claude/skills")); !os.IsNotExist(err) {
		t.Error("emptied skill directory was left behind")
	}
	if _, err := os.Stat(filepath.Join(home, ".claude/settings.json")); err != nil {
		t.Errorf("still-declared file was removed: %v", err)
	}
}

// The agent owns its home and this runs as root, so the manifest is untrusted
// input: an entry escaping the home would have root delete an arbitrary file.
func TestMaterializeIgnoresManifestEntriesOutsideTheHome(t *testing.T) {
	home := t.TempDir()
	outside := filepath.Join(t.TempDir(), "precious")
	if err := os.WriteFile(outside, []byte("do not delete"), 0o644); err != nil {
		t.Fatal(err)
	}
	escape := "../" + filepath.Base(filepath.Dir(outside)) + "/precious"
	manifest := strings.Join([]string{escape, "/etc/shadow", ".claude/gone"}, "\n")
	if err := os.WriteFile(filepath.Join(home, manifestName), []byte(manifest), 0o600); err != nil {
		t.Fatal(err)
	}

	result := buildResult(t, map[string]string{".claude/settings.json": "{}"})
	if err := Materialize(result, home, currentUserName(t)); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(outside); err != nil {
		t.Errorf("a manifest entry escaped the home and deleted %s", outside)
	}
}

func TestSafeRel(t *testing.T) {
	for _, rel := range []string{".claude/settings.json", "a", "a/b/c"} {
		if !safeRel(rel) {
			t.Errorf("safeRel(%q) = false, want true", rel)
		}
	}
	for _, rel := range []string{"", "/etc/shadow", "..", "../x", "a/../../x", "./a"} {
		if safeRel(rel) {
			t.Errorf("safeRel(%q) = true, want false", rel)
		}
	}
}
