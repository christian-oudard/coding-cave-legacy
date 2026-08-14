package nixcache

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// nixbase32 is the alphabet nix encodes store-path hashes in: base32 without
// e, o, u and t. Anything outside it is not a store path hash, so the check
// below is exact rather than a heuristic.
const nixbase32 = "0123456789abcdfghijklmnpqrsvwxyz"

// hashLen is the length of a store path hash in that encoding: 20 bytes of
// truncated sha256, 32 characters.
const hashLen = 32

// validHash is the boundary check for everything the guest sends. The hash
// arrives in a URL from inside the VM and is about to select a path on the
// host whose bytes are then served back, so a request that is not exactly a
// store hash must be rejected before it reaches a filesystem lookup or an
// argv. Every caller treats a false here as a 404, not as an error to report:
// a cache miss is a normal answer and the guest goes on to the next
// substituter, which is also the only thing we want to tell it.
func validHash(s string) bool {
	if len(s) != hashLen {
		return false
	}
	for _, c := range s {
		if !strings.ContainsRune(nixbase32, c) {
			return false
		}
	}
	return true
}

// index maps a store-path hash to its full path. Nix answers this from its
// sqlite DB; we cannot, because reading that DB needs cgo and the host binary
// is a static build, and shelling out cannot do it either since every nix
// query wants the full path we are trying to find.
//
// So: read the store directory. It is one readdir of a few thousand entries,
// and it is redone only when the directory's mtime moves, which is exactly
// when a path is added or removed. WantMassQuery means nix asks for many
// hashes in a row, most of them absent, and a design that re-scanned per miss
// would turn one `nix build` into thousands of readdirs.
type index struct {
	dir string

	mu    sync.Mutex
	mtime int64
	paths map[string]string
}

func newIndex(dir string) *index { return &index{dir: dir} }

// lookup returns the absolute store path for a hash, or "" if the host does
// not have it.
func (ix *index) lookup(hash string) string {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	st, err := os.Stat(ix.dir)
	if err != nil {
		return ""
	}
	if ix.paths == nil || st.ModTime().UnixNano() != ix.mtime {
		ix.scan(st.ModTime().UnixNano())
	}
	return ix.paths[hash]
}

func (ix *index) scan(mtime int64) {
	ents, err := os.ReadDir(ix.dir)
	if err != nil {
		return
	}
	paths := make(map[string]string, len(ents))
	for _, e := range ents {
		name := e.Name()
		// Store entries are <hash>-<name>; .links, .lock and the in-flight
		// .tmp-* directories are not, and neither is a bare hash with no name.
		if len(name) <= hashLen || name[hashLen] != '-' || !validHash(name[:hashLen]) {
			continue
		}
		paths[name[:hashLen]] = ix.dir + "/" + name
	}
	ix.mtime, ix.paths = mtime, paths
}

// info is the metadata a narinfo is made of.
type info struct {
	path       string
	narHash    string // "sha256:<nixbase32>", the form narinfo wants
	narSize    int64
	references []string
}

// query asks nix about a path. `nix-store --query` rather than `nix path-info
// --json`: it is the stable interface (path-info's JSON shape is versioned and
// currently warns that bare --json will become an error), and --hash returns
// the hash already in narinfo's encoding, so nothing here has to implement
// nixbase32.
func query(path string) (*info, error) {
	hash, err := nixQuery("--hash", path)
	if err != nil {
		return nil, err
	}
	size, err := nixQuery("--size", path)
	if err != nil {
		return nil, err
	}
	n, err := strconv.ParseInt(size, 10, 64)
	if err != nil {
		return nil, fmt.Errorf("nar size of %s: %w", path, err)
	}
	refs, err := nixQuery("--references", path)
	if err != nil {
		return nil, err
	}
	// Sorted, because nix holds references in a set and its fingerprint is
	// that set joined in order. `nix-store --query` prints them in the order
	// the database returns, which is not the same, and a fingerprint built
	// from that order produces a signature nix rejects as untrusted with no
	// hint as to why. Invisible on any path with fewer than two references.
	sorted := strings.Fields(refs)
	sort.Strings(sorted)
	return &info{path: path, narHash: hash, narSize: n, references: sorted}, nil
}

func nixQuery(what, path string) (string, error) {
	var errbuf bytes.Buffer
	cmd := exec.Command("nix-store", "--query", what, path)
	cmd.Stderr = &errbuf
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("nix-store --query %s %s: %w: %s",
			what, path, err, strings.TrimSpace(errbuf.String()))
	}
	return strings.TrimSpace(string(out)), nil
}

// dump streams the path's NAR. Uncompressed, deliberately: this is a vsock
// link at memory speed, so compressing would spend the scarce resource (CPU)
// to save the abundant one, and it keeps NarSize usable as Content-Length.
func dump(path string, w io.Writer) error {
	cmd := exec.Command("nix-store", "--dump", path)
	cmd.Stdout = w
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
