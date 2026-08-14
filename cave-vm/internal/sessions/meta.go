package sessions

import (
	"bufio"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Meta is what the archive knows about a record beyond its bytes: who it
// belongs to, which working directory it came from, and when it was first and
// last seen.
//
// A file beside the record, not a database. The record has to be findable and
// readable with ls and cat whether or not anything else works, and a sqlite
// file next to it would be a second source of truth that can disagree, need
// migrating, and lock. Everything here is derivable again by re-reading a
// guest, so losing it costs metadata rather than history.
type Meta struct {
	Agent   string
	Project string
	First   time.Time
	Last    time.Time
}

func (a Archive) metaPath(id string) string { return a.Path(id) + ".meta" }

// ReadMeta returns what is known, and no error for a record that has none: an
// archive predating this file, or one restored by hand, is still a record.
func (a Archive) ReadMeta(id string) Meta {
	f, err := os.Open(a.metaPath(id))
	if err != nil {
		return Meta{}
	}
	defer f.Close()
	var m Meta
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		key, value, ok := strings.Cut(sc.Text(), "=")
		if !ok {
			continue
		}
		switch key {
		case "agent":
			m.Agent = value
		case "project":
			m.Project = value
		case "first":
			m.First = parseUnix(value)
		case "last":
			m.Last = parseUnix(value)
		}
	}
	return m
}

func parseUnix(s string) time.Time {
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return time.Time{}
	}
	return time.Unix(n, 0)
}

// WriteMeta records a sighting. First is kept from whatever is already there,
// so the archive remembers when a session appeared rather than when it was
// last touched.
func (a Archive) WriteMeta(id string, m Meta) error {
	if !ValidID(id) {
		return fmt.Errorf("sessions: %q is not a session id", id)
	}
	old := a.ReadMeta(id)
	if !old.First.IsZero() {
		m.First = old.First
	}
	if m.First.IsZero() {
		m.First = m.Last
	}
	if m.Agent == "" {
		m.Agent = old.Agent
	}
	body := fmt.Sprintf("agent=%s\nproject=%s\nfirst=%d\nlast=%d\n",
		m.Agent, m.Project, m.First.Unix(), m.Last.Unix())
	return os.WriteFile(a.metaPath(id), []byte(body), 0o600)
}

// Owned is every record belonging to an agent, which is what a bulk restore
// puts back into its picker. Records with no meta belong to nobody in
// particular and are not restored as anyone's own; they are still readable
// through the shared directory.
func (a Archive) Owned(agent string) ([]string, error) {
	records, err := a.List()
	if err != nil {
		return nil, err
	}
	var ids []string
	for _, r := range records {
		if a.ReadMeta(r.ID).Agent == agent {
			ids = append(ids, r.ID)
		}
	}
	sort.Strings(ids)
	return ids, nil
}
