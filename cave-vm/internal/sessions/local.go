package sessions

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Local is the guest side: claude's own transcripts, under
// <claude>/projects/<encoded-cwd>/<uuid>.jsonl. It implements Guest so the
// copying logic can be exercised against a real directory rather than a fake.
//
// One claude can `/resume` many sessions and append to any of them, and
// background jobs create transcripts nobody predicted, so this scans and never
// assumes it knows which ids exist.
type Local struct {
	// ClaudeDir is the guest's ~/.claude.
	ClaudeDir string
	// Cwd is where a restored session will be resumed, and decides the
	// project directory a written record lands in. Only the guest knows it.
	Cwd string
}

func (l Local) projects() string { return filepath.Join(l.ClaudeDir, "projects") }

// List reports every transcript under every project directory. Files whose
// stem is not a session id are skipped: claude keeps other things there, and
// anything that cannot be a session cannot be a record.
func (l Local) List() ([]Record, error) {
	dirs, err := os.ReadDir(l.projects())
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []Record
	for _, d := range dirs {
		if !d.IsDir() {
			continue
		}
		files, err := os.ReadDir(filepath.Join(l.projects(), d.Name()))
		if err != nil {
			continue
		}
		for _, f := range files {
			id, ok := recordID(f.Name())
			if !ok {
				continue
			}
			info, err := f.Info()
			if err != nil {
				continue
			}
			out = append(out, Record{ID: id, Size: info.Size()})
		}
	}
	return out, nil
}

// find locates a transcript by id across the project directories, since the
// same session can only exist in one of them but the caller does not know
// which.
func (l Local) find(id string) (string, error) {
	if !ValidID(id) {
		return "", fmt.Errorf("sessions: %q is not a session id", id)
	}
	dirs, err := os.ReadDir(l.projects())
	if err != nil {
		return "", err
	}
	for _, d := range dirs {
		p := filepath.Join(l.projects(), d.Name(), id+".jsonl")
		if st, err := os.Stat(p); err == nil && st.Mode().IsRegular() {
			return p, nil
		}
	}
	return "", fmt.Errorf("sessions: no transcript for %s", id)
}

func (l Local) Read(id string) (io.ReadCloser, error) {
	p, err := l.find(id)
	if err != nil {
		return nil, err
	}
	return os.Open(p)
}

// Write puts a record where claude will look for it when resuming in Cwd. It
// refuses to overwrite: a restore is for a session the guest does not have, and
// clobbering a live transcript with an older copy of itself would lose whatever
// the agent has done since.
func (l Local) Write(id string, r io.Reader) error {
	if !ValidID(id) {
		return fmt.Errorf("sessions: %q is not a session id", id)
	}
	if l.Cwd == "" {
		return fmt.Errorf("sessions: no cwd, so there is no project to restore into")
	}
	dir := filepath.Join(l.projects(), EncodeProject(l.Cwd))
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	dest := filepath.Join(dir, id+".jsonl")
	f, err := os.OpenFile(dest, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, r)
	return err
}

// recordID accepts a filename only if it is a session id with the jsonl
// extension, which is the same rule that makes the stem the id.
func recordID(name string) (string, bool) {
	const ext = ".jsonl"
	if len(name) <= len(ext) || name[len(name)-len(ext):] != ext {
		return "", false
	}
	id := name[:len(name)-len(ext)]
	return id, ValidID(id)
}
