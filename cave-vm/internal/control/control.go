// Package control is the host's command channel into the guest: JSON over
// HTTP, one client, no framework.
//
// It exists because the VM is an appliance driven by `cav` rather than a
// machine you log into, and the difference is measurable. Against a running
// guest over this vsock plane, a cold ssh call costs 85ms and a call on an
// already-open vsock connection costs 0.035ms. Nearly all of ssh's cost is key
// exchange and session setup, not the plane. A status that asks the guest once
// is merely sluggish; the command a human runs most should not be.
//
// The transport is deliberately not named anywhere in the protocol. Today the
// stream is a vsock connection; a cave hosted somewhere other than this machine
// would make it a TLS connection, and neither end can tell. That is why every
// call carries a bearer token even locally, where the unix socket's permissions
// would already have been enough: a local-only exemption is a capability that
// exists on one transport and not the other, which is how the remote case ends
// up unbuildable. One code path, one set of privileges.
package control

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Port is the control plane's vsock port. Host-initiated, and in the fixed
// table like every other: under vfkit the port list is the VM's launch argv, so
// nothing may allocate one after boot. Agents are multiplexed by id inside a
// request, never by port.
const Port = 7001

// Health is what the guest says about itself. System is the toplevel it
// actually activated, which is not necessarily what the host last deployed.
type Health struct {
	System string `json:"system"`
	Uptime int    `json:"uptimeSec"`
	Now    int64  `json:"nowUnixNano"`
}

// Agent is one running agent session. The guest owns this fact: systemd knows
// whether the unit is up and the socket directory is the roster, so the host
// asks rather than keeping a register of its own and being wrong about it.
type Agent struct {
	ID    string `json:"id"`
	State string `json:"state"`
	Info  string `json:"info,omitempty"`
}

// Guest is everything the control plane can do inside the VM. An interface
// because the operations are systemd and clock calls that cannot run in a
// test, not because a second implementation is expected.
type Guest interface {
	Health() (Health, error)
	Agents() ([]Agent, error)
	StopAgent(id string) error
	SetTime(time.Time) error
}

// SessionRecord is one transcript as the guest sees it. Deliberately not the
// sessions package's own type: this package must not import that one, because
// the host-side implementation of its Guest interface is built on this
// package's client, and the two would import each other.
type SessionRecord struct {
	ID      string `json:"id"`
	Size    int64  `json:"size"`
	Project string `json:"project"`
	// Agent is whose transcript it is. Only the guest can say: agents have
	// separate homes and the host cannot see them, so an answer it worked out
	// for itself would be a guess about which it had asked for.
	Agent string `json:"agent,omitempty"`
}

// Sessions is the transcript half of the control plane, and it is the reason
// an agent that nobody attached to still gets its history copied out: over ssh
// a sync cost a process and 85ms per record, which is too much to do on every
// `status`, and here it does not.
//
// The host names every id and constructs every path; this only ever hands back
// what it was asked for. That is the same rule the ssh path follows, and the
// reason neither is rsync or a tar of a directory: a batch copier lets the
// sender choose the names that land on the host.
type Sessions interface {
	List() ([]SessionRecord, error)
	Read(id string) (io.ReadCloser, error)
	Write(id string, r io.Reader, shared bool) error
}

// Token is the shared secret. It is generated on the host, staged into the
// guest on the seed disk beside authorized_keys, and read from a file at both
// ends so it never appears in an argument list.
func ReadToken(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	tok := strings.TrimSpace(string(b))
	if tok == "" {
		return "", fmt.Errorf("%s is empty", path)
	}
	return tok, nil
}

// EnsureToken returns the host's token, creating it on first use. Owner-only,
// like every other secret in the state dir.
func EnsureToken(path string) (string, error) {
	if tok, err := ReadToken(path); err == nil {
		return tok, nil
	}
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	tok := hex.EncodeToString(b[:])
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(tok+"\n"), 0o600); err != nil {
		return "", err
	}
	return tok, nil
}

// Handler is the guest's side. Routes are matched with the stdlib's method and
// path patterns, so there is no router and no dependency.
//
// s may be nil, in which case the transcript routes do not exist at all rather
// than existing and failing.
func Handler(g Guest, s Sessions, token string) http.Handler {
	mux := http.NewServeMux()
	if s != nil {
		registerSessions(mux, s)
	}
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		h, err := g.Health()
		reply(w, h, err)
	})
	mux.HandleFunc("GET /agents", func(w http.ResponseWriter, r *http.Request) {
		a, err := g.Agents()
		if a == nil {
			a = []Agent{}
		}
		reply(w, struct {
			Agents []Agent `json:"agents"`
		}{a}, err)
	})
	mux.HandleFunc("POST /agents/{id}/stop", func(w http.ResponseWriter, r *http.Request) {
		reply(w, nil, g.StopAgent(r.PathValue("id")))
	})
	// The clock, which nothing else corrects: the guest has no NIC, no NTP and
	// no timesyncd, and under HVF on aarch64 its counter does not track the
	// host across a suspend. A stale clock fails TLS certificate validation and
	// therefore every agent request, while looking like nothing is wrong.
	mux.HandleFunc("POST /time", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			UnixNano int64 `json:"unixNano"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if body.UnixNano <= 0 {
			http.Error(w, "unixNano must be positive", http.StatusBadRequest)
			return
		}
		reply(w, nil, g.SetTime(time.Unix(0, body.UnixNano)))
	})
	return authenticated(mux, token)
}

func registerSessions(mux *http.ServeMux, s Sessions) {
	mux.HandleFunc("GET /sessions", func(w http.ResponseWriter, r *http.Request) {
		recs, err := s.List()
		if recs == nil {
			recs = []SessionRecord{}
		}
		reply(w, struct {
			Sessions []SessionRecord `json:"sessions"`
		}{recs}, err)
	})
	mux.HandleFunc("GET /sessions/{id}", func(w http.ResponseWriter, r *http.Request) {
		rc, err := s.Read(r.PathValue("id"))
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		defer rc.Close()
		// Streamed rather than buffered: a transcript is the one thing here
		// that grows without bound, and the guest has no reason to hold a
		// whole one in memory to hand it over.
		w.Header().Set("Content-Type", "application/x-ndjson")
		io.Copy(w, rc)
	})
	mux.HandleFunc("POST /sessions/{id}", func(w http.ResponseWriter, r *http.Request) {
		// Which directory a restored record lands in: claude's own, where its
		// picker will find it, or the read-only view of every agent's records.
		reply(w, nil, s.Write(r.PathValue("id"), r.Body, r.URL.Query().Get("dest") == "shared"))
	})
}

// authenticated rejects anything without the token, before routing, so an
// unauthenticated caller cannot learn which paths exist.
func authenticated(next http.Handler, token string) http.Handler {
	want := []byte("Bearer " + token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := []byte(r.Header.Get("Authorization"))
		// Constant time, and length-checked first because the comparison
		// returns 0 for unequal lengths without looking at the contents.
		if len(got) != len(want) || subtle.ConstantTimeCompare(got, want) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func reply(w http.ResponseWriter, v any, err error) {
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	if v == nil {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
