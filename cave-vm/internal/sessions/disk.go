package sessions

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"strconv"
	"strings"
)

// Disk reads records straight out of a stopped guest image with debugfs,
// unprivileged and read-only, the way `start` already reads the system profile
// off the same disk.
//
// This is what makes a reformat safe: the one time you are certain to reach for
// it is a guest too broken to boot or to answer ssh, which is exactly when the
// ssh path silently does nothing and the disk is about to be deleted. It is
// also the more trustworthy of the two, since no guest code runs at all.
//
// Only ever with the VM stopped: reading a filesystem a live kernel is writing
// gives torn data, and the callers are `reformat` and a cold host, both of
// which have already stopped it.
type Disk struct {
	// Image is the raw disk file.
	Image string
	// ClaudeDir is the guest-side path, which the host knows only because it
	// is the host that built the guest.
	ClaudeDir string
}

func (d Disk) claude() string {
	if d.ClaudeDir == "" {
		return "/root/.claude"
	}
	return d.ClaudeDir
}

func (d Disk) projects() string { return path.Join(d.claude(), "projects") }

// entry is one line of `debugfs ls -p`: /inode/mode/uid/gid/name/size/
type entry struct {
	name string
	ino  string
	dir  bool
	size int64
}

// spec is how a directory entry is named back to debugfs: by inode, never by
// name.
//
// debugfs takes a whole command as one string and re-tokenizes it itself, so a
// name reaching that string is a name debugfs gets to interpret. A filename
// cannot contain a slash, which is what the parser below relies on, but it can
// contain a space -- and a space turns `ls -p <dir>` into an `ls` with two
// arguments, which is a usage error, on which debugfs prints its usage and
// exits 0. An empty listing and a refused one become the same thing.
//
// That is not only reachable on purpose. EncodeProject builds these names from
// a working directory, so any project whose path contains a space loses its
// history down this path -- silently, and on the one reader used when the
// guest is too broken to answer, immediately before a reformat destroys the
// disk it was reading.
//
// An inode has no spelling to get wrong.
func (e entry) spec() string { return "<" + e.ino + ">" }

func (d Disk) ls(spec string) ([]entry, error) {
	cmd := exec.Command("debugfs", "-R", "ls -p "+spec, d.Image)
	var errb bytes.Buffer
	cmd.Stderr = &errb
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("sessions: reading %s in %s: %w: %s",
			spec, d.Image, err, strings.TrimSpace(errb.String()))
	}
	// debugfs answers a command it could not parse by printing its usage and
	// exiting 0, so success is not enough to know it did anything. Without
	// this, "I could not read that" arrives as "there was nothing there",
	// which is the difference between a rescue that failed and a rescue that
	// found nothing to do.
	if bytes.Contains(out, []byte("Usage:")) || bytes.Contains(errb.Bytes(), []byte("Usage:")) {
		return nil, fmt.Errorf("sessions: debugfs could not read %s in %s: %s",
			spec, d.Image, strings.TrimSpace(errb.String()))
	}
	var entries []entry
	sc := bufio.NewScanner(bytes.NewReader(out))
	for sc.Scan() {
		f := strings.Split(sc.Text(), "/")
		// Leading and trailing empties from the delimiters, so a well-formed
		// line has eight fields. A filename cannot contain a separator, so
		// nothing here can be a path.
		if len(f) != 8 {
			continue
		}
		name := f[5]
		if name == "." || name == ".." || name == "" {
			continue
		}
		size, _ := strconv.ParseInt(f[6], 10, 64)
		entries = append(entries, entry{
			name: name,
			ino:  strings.TrimSpace(f[1]),
			dir:  strings.HasPrefix(f[2], "040"),
			size: size,
		})
	}
	return entries, sc.Err()
}

func (d Disk) List() ([]Record, error) {
	dirs, err := d.ls(d.projects())
	if err != nil {
		// A guest that never ran an agent has no projects directory, which is
		// not an error: it has no sessions.
		return nil, nil
	}
	var out []Record
	for _, pd := range dirs {
		if !pd.dir {
			continue
		}
		// By inode: pd.name came out of the image and must not go back into a
		// debugfs command.
		files, err := d.ls(pd.spec())
		if err != nil {
			continue
		}
		for _, f := range files {
			id, ok := recordID(f.name)
			if !ok || f.dir {
				continue
			}
			out = append(out, Record{ID: id, Size: f.size, Project: pd.name})
		}
	}
	return out, nil
}

// find returns the debugfs spec for a record: its inode, not its path.
//
// The path would be rebuilt out of a project name that came from the image,
// which is the thing that must not be handed back to debugfs -- the same
// reason ls walks by inode. Rebuilding it also made the dump depend on a name
// still meaning what it meant during the listing.
func (d Disk) find(id string) (string, error) {
	dirs, err := d.ls(d.projects())
	if err != nil {
		return "", err
	}
	for _, pd := range dirs {
		if !pd.dir {
			continue
		}
		files, err := d.ls(pd.spec())
		if err != nil {
			continue
		}
		for _, f := range files {
			if rid, ok := recordID(f.name); ok && !f.dir && rid == id {
				return f.spec(), nil
			}
		}
	}
	return "", fmt.Errorf("sessions: no transcript for %s in %s", id, d.Image)
}

// Read dumps one record to a temp file, because debugfs writes to a path
// rather than to a stream. The reader removes it when closed.
func (d Disk) Read(id string) (io.ReadCloser, error) {
	if !ValidID(id) {
		return nil, fmt.Errorf("sessions: %q is not a session id", id)
	}
	src, err := d.find(id)
	if err != nil {
		return nil, err
	}
	tmp, err := os.CreateTemp("", "cave-session")
	if err != nil {
		return nil, err
	}
	tmp.Close()
	cmd := exec.Command("debugfs", "-R", "dump "+src+" "+tmp.Name(), d.Image)
	var errb bytes.Buffer
	cmd.Stderr = &errb
	if err := cmd.Run(); err != nil {
		os.Remove(tmp.Name())
		return nil, fmt.Errorf("sessions: dumping %s: %w: %s",
			src, err, strings.TrimSpace(errb.String()))
	}
	f, err := os.Open(tmp.Name())
	if err != nil {
		os.Remove(tmp.Name())
		return nil, err
	}
	return tempReader{f}, nil
}

// Write is not offered. Restoring means putting a record where a *running*
// claude will look for it, and writing into a filesystem we are about to boot,
// or about to destroy, buys nothing worth the risk of doing it offline.
func (d Disk) Write(string, io.Reader, Dest, string) error {
	return fmt.Errorf("sessions: a stopped disk is read-only")
}

type tempReader struct{ f *os.File }

func (t tempReader) Read(p []byte) (int, error) { return t.f.Read(p) }

func (t tempReader) Close() error {
	err := t.f.Close()
	os.Remove(t.f.Name())
	return err
}
