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
	dir  bool
	size int64
}

func (d Disk) ls(dir string) ([]entry, error) {
	cmd := exec.Command("debugfs", "-R", "ls -p "+dir, d.Image)
	var errb bytes.Buffer
	cmd.Stderr = &errb
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("sessions: reading %s in %s: %w: %s",
			dir, d.Image, err, strings.TrimSpace(errb.String()))
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
		files, err := d.ls(path.Join(d.projects(), pd.name))
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

func (d Disk) find(id string) (string, error) {
	records, err := d.List()
	if err != nil {
		return "", err
	}
	for _, r := range records {
		if r.ID == id {
			return path.Join(d.projects(), r.Project, id+".jsonl"), nil
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
func (d Disk) Write(string, io.Reader, Dest) error {
	return fmt.Errorf("sessions: a stopped disk is read-only")
}

type tempReader struct{ f *os.File }

func (t tempReader) Read(p []byte) (int, error) { return t.f.Read(p) }

func (t tempReader) Close() error {
	err := t.f.Close()
	os.Remove(t.f.Name())
	return err
}
