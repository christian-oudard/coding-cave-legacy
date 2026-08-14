package sessions

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Archive is the host's record: a flat directory of <uuid>.jsonl, and nothing
// else. Flat and plain, so a record is findable with ls and readable with cat
// whether or not the registry, or this code, is working. Versioning and
// retention are the business of whatever backs up the data dir.
type Archive struct{ Dir string }

// divergedDir holds copies that failed the checks below. Deliberately not
// beside the records: a name like <uuid>.1.jsonl is not a session id, so
// restoring one would produce a transcript claude cannot resume.
const divergedDir = "diverged"

func (a Archive) Path(id string) string { return filepath.Join(a.Dir, id+".jsonl") }

// List is what the host holds, which is the other half of deciding what to
// fetch.
func (a Archive) List() ([]Record, error) {
	files, err := os.ReadDir(a.Dir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []Record
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
	return out, nil
}

func (a Archive) size(id string) (int64, bool) {
	st, err := os.Stat(a.Path(id))
	if err != nil {
		return 0, false
	}
	return st.Size(), true
}

// Stage writes a fetched copy beside the archive, without touching the record.
// Separate from Commit because a fetch can fail halfway: only a copy that
// arrived whole is worth comparing, let alone keeping.
func (a Archive) Stage(id string, r io.Reader) (string, error) {
	if !ValidID(id) {
		return "", fmt.Errorf("sessions: %q is not a session id", id)
	}
	if err := os.MkdirAll(a.Dir, 0o700); err != nil {
		return "", err
	}
	f, err := os.CreateTemp(a.Dir, "."+id+".*")
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err := io.Copy(f, r); err != nil {
		os.Remove(f.Name())
		return "", err
	}
	return f.Name(), nil
}

// Outcome is what happened to one record, which is what a checkpoint reports.
type Outcome int

const (
	Added     Outcome = iota // the host had no record under this id
	Grew                     // longer than what the host held, and starts with it
	Unchanged                // nothing to do
	Diverged                 // shorter, or a different prefix: kept, not applied
)

func (o Outcome) String() string {
	switch o {
	case Added:
		return "added"
	case Grew:
		return "grew"
	case Unchanged:
		return "unchanged"
	default:
		return "diverged"
	}
}

// Commit moves a staged copy into place, but only if it can only be an
// extension of the record already held: at least as long, and starting with
// the same bytes. Both checks are local, because the whole file is already in
// hand, so the prefix check costs nothing extra and catches what length alone
// misses, a record truncated and then grown past its old size.
//
// Anything else is filed under diverged/ rather than dropped or applied. A
// guest that rewrites its history, or corrupts it, then costs disk instead of
// the record.
func (a Archive) Commit(id, staged string) (Outcome, error) {
	old, exists := a.size(id)
	if !exists {
		return Added, os.Rename(staged, a.Path(id))
	}
	st, err := os.Stat(staged)
	if err != nil {
		return Diverged, err
	}
	switch {
	case st.Size() < old:
		return Diverged, a.setAside(id, staged)
	case !prefixMatches(a.Path(id), staged, old):
		return Diverged, a.setAside(id, staged)
	case st.Size() == old:
		return Unchanged, os.Remove(staged)
	default:
		return Grew, os.Rename(staged, a.Path(id))
	}
}

func (a Archive) setAside(id, staged string) error {
	dir := filepath.Join(a.Dir, divergedDir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	for n := 1; ; n++ {
		p := filepath.Join(dir, fmt.Sprintf("%s.%d.jsonl", id, n))
		if _, err := os.Stat(p); os.IsNotExist(err) {
			return os.Rename(staged, p)
		}
	}
}

// prefixMatches compares the first n bytes of two files, in chunks, since a
// transcript can be large and there is no reason to hold one in memory.
func prefixMatches(a, b string, n int64) bool {
	fa, err := os.Open(a)
	if err != nil {
		return false
	}
	defer fa.Close()
	fb, err := os.Open(b)
	if err != nil {
		return false
	}
	defer fb.Close()

	bufA := make([]byte, 64<<10)
	bufB := make([]byte, 64<<10)
	for n > 0 {
		size := int64(len(bufA))
		if n < size {
			size = n
		}
		ra, errA := io.ReadFull(fa, bufA[:size])
		rb, errB := io.ReadFull(fb, bufB[:size])
		if errA != nil || errB != nil || ra != rb {
			return false
		}
		if !bytes.Equal(bufA[:ra], bufB[:rb]) {
			return false
		}
		n -= size
	}
	return true
}
