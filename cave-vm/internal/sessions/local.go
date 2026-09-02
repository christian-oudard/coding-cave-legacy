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
	// SharedDir holds every agent's records, for reading rather than
	// resuming. Deliberately outside ClaudeDir so claude never scans it: an
	// agent should be able to read another's session without being offered it
	// in its own picker.
	SharedDir string
	// Agent is whose home this is, stamped onto every record it reports. A
	// Local is one agent's transcripts, because a home is: the guest holds one
	// of these per agent, and which agent a record belongs to is answered by
	// which of them found it.
	Agent string
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
			out = append(out, Record{
				ID: id, Size: info.Size(), Project: d.Name(), Agent: l.Agent,
			})
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

// Write puts a record into the guest: where claude will look for it when
// resuming in Cwd, or into the shared directory, which is for reading only.
//
// Resumable refuses to overwrite, because a restore is for a session the guest
// does not have and clobbering a live transcript with an older copy of itself
// would lose whatever the agent has done since. Shared replaces freely: it is
// a copy of the host's record and the host's record is the truth.
//
// The agent is ignored: a Local is one agent's home, so writing to this one
// writes to that agent by construction. Choosing between homes belongs to
// whoever holds several of them, which in the guest is the daemon.
func (l Local) Write(id string, r io.Reader, dest Dest, _ string) error {
	if !ValidID(id) {
		return fmt.Errorf("sessions: %q is not a session id", id)
	}
	if dest == Shared {
		if l.SharedDir == "" {
			return fmt.Errorf("sessions: no shared directory configured")
		}
		if err := os.MkdirAll(l.SharedDir, 0o755); err != nil {
			return err
		}
		// Readable by every agent, which is the entire point of this
		// directory and was the one thing it did not do. The records were
		// written 0600 and owned by root, like a home's are, so the view whose
		// purpose is "an agent can read another's session" answered permission
		// denied to every agent that was not root -- and denied it quietly,
		// since nothing reads this until somebody goes looking.
		//
		// Inside the guest that is the whole population: agents are colleagues
		// and the boundary that means anything is the VM's, so there is nobody
		// here to keep it from.
		return writeFile(filepath.Join(l.SharedDir, id+".jsonl"), r, true, 0o644)
	}
	if l.Cwd == "" {
		return fmt.Errorf("sessions: no cwd, so there is no project to restore into")
	}
	dir := filepath.Join(l.projects(), EncodeProject(l.Cwd))
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return writeFile(filepath.Join(dir, id+".jsonl"), r, false, 0o600)
}

// perm is the mode a new file gets: a record in an agent's own home is that
// agent's business, one in the shared view is everybody's.
func writeFile(dest string, r io.Reader, replace bool, perm os.FileMode) error {
	flags := os.O_WRONLY | os.O_CREATE | os.O_EXCL
	if replace {
		flags = os.O_WRONLY | os.O_CREATE | os.O_TRUNC
	}
	f, err := os.OpenFile(dest, flags, perm)
	if err != nil {
		return err
	}
	defer f.Close()
	// Open only applies the mode to a file it creates, so a record already
	// there keeps whatever it was written with. That is the case that matters
	// here: the shared view is full of copies made before it was readable, and
	// rewriting them without this would leave every one of them unreadable.
	if err := f.Chmod(perm); err != nil {
		return err
	}
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
