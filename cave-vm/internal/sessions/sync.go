package sessions

import (
	"fmt"
	"os"
	"time"
)

// Change is one record's fate in a sync.
type Change struct {
	ID      string
	Outcome Outcome
	Err     error
}

// Sync copies out every record the host does not have, or whose length has
// grown. Equal lengths are skipped, and a guest record shorter than the host's
// is fetched anyway so that Commit can file it under diverged/ rather than lose
// the evidence that something rewrote it.
//
// Append-only, not a checkpoint: there are no versions and nothing rolls back.
// A record only ever gains bytes, which is what a transcript does, and anything
// that is not that is set aside rather than applied.
//
// The host names everything: ids that are not session ids are discarded here,
// and every path on both sides is constructed from an id that survived that
// check. Nothing the guest says becomes a path.
//
// One failure does not stop the rest. A sync runs at moments that matter (an
// agent exiting, a reformat about to destroy the disk), so getting eight
// records out of ten beats getting none.
// The agent is no longer a parameter. It used to be one, and the host passed
// the same value for every record it copied, so a sync labelled whatever it
// found with whichever agent it had been told about -- and since the guest
// returned one home's transcripts however many agents it held, that label was
// wrong and the other agents' records were never copied at all. Which agent a
// record belongs to is a fact about where it lives, and only the guest can see
// that, so the guest says it.
func Sync(g Guest, a Archive) ([]Change, error) {
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
		c := copyOut(g, a, r.ID)
		if c.Err == nil && c.Outcome != Diverged {
			// Metadata follows the bytes, so a record that failed to copy
			// does not get an entry claiming it is here.
			if err := a.WriteMeta(r.ID, Meta{
				Agent:   r.Agent,
				Project: r.Project,
				Last:    time.Now(),
			}); err != nil {
				c.Err = err
			}
		}
		changes = append(changes, c)
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
// the live one and the archive's is a copy of it.
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
		changes = append(changes, place(g, a, id, Resumable))
	}
	return changes, nil
}

// Share copies every record the archive holds into the guest's shared
// directory, which claude does not scan. That is how an agent reads another
// agent's session without being offered it in its own picker, and it is a
// whole-archive copy because the archive is the truth and the guest's copy of
// it is disposable.
func Share(g Guest, a Archive) ([]Change, error) {
	records, err := a.List()
	if err != nil {
		return nil, err
	}
	var changes []Change
	for _, r := range records {
		changes = append(changes, place(g, a, r.ID, Shared))
	}
	return changes, nil
}

// place puts a record back into the guest, into the home of whichever agent
// the archive says wrote it. The archive is the only thing that still knows:
// the guest lost the record, which is why it is being restored.
func place(g Guest, a Archive, id string, dest Dest) Change {
	f, err := os.Open(a.Path(id))
	if err != nil {
		return Change{ID: id, Err: err}
	}
	defer f.Close()
	agent := ""
	if dest == Resumable {
		agent = a.ReadMeta(id).Agent
	}
	if err := g.Write(id, f, dest, agent); err != nil {
		return Change{ID: id, Err: err}
	}
	return Change{ID: id, Outcome: Added}
}
