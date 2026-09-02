package projects

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// world builds a registry directory the way `vmcav init` leaves one.
func world(t *testing.T) (dir string, repo func(name string) string) {
	t.Helper()
	root := t.TempDir()
	dir = filepath.Join(root, "repos")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	return dir, func(name string) string {
		p := filepath.Join(root, name, ".coding-cave", "cave-shared.git")
		if err := os.MkdirAll(p, 0o755); err != nil {
			t.Fatal(err)
		}
		return p
	}
}

func register(t *testing.T, dir, slug, target string) {
	t.Helper()
	if err := os.Symlink(target, filepath.Join(dir, slug+".git")); err != nil {
		t.Fatal(err)
	}
}

func TestListReadsRegistrationsInOrder(t *testing.T) {
	dir, repo := world(t)
	beta, alpha := repo("beta"), repo("alpha")
	register(t, dir, "beta", beta)
	register(t, dir, "alpha", alpha)

	got, err := List(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].Slug != "alpha" || got[1].Slug != "beta" {
		t.Fatalf("got %+v", got)
	}
	if !got[0].Present || got[0].Target != alpha {
		t.Errorf("alpha: %+v", got[0])
	}
}

// The bug this package exists for. A registration is a symlink into the
// project's own checkout, so moving or deleting that checkout leaves it
// pointing at nothing. `readlink -f` yields the empty string there, so the
// shell printed the slug with a blank second column -- and the useful half,
// where it used to point, is exactly what you need to re-register it.
func TestAMovedProjectKeepsSayingWhereItPointed(t *testing.T) {
	dir, repo := world(t)
	gone := repo("gone")
	register(t, dir, "gone", gone)
	if err := os.RemoveAll(filepath.Dir(filepath.Dir(gone))); err != nil {
		t.Fatal(err)
	}

	got, err := List(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("a dangling registration disappeared: %+v", got)
	}
	if got[0].Present {
		t.Error("a target that is gone was reported present")
	}
	if got[0].Target != gone {
		t.Errorf("lost where it pointed: %q, want %q", got[0].Target, gone)
	}
}

// And it is kept out of what the daemon is told to serve. A dead path there
// answers the guest with "repository not found", which reads exactly like
// never having registered the project at all.
func TestServePathsLeavesOutWhatIsNotThere(t *testing.T) {
	dir, repo := world(t)
	live, dead := repo("live"), repo("dead")
	register(t, dir, "live", live)
	register(t, dir, "dead", dead)
	if err := os.RemoveAll(filepath.Dir(filepath.Dir(dead))); err != nil {
		t.Fatal(err)
	}

	serve, missing, err := ServePaths(dir)
	if err != nil {
		t.Fatal(err)
	}
	// Both the link and the target, because git daemon compares the resolved
	// path against what it was given.
	want := []string{filepath.Join(dir, "live.git"), live}
	if len(serve) != 2 || serve[0] != want[0] || serve[1] != want[1] {
		t.Errorf("serve = %q, want %q", serve, want)
	}
	if len(missing) != 1 || missing[0].Slug != "dead" {
		t.Errorf("missing = %+v, want the dead one named", missing)
	}
	for _, p := range serve {
		if p == dead {
			t.Error("a dead target was handed to the daemon")
		}
	}
}

// A real directory is not a registration. Serving it would mean serving
// whatever happened to be in the state directory.
func TestOnlySymlinksAreRegistrations(t *testing.T) {
	dir, _ := world(t)
	if err := os.MkdirAll(filepath.Join(dir, "notalink.git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "notes"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := List(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Errorf("got %+v, want nothing", got)
	}
}

// A host that has never run `init` has no registry directory, and that is an
// answer rather than a failure: status runs on every command and must not
// complain at a fresh host.
func TestNoRegistryDirectoryIsNotAnError(t *testing.T) {
	got, err := List(filepath.Join(t.TempDir(), "never-made"))
	if err != nil || got != nil {
		t.Errorf("got %+v, %v", got, err)
	}
}

func writeHook(t *testing.T, target, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(target, "hooks"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(target, "hooks", "pre-receive"), []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestGuardTellsOursFromAnotherToolsFromNone(t *testing.T) {
	dir, repo := world(t)

	ours := repo("ours")
	writeHook(t, ours, "#!/bin/sh\nexec cave-vm-git-pre-receive \"$@\"\n")
	register(t, dir, "ours", ours)

	foreign := repo("foreign")
	writeHook(t, foreign, "#!/bin/sh\nexec some-other-tool \"$@\"\n")
	register(t, dir, "foreign", foreign)

	none := repo("none")
	register(t, dir, "none", none)

	all, err := List(dir)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]Guard{}
	for _, p := range all {
		got[p.Slug] = p.Guard()
	}
	for slug, want := range map[string]Guard{
		"ours": GuardOurs, "foreign": GuardForeign, "none": GuardMissing,
	} {
		if got[slug] != want {
			t.Errorf("%s: got %v, want %v", slug, got[slug], want)
		}
	}
}

// A registration pointing at nothing has no guard because it has no
// repository. The shell could not say that: it resolved the dead link to the
// empty string, looked for a hook under "/hooks/pre-receive", found none, and
// reported a missing branch guard on a project that had simply moved.
func TestAMovedProjectIsNotAMissingGuard(t *testing.T) {
	dir, repo := world(t)
	gone := repo("gone")
	writeHook(t, gone, "#!/bin/sh\nexec cave-vm-git-pre-receive \"$@\"\n")
	register(t, dir, "gone", gone)
	if err := os.RemoveAll(filepath.Dir(filepath.Dir(gone))); err != nil {
		t.Fatal(err)
	}

	all, err := List(dir)
	if err != nil {
		t.Fatal(err)
	}
	if g := all[0].Guard(); g != GuardNoProject {
		t.Errorf("got %v, want GuardNoProject", g)
	}
	// And the hint still names a directory rather than the shared repo inside
	// it, which is what `vmcav init` takes.
	if d := all[0].CheckoutDir(); !strings.HasSuffix(d, "gone") {
		t.Errorf("CheckoutDir = %q, want the project directory", d)
	}
}
