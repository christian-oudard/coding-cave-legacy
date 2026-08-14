package sessions

import (
	"fmt"
	"os"
)

// Change is one record's fate in a checkpoint.
type Change struct {
	ID      string
	Outcome Outcome
	Err     error
}

// Checkpoint copies out every record the host does not have, or whose length
// has grown. Equal lengths are skipped, and a guest record shorter than the
// host's is fetched anyway so that Commit can file it under diverged/ rather
// than lose the evidence that something rewrote it.
//
// The host names everything: ids that are not session ids are discarded here,
// and every path on both sides is constructed from an id that survived that
// check. Nothing the guest says becomes a path.
//
// One failure does not stop the rest. A checkpoint runs at moments that matter
// (an agent exiting, a reformat about to destroy the disk), so getting eight
// records out of ten beats getting none.
func Checkpoint(g Guest, a Archive) ([]Change, error) {
	guestRecords, err := g.List()
	if err != nil {
		return nil, err
	}
	held := map[string]int64{}
	hostRecords, err := a.List()
	if err != nil {
		return nil, err
	}
	for _, r := range hostRecords {
		held[r.ID] = r.Size
	}

	var changes []Change
	for _, r := range guestRecords {
		if !ValidID(r.ID) {
			continue
		}
		if size, ok := held[r.ID]; ok && size == r.Size {
			continue
		}
		changes = append(changes, copyOut(g, a, r.ID))
	}
	return changes, nil
}

func copyOut(g Guest, a Archive, id string) Change {
	rc, err := g.Read(id)
	if err != nil {
		return Change{ID: id, Err: err}
	}
	staged, err := a.Stage(id, rc)
	// Close reports a fetch that died partway, which must not be committed:
	// a truncated copy is exactly what the checks in Commit exist to catch,
	// but there is no reason to make them catch our own.
	if cerr := rc.Close(); err == nil && cerr != nil {
		err = cerr
		os.Remove(staged)
		staged = ""
	}
	if err != nil {
		return Change{ID: id, Err: err}
	}
	outcome, err := a.Commit(id, staged)
	return Change{ID: id, Outcome: outcome, Err: err}
}

// Restore puts records into a guest that does not have them, which is what a
// fresh or reformatted guest needs before claude can offer them to `/resume`.
// The guest decides where they land, from the cwd the session will run in;
// the host only says which ids.
//
// Records the guest already has are left alone, because the guest's copy is
// the live one and the archive's is a checkpoint of it.
func Restore(g Guest, a Archive, ids []string) ([]Change, error) {
	have := map[string]bool{}
	guestRecords, err := g.List()
	if err != nil {
		return nil, err
	}
	for _, r := range guestRecords {
		have[r.ID] = true
	}

	var changes []Change
	for _, id := range ids {
		if !ValidID(id) {
			changes = append(changes, Change{ID: id, Err: fmt.Errorf("not a session id")})
			continue
		}
		if have[id] {
			changes = append(changes, Change{ID: id, Outcome: Unchanged})
			continue
		}
		f, err := os.Open(a.Path(id))
		if err != nil {
			changes = append(changes, Change{ID: id, Err: err})
			continue
		}
		err = g.Write(id, f)
		f.Close()
		changes = append(changes, Change{ID: id, Outcome: Added, Err: err})
	}
	return changes, nil
}
