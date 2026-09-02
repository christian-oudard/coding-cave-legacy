package sessions

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	sidA = "0f9a1c2e-3b4d-4f5a-8b6c-7d8e9f0a1b2c"
	sidB = "11111111-2222-4333-8444-555555555555"
)

func tempDir(t *testing.T) string {
	t.Helper()
	d, err := os.MkdirTemp("", "sessions")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(d) })
	return d
}

// guest builds a Local over a fresh ~/.claude with the given transcripts.
func guest(t *testing.T, cwd string, files map[string]string) Local {
	t.Helper()
	home := tempDir(t)
	l := Local{ClaudeDir: filepath.Join(home, ".claude"), Cwd: cwd, Agent: "7"}
	dir := filepath.Join(l.projects(), EncodeProject(cwd))
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return l
}

func archive(t *testing.T, records map[string]string) Archive {
	t.Helper()
	a := Archive{Dir: tempDir(t)}
	for id, body := range records {
		if err := os.WriteFile(a.Path(id), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return a
}

func read(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestIdsThatAreNotSessionIdsAreRejected(t *testing.T) {
	for _, bad := range []string{
		"", "..", "../../etc/passwd", "/etc/passwd",
		"0f9a1c2e3b4d4f5a8b6c7d8e9f0a1b2c",
		"0f9a1c2e-3b4d-4f5a-8b6c-7d8e9f0a1b2", // short
		"0f9a1c2e-3b4d-4f5a-8b6c-7d8e9f0a1b2z", // not hex
		"0f9a1c2e-3b4d-4f5a-8b6c-7d8e9f0a1b2c/x",
	} {
		if ValidID(bad) {
			t.Errorf("accepted %q", bad)
		}
	}
	if !ValidID(sidA) {
		t.Error("rejected a real session id")
	}
}

func TestEncodeProjectMatchesClaudesLayout(t *testing.T) {
	if got := EncodeProject("/root/coding-cave"); got != "-root-coding-cave" {
		t.Fatalf("got %q", got)
	}
}

// The transcripts of a whole agent, not the one id we launched: /resume and
// background jobs both make sessions nobody predicted.
func TestListReportsEveryTranscriptAcrossProjects(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{
		sidA + ".jsonl": "one\n",
		"notes.md":      "ignored",
		"nonsense.jsonl": "ignored",
	})
	other := filepath.Join(l.projects(), EncodeProject("/root/other"))
	if err := os.MkdirAll(other, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(other, sidB+".jsonl"), []byte("two!\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	got, err := l.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("got %v", got)
	}
	sizes := map[string]int64{}
	for _, r := range got {
		sizes[r.ID] = r.Size
	}
	if sizes[sidA] != 4 || sizes[sidB] != 5 {
		t.Fatalf("got %v", sizes)
	}
}

func TestListOfAGuestWithNoSessionsIsEmpty(t *testing.T) {
	l := Local{ClaudeDir: filepath.Join(tempDir(t), ".claude")}
	got, err := l.List()
	if err != nil || len(got) != 0 {
		t.Fatalf("got %v, %v", got, err)
	}
}

func TestSyncCopiesOutWhatTheHostLacks(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "hello\n"})
	a := archive(t, nil)

	changes, err := Sync(l, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].Outcome != Added || changes[0].Err != nil {
		t.Fatalf("got %+v", changes)
	}
	if read(t, a.Path(sidA)) != "hello\n" {
		t.Fatal("record did not land")
	}
}

func TestSyncCopiesARecordThatGrew(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "one\ntwo\n"})
	a := archive(t, map[string]string{sidA: "one\n"})

	changes, err := Sync(l, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].Outcome != Grew {
		t.Fatalf("got %+v", changes)
	}
	if read(t, a.Path(sidA)) != "one\ntwo\n" {
		t.Fatal("record was not extended")
	}
}

func TestSyncSkipsARecordOfTheSameLength(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "one\n"})
	a := archive(t, map[string]string{sidA: "one\n"})

	changes, err := Sync(l, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 0 {
		t.Fatalf("fetched a record it already had: %+v", changes)
	}
}

// The corruption path: a truncated guest file must not replace a good record.
func TestAShorterRecordIsKeptAsideNotApplied(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "one\n"})
	a := archive(t, map[string]string{sidA: "one\ntwo\nthree\n"})

	changes, err := Sync(l, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].Outcome != Diverged || changes[0].Err != nil {
		t.Fatalf("got %+v", changes)
	}
	if read(t, a.Path(sidA)) != "one\ntwo\nthree\n" {
		t.Fatal("a shorter copy replaced the record")
	}
	aside := filepath.Join(a.Dir, divergedDir, sidA+".1.jsonl")
	if read(t, aside) != "one\n" {
		t.Fatal("the divergent copy was not kept")
	}
}

// What a length test alone misses: truncated, then grown past the old size.
func TestARewrittenRecordIsKeptAsideNotApplied(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "different!\n"})
	a := archive(t, map[string]string{sidA: "one\ntwo\n"})

	changes, err := Sync(l, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].Outcome != Diverged {
		t.Fatalf("got %+v", changes)
	}
	if read(t, a.Path(sidA)) != "one\ntwo\n" {
		t.Fatal("a rewritten copy replaced the record")
	}
}

// Nothing kept aside may be mistaken for a record, or the restore path would
// hand claude a transcript it cannot resume.
func TestDivergedCopiesAreNotRecords(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "x\n"})
	a := archive(t, map[string]string{sidA: "one\ntwo\n"})
	if _, err := Sync(l, a); err != nil {
		t.Fatal(err)
	}
	got, err := a.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].ID != sidA || got[0].Size != 8 {
		t.Fatalf("got %+v", got)
	}
}

func TestSyncIgnoresIdsThatAreNotSessionIds(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{
		"..-..-etc-passwd.jsonl": "no",
		sidA + ".jsonl":          "yes\n",
	})
	a := archive(t, nil)
	if _, err := Sync(l, a); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(a.Dir)
	if err != nil {
		t.Fatal(err)
	}
	// The record and its sidecar, and nothing carrying the name the guest
	// tried to use.
	for _, e := range entries {
		if !strings.HasPrefix(e.Name(), sidA) {
			t.Fatalf("a name from the guest reached the archive: %q", e.Name())
		}
	}
	if len(entries) != 2 {
		t.Fatalf("got %v", entries)
	}
}

// One bad record does not cost the others, because a checkpoint runs when
// something is about to be destroyed.
func TestSyncContinuesPastAFailure(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{
		sidA + ".jsonl": "a\n",
		sidB + ".jsonl": "b\n",
	})
	// Make one unreadable, which is what a vanished or broken file looks like.
	// A dangling symlink rather than chmod 0: the agent in the guest runs as
	// root, root ignores mode bits, and a test that simulates a failure by
	// removing permissions therefore stops simulating anything the moment it
	// runs where the agent actually lives. This fails for every user.
	bad := filepath.Join(l.projects(), EncodeProject("/root/coding-cave"), sidA+".jsonl")
	if err := os.Remove(bad); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(t.TempDir(), "gone"), bad); err != nil {
		t.Fatal(err)
	}
	a := archive(t, nil)

	changes, err := Sync(l, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 2 {
		t.Fatalf("got %+v", changes)
	}
	var failed, added int
	for _, c := range changes {
		if c.Err != nil {
			failed++
		} else if c.Outcome == Added {
			added++
		}
	}
	if failed != 1 || added != 1 {
		t.Fatalf("got %+v", changes)
	}
}

func TestRestorePutsARecordWhereClaudeLooksForIt(t *testing.T) {
	l := guest(t, "/root/coding-cave", nil)
	a := archive(t, map[string]string{sidA: "history\n"})

	changes, err := Restore(l, a, []string{sidA})
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].Err != nil || changes[0].Outcome != Added {
		t.Fatalf("got %+v", changes)
	}
	want := filepath.Join(l.projects(), "-root-coding-cave", sidA+".jsonl")
	if read(t, want) != "history\n" {
		t.Fatal("the record is not where claude will look")
	}
}

// The guest's copy is the live one; a restore must not overwrite it with an
// older checkpoint of itself.
func TestRestoreLeavesARecordTheGuestAlreadyHas(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "live and longer\n"})
	a := archive(t, map[string]string{sidA: "old\n"})

	changes, err := Restore(l, a, []string{sidA})
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].Outcome != Unchanged {
		t.Fatalf("got %+v", changes)
	}
	live := filepath.Join(l.projects(), "-root-coding-cave", sidA+".jsonl")
	if read(t, live) != "live and longer\n" {
		t.Fatal("restore clobbered the live transcript")
	}
}

func TestRestoreOfAnIdTheArchiveLacksReportsIt(t *testing.T) {
	l := guest(t, "/root/coding-cave", nil)
	a := archive(t, nil)

	changes, err := Restore(l, a, []string{sidA})
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].Err == nil {
		t.Fatalf("got %+v", changes)
	}
}

func TestParseListDropsMalformedLines(t *testing.T) {
	got, err := parseList(strings.NewReader(
		sidA + " 42\n" +
			"garbage\n" +
			"../../etc/passwd 10\n" +
			sidB + " notanumber\n" +
			sidB + " 7\n"))
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != (Record{ID: sidA, Size: 42}) || got[1] != (Record{ID: sidB, Size: 7}) {
		t.Fatalf("got %+v", got)
	}
}

func TestSyncRecordsWhoOwnsARecord(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "hi\n"})
	a := archive(t, nil)
	if _, err := Sync(l, a); err != nil {
		t.Fatal(err)
	}
	m := a.ReadMeta(sidA)
	if m.Agent != "7" || m.Project != "-root-coding-cave" || m.First.IsZero() {
		t.Fatalf("got %+v", m)
	}
	owned, err := a.Owned("7")
	if err != nil || len(owned) != 1 || owned[0] != sidA {
		t.Fatalf("got %v, %v", owned, err)
	}
	if other, _ := a.Owned("8"); len(other) != 0 {
		t.Fatalf("another agent claimed it: %v", other)
	}
}

// Each agent's records are its own, and a sync says so without being told.
//
// This is the failure it was written for: the host used to pass an agent into
// Sync and label everything it copied with it, while the guest returned one
// home's transcripts however many agents it held. So a sync copied the root
// agent's sessions, filed them under whichever agent had been named, and never
// copied anyone else's at all.
func TestSyncAttributesEachHomeToItsOwnAgent(t *testing.T) {
	one := guest(t, "/projects/p", map[string]string{sidA + ".jsonl": "from one\n"})
	one.Agent = "1"
	two := guest(t, "/projects/p", map[string]string{sidB + ".jsonl": "from two\n"})
	two.Agent = "2"
	a := archive(t, nil)

	for _, g := range []Local{one, two} {
		if _, err := Sync(g, a); err != nil {
			t.Fatal(err)
		}
	}

	if got := a.ReadMeta(sidA).Agent; got != "1" {
		t.Fatalf("agent 1's record is attributed to %q", got)
	}
	if got := a.ReadMeta(sidB).Agent; got != "2" {
		t.Fatalf("agent 2's record is attributed to %q", got)
	}
	for agent, want := range map[string]string{"1": sidA, "2": sidB} {
		owned, err := a.Owned(agent)
		if err != nil || len(owned) != 1 || owned[0] != want {
			t.Fatalf("agent %s owns %v (%v), want %s", agent, owned, err, want)
		}
	}
}

// A restore puts a record back for the agent that wrote it, not for whoever
// asked. The archive is the only thing that still knows -- the guest lost the
// record, which is why it is being restored -- so Restore reads the owner off
// the metadata and hands it to the guest, which is the only side that can turn
// an agent into a home.
func TestRestoreAsksForTheOwningAgentsHome(t *testing.T) {
	src := guest(t, "/projects/p", map[string]string{sidA + ".jsonl": "agent three's\n"})
	src.Agent = "3"
	a := archive(t, nil)
	if _, err := Sync(src, a); err != nil {
		t.Fatal(err)
	}

	var askedFor string
	empty := &recordingGuest{Local: guest(t, "/projects/p", nil), seen: &askedFor}
	if _, err := Restore(empty, a, []string{sidA}); err != nil {
		t.Fatal(err)
	}
	if askedFor != "3" {
		t.Fatalf("restored into agent %q's home, want 3", askedFor)
	}
}

// recordingGuest is a Local that remembers which agent a write was aimed at.
type recordingGuest struct {
	Local
	seen *string
}

func (g *recordingGuest) Write(id string, r io.Reader, dest Dest, agent string) error {
	*g.seen = agent
	return g.Local.Write(id, r, dest, agent)
}

// The archive remembers when a session appeared, not just when it was last
// touched, so a later sync does not rewrite its history.
func TestSyncKeepsTheFirstSighting(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "one\n"})
	a := archive(t, nil)
	if _, err := Sync(l, a); err != nil {
		t.Fatal(err)
	}
	first := a.ReadMeta(sidA).First

	path := filepath.Join(l.projects(), EncodeProject("/root/coding-cave"), sidA+".jsonl")
	if err := os.WriteFile(path, []byte("one\ntwo\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Sync(l, a); err != nil {
		t.Fatal(err)
	}
	if got := a.ReadMeta(sidA).First; !got.Equal(first) {
		t.Fatalf("first moved from %v to %v", first, got)
	}
}

// A record whose bytes did not land must not get metadata claiming it did.
func TestADivergedRecordDoesNotGetMetadata(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "x\n"})
	a := archive(t, map[string]string{sidA: "one\ntwo\n"})
	if _, err := Sync(l, a); err != nil {
		t.Fatal(err)
	}
	if m := a.ReadMeta(sidA); m.Agent != "" {
		t.Fatalf("got %+v", m)
	}
}

// Sidecars are not records: a stray .meta must never look like a session.
func TestMetadataFilesAreNotListedAsRecords(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "hi\n"})
	a := archive(t, nil)
	if _, err := Sync(l, a); err != nil {
		t.Fatal(err)
	}
	got, err := a.List()
	if err != nil || len(got) != 1 || got[0].ID != sidA {
		t.Fatalf("got %+v, %v", got, err)
	}
}

// Every agent's records are readable, without appearing in anyone's picker.
func TestShareCopiesEveryRecordOutsideThePicker(t *testing.T) {
	l := guest(t, "/root/coding-cave", nil)
	l.SharedDir = filepath.Join(tempDir(t), "cave-sessions")
	a := archive(t, map[string]string{sidA: "mine\n", sidB: "theirs\n"})

	changes, err := Share(l, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 2 {
		t.Fatalf("got %+v", changes)
	}
	if read(t, filepath.Join(l.SharedDir, sidB+".jsonl")) != "theirs\n" {
		t.Fatal("another agent's record is not readable")
	}
	// Not in the picker: List only reports what claude would offer.
	got, err := l.List()
	if err != nil || len(got) != 0 {
		t.Fatalf("shared records leaked into the picker: %+v", got)
	}
}

// The shared copy is disposable, so a second share overwrites rather than
// failing the way a restore into the picker does.
func TestShareReplacesAnOlderSharedCopy(t *testing.T) {
	l := guest(t, "/root/coding-cave", nil)
	l.SharedDir = filepath.Join(tempDir(t), "cave-sessions")
	a := archive(t, map[string]string{sidA: "one\n"})
	if _, err := Share(l, a); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(a.Path(sidA), []byte("one\ntwo\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	changes, err := Share(l, a)
	if err != nil || changes[0].Err != nil {
		t.Fatalf("got %+v, %v", changes, err)
	}
	if read(t, filepath.Join(l.SharedDir, sidA+".jsonl")) != "one\ntwo\n" {
		t.Fatal("the shared copy was not refreshed")
	}
}

// The project is metadata, and optional: a guest that does not report it still
// syncs rather than being dropped as malformed.
func TestParseListTakesTheProjectWhenGiven(t *testing.T) {
	got, err := parseList(strings.NewReader(
		sidA + " 42 -root-coding-cave\n" +
			sidB + " 7\n" +
			sidA + " 1 a b c\n"))
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].Project != "-root-coding-cave" || got[1].Project != "" {
		t.Fatalf("got %+v", got)
	}
}

// The shared view exists so an agent can read another agent's session. It was
// written 0600 and owned by whoever ran the daemon, which is root, so every
// agent that was not root got permission denied -- and got it silently, since
// nothing reads this directory until somebody goes looking for a session that
// is not their own.
func TestSharedRecordsAreReadableByEveryAgent(t *testing.T) {
	l := guest(t, "/projects/p", nil)
	l.SharedDir = filepath.Join(tempDir(t), "shared")

	if err := l.Write(sidA, strings.NewReader("shared\n"), Shared, ""); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(filepath.Join(l.SharedDir, sidA+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm()&0o044 != 0o044 {
		t.Fatalf("shared record is %v; no other agent can read it", fi.Mode().Perm())
	}
}

// A record copied in before the mode was fixed has to become readable when it
// is next written, which O_TRUNC alone does not do: open applies a mode only
// to a file it creates.
func TestRewritingASharedRecordFixesItsMode(t *testing.T) {
	l := guest(t, "/projects/p", nil)
	l.SharedDir = filepath.Join(tempDir(t), "shared")
	if err := os.MkdirAll(l.SharedDir, 0o755); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(l.SharedDir, sidA+".jsonl")
	if err := os.WriteFile(stale, []byte("old\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	if err := l.Write(sidA, strings.NewReader("new\n"), Shared, ""); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(stale)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm()&0o044 != 0o044 {
		t.Fatalf("rewritten record is still %v", fi.Mode().Perm())
	}
}

// An agent's own transcripts stay its own. The shared view is the place for
// reading each other's; a home is not.
func TestRestoredRecordsStayPrivateToTheirAgent(t *testing.T) {
	l := guest(t, "/projects/p", nil)
	if err := l.Write(sidB, strings.NewReader("mine\n"), Resumable, ""); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(filepath.Join(l.projects(), EncodeProject("/projects/p"), sidB+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("a record in an agent's home is %v, want 0600", fi.Mode().Perm())
	}
}
