// Package projects is the registry of what the guest can clone.
//
// The registry is the served directory itself: <state>/repos/<slug>.git is a
// symlink into that project's own shared bare repo, so registering is creating
// a link and listing is reading them back. There is no second list to fall out
// of step with the filesystem, which is the failure a registry file invites.
//
// This was shell, and the two bugs it grew are both about a link that points
// at nothing. Reading a registration with `readlink -f` yields the empty
// string when the target is gone, so a moved project listed with a blank
// second column and was handed to the git daemon anyway -- which answered the
// guest with "repository not found", indistinguishable from never having
// registered it. And `ln -sfn` forces, so two checkouts whose directories
// share a basename silently took the name from each other.
//
// Both are questions about the difference between "where this points" and
// "what is there", which is why Target and Present are separate fields rather
// than one resolved path.
package projects

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// A Project is one registration.
type Project struct {
	// Slug is the name the guest clones by: git://127.0.0.1/<slug>.git.
	Slug string
	// Target is where the registration points, whether or not anything is
	// there. Read with readlink rather than resolved, because a link whose
	// target has gone still records where it pointed, and that path is the
	// useful half of the report -- it is what you need to re-register it.
	Target string
	// Present is whether the target is a directory now.
	Present bool
}

// Dir is where registrations live for a state directory.
func Dir(state string) string { return filepath.Join(state, "repos") }

// List reads the registrations back, in name order.
//
// Only symlinks count. A real directory sitting there is not something this
// created, and serving it would be serving whatever somebody happened to put
// in the state directory.
func List(dir string) ([]Project, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			// No registrations yet is an answer. A host that has never run
			// `init` should not have status complain at it.
			return nil, nil
		}
		return nil, err
	}
	var out []Project
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".git") {
			continue
		}
		full := filepath.Join(dir, name)
		// Lstat, so the link itself is what is examined rather than what it
		// points at: a dangling one must still be found.
		st, err := os.Lstat(full)
		if err != nil || st.Mode()&os.ModeSymlink == 0 {
			continue
		}
		target, err := os.Readlink(full)
		if err != nil {
			continue
		}
		out = append(out, Project{
			Slug:    strings.TrimSuffix(name, ".git"),
			Target:  target,
			Present: isDir(target),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Slug < out[j].Slug })
	return out, nil
}

// Link is the path of a registration, which is what the guest asks for by
// name and what git daemon has to be given alongside the target.
func (p Project) Link(dir string) string { return filepath.Join(dir, p.Slug+".git") }

// ServePaths is what the git daemon may serve: each registration and what its
// link resolves to.
//
// Both, because git daemon compares the resolved path against what it was
// given -- list only the link and every request is refused, list only the
// target and the guest cannot ask for it by project name.
//
// A registration whose target has moved is left out rather than handed over
// dead, and returned separately so the caller can say which and why. Serving
// it would answer the guest with "repository not found", which from inside is
// exactly what never having registered the project looks like.
func ServePaths(dir string) (serve []string, missing []Project, err error) {
	all, err := List(dir)
	if err != nil {
		return nil, nil, err
	}
	for _, p := range all {
		if !p.Present {
			missing = append(missing, p)
			continue
		}
		serve = append(serve, p.Link(dir), p.Target)
	}
	return serve, missing, nil
}

func isDir(path string) bool {
	if path == "" {
		return false
	}
	st, err := os.Stat(path)
	return err == nil && st.IsDir()
}
