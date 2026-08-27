package userenv

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/user"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// manifestName records which paths in a home this package put there, so that
// the next materialize can take away what the spec no longer declares.
//
// Without it, deleting a skill from cave.nix leaves the skill in every home
// that ever had it, and claude goes on loading it. `main` has that wart and
// gets away with it because a cave home is cheap to discard; a guest home
// persists across every reformat the design promises to survive.
const manifestName = ".cave-env-manifest"

// profileLink is where the environment's merged package set lands. Named for
// what it is rather than .nix-profile, which is nix's own per-user profile and
// is managed by `nix profile`: colliding with it would make `nix profile list`
// report an environment nobody installed that way, and the next `nix profile`
// command would fight this one.
const profileLink = ".cave-profile"

// Materialize copies a built environment into one agent's home.
//
// Real files, not symlinks into the store, and writable by the agent. The
// store here is persistent -- unlike `main`, where a cave's store overlay
// disappears -- so dangling links are not the reason. The reason is that
// claude edits its own settings.json when you use `/model` or `/config`, and a
// read-only store symlink turns an ordinary interaction into an error. The
// declared value comes back at the next build, which is the same bargain
// `main` strikes.
//
// Everything it writes is inside home. It runs as root out of seed-agent-home@
// and the manifest it reads back is a file the agent itself can edit, so every
// path from that file is checked before it is used rather than trusted.
func Materialize(result, home, username string) error {
	uid, gid, err := lookupUser(username)
	if err != nil {
		return err
	}
	src := filepath.Join(result, "files")
	if _, err := os.Stat(src); err != nil {
		return fmt.Errorf("no files/ in %s: %w", result, err)
	}

	written := map[string]bool{}
	err = filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return nil
		}
		dest := filepath.Join(home, rel)
		if d.IsDir() {
			// 0700: an agent's home is its own. Another agent cannot read it,
			// and the root agent can, which is the blast-radius model the
			// guest already has rather than a wall between colleagues.
			if err := os.MkdirAll(dest, 0o700); err != nil {
				return err
			}
			return os.Chown(dest, uid, gid)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		written[rel] = true
		return copyFile(path, dest, info.Mode(), uid, gid)
	})
	if err != nil {
		return err
	}

	if err := pruneStale(home, written, uid, gid); err != nil {
		return err
	}
	if err := writeManifest(home, written, uid, gid); err != nil {
		return err
	}
	return linkProfile(result, home, uid, gid)
}

// copyFile writes one file as a real, writable copy. The source is in the
// store, where nix has made everything read-only (0444/0555), so the mode is
// derived from the executable bit rather than carried across: mkCaveEnv's
// rsync --chmod cannot survive nix hardening the output path afterwards.
func copyFile(src, dest string, mode fs.FileMode, uid, gid int) error {
	perm := os.FileMode(0o600)
	if mode&0o111 != 0 {
		perm = 0o700
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	// Written to a temporary and renamed, so a file claude is reading is never
	// observed half-written, and so an existing read-only file (one a previous
	// version of this code symlinked into the store) is replaced rather than
	// reported as permission denied.
	tmp := dest + ".cave-tmp"
	os.Remove(tmp)
	out, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, perm)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if err := out.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Chown(tmp, uid, gid); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, dest)
}

// pruneStale removes what a previous materialize wrote and this one did not.
func pruneStale(home string, written map[string]bool, uid, gid int) error {
	previous, err := readManifest(home)
	if err != nil {
		return err
	}
	var dirs []string
	for _, rel := range previous {
		if written[rel] {
			continue
		}
		if err := os.Remove(filepath.Join(home, rel)); err != nil && !os.IsNotExist(err) {
			return err
		}
		dirs = append(dirs, filepath.Dir(rel))
	}
	// Deepest first, so a directory emptied by removing its subdirectory is
	// still reachable when its own turn comes.
	sort.Sort(sort.Reverse(sort.StringSlice(dirs)))
	for _, dir := range dirs {
		for dir != "." && dir != "/" {
			// Only ever if empty: os.Remove on a non-empty directory fails,
			// which is exactly the guard wanted. A directory the agent has put
			// its own files in stays.
			if err := os.Remove(filepath.Join(home, dir)); err != nil {
				break
			}
			dir = filepath.Dir(dir)
		}
	}
	return nil
}

// readManifest returns the paths a previous run recorded, dropping any that
// are not a plain relative path inside the home.
//
// This runs as root and the file belongs to the agent, so an entry of
// `../../etc/shadow` is a thing that can be written, and pruneStale would
// delete it. Filtering here rather than at the delete keeps the check in one
// place and makes the manifest's contract -- relative, inside the home -- the
// thing being enforced.
func readManifest(home string) ([]string, error) {
	b, err := os.ReadFile(filepath.Join(home, manifestName))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var out []string
	for _, line := range strings.Split(string(b), "\n") {
		rel := strings.TrimSpace(line)
		if rel == "" || rel == manifestName {
			continue
		}
		if !safeRel(rel) {
			continue
		}
		out = append(out, rel)
	}
	return out, nil
}

// safeRel reports whether a manifest entry names something inside the home.
func safeRel(rel string) bool {
	if rel == "" || filepath.IsAbs(rel) {
		return false
	}
	clean := filepath.Clean(rel)
	if clean != rel || clean == "." {
		return false
	}
	return clean != ".." && !strings.HasPrefix(clean, "../")
}

func writeManifest(home string, written map[string]bool, uid, gid int) error {
	names := make([]string, 0, len(written))
	for rel := range written {
		names = append(names, rel)
	}
	sort.Strings(names)
	path := filepath.Join(home, manifestName)
	if err := os.WriteFile(path, []byte(strings.Join(names, "\n")+"\n"), 0o600); err != nil {
		return err
	}
	return os.Chown(path, uid, gid)
}

// linkProfile points ~/.cave-profile at the environment's package set. Through
// to the buildEnv rather than at result/home-path, because the spec directory
// is root's and an agent that could not read it would get a link it cannot
// follow, which fails at exec time as "no such file".
func linkProfile(result, home string, uid, gid int) error {
	target, err := filepath.EvalSymlinks(filepath.Join(result, "home-path"))
	if err != nil {
		return err
	}
	link := filepath.Join(home, profileLink)
	tmp := link + ".cave-tmp"
	os.Remove(tmp)
	if err := os.Symlink(target, tmp); err != nil {
		return err
	}
	if err := os.Lchown(tmp, uid, gid); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, link)
}

// lookupUser resolves a name to ids. "root" is answered without consulting
// anything: it is the one account whose ids are fixed by definition, and the
// root agent's session must not depend on /etc/passwd being parseable.
func lookupUser(name string) (int, int, error) {
	if name == "root" {
		return 0, 0, nil
	}
	u, err := user.Lookup(name)
	if err != nil {
		return 0, 0, fmt.Errorf("no such user %q: %w", name, err)
	}
	uid, err := strconv.Atoi(u.Uid)
	if err != nil {
		return 0, 0, errors.New("non-numeric uid for " + name)
	}
	gid, err := strconv.Atoi(u.Gid)
	if err != nil {
		return 0, 0, errors.New("non-numeric gid for " + name)
	}
	return uid, gid, nil
}
