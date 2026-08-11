package agentd

import (
	"fmt"
	"sort"
	"sync"
)

// Registry is the set of agents the daemon is holding. One entry today; the
// reason it is a map and not a field is that an agent is addressed by name
// over the socket, so adding a second one is a matter of calling Start twice
// rather than reworking the protocol.
type Registry struct {
	mu       sync.Mutex
	sessions map[string]*Session
}

func NewRegistry() *Registry {
	return &Registry{sessions: make(map[string]*Session)}
}

func (r *Registry) Start(id string, argv, env []string, dir string, rows, cols uint16) (*Session, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if s, ok := r.sessions[id]; ok && s.Running() {
		return nil, fmt.Errorf("agentd: agent %q is already running", id)
	}
	s, err := StartSession(id, argv, env, dir, rows, cols)
	if err != nil {
		return nil, err
	}
	r.sessions[id] = s
	return s, nil
}

// Restart brings back an agent that has exited, under the same name and the
// same command. This is what `dtach -A` did for the one-agent case: asking
// for the agent gets you the agent, whether or not the last one is still
// alive. An agent that is still running is returned untouched.
func (r *Registry) Restart(id string) (*Session, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	old, ok := r.sessions[id]
	if !ok {
		return nil, fmt.Errorf("agentd: no agent %q", id)
	}
	if old.Running() {
		return old, nil
	}
	s, err := StartSession(id, old.argv, old.env, old.dir, 0, 0)
	if err != nil {
		return nil, err
	}
	r.sessions[id] = s
	return s, nil
}

func (r *Registry) Get(id string) (*Session, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	s, ok := r.sessions[id]
	return s, ok
}

// List is sorted so that the output of a status command does not reshuffle
// itself between runs.
func (r *Registry) List() []*Session {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*Session, 0, len(r.sessions))
	for _, s := range r.sessions {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (r *Registry) CloseAll() {
	for _, s := range r.List() {
		_ = s.Close()
	}
}
