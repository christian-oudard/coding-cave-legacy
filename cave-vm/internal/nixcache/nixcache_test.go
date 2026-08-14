package nixcache

import (
	"crypto/ed25519"
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func testServer(t *testing.T, storeDir string) *Server {
	t.Helper()
	s, err := New(storeDir, filepath.Join(t.TempDir(), "cache-key"))
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func get(t *testing.T, s *Server, path string) *http.Response {
	t.Helper()
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
	return w.Result()
}

// A hash from the guest selects a host path whose bytes are then served back,
// so validation is the whole of the boundary. Read-only does not cover this:
// an escaped hash discloses host files rather than writing any.
func TestValidHashRejectsAnythingButAStoreHash(t *testing.T) {
	good := "57iz36553175g3178pvxjij8z5rcsd4n"
	if !validHash(good) {
		t.Fatalf("rejected a real store hash %q", good)
	}
	bad := []string{
		"", "short",
		strings.Repeat("a", 31), strings.Repeat("a", 33),
		"../../../../etc/passwd",
		"57iz36553175g3178pvxjij8z5rcsd4n-hello", // hash plus a name
		"57iz36553175g3178pvxjij8z5rcsd4/",       // a separator
		"57iz36553175g3178pvxjij8z5rcsd4*",       // a glob metacharacter
		"57iz36553175g3178pvxjij8z5rcsd4;",       // a shell metacharacter
		"57iz36553175g3178pvxjij8z5rcsd4 ",       // an argv split
		"57iz36553175g3178pvxjij8z5rcsd4e",       // e, o, u, t are not nixbase32
		"57iz36553175g3178pvxjij8z5rcsd4O",
		"57IZ36553175G3178PVXJIJ8Z5RCSD4N", // uppercase
	}
	for _, h := range bad {
		if validHash(h) {
			t.Errorf("accepted %q", h)
		}
	}
}

// The same rejection seen from the outside, since validHash only matters if
// every entry point actually goes through it.
func TestTraversalAndJunkAre404(t *testing.T) {
	s := testServer(t, t.TempDir())
	for _, p := range []string{
		"/../../../../etc/passwd.narinfo",
		"/nar/../../../../etc/passwd.nar",
		"/%2e%2e%2f%2e%2e%2fetc%2fpasswd.narinfo",
		"/.narinfo",
		"/nar/.nar",
		"/etc/passwd",
	} {
		if got := get(t, s, p).StatusCode; got != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", p, got)
		}
	}
}

// A cache that answered a write verb would be a way into the host store, so
// the refusal is asserted rather than left to the absence of a route.
func TestWritesAreRefused(t *testing.T) {
	s := testServer(t, t.TempDir())
	for _, m := range []string{"PUT", "POST", "DELETE"} {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest(m, "/57iz36553175g3178pvxjij8z5rcsd4n.narinfo", nil))
		if w.Code != http.StatusMethodNotAllowed {
			t.Errorf("%s = %d, want 405", m, w.Code)
		}
	}
}

// Priority has to beat cache.nixos.org's 40 or the guest reaches the internet
// first and the whole phase buys nothing.
func TestCacheInfoOutranksCacheNixosOrg(t *testing.T) {
	s := testServer(t, "/nix/store")
	body := readBody(t, get(t, s, "/nix-cache-info"))
	for _, want := range []string{"StoreDir: /nix/store", "WantMassQuery: 1", "Priority: 10"} {
		if !strings.Contains(body, want) {
			t.Errorf("nix-cache-info missing %q:\n%s", want, body)
		}
	}
}

// There is no endpoint that lists, which is what bounds the accepted privacy
// cost to "probe a name you already know" rather than "read the host's store".
func TestNothingEnumeratesTheStore(t *testing.T) {
	dir := t.TempDir()
	os.Mkdir(filepath.Join(dir, "57iz36553175g3178pvxjij8z5rcsd4n-secret-project"), 0o755)
	s := testServer(t, dir)
	for _, p := range []string{"/", "/nar/", "/nix-cache-info"} {
		if strings.Contains(readBody(t, get(t, s, p)), "secret-project") {
			t.Errorf("GET %s leaked a store path name", p)
		}
	}
}

// The index is keyed on the store directory's mtime, so a path that appears
// after the first scan has to be found without restarting the server.
func TestIndexSeesPathsAddedAfterTheFirstScan(t *testing.T) {
	dir := t.TempDir()
	ix := newIndex(dir)
	hash := "57iz36553175g3178pvxjij8z5rcsd4n"
	if ix.lookup(hash) != "" {
		t.Fatal("found a path in an empty store")
	}
	want := filepath.Join(dir, hash+"-hello")
	if err := os.Mkdir(want, 0o755); err != nil {
		t.Fatal(err)
	}
	// Directory mtimes have coarse resolution on some filesystems; make the
	// change unmistakable rather than have the test depend on that.
	os.Chtimes(dir, time.Time{}, time.Now().Add(time.Second))
	if got := ix.lookup(hash); got != want {
		t.Errorf("lookup after add = %q, want %q", got, want)
	}
}

// Entries that are not store paths share the directory, and a bare hash with
// no name would map a hash to a path nix cannot use.
func TestIndexSkipsNonStoreEntries(t *testing.T) {
	dir := t.TempDir()
	hash := "57iz36553175g3178pvxjij8z5rcsd4n"
	for _, name := range []string{".links", ".lock", hash, "not-a-hash-at-all"} {
		os.Mkdir(filepath.Join(dir, name), 0o755)
	}
	if got := newIndex(dir).lookup(hash); got != "" {
		t.Errorf("lookup of a bare hash = %q, want none", got)
	}
}

// The narinfo carries references as basenames and the fingerprint as absolute
// paths. Swapping them yields a narinfo that parses and fails to verify, with
// nothing in nix's error to say which half was wrong.
func TestReferencesAreBasenamesInNarinfoAndPathsInTheFingerprint(t *testing.T) {
	inf := &info{
		path:    "/nix/store/57iz36553175g3178pvxjij8z5rcsd4n-hello",
		narHash: "sha256:0s6a81a0m4h6kjhd7rra38kh795fmbgq76c29wqwhbc5anzjcb30",
		narSize: 4064528,
		references: []string{
			"/nix/store/chqq8mpmpyfi9kgsngya71akv5xicn03-gcc-15.2.0-lib",
			"/nix/store/57iz36553175g3178pvxjij8z5rcsd4n-hello",
		},
	}
	key, err := LoadKey(filepath.Join(t.TempDir(), "k"), KeyName)
	if err != nil {
		t.Fatal(err)
	}
	body := narinfoBody(inf, key)
	want := "References: chqq8mpmpyfi9kgsngya71akv5xicn03-gcc-15.2.0-lib 57iz36553175g3178pvxjij8z5rcsd4n-hello\n"
	if !strings.Contains(body, want) {
		t.Errorf("narinfo references wrong:\n%s", body)
	}
	if !strings.Contains(body, "URL: nar/57iz36553175g3178pvxjij8z5rcsd4n.nar\n") {
		t.Errorf("narinfo URL wrong:\n%s", body)
	}
	fp := fingerprint(inf)
	if !strings.Contains(fp, "/nix/store/chqq8mpmpyfi9kgsngya71akv5xicn03-gcc-15.2.0-lib,/nix/store/57iz36553175g3178pvxjij8z5rcsd4n-hello") {
		t.Errorf("fingerprint references wrong: %s", fp)
	}
}

// The key file is nix's own format, so the public half can be pasted into
// trusted-public-keys and the secret half would work with `nix store sign -k`.
func TestKeyIsNixFormatAndPersists(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cache-key")
	k1, err := LoadKey(path, KeyName)
	if err != nil {
		t.Fatal(err)
	}
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != 0o600 {
		t.Errorf("secret key mode %v, want 0600", st.Mode().Perm())
	}
	pub, err := os.ReadFile(path + ".pub")
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(pub)) != k1.Public() {
		t.Errorf("pub file %q != %q", strings.TrimSpace(string(pub)), k1.Public())
	}
	name, b64, ok := strings.Cut(k1.Public(), ":")
	if !ok || name != KeyName {
		t.Fatalf("public key %q is not <name>:<base64>", k1.Public())
	}
	if raw, err := base64.StdEncoding.DecodeString(b64); err != nil || len(raw) != ed25519.PublicKeySize {
		t.Errorf("public half is not a 32-byte ed25519 key: %v", err)
	}
	// A regenerated key would invalidate every signature the guest already
	// trusts, so a second load must be the same key.
	k2, err := LoadKey(path, KeyName)
	if err != nil {
		t.Fatal(err)
	}
	if k2.Public() != k1.Public() {
		t.Error("LoadKey regenerated an existing key")
	}
}

func TestSignatureVerifiesUnderThePublishedKey(t *testing.T) {
	k, err := LoadKey(filepath.Join(t.TempDir(), "k"), KeyName)
	if err != nil {
		t.Fatal(err)
	}
	_, b64, _ := strings.Cut(k.Public(), ":")
	pub, _ := base64.StdEncoding.DecodeString(b64)
	_, sig64, _ := strings.Cut(k.Sign("1;/nix/store/x;sha256:y;1;"), ":")
	sig, _ := base64.StdEncoding.DecodeString(sig64)
	if !ed25519.Verify(pub, []byte("1;/nix/store/x;sha256:y;1;"), sig) {
		t.Error("signature does not verify under the key we publish to the guest")
	}
}

// The one test that proves the fingerprint format rather than just proving we
// agree with ourselves: take a locally-present path that cache.nixos.org
// signed, build the fingerprint from our own queries, and verify their
// signature against it with their published key. If our format is wrong in any
// byte, this fails. No network: the path and its signature are already in the
// local store and DB.
func TestFingerprintMatchesCacheNixosOrg(t *testing.T) {
	requireNix(t)
	const cacheNixosOrgKey = "6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
	path, sig := signedStorePath(t, "cache.nixos.org-1")
	inf, err := query(path)
	if err != nil {
		t.Fatal(err)
	}
	// Reference ordering is the part of the format that is easiest to get
	// wrong and impossible to see: nix keeps references in a set and joins
	// them in order, while `nix-store --query` prints them in database order.
	// A path with one reference verifies under either, so this test would pass
	// while every real closure failed.
	if len(inf.references) < 2 {
		t.Fatalf("%s has %d references; this test proves nothing without several",
			path, len(inf.references))
	}
	pub, err := base64.StdEncoding.DecodeString(cacheNixosOrgKey)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := base64.StdEncoding.DecodeString(sig)
	if err != nil {
		t.Fatal(err)
	}
	if !ed25519.Verify(pub, []byte(fingerprint(inf)), raw) {
		t.Errorf("our fingerprint does not match nix's for %s:\n  %s", path, fingerprint(inf))
	}
}

// End to end against the real store: a present path answers, and its nar is
// exactly the bytes and length the narinfo promised.
func TestNarinfoAndNarForARealStorePath(t *testing.T) {
	requireNix(t)
	path := ownStorePath(t)
	s := testServer(t, "/nix/store")
	hash := storeHash(path)

	body := readBody(t, get(t, s, "/"+hash+".narinfo"))
	if !strings.Contains(body, "StorePath: "+path+"\n") {
		t.Fatalf("narinfo for %s:\n%s", path, body)
	}
	if !strings.Contains(body, "Compression: none\n") {
		t.Errorf("narinfo does not say uncompressed:\n%s", body)
	}
	var narSize string
	for _, line := range strings.Split(body, "\n") {
		if v, ok := strings.CutPrefix(line, "NarSize: "); ok {
			narSize = v
		}
	}
	if narSize == "" {
		t.Fatalf("no NarSize:\n%s", body)
	}

	resp := get(t, s, "/nar/"+hash+".nar")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("nar = %d", resp.StatusCode)
	}
	if got := resp.Header.Get("Content-Length"); got != narSize {
		t.Errorf("Content-Length %q != NarSize %q", got, narSize)
	}
	nar := readBody(t, resp)
	if strconv.Itoa(len(nar)) != narSize {
		t.Errorf("nar is %d bytes, NarSize says %s", len(nar), narSize)
	}
	// The NAR format's own magic, so a truncated or empty stream cannot pass.
	if !strings.Contains(nar[:min(64, len(nar))], "nix-archive-1") {
		t.Errorf("nar does not start with the nix-archive-1 magic")
	}
}

// Phase 4's acceptance property in miniature, and the only test where the
// client is nix rather than us: nix fetches the narinfo and the nar from a
// live server and verifies the signature under the key we hand the guest. Our
// own assertions can only prove we agree with ourselves; this proves the guest
// will accept what we serve. No network, since the only address involved is
// loopback and the path is already local.
func TestNixItselfVerifiesWhatWeServe(t *testing.T) {
	requireNix(t)
	path := ownStorePath(t)
	s := testServer(t, "/nix/store")
	srv := httptest.NewServer(s)
	defer srv.Close()

	run := func(key string) (string, error) {
		out, err := exec.Command("nix", "store", "verify",
			"--store", srv.URL, "--sigs-needed", "1",
			"--option", "trusted-public-keys", key, path).CombinedOutput()
		return string(out), err
	}
	out, err := run(s.PublicKey())
	if err != nil || strings.Contains(out, "untrusted") {
		t.Fatalf("nix rejected our cache (%v):\n%s", err, out)
	}
	// And the trust is doing work: the same path under a key nix does not know
	// must fail, or the check above would pass no matter what we signed.
	out, err = run("nobody-1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
	if err == nil && !strings.Contains(out, "untrusted") {
		t.Errorf("nix accepted our cache under a key it does not trust:\n%s", out)
	}
}

// An absent hash is a 404 rather than an error, because that is what sends the
// guest on to the next substituter instead of failing the build.
func TestAbsentPathIs404(t *testing.T) {
	s := testServer(t, "/nix/store")
	// Well-formed and vanishingly unlikely to exist.
	if got := get(t, s, "/00000000000000000000000000000000.narinfo").StatusCode; got != http.StatusNotFound {
		t.Errorf("absent narinfo = %d, want 404", got)
	}
	if got := get(t, s, "/nar/00000000000000000000000000000000.nar").StatusCode; got != http.StatusNotFound {
		t.Errorf("absent nar = %d, want 404", got)
	}
}

func requireNix(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("nix-store"); err != nil {
		t.Skip("nix-store not on PATH")
	}
}

// ownStorePath finds a store path that certainly exists: the one holding
// nix-store itself.
func ownStorePath(t *testing.T) string {
	t.Helper()
	bin, err := exec.LookPath("nix-store")
	if err != nil {
		t.Skip(err)
	}
	real, err := filepath.EvalSymlinks(bin)
	if err != nil {
		t.Fatal(err)
	}
	rest, ok := strings.CutPrefix(real, "/nix/store/")
	if !ok {
		t.Skipf("%s is not in /nix/store", real)
	}
	return "/nix/store/" + strings.SplitN(rest, "/", 2)[0]
}

// signedStorePath returns a local path carrying a signature by the named key,
// with that signature. Skips if the store has none, which is the case only for
// a machine that built everything from source.
func signedStorePath(t *testing.T, keyName string) (path, sig string) {
	t.Helper()
	out, err := exec.Command("nix-store", "--query", "--requisites", ownStorePath(t)).Output()
	if err != nil {
		t.Skip(err)
	}
	for _, p := range strings.Fields(string(out)) {
		inf, err := query(p)
		if err != nil || len(inf.references) < 2 {
			continue
		}
		sigs, err := exec.Command("nix", "path-info", "--sigs", p).Output()
		if err != nil {
			continue
		}
		for _, f := range strings.Fields(string(sigs)) {
			if s, ok := strings.CutPrefix(f, keyName+":"); ok {
				return p, s
			}
		}
	}
	t.Skipf("no local path is signed by %s", keyName)
	return "", ""
}

func readBody(t *testing.T, resp *http.Response) string {
	t.Helper()
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
