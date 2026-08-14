package sessions

import (
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
	l := Local{ClaudeDir: filepath.Join(home, ".claude"), Cwd: cwd}
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

func TestCheckpointCopiesOutWhatTheHostLacks(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "hello\n"})
	a := archive(t, nil)

	changes, err := Checkpoint(l, a)
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

func TestCheckpointCopiesARecordThatGrew(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "one\ntwo\n"})
	a := archive(t, map[string]string{sidA: "one\n"})

	changes, err := Checkpoint(l, a)
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

func TestCheckpointSkipsARecordOfTheSameLength(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{sidA + ".jsonl": "one\n"})
	a := archive(t, map[string]string{sidA: "one\n"})

	changes, err := Checkpoint(l, a)
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

	changes, err := Checkpoint(l, a)
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

	changes, err := Checkpoint(l, a)
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
	if _, err := Checkpoint(l, a); err != nil {
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

func TestCheckpointIgnoresIdsThatAreNotSessionIds(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{
		"..-..-etc-passwd.jsonl": "no",
		sidA + ".jsonl":          "yes\n",
	})
	a := archive(t, nil)
	if _, err := Checkpoint(l, a); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(a.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != sidA+".jsonl" {
		t.Fatalf("got %v", entries)
	}
}

// One bad record does not cost the others, because a checkpoint runs when
// something is about to be destroyed.
func TestCheckpointContinuesPastAFailure(t *testing.T) {
	l := guest(t, "/root/coding-cave", map[string]string{
		sidA + ".jsonl": "a\n",
		sidB + ".jsonl": "b\n",
	})
	// Make one unreadable, which is what a vanished or broken file looks like.
	bad := filepath.Join(l.projects(), EncodeProject("/root/coding-cave"), sidA+".jsonl")
	if err := os.Chmod(bad, 0); err != nil {
		t.Fatal(err)
	}
	a := archive(t, nil)

	changes, err := Checkpoint(l, a)
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
	if len(got) != 2 || got[0] != (Record{sidA, 42}) || got[1] != (Record{sidB, 7}) {
		t.Fatalf("got %+v", got)
	}
}
