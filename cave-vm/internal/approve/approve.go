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
	"os/exec"
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
func Land(dir string, c Candidate) (int, error) {
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
	n, err := countAhead(dir, c.BaseRef(), c.Ref())
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
		if _, err := git(dir, "checkout", "-B", c.Base, tip); err != nil {
			return 0, err
		}
	case on == c.Base:
		if _, err := git(dir, "merge", "--ff-only", tip); err != nil {
			return 0, err
		}
	default:
		// Standing on the agent branch is the ordinary case: review put you
		// there. Move the base, then follow it, so approving is also how you
		// leave the branch you were reading.
		if _, err := git(dir, "branch", "-f", c.Base, tip); err != nil {
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

	// The branch has served its purpose. Left behind it is a phantom with no
	// pending commits: invisible in status, confusing in review. The agent's
	// next push recreates it. Best-effort, all of it -- the work has landed,
	// and tidying that fails is not a failed approve.
	quiet(shared, "branch", "-D", c.Branch)
	quiet(dir, "branch", "-D", c.Branch)
	quiet(dir, "fetch", "--prune", Remote)
	return n, nil
}
