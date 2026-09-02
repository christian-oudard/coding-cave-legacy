// Package approve is the last hop of the git ferry: taking what an agent
// pushed to agent-<id>/<base> and landing it on <base>.
//
// The agent commits in the guest and pushes over the vsock git bridge into
// the shared bare repo, where the pre-receive guard confines it to its own
// namespace. Moving that work onto a base branch is the one step it
// deliberately cannot take itself, so it is the one step a human runs.
//
// Everything here happens in the human's working checkout, against the
// remote-tracking refs of the `cave` remote, which is what `main`'s
// cav review/approve do (cav-rs/src/review.rs, cav-rs/src/approve.rs). The
// earlier version of this package moved refs inside the bare repo instead,
// which landed the commit somewhere the human's checkout could not see: the
// approve reported success and `git log` did not have it. Reviewing means
// standing on the agent's code with your own tools, and approving means
// coming away with it, so both verbs move the checkout.
package approve

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"codingcave.dev/cave-vm/internal/registry"
)

// Remote is the human checkout's name for the project's shared bare repo,
// the same name `main` uses (constants::CAVE_SHARED_REMOTE).
const Remote = "cave"

// Candidate is one agent branch holding commits its base does not have.
type Candidate struct {
	Branch string // agent-1/vm-cave
	Base   string // vm-cave
	Agent  string // 1
	Ahead  int    // commits the base does not have
	// Behind is commits the base has that the branch does not, which is what
	// makes a landing something other than a fast-forward. Non-zero alongside
	// Ahead means the two have diverged: the work is still pending, and
	// `approve` will refuse it until the agent rebases.
	//
	// Worth counting rather than discovering. It becomes non-zero the moment
	// somebody lands another agent's work, so the agent that was not landed
	// finds out by being refused -- after deciding to approve, which is the
	// wrong end of the operation to learn it at.
	Behind int
}

// Ref is the branch as the checkout knows it: a remote-tracking ref, since
// the shared repo is the only place the agent's work exists.
func (c Candidate) Ref() string { return Remote + "/" + c.Branch }

// BaseRef is the base as the shared repo has it, which is what a landing is
// measured against. The local branch of the same name may be behind it.
func (c Candidate) BaseRef() string { return Remote + "/" + c.Base }

// ErrNothingPending means no agent branch is ahead of its base. Not a
// failure of the command so much as an answer to it.
var ErrNothingPending = errors.New("nothing to approve")

// ErrNoRemote is the checkout having no `cave` remote, which means nothing
// has ever created the shared repo for it.
var ErrNoRemote = errors.New("no " + Remote + " remote in this checkout")

// AmbiguousError is more than one agent having work pending with no agent
// named. Landing the wrong agent's work is not something you find out about
// until later, so this never resolves itself by guessing.
type AmbiguousError struct{ Candidates []Candidate }

func (e *AmbiguousError) Error() string {
	return fmt.Sprintf("%d agents have work pending", len(e.Candidates))
}

// NotFastForwardError is the base having moved on since the agent forked.
type NotFastForwardError struct{ Branch, Base string }

func (e *NotFastForwardError) Error() string {
	return fmt.Sprintf("%s has moved on; %s has to be rebased onto it first", e.Base, e.Branch)
}

func git(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	out, err := cmd.Output()
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) && len(ee.Stderr) > 0 {
			return "", fmt.Errorf("git %s: %s", args[0], strings.TrimSpace(string(ee.Stderr)))
		}
		return "", fmt.Errorf("git %s: %w", args[0], err)
	}
	return strings.TrimSpace(string(out)), nil
}

// quiet runs a command whose failure is the answer rather than a problem.
func quiet(dir string, args ...string) bool {
	_, err := git(dir, args...)
	return err == nil
}

func exists(dir, ref string) bool {
	return quiet(dir, "rev-parse", "--verify", "--quiet", ref)
}

func countAhead(dir, base, branch string) (int, error) {
	out, err := git(dir, "rev-list", "--count", base+".."+branch)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(out)
}

// current is the checked-out branch, empty on a detached HEAD.
func current(dir string) string {
	out, _ := git(dir, "symbolic-ref", "--quiet", "--short", "HEAD")
	return out
}

// Shared is where the `cave` remote points: the project's shared bare repo.
// Read from the remote rather than assembled from a path, so one place
// decides where it lives and this agrees with it by construction.
func Shared(dir string) (string, error) {
	url, err := git(dir, "remote", "get-url", Remote)
	if err != nil || url == "" {
		return "", ErrNoRemote
	}
	return url, nil
}

// Fetch brings the shared repo's branches into this checkout's
// remote-tracking refs, which is what everything below reads. --prune, so a
// branch deleted by an earlier approve stops being offered.
//
// Every command starts with this: nothing announces a push, so a review that
// skipped it would show whatever the last fetch happened to catch. That was
// the other half of the missing-commit bug -- a stale cave/<base> makes an
// already-landed branch look pending, and a landed one look absent.
func Fetch(dir string) error {
	if _, err := Shared(dir); err != nil {
		return err
	}
	_, err := git(dir, "fetch", "--prune", Remote)
	return err
}

// Pending lists every agent branch with commits its base does not have,
// oldest agent id first. An agent branch whose base is gone, or that is not
// ahead of it, is not something to approve and is not listed.
func Pending(dir string) ([]Candidate, error) {
	out, err := git(dir, "for-each-ref", "--format=%(refname:short)",
		"refs/remotes/"+Remote+"/")
	if err != nil {
		return nil, err
	}
	var found []Candidate
	for _, ref := range strings.Split(out, "\n") {
		branch := strings.TrimPrefix(ref, Remote+"/")
		if branch == ref || branch == "HEAD" {
			continue
		}
		// Only the first prefix is the agent's: agent-1/agent-8/vm-cave is
		// agent 1's work on the branch agent-8/vm-cave, which is a base like
		// any other. registry.ResolveBase would strip both.
		id, base, ok := registry.ParseDevBranch(branch)
		if !ok {
			continue
		}
		c := Candidate{Branch: branch, Base: base, Agent: id}
		if !exists(dir, c.BaseRef()) {
			continue
		}
		n, err := countAhead(dir, c.BaseRef(), c.Ref())
		if err != nil || n == 0 {
			continue
		}
		c.Ahead = n
		// The same question the other way round. An error here leaves it at
		// zero rather than dropping the candidate: being unsure whether a
		// rebase is needed is not a reason to stop reporting work that is
		// certainly pending.
		if b, err := countAhead(dir, c.Ref(), c.BaseRef()); err == nil {
			c.Behind = b
		}
		found = append(found, c)
	}
	return found, nil
}

// Resolve picks the single candidate to act on. An empty agent means "the
// only one there is", except that standing on an agent branch is an answer:
// review put you there, so approve does not ask again.
func Resolve(dir, agent string) (Candidate, error) {
	all, err := Pending(dir)
	if err != nil {
		return Candidate{}, err
	}
	var match []Candidate
	for _, c := range all {
		if agent == "" || c.Agent == agent {
			match = append(match, c)
		}
	}
	if len(match) > 1 {
		if on := current(dir); on != "" {
			for _, c := range match {
				if c.Branch == on {
					return c, nil
				}
			}
		}
	}
	switch len(match) {
	case 0:
		return Candidate{}, ErrNothingPending
	case 1:
		return match[0], nil
	default:
		return Candidate{}, &AmbiguousError{Candidates: match}
	}
}

// Log is the pending commits, one line each.
func Log(dir string, c Candidate) (string, error) {
	return git(dir, "--no-pager", "log", "--oneline", "--no-decorate", c.BaseRef()+".."+c.Ref())
}

// Subjects is the subject line of each unlanded commit, newest first, and at
// most limit of them.
//
// Separate from Log, which is for a person who has decided to look: it prints
// abbreviated shas because the next thing they do is name one. This is for
// `status`, where nobody asked -- the question there is whether to look at
// all, and a count does not answer it. Six commits called "wip" and six that
// rewrote the approval flow are the same line otherwise.
//
// Capped, because status is a summary and an agent left running overnight can
// have a hundred. The caller says how many it has room for and is told what it
// did not see.
func Subjects(dir string, c Candidate, limit int) ([]string, error) {
	out, err := git(dir, "--no-pager", "log", "--format=%s",
		fmt.Sprintf("--max-count=%d", limit), c.BaseRef()+".."+c.Ref())
	if err != nil {
		return nil, err
	}
	if out == "" {
		return nil, nil
	}
	return strings.Split(out, "\n"), nil
}

// Stat is which files those commits touched and by how much.
func Stat(dir string, c Candidate) (string, error) {
	return git(dir, "--no-pager", "diff", "--stat", c.BaseRef()+"..."+c.Ref())
}

// Checkout puts the working tree on the agent's branch, which is what
// reviewing is: reading the code with the tools you already have, rather
// than through a diff this program prints at you. There was a `vmcav diff`
// for that and it is gone; `main` never had one.
//
// Already standing on the branch means an earlier review left you here and
// the agent has pushed since, so the ref is reset to what the shared repo
// now has rather than merged with it -- the agent's history is the truth,
// and a local copy of it is a cache.
func Checkout(dir string, c Candidate) error {
	if current(dir) == c.Branch {
		_, err := git(dir, "reset", "--hard", c.Ref())
		return err
	}
	_, err := git(dir, "checkout", "-B", c.Branch, c.Ref())
	return err
}

// Land fast-forwards the base onto the agent's work and reports how many
// commits moved: in this checkout first, then in the shared repo the agent
// clones from. It leaves you standing on the base with the work in it,
// which is the whole point of approving.
//
// Fast-forward only, so the base's history stays what the agent actually
// built on. A merge here would land work that was never tested against what
// the base has since grown, and hide that it wasn't.
// Undo records live in the shared repo beside the agent tips, which is the
// same idea one step later: agent-tip remembers what an agent pushed so a
// force-push can be told from a clobber, and these remember what a landing
// moved so it can be taken back.
//
// Two refs because undoing needs two facts. Where the base was before says
// what to go back to; where the landing left it says whether anything has
// happened since, and rolling back over somebody's later commits would be a
// worse mistake than the one being undone.
func undoFrom(base string) string { return "refs/cav/undo-from/" + base }
func undoTo(base string) string   { return "refs/cav/undo-to/" + base }

// UndoRecord is a landing that can still be taken back.
type UndoRecord struct {
	Base   string // the base branch that moved
	Branch string // the agent branch it came from
	From   string // where the base was before
	To     string // where the landing left it
	Ahead  int    // commits the landing added
}

// Undoable is the landing that Reject would take back, if there is one.
func Undoable(dir, base string) (UndoRecord, error) {
	if base == "" {
		base = current(dir)
	}
	if base == "" {
		return UndoRecord{}, fmt.Errorf("no branch to undo (detached HEAD; name a base)")
	}
	shared, err := Shared(dir)
	if err != nil {
		return UndoRecord{}, err
	}
	from, err := git(shared, "rev-parse", "--verify", "--quiet", undoFrom(base))
	if err != nil || from == "" {
		return UndoRecord{}, fmt.Errorf("nothing to undo on %s", base)
	}
	to, _ := git(shared, "rev-parse", "--verify", "--quiet", undoTo(base))
	// The branch name is not a sha, so it cannot be a ref. The refs are there
	// to keep the commits reachable -- a full landing deletes the agent branch,
	// and without them the only thing holding its objects would be gone.
	branch := ""
	if b, err := os.ReadFile(undoBranchFile(shared, base)); err == nil {
		branch = strings.TrimSpace(string(b))
	}
	n, _ := countAhead(shared, from, to)
	return UndoRecord{Base: base, Branch: branch, From: from, To: to, Ahead: n}, nil
}

// undoBranchFile is where the landed branch's name is kept, since a ref can
// only hold a sha. Beside the refs in the shared repo, so a record is one
// thing in one place.
func undoBranchFile(shared, base string) string {
	return filepath.Join(shared, "cave-undo", strings.ReplaceAll(base, "/", "-"))
}

// WouldLand is how many commits Land would move, and whether it would refuse.
// Asked before the confirmation, so a bad commit is reported instead of being
// prompted about, and so the question names the number actually at stake
// rather than the whole branch.
func WouldLand(dir string, c Candidate, upTo string) (int, error) {
	target, err := resolveTarget(dir, c, upTo)
	if err != nil {
		return 0, err
	}
	return countAhead(dir, c.BaseRef(), target)
}

// resolveTarget is which commit a landing moves the base to, and the whole of
// the checking that a partial one needs.
func resolveTarget(dir string, c Candidate, upTo string) (string, error) {
	if upTo == "" {
		return git(dir, "rev-parse", c.Ref())
	}
	target, err := git(dir, "rev-parse", upTo+"^{commit}")
	if err != nil {
		return "", fmt.Errorf("%s is not a commit in this checkout", upTo)
	}
	if !quiet(dir, "merge-base", "--is-ancestor", target, c.Ref()) {
		return "", fmt.Errorf("%s is not on %s, so approving it would land work nobody reviewed",
			upTo, c.Branch)
	}
	if quiet(dir, "merge-base", "--is-ancestor", target, c.BaseRef()) {
		return "", fmt.Errorf("%s is already on %s", upTo, c.Base)
	}
	return target, nil
}

// upTo, when set, lands the branch only as far as that commit and leaves the
// rest of it pending. Reviewing is reading, and reading often ends in "the
// first three of these are right and the fourth needs work" -- without this
// the answer is all or nothing, and the way out is asking the agent to move
// commits it has already pushed.
//
// The commit has to be one of the pending ones. Anything already in the base
// would move it nowhere, and anything off the branch would land work that was
// never under review, which is the one thing approving must not do.
func Land(dir string, c Candidate, upTo string) (int, error) {
	shared, err := Shared(dir)
	if err != nil {
		return 0, err
	}
	tip, err := git(dir, "rev-parse", c.Ref())
	if err != nil {
		return 0, err
	}
	if !quiet(dir, "merge-base", "--is-ancestor", c.BaseRef(), c.Ref()) {
		return 0, &NotFastForwardError{Branch: c.Branch, Base: c.Base}
	}
	target, err := resolveTarget(dir, c, upTo)
	if err != nil {
		return 0, err
	}
	// Where the base is before any of this, which is what an undo goes back
	// to. Read from the shared repo, since that is the copy Reject restores
	// and the one an agent clones.
	was, _ := git(shared, "rev-parse", "--verify", "--quiet", "refs/heads/"+c.Base)
	n, err := countAhead(dir, c.BaseRef(), target)
	if err != nil {
		return 0, err
	}

	// The checkout first, because it is the step that can refuse: git stops a
	// ff-merge that would overwrite uncommitted work, and its message is the
	// right one to surface. Nothing else has moved when it does.
	//
	// Three ways to move a branch, and which is right depends on where you
	// are standing. update-ref would move the ref out from under the worktree
	// holding it, leaving every landed file staged as a deletion.
	switch on := current(dir); {
	case !exists(dir, "refs/heads/"+c.Base):
		if _, err := git(dir, "checkout", "-B", c.Base, target); err != nil {
			return 0, err
		}
	case on == c.Base:
		if _, err := git(dir, "merge", "--ff-only", target); err != nil {
			return 0, err
		}
	default:
		// Standing on the agent branch is the ordinary case: review put you
		// there. Move the base, then follow it, so approving is also how you
		// leave the branch you were reading.
		if _, err := git(dir, "branch", "-f", c.Base, target); err != nil {
			return 0, err
		}
		if _, err := git(dir, "checkout", c.Base); err != nil {
			return 0, err
		}
	}

	// Then the shared repo, which is the agent's origin: until its base moves
	// too, the agent rebases onto a base that is missing its own landed work.
	//
	// A fetch into the bare repo rather than a push out of the checkout, the
	// same way `vmcav push` does it. Fetch does not run pre-receive at all, so
	// this cannot be refused by a hook that is there to gate the guest, and it
	// refuses a non-fast-forward without a leading + rather than needing one.
	if _, err := git(shared, "fetch", dir,
		"refs/heads/"+c.Base+":refs/heads/"+c.Base); err != nil {
		return 0, err
	}

	// The branch has served its purpose, unless part of it is still pending.
	// Left behind after a full landing it is a phantom with no commits:
	// invisible in status, confusing in review. The agent's next push
	// recreates it. Best-effort, all of it -- the work has landed, and tidying
	// that fails is not a failed approve.
	//
	// After a partial landing the branch is the work that was not approved,
	// so deleting it would throw away exactly what was held back. It stays,
	// and status keeps offering the rest.
	// What this landing moved, so it can be taken back. Written before the
	// branch is deleted, because after that these refs are the only thing
	// keeping its commits reachable.
	//
	// Best-effort like the tidying below: the work has landed either way, and
	// an approve that failed because it could not write an undo record would
	// be refusing to do the thing that worked.
	if was != "" {
		quiet(shared, "update-ref", undoFrom(c.Base), was)
		quiet(shared, "update-ref", undoTo(c.Base), target)
		if err := os.MkdirAll(filepath.Dir(undoBranchFile(shared, c.Base)), 0o755); err == nil {
			os.WriteFile(undoBranchFile(shared, c.Base), []byte(c.Branch+"\n"), 0o644)
		}
	}

	if target == tip {
		quiet(shared, "branch", "-D", c.Branch)
		quiet(dir, "branch", "-D", c.Branch)
	}
	quiet(dir, "fetch", "--prune", Remote)
	return n, nil
}

// Reject takes back the last landing on a base: the base goes back to where it
// was, and the agent branch comes back holding the work.
//
// The divergence check is the whole of the care here. A landing is only safe
// to undo while nothing has been built on it -- roll the base back over
// somebody's later commits and the mistake being fixed is smaller than the one
// being made. So this refuses unless the base is exactly where the landing
// left it, in the shared repo and in the checkout both.
//
// It restores rather than deletes: the agent branch comes back pointing at the
// landed commits, so the work returns to being pending and can be reviewed
// again or landed again. Undoing an approval should put things back, not throw
// the work away.
func Reject(dir string, r UndoRecord) error {
	shared, err := Shared(dir)
	if err != nil {
		return err
	}
	now, err := git(shared, "rev-parse", "--verify", "--quiet", "refs/heads/"+r.Base)
	if err != nil || now == "" {
		return fmt.Errorf("%s is not in the shared repo any more", r.Base)
	}
	if now != r.To {
		return fmt.Errorf("%s has moved since it was landed, so undoing it would "+
			"discard what came after; rewind it yourself if that is what you want", r.Base)
	}
	// The checkout too. Approving moves both, and leaving them disagreeing is
	// how a later approve reports a fast-forward it cannot do.
	if local, _ := git(dir, "rev-parse", "--verify", "--quiet", "refs/heads/"+r.Base); local != "" && local != r.To {
		return fmt.Errorf("your %s is not where the landing left it; "+
			"undoing would overwrite what you have", r.Base)
	}

	// The branch first, so the commits have a name of their own before the
	// base stops holding them.
	if r.Branch != "" {
		if _, err := git(shared, "update-ref", "refs/heads/"+r.Branch, r.To); err != nil {
			return err
		}
	}
	if _, err := git(shared, "update-ref", "refs/heads/"+r.Base, r.From); err != nil {
		return err
	}
	// Standing on the base means moving the working tree with it; standing
	// anywhere else means moving the ref alone, the same distinction Land makes.
	if current(dir) == r.Base {
		if _, err := git(dir, "reset", "--hard", r.From); err != nil {
			return err
		}
	} else {
		quiet(dir, "branch", "-f", r.Base, r.From)
	}
	quiet(shared, "update-ref", "-d", undoFrom(r.Base))
	quiet(shared, "update-ref", "-d", undoTo(r.Base))
	os.Remove(undoBranchFile(shared, r.Base))
	quiet(dir, "fetch", "--prune", Remote)
	return nil
}
