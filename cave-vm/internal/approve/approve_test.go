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
	out, err := tryRun(dir, args...)
	if err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	return out
}

func tryRun(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	cmd.Env = append(os.Environ(),
		"GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null",
		"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t")
	out, err := cmd.CombinedOutput()
	return strings.TrimSpace(string(out)), err
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

// world is the shape the flow actually runs in: the human's working checkout,
// and the shared bare repo it knows as `cave`, which is the only place the
// agent's branches exist. Nothing here is ever the agent's own checkout --
// from the host's side that is on the far end of a git daemon.
type world struct{ repo, shared string }

// newWorld builds a base branch, and an agent branch two commits ahead of it
// in the shared repo only. The agent's commits touch agent.txt and the base's
// history does not, so a diverging base does not also produce a content
// conflict.
func newWorld(t *testing.T) world {
	t.Helper()
	dir := t.TempDir()
	w := world{repo: filepath.Join(dir, "repo"), shared: filepath.Join(dir, "cave-shared.git")}
	if err := os.Mkdir(w.repo, 0o755); err != nil {
		t.Fatal(err)
	}
	run(t, w.repo, "init", "-q", "-b", "main")
	write(t, w.repo, "base.txt", "base\n")
	commit(t, w.repo, "base commit")

	run(t, ".", "clone", "--bare", "-q", w.repo, w.shared)
	run(t, w.shared, "remote", "remove", "origin")
	run(t, w.repo, "remote", "add", Remote, w.shared)

	w.agentPushes(t, "1", "main", "one\n", "agent commit one")
	w.agentPushes(t, "1", "main", "one\ntwo\n", "agent commit two")
	return w
}

// agentPushes puts a commit on agent-<id>/<base> in the shared repo without
// leaving anything behind in the checkout, which is what a push over the
// vsock bridge looks like from here. It builds on whatever that branch
// already has, or forks the base, or -- for a branch whose base does not
// exist at all -- whatever is under HEAD.
func (w world) agentPushes(t *testing.T, id, base, body, msg string) string {
	t.Helper()
	back := head(t, w.repo)
	branch := "agent-" + id + "/" + base
	// What the shared repo has, not what this checkout knows about it: the
	// tracking ref is deliberately left stale below, and the agent builds on
	// its own branch regardless of whether the host has fetched it.
	start, err := tryRun(w.shared, "rev-parse", "--verify", "--quiet", "refs/heads/"+branch)
	if err != nil || start == "" {
		start = base
		if _, err := tryRun(w.repo, "rev-parse", "--verify", "--quiet", "refs/heads/"+base); err != nil {
			start = back
		}
	}
	run(t, w.repo, "checkout", "-q", "-B", "tmp-agent", start)
	write(t, w.repo, "agent.txt", body)
	commit(t, w.repo, msg)
	sha := run(t, w.repo, "rev-parse", "HEAD")
	run(t, w.repo, "push", "-q", "--force", Remote, "HEAD:refs/heads/"+branch)
	// The real push arrives over a git daemon, so this checkout learns about
	// it only by fetching. Pushing from here updates the tracking ref as a
	// side effect, which would hide the staleness these tests are about.
	tryRun(w.repo, "update-ref", "-d", "refs/remotes/"+Remote+"/"+branch)
	// Switching back drops agent.txt, or restores the version the branch
	// being returned to has: it is tracked either way.
	run(t, w.repo, "checkout", "-q", back)
	run(t, w.repo, "branch", "-qD", "tmp-agent")
	return sha
}

func (w world) fetch(t *testing.T) {
	t.Helper()
	if err := Fetch(w.repo); err != nil {
		t.Fatal(err)
	}
}

func (w world) resolve(t *testing.T, agent string) Candidate {
	t.Helper()
	w.fetch(t)
	c, err := Resolve(w.repo, agent)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func tip(t *testing.T, dir, ref string) string {
	t.Helper()
	return run(t, dir, "rev-parse", ref)
}

func head(t *testing.T, dir string) string {
	t.Helper()
	return run(t, dir, "symbolic-ref", "--short", "HEAD")
}

func TestPendingFindsTheAgentBranch(t *testing.T) {
	w := newWorld(t)
	w.fetch(t)
	got, err := Pending(w.repo)
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

// The bug this whole package was restructured for: a push the checkout has
// not fetched is still pending, and reading stale remote-tracking refs is how
// an approve reports success over work nobody can see.
func TestPendingSeesAPushItHasNotFetchedYet(t *testing.T) {
	w := newWorld(t)
	w.fetch(t)
	w.agentPushes(t, "2", "main", "second agent\n", "another agent's commit")

	before, err := Pending(w.repo)
	if err != nil {
		t.Fatal(err)
	}
	if len(before) != 1 {
		t.Fatalf("stale refs already show %d candidates", len(before))
	}
	w.fetch(t)
	after, err := Pending(w.repo)
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != 2 {
		t.Fatalf("got %d candidates after fetching, want 2: %+v", len(after), after)
	}
}

func TestPendingIgnoresABranchThatIsNotAhead(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	if _, err := Land(w.repo, c, ""); err != nil {
		t.Fatal(err)
	}
	w.fetch(t)
	got, err := Pending(w.repo)
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
	w := newWorld(t)
	w.agentPushes(t, "3", "gone", "orphan\n", "no base for this")
	w.fetch(t)
	got, err := Pending(w.repo)
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
	w := newWorld(t)
	run(t, w.shared, "branch", "-D", "agent-1/main")
	w.fetch(t)
	if _, err := Resolve(w.repo, ""); !errors.Is(err, ErrNothingPending) {
		t.Fatalf("got %v", err)
	}
}

// Landing the wrong agent's work is not something you find out about until
// later, so an unnamed agent with two candidates is refused, not guessed.
func TestResolveRefusesToGuessBetweenAgents(t *testing.T) {
	w := newWorld(t)
	w.agentPushes(t, "2", "main", "other\n", "the other agent")
	w.fetch(t)

	var amb *AmbiguousError
	_, err := Resolve(w.repo, "")
	if !errors.As(err, &amb) {
		t.Fatalf("got %v, want an ambiguity", err)
	}
	if len(amb.Candidates) != 2 {
		t.Fatalf("got %d candidates", len(amb.Candidates))
	}
	c, err := Resolve(w.repo, "2")
	if err != nil {
		t.Fatal(err)
	}
	if c.Branch != "agent-2/main" {
		t.Fatalf("naming an agent picked %s", c.Branch)
	}
}

// Standing on an agent branch is an answer: review put you there, so approve
// does not ask which agent again.
func TestResolvePrefersTheBranchYouAreStandingOn(t *testing.T) {
	w := newWorld(t)
	w.agentPushes(t, "2", "main", "other\n", "the other agent")
	w.fetch(t)
	if err := Checkout(w.repo, Candidate{Branch: "agent-2/main", Base: "main", Agent: "2"}); err != nil {
		t.Fatal(err)
	}
	c, err := Resolve(w.repo, "")
	if err != nil {
		t.Fatalf("got %v, want the branch under HEAD", err)
	}
	if c.Branch != "agent-2/main" {
		t.Fatalf("resolved %s while standing on agent-2/main", c.Branch)
	}
}

func TestCheckoutPutsTheWorktreeOnTheAgentsCode(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	if err := Checkout(w.repo, c); err != nil {
		t.Fatal(err)
	}
	if got := head(t, w.repo); got != "agent-1/main" {
		t.Fatalf("HEAD is on %s", got)
	}
	body, err := os.ReadFile(filepath.Join(w.repo, "agent.txt"))
	if err != nil || string(body) != "one\ntwo\n" {
		t.Fatalf("worktree is not the agent's: %q %v", body, err)
	}
}

// Reviewing twice with a push in between: the agent's history is the truth,
// so a local copy that has drifted from it is reset rather than merged.
func TestCheckoutResetsALocalCopyThatHasDrifted(t *testing.T) {
	w := newWorld(t)
	if err := Checkout(w.repo, w.resolve(t, "")); err != nil {
		t.Fatal(err)
	}
	write(t, w.repo, "local.txt", "not the agent's\n")
	commit(t, w.repo, "a commit only this checkout has")

	want := w.agentPushes(t, "1", "main", "one\ntwo\nthree\n", "agent commit three")
	c := w.resolve(t, "")
	if err := Checkout(w.repo, c); err != nil {
		t.Fatal(err)
	}
	if got := tip(t, w.repo, "HEAD"); got != want {
		t.Fatalf("HEAD is at %s, want the pushed tip %s", got, want)
	}
	if _, err := os.Stat(filepath.Join(w.repo, "local.txt")); !os.IsNotExist(err) {
		t.Fatal("the drifted commit survived the reset")
	}
}

// The ordinary flow, end to end: review, then approve, and you come away
// standing on the base with the work in it. This is what the shared-repo-only
// version got wrong -- it moved a ref the checkout could not see.
func TestLandLeavesYouOnTheBaseWithTheWork(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	want := tip(t, w.repo, c.Ref())
	if err := Checkout(w.repo, c); err != nil {
		t.Fatal(err)
	}

	n, err := Land(w.repo, c, "")
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("landed %d commits, want 2", n)
	}
	if got := head(t, w.repo); got != "main" {
		t.Fatalf("approve left HEAD on %s", got)
	}
	if got := tip(t, w.repo, "refs/heads/main"); got != want {
		t.Fatalf("local main is at %s, want %s", got, want)
	}
	// The worktree has to come along, or every landed file shows up as a
	// staged deletion in the shell you are standing in.
	body, err := os.ReadFile(filepath.Join(w.repo, "agent.txt"))
	if err != nil || string(body) != "one\ntwo\n" {
		t.Fatalf("worktree not updated: %q %v", body, err)
	}
}

// The agent's origin has to move too, or its next rebase is onto a base that
// is missing its own landed work.
func TestLandMovesTheBaseInTheSharedRepo(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	want := tip(t, w.repo, c.Ref())
	if _, err := Land(w.repo, c, ""); err != nil {
		t.Fatal(err)
	}
	if got := tip(t, w.shared, "refs/heads/main"); got != want {
		t.Fatalf("the shared repo's main is at %s, want %s", got, want)
	}
}

// A branch with nothing left on it is a phantom: invisible in status,
// confusing in review. The agent's next push recreates it.
func TestLandCleansUpTheAgentBranch(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	if err := Checkout(w.repo, c); err != nil {
		t.Fatal(err)
	}
	if _, err := Land(w.repo, c, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := tryRun(w.shared, "rev-parse", "--verify", "--quiet", "refs/heads/"+c.Branch); err == nil {
		t.Fatal("the agent branch is still in the shared repo")
	}
	if _, err := tryRun(w.repo, "rev-parse", "--verify", "--quiet", "refs/heads/"+c.Branch); err == nil {
		t.Fatal("the local copy of the agent branch survived")
	}
	if _, err := tryRun(w.repo, "rev-parse", "--verify", "--quiet", c.Ref()); err == nil {
		t.Fatal("the remote-tracking ref was not pruned")
	}
}

func TestLandFromTheBaseBranch(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	want := tip(t, w.repo, c.Ref())
	if got := head(t, w.repo); got != "main" {
		t.Fatalf("this test means to start on the base, not %s", got)
	}
	if _, err := Land(w.repo, c, ""); err != nil {
		t.Fatal(err)
	}
	if got := tip(t, w.repo, "refs/heads/main"); got != want {
		t.Fatalf("local main is at %s, want %s", got, want)
	}
}

func TestLandRefusesWhenTheBaseHasMovedOn(t *testing.T) {
	w := newWorld(t)
	write(t, w.repo, "local.txt", "local\n")
	commit(t, w.repo, "base moved on")
	run(t, w.repo, "push", "-q", Remote, "main")
	w.fetch(t)
	before := tip(t, w.shared, "refs/heads/main")

	c, err := Resolve(w.repo, "")
	if err != nil {
		t.Fatal(err)
	}
	var nff *NotFastForwardError
	if _, err := Land(w.repo, c, ""); !errors.As(err, &nff) {
		t.Fatalf("got %v, want a non-fast-forward", err)
	}
	if got := tip(t, w.shared, "refs/heads/main"); got != before {
		t.Fatal("a refused approve moved the base anyway")
	}
}

// git's own rule, not one invented here: uncommitted work that the landing
// commits would overwrite stops the merge. Nothing else has moved when it
// does, which is why the checkout is the first thing Land touches.
func TestLandRefusesOverUncommittedWork(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	write(t, w.repo, "agent.txt", "mine\n")
	before := tip(t, w.shared, "refs/heads/main")

	if _, err := Land(w.repo, c, ""); err == nil {
		t.Fatal("landed over uncommitted work")
	}
	if got := tip(t, w.shared, "refs/heads/main"); got != before {
		t.Fatal("a refused approve moved the shared base anyway")
	}
	if _, err := tryRun(w.shared, "rev-parse", "--verify", "--quiet", "refs/heads/"+c.Branch); err != nil {
		t.Fatal("a refused approve deleted the agent branch")
	}
}

func TestReviewOutput(t *testing.T) {
	w := newWorld(t)
	c := w.resolve(t, "")
	log, err := Log(w.repo, c)
	if err != nil || !strings.Contains(log, "agent commit two") {
		t.Fatalf("log %q %v", log, err)
	}
	stat, err := Stat(w.repo, c)
	if err != nil || !strings.Contains(stat, "agent.txt") {
		t.Fatalf("stat %q %v", stat, err)
	}
}

// A checkout with no shared repo is a situation to name, not a git error to
// pass through: it means nothing has ever created one for this project.
func TestNoRemoteIsItsOwnAnswer(t *testing.T) {
	dir := t.TempDir()
	run(t, dir, "init", "-q", "-b", "main")
	if _, err := Shared(dir); !errors.Is(err, ErrNoRemote) {
		t.Fatalf("got %v, want ErrNoRemote", err)
	}
	if err := Fetch(dir); !errors.Is(err, ErrNoRemote) {
		t.Fatalf("got %v, want ErrNoRemote", err)
	}
}

// Reviewing is reading, and reading often ends in "the first of these is
// right and the second is not". Without partial approval the answer is all or
// nothing, and the way out is asking an agent to rewrite commits it has
// already pushed.
func TestLandUpToACommitLeavesTheRestPending(t *testing.T) {
	w := newWorld(t)
	if err := Fetch(w.repo); err != nil {
		t.Fatal(err)
	}
	c, err := Resolve(w.repo, "1")
	if err != nil {
		t.Fatal(err)
	}
	if c.Ahead != 2 {
		t.Fatalf("expected two pending commits, got %d", c.Ahead)
	}
	first := run(t, w.repo, "rev-parse", c.Ref()+"~1")

	n, err := Land(w.repo, c, first)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("landed %d commits, want 1", n)
	}
	if got := run(t, w.repo, "rev-parse", "main"); got != first {
		t.Fatalf("main is at %s, want %s", got, first)
	}
	// The branch is the work that was held back, so deleting it would throw
	// away exactly what was not approved.
	if _, err := tryRun(w.shared, "rev-parse", "--verify", "--quiet",
		"refs/heads/"+c.Branch); err != nil {
		t.Fatal("the agent branch was deleted with work still on it")
	}
	if err := Fetch(w.repo); err != nil {
		t.Fatal(err)
	}
	rest, err := Pending(w.repo)
	if err != nil || len(rest) != 1 || rest[0].Ahead != 1 {
		t.Fatalf("what is left is %+v (%v), want one commit still pending", rest, err)
	}
}

// A commit off the branch would land work that was never under review, which
// is the one thing approving must not do.
func TestLandRefusesACommitThatIsNotOnTheBranch(t *testing.T) {
	w := newWorld(t)
	if err := Fetch(w.repo); err != nil {
		t.Fatal(err)
	}
	c, err := Resolve(w.repo, "1")
	if err != nil {
		t.Fatal(err)
	}
	// A commit that exists but is on nobody's agent branch.
	run(t, w.repo, "checkout", "-q", "-B", "elsewhere", "main")
	write(t, w.repo, "sneaky.txt", "not reviewed\n")
	commit(t, w.repo, "unreviewed work")
	off := run(t, w.repo, "rev-parse", "HEAD")

	if _, err := Land(w.repo, c, off); err == nil {
		t.Fatal("landed a commit that was not on the agent's branch")
	}
	if got := run(t, w.repo, "rev-parse", "main"); got == off {
		t.Fatal("the base moved to unreviewed work")
	}
}

// Already on the base means the landing would move nothing, which is a typo
// rather than a request.
func TestLandRefusesACommitAlreadyOnTheBase(t *testing.T) {
	w := newWorld(t)
	if err := Fetch(w.repo); err != nil {
		t.Fatal(err)
	}
	c, err := Resolve(w.repo, "1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Land(w.repo, c, run(t, w.repo, "rev-parse", c.BaseRef())); err == nil {
		t.Fatal("accepted a commit the base already has")
	}
}

// The confirmation has to name the number actually at stake: asking about the
// whole branch when one commit is being landed is how a yes means something
// nobody intended.
func TestWouldLandCountsOnlyWhatIsBeingLanded(t *testing.T) {
	w := newWorld(t)
	if err := Fetch(w.repo); err != nil {
		t.Fatal(err)
	}
	c, err := Resolve(w.repo, "1")
	if err != nil {
		t.Fatal(err)
	}
	whole, err := WouldLand(w.repo, c, "")
	if err != nil || whole != 2 {
		t.Fatalf("whole branch counts %d (%v), want 2", whole, err)
	}
	part, err := WouldLand(w.repo, c, run(t, w.repo, "rev-parse", c.Ref()+"~1"))
	if err != nil || part != 1 {
		t.Fatalf("partial counts %d (%v), want 1", part, err)
	}
}
