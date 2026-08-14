// Package nixcache serves the host's /nix/store to the guest over the standard
// nix binary-cache HTTP protocol.
//
// This is what replaces every store-sharing mechanism the Rust implementation
// tried (see docs/store-design-review.md): no projected tree, no manifest, no
// want-list, no sqlite crossing the VM boundary. A narinfo request is the
// want-list entry, answered synchronously, and the guest's own nix-daemon
// decides what it needs.
//
// Three properties fall out of using the real protocol rather than inventing
// one. The guest verifies signatures, so nothing unsigned enters its store even
// though the transport is trusted. Host GC needs no coordination: if a path
// disappears between the narinfo and the nar, the nar fails and the guest falls
// through to cache.nixos.org through crinj. And it is read-only by
// construction, since the protocol has no verb that writes.
//
// Accepted, recorded rather than solved: lookups are point queries by hash, so
// an agent can probe whether the host has a derivation it can already name. It
// cannot enumerate the store, because there is no endpoint that lists.
package nixcache

import (
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// KeyName is what the guest's trusted-public-keys entry is called. Per state
// dir, not per fleet, matching where the key is generated.
const KeyName = "cave-host-1"

type Server struct {
	key *Key
	ix  *index
	dir string
}

// New loads or generates the signing key and indexes the store. storeDir is
// nix's StoreDir, which the guest checks against its own: a mismatch there
// makes every path unusable, so it is served rather than assumed.
func New(storeDir, keyPath string) (*Server, error) {
	key, err := LoadKey(keyPath, KeyName)
	if err != nil {
		return nil, err
	}
	return &Server{key: key, ix: newIndex(storeDir), dir: storeDir}, nil
}

func (s *Server) PublicKey() string { return s.key.Public() }

// Serve runs the cache on a listener until it fails. It is what the vsock plane
// calls for port 7100; there is no TCP listener anywhere, so the host's store is
// reachable from the guest and from nothing else on the host.
func (s *Server) Serve(ln net.Listener) {
	srv := &http.Server{Handler: s, ReadHeaderTimeout: 10 * time.Second}
	srv.Serve(ln)
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		// The protocol's write verbs exist, and this cache implements none of
		// them. Saying so explicitly beats a 404, which would read as "not
		// here yet".
		http.Error(w, "this cache is read-only", http.StatusMethodNotAllowed)
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/")
	switch {
	case path == "nix-cache-info":
		s.cacheInfo(w)
	case strings.HasSuffix(path, ".narinfo"):
		s.narinfo(w, strings.TrimSuffix(path, ".narinfo"))
	case strings.HasPrefix(path, "nar/") && strings.HasSuffix(path, ".nar"):
		s.nar(w, r, strings.TrimSuffix(strings.TrimPrefix(path, "nar/"), ".nar"))
	default:
		http.NotFound(w, r)
	}
}

// cacheInfo advertises a priority ahead of cache.nixos.org's 40, which is the
// whole point: the guest lists both substituters and must reach for the local
// one first, or every build pays for the internet it was meant to avoid.
func (s *Server) cacheInfo(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/x-nix-cache-info")
	fmt.Fprintf(w, "StoreDir: %s\nWantMassQuery: 1\nPriority: 10\n", s.dir)
}

// resolve turns a guest-supplied hash into a store path, or nothing.
//
// This is the boundary, and it is a boundary for a reason that read-only does
// not cover: the response body is host file contents, so a hash that escaped
// validation would be a disclosure bug rather than a write bug. Validate first,
// then look up in an index built from the store's own directory entries, so the
// value that reaches the filesystem and nix's argv is a path this process
// found, never a string the guest composed.
func (s *Server) resolve(hash string) string {
	if !validHash(hash) {
		return ""
	}
	return s.ix.lookup(hash)
}

func (s *Server) narinfo(w http.ResponseWriter, hash string) {
	path := s.resolve(hash)
	if path == "" {
		http.Error(w, "not in this store", http.StatusNotFound)
		return
	}
	inf, err := query(path)
	if err != nil {
		// Racing the host's GC lands here. A 404 is the truthful answer and
		// the one the guest can act on: it moves to the next substituter.
		http.Error(w, "not in this store", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "text/x-nix-narinfo")
	fmt.Fprint(w, narinfoBody(inf, s.key))
}

// narinfoBody is the narinfo text format. FileHash and FileSize repeat NarHash
// and NarSize because Compression is none, which is what "the file is the nar"
// means to a client.
func narinfoBody(inf *info, key *Key) string {
	var b strings.Builder
	fmt.Fprintf(&b, "StorePath: %s\n", inf.path)
	fmt.Fprintf(&b, "URL: nar/%s.nar\n", storeHash(inf.path))
	fmt.Fprintf(&b, "Compression: none\n")
	fmt.Fprintf(&b, "FileHash: %s\n", inf.narHash)
	fmt.Fprintf(&b, "FileSize: %d\n", inf.narSize)
	fmt.Fprintf(&b, "NarHash: %s\n", inf.narHash)
	fmt.Fprintf(&b, "NarSize: %d\n", inf.narSize)
	if len(inf.references) > 0 {
		// References are basenames here and absolute paths in the
		// fingerprint. Getting that backwards produces a narinfo that parses
		// and fails to verify, with nothing in the error to say why.
		names := make([]string, len(inf.references))
		for i, r := range inf.references {
			names[i] = baseName(r)
		}
		fmt.Fprintf(&b, "References: %s\n", strings.Join(names, " "))
	}
	fmt.Fprintf(&b, "Sig: %s\n", key.Sign(fingerprint(inf)))
	return b.String()
}

// fingerprint is what nix signs and what the guest verifies: version 1, the
// absolute path, the nar hash and size, and the references as absolute paths
// joined by commas. The format is nix's, so it is written out here exactly
// rather than being derived from the narinfo above.
func fingerprint(inf *info) string {
	return fmt.Sprintf("1;%s;%s;%d;%s",
		inf.path, inf.narHash, inf.narSize, strings.Join(inf.references, ","))
}

func storeHash(path string) string { return baseName(path)[:hashLen] }

func baseName(path string) string {
	if i := strings.LastIndexByte(path, '/'); i >= 0 {
		return path[i+1:]
	}
	return path
}

func (s *Server) nar(w http.ResponseWriter, r *http.Request, hash string) {
	path := s.resolve(hash)
	if path == "" {
		http.Error(w, "not in this store", http.StatusNotFound)
		return
	}
	inf, err := query(path)
	if err != nil {
		http.Error(w, "not in this store", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/x-nix-nar")
	w.Header().Set("Content-Length", strconv.FormatInt(inf.narSize, 10))
	if r.Method == http.MethodHead {
		return
	}
	// Once bytes are flowing there is no way to turn a failure into a status
	// code, so a dump that dies mid-stream reaches the guest as a short body.
	// That is the right outcome: nix checks NarHash, so a truncated nar is
	// rejected rather than imported.
	dump(path, w)
}
