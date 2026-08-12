package approve

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func run(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	cmd.Env = append(os.Environ(),
		"GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null",
		"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	return strings.TrimSpace(string(out))
}

func write(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func commit(t *testing.T, dir, msg string) {
	t.Helper()
	run(t, dir, "add", "-A")
	run(t, dir, "commit", "-q", "-m", msg)
}

// newRepo builds the shape the guest's pushes leave behind: a base branch,
// and an agent branch two commits ahead of it. The agent's commits touch
// agent.txt and the base's history does not, so a diverging base does not
// also produce a content conflict.
func newRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	run(t, dir, "init", "-q", "-b", "main")
	write(t, dir, "base.txt", "base\n")
	commit(t, dir, "base commit")
	run(t, dir, "checkout", "-q", "-b", "agent-1/main")
	write(t, dir, "agent.txt", "one\n")
	commit(t, dir, "agent commit one")
	write(t, dir, "agent.txt", "one\ntwo\n")
	commit(t, dir, "agent commit two")
	run(t, dir, "checkout", "-q", "main")
	return dir
}

func tip(t *testing.T, dir, branch string) string {
	t.Helper()
	return run(t, dir, "rev-parse", "refs/heads/"+branch)
}

func TestPendingFindsTheAgentBranch(t *testing.T) {
	dir := newRepo(t)
	got, err := Pending(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d candidates, want 1: %+v", len(got), got)
	}
	want := Candidate{Branch: "agent-1/main", Base: "main", Agent: "1", Ahead: 2}
	if got[0] != want {
		t.Fatalf("got %+v, want %+v", got[0], want)
	}
}

func TestPendingIgnoresABranchThatIsNotAhead(t *testing.T) {
	dir := newRepo(t)
	run(t, dir, "merge", "--ff-only", "agent-1/main")
	got, err := Pending(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Fatalf("still pending after landing: %+v", got)
	}
}

// An agent branch whose base has been deleted has nowhere to land, so it is
// not a candidate rather than an error at approve time.
func TestPendingIgnoresABranchWithNoBase(t *testing.T) {
	dir := newRepo(t)
	run(t, dir, "checkout", "-q", "-b", "agent-3/gone", "agent-1/main")
	run(t, dir, "checkout", "-q", "main")
	got, err := Pending(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range got {
		if c.Agent == "3" {
			t.Fatalf("a branch with no base is a candidate: %+v", c)
		}
	}
}

func TestResolveReportsNothingPending(t *testing.T) {
	dir := newRepo(t)
	run(t, dir, "branch", "-D", "agent-1/main")
	if _, err := Resolve(dir, ""); !errors.Is(err, ErrNothingPending) {
		t.Fatalf("got %v", err)
	}
}

// Landing the wrong agent's work is not something you find out about until
// later, so an unnamed agent with two candidates is refused, not guessed.
func TestResolveRefusesToGuessBetweenAgents(t *testing.T) {
	dir := newRepo(t)
	run(t, dir, "checkout", "-q", "-b", "agent-2/main", "main")
	write(t, dir, "other.txt", "other\n")
	commit(t, dir, "the other agent")
	run(t, dir, "checkout", "-q", "main")

	var amb *AmbiguousError
	_, err := Resolve(dir, "")
	if !errors.As(err, &amb) {
		t.Fatalf("got %v, want an ambiguity", err)
	}
	if len(amb.Candidates) != 2 {
		t.Fatalf("got %d candidates", len(amb.Candidates))
	}
	c, err := Resolve(dir, "2")
	if err != nil {
		t.Fatal(err)
	}
	if c.Branch != "agent-2/main" {
		t.Fatalf("naming an agent picked %s", c.Branch)
	}
}

func TestLandFastForwardsACheckedOutBase(t *testing.T) {
	dir := newRepo(t)
	want := tip(t, dir, "agent-1/main")
	c, err := Resolve(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	n, err := Land(dir, c)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("landed %d commits, want 2", n)
	}
	if got := tip(t, dir, "main"); got != want {
		t.Fatalf("main is at %s, want %s", got, want)
	}
	// The worktree has to come along, or every landed file shows up as a
	// staged deletion in the shell you are standing in.
	body, err := os.ReadFile(filepath.Join(dir, "agent.txt"))
	if err != nil || string(body) != "one\ntwo\n" {
		t.Fatalf("worktree not updated: %q %v", body, err)
	}
}

func TestLandMovesABaseThatIsNotCheckedOut(t *testing.T) {
	dir := newRepo(t)
	run(t, dir, "checkout", "-q", "-b", "sidetrack")
	want := tip(t, dir, "agent-1/main")
	c, _ := Resolve(dir, "")
	if _, err := Land(dir, c); err != nil {
		t.Fatal(err)
	}
	if got := tip(t, dir, "main"); got != want {
		t.Fatalf("main is at %s, want %s", got, want)
	}
	if got := run(t, dir, "symbolic-ref", "--short", "HEAD"); got != "sidetrack" {
		t.Fatalf("HEAD moved to %s", got)
	}
}

// The real target: the project's shared bare repo, where agent branches
// actually land. Nobody is standing in it, so the ref just moves, and HEAD
// naming the base must not be read as "the base is checked out".
func TestLandMovesTheBaseInABareRepo(t *testing.T) {
	src := newRepo(t)
	bare := filepath.Join(t.TempDir(), "shared.git")
	run(t, ".", "clone", "--bare", "-q", src, bare)

	c, err := Resolve(bare, "")
	if err != nil {
		t.Fatal(err)
	}
	want := tip(t, bare, "agent-1/main")
	n, err := Land(bare, c)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("landed %d commits, want 2", n)
	}
	if got := tip(t, bare, "main"); got != want {
		t.Fatalf("main is at %s, want %s", got, want)
	}
}

func TestLandRefusesWhenTheBaseHasMovedOn(t *testing.T) {
	dir := newRepo(t)
	write(t, dir, "local.txt", "local\n")
	commit(t, dir, "base moved on")
	before := tip(t, dir, "main")

	c, _ := Resolve(dir, "")
	var nff *NotFastForwardError
	if _, err := Land(dir, c); !errors.As(err, &nff) {
		t.Fatalf("got %v, want a non-fast-forward", err)
	}
	if got := tip(t, dir, "main"); got != before {
		t.Fatal("a refused approve moved the base anyway")
	}
}

// git's own rule, not one invented here: uncommitted work that the landing
// commits would overwrite stops the merge.
func TestLandRefusesOverUncommittedWork(t *testing.T) {
	dir := newRepo(t)
	write(t, dir, "agent.txt", "mine\n")
	before := tip(t, dir, "main")

	c, _ := Resolve(dir, "")
	if _, err := Land(dir, c); err == nil {
		t.Fatal("landed over uncommitted work")
	}
	if got := tip(t, dir, "main"); got != before {
		t.Fatal("a refused approve moved the base anyway")
	}
}

func TestReviewOutput(t *testing.T) {
	dir := newRepo(t)
	c, _ := Resolve(dir, "")
	log, err := Log(dir, c)
	if err != nil || !strings.Contains(log, "agent commit two") {
		t.Fatalf("log %q %v", log, err)
	}
	stat, err := Stat(dir, c)
	if err != nil || !strings.Contains(stat, "agent.txt") {
		t.Fatalf("stat %q %v", stat, err)
	}
	diff, err := Diff(dir, c)
	if err != nil || !strings.Contains(diff, "+two") {
		t.Fatalf("diff %q %v", diff, err)
	}
}
