// Package registry assigns and tracks agent ids. The single-VM bootstrap
// does not yet need extra projects, an archived display range, or
// session-log mtime exit estimates, so those are left out.
//
// An agent id is the lowest free positive integer. A slot is reclaimable
// once its agent has been gone longer than ReclaimableAfterSecs, at which
// point the next Acquire reuses the number rather than minting a higher one,
// so ids do not drift upward. On reuse the previous occupant's git refs are
// wiped (branches are interface, not history), via the injected Wipe hook.
//
// State is a JSON file guarded by an advisory (flock) lock, so concurrent
// acquirers on the same file cannot collide. Liveness and the clock are
// injectable: the reclaim logic keys off whether an agent's recorded pid is
// still alive, and in the VM that pid lives guest-side, so the caller
// decides how to check it.
package registry

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

// ReclaimableAfterSecs is how long after an agent exits its id may be reused.
const ReclaimableAfterSecs = 24 * 60 * 60

// Agent is one row of the registry.
type Agent struct {
	ID        string   `json:"id"`
	UUID      string   `json:"uuid"`
	Project   string   `json:"project"`
	HostPID   int      `json:"host_pid,omitempty"`
	StartedAt float64  `json:"started_at"`
	ExitedAt  *float64 `json:"exited_at,omitempty"`
	SessionID string   `json:"session_id,omitempty"`
	// Root marks the single agent per VM that runs as root. Every agent gets
	// a normal id; root is a property, not a reserved number. The registry
	// enforces at most one running root agent.
	Root bool `json:"root,omitempty"`
}

// activeRoot returns the running root agent, or nil. "Running" means not
// exited; sweepDead has already retired dead pids to exited by the time this
// is consulted inside a locked section.
func activeRoot(s *state) *Agent {
	for i := range s.Agents {
		if s.Agents[i].Root && s.Agents[i].ExitedAt == nil {
			return &s.Agents[i]
		}
	}
	return nil
}

// Registry is the agent table backed by a JSON file at Path.
type Registry struct {
	Path string
	// Now returns the current unix time in seconds; nil uses the wall clock.
	Now func() float64
	// Alive reports whether the recorded pid is still running; nil uses a
	// real signal-0 probe (only correct when pids are host-local).
	Alive func(pid int) bool
	// Wipe, if set, is called after an id is reclaimed for a new agent, with
	// the previous occupant's project slug and the reused id, so the caller
	// can delete that id's stale git refs.
	Wipe func(project, id string)
}

// New returns a Registry at path, creating its parent directory.
func New(path string) (*Registry, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	return &Registry{Path: path}, nil
}

func (r *Registry) now() float64 {
	if r.Now != nil {
		return r.Now()
	}
	return float64(time.Now().UnixNano()) / 1e9
}

func (r *Registry) alive(pid int) bool {
	if r.Alive != nil {
		return r.Alive(pid)
	}
	if pid <= 0 {
		return false
	}
	// signal 0: no signal sent, but errors report reachability.
	err := syscall.Kill(pid, 0)
	return err == nil || err == syscall.EPERM
}

// --- Store: JSON file under an flock ---

type state struct {
	Agents []Agent `json:"agents"`
}

func (r *Registry) withLock(fn func(*state) (mutated bool, err error)) error {
	f, err := os.OpenFile(r.Path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)

	data, err := io.ReadAll(f)
	if err != nil {
		return err
	}
	var s state
	if len(data) > 0 {
		if err := json.Unmarshal(data, &s); err != nil {
			return err
		}
	}
	mutated, err := fn(&s)
	if err != nil || !mutated {
		return err
	}
	out, err := json.MarshalIndent(&s, "", "  ")
	if err != nil {
		return err
	}
	if err := f.Truncate(0); err != nil {
		return err
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	_, err = f.Write(out)
	return err
}

func find(s *state, id string) *Agent {
	for i := range s.Agents {
		if s.Agents[i].ID == id {
			return &s.Agents[i]
		}
	}
	return nil
}

// sweepDead marks agents whose recorded pid is gone as exited. We stamp the
// exit at now rather than estimating it from the session-log mtime, which
// only delays a reclaim (never loses a still-live agent), fine for bootstrap.
func (r *Registry) sweepDead(s *state, now float64) bool {
	changed := false
	for i := range s.Agents {
		a := &s.Agents[i]
		if a.ExitedAt == nil && a.HostPID != 0 && !r.alive(a.HostPID) {
			t := now
			a.ExitedAt = &t
			a.HostPID = 0
			changed = true
		}
	}
	return changed
}

// --- Agent lifecycle ---

// Acquire assigns the lowest free id to a new agent for project (a slug) and
// returns it. With root set, the agent is the VM's root agent; Acquire fails
// if another root agent is already running (at most one per VM). If the id
// reuses a reclaimed slot, the old occupant's refs are wiped via Wipe after
// the store lock is released.
func (r *Registry) Acquire(project string, root bool) (string, error) {
	var id, wipeProject string
	var doWipe bool
	err := r.withLock(func(s *state) (bool, error) {
		now := r.now()
		r.sweepDead(s, now)

		if root {
			if a := activeRoot(s); a != nil {
				return false, fmt.Errorf("a root agent is already running (agent %s); only one per VM", a.ID)
			}
		}

		cutoff := now - ReclaimableAfterSecs
		inUse := map[string]bool{}
		for _, a := range s.Agents {
			if a.ExitedAt == nil || *a.ExitedAt > cutoff {
				inUse[a.ID] = true
			}
		}
		n := 1
		for inUse[strconv.Itoa(n)] {
			n++
		}
		id = strconv.Itoa(n)

		row := Agent{
			ID:        id,
			UUID:      newUUID(),
			Project:   project,
			HostPID:   os.Getpid(),
			StartedAt: now,
			SessionID: newUUID(),
			Root:      root,
		}
		if old := find(s, id); old != nil {
			// Reclaiming a stale slot: wipe the prior occupant's refs.
			doWipe, wipeProject = true, old.Project
			*old = row
		} else {
			s.Agents = append(s.Agents, row)
		}
		return true, nil
	})
	if err != nil {
		return "", err
	}
	if doWipe && r.Wipe != nil {
		r.Wipe(wipeProject, id)
	}
	return id, nil
}

// Continue re-activates an agent. With idHint set it re-activates that id
// (error if unknown or still running). With idHint empty it re-activates the
// most recently exited agent for project, or returns "" if there is none.
func (r *Registry) Continue(project, idHint string) (string, error) {
	var result string
	err := r.withLock(func(s *state) (bool, error) {
		now := r.now()
		r.sweepDead(s, now)

		reactivate := func(a *Agent) error {
			if a.Root {
				if other := activeRoot(s); other != nil && other.ID != a.ID {
					return fmt.Errorf("cannot resume root agent %s: agent %s is already root", a.ID, other.ID)
				}
			}
			a.HostPID = os.Getpid()
			a.StartedAt = now
			a.ExitedAt = nil
			result = a.ID
			return nil
		}

		if idHint != "" {
			a := find(s, idHint)
			if a == nil {
				return true, fmt.Errorf("agent %s not found (may have been reclaimed)", idHint)
			}
			if a.ExitedAt == nil && a.HostPID != 0 && r.alive(a.HostPID) {
				return true, fmt.Errorf("agent %s is still running", idHint)
			}
			return true, reactivate(a)
		}

		var latest *Agent
		for i := range s.Agents {
			a := &s.Agents[i]
			if a.Project == project && a.ExitedAt != nil {
				if latest == nil || *a.ExitedAt > *latest.ExitedAt {
					latest = a
				}
			}
		}
		if latest == nil {
			return true, nil // no agents to continue
		}
		return true, reactivate(latest)
	})
	return result, err
}

// RootAgent returns the id of the running root agent, if any.
func (r *Registry) RootAgent() (string, bool, error) {
	var id string
	var ok bool
	err := r.withLock(func(s *state) (bool, error) {
		changed := r.sweepDead(s, r.now())
		if a := activeRoot(s); a != nil {
			id, ok = a.ID, true
		}
		return changed, nil
	})
	return id, ok, err
}

// IsRunning reports whether the agent is currently alive, updating its exit
// state if a dead pid is discovered.
func (r *Registry) IsRunning(id string) (bool, error) {
	var running bool
	err := r.withLock(func(s *state) (bool, error) {
		a := find(s, id)
		if a == nil || a.ExitedAt != nil || a.HostPID == 0 {
			return false, nil
		}
		if r.alive(a.HostPID) {
			running = true
			return false, nil
		}
		t := r.now()
		a.ExitedAt = &t
		a.HostPID = 0
		return true, nil
	})
	return running, err
}

// DisplayName is the "slug@id" label. project is already a slug.
func DisplayName(project, id string) string {
	return project + "@" + id
}

// --- Sessions ---

// SetSession records the claude session id for an agent, clearing it from any
// other agent that held it (a session belongs to one agent).
func (r *Registry) SetSession(id, sessionID string) error {
	return r.withLock(func(s *state) (bool, error) {
		for i := range s.Agents {
			a := &s.Agents[i]
			if a.SessionID == sessionID && a.ID != id {
				a.SessionID = ""
			}
			if a.ID == id {
				a.SessionID = sessionID
			}
		}
		return true, nil
	})
}

// Session returns the agent's recorded session id, if any.
func (r *Registry) Session(id string) (string, error) {
	var sid string
	err := r.withLock(func(s *state) (bool, error) {
		if a := find(s, id); a != nil {
			sid = a.SessionID
		}
		return false, nil
	})
	return sid, err
}

// AgentBySession returns the id of the agent holding sessionID, or "".
func (r *Registry) AgentBySession(sessionID string) (string, error) {
	var id string
	err := r.withLock(func(s *state) (bool, error) {
		for i := range s.Agents {
			if s.Agents[i].SessionID == sessionID {
				id = s.Agents[i].ID
				break
			}
		}
		return false, nil
	})
	return id, err
}

// newUUID returns a random RFC-4122 v4 uuid without pulling a dependency.
func newUUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}
