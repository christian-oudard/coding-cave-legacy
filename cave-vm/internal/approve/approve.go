// Package approve is the last hop of the git ferry: taking what an agent
// pushed to agent-<id>/<base> and landing it on <base>.
//
// The agent commits in the guest and pushes over the vsock git bridge into
// the host repo, where the pre-receive guard confines it to its own
// namespace. Moving that work onto a base branch is the one step it
// deliberately cannot take itself, so it is the one step a human runs. All of
// it is ordinary git in the host repo: no VM, no daemon, and it works with
// the guest stopped.
package approve

import (
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"strings"

	"codingcave.dev/cave-vm/internal/registry"
)

// Candidate is one agent branch holding commits its base does not have.
type Candidate struct {
	Branch string // agent-1/vm-cave
	Base   string // vm-cave
	Agent  string // 1
	Ahead  int    // commits the base does not have
}

// ErrNothingPending means no agent branch is ahead of its base. Not a
// failure of the command so much as an answer to it.
var ErrNothingPending = errors.New("nothing to approve")

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

func exists(dir, ref string) bool {
	_, err := git(dir, "rev-parse", "--verify", "--quiet", "refs/heads/"+ref)
	return err == nil
}

func countAhead(dir, base, branch string) (int, error) {
	out, err := git(dir, "rev-list", "--count", "refs/heads/"+base+"..refs/heads/"+branch)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(out)
}

// Pending lists every agent branch with commits its base does not have,
// oldest agent id first. An agent branch whose base is gone, or that is not
// ahead of it, is not something to approve and is not listed.
func Pending(dir string) ([]Candidate, error) {
	out, err := git(dir, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
	if err != nil {
		return nil, err
	}
	var found []Candidate
	for _, branch := range strings.Split(out, "\n") {
		// Only the first prefix is the agent's: agent-1/agent-8/vm-cave is
		// agent 1's work on the branch agent-8/vm-cave, which is a base like
		// any other. registry.ResolveBase would strip both.
		id, base, ok := registry.ParseDevBranch(branch)
		if !ok || !exists(dir, base) {
			continue
		}
		n, err := countAhead(dir, base, branch)
		if err != nil || n == 0 {
			continue
		}
		found = append(found, Candidate{Branch: branch, Base: base, Agent: id, Ahead: n})
	}
	return found, nil
}

// Resolve picks the single candidate to act on. An empty agent means "the
// only one there is".
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
	return git(dir, "--no-pager", "log", "--oneline", "--no-decorate", c.Base+".."+c.Branch)
}

// Stat is which files those commits touched and by how much.
func Stat(dir string, c Candidate) (string, error) {
	return git(dir, "--no-pager", "diff", "--stat", c.Base+"..."+c.Branch)
}

// Diff is the whole change the base would take on.
func Diff(dir string, c Candidate) (string, error) {
	return git(dir, "--no-pager", "diff", c.Base+"..."+c.Branch)
}

// Land fast-forwards the base onto the agent's work and reports how many
// commits moved.
//
// Fast-forward only, so the base's history stays what the agent actually
// built on. A merge here would land work that was never tested against what
// the base has since grown, and hide that it wasn't.
func Land(dir string, c Candidate) (int, error) {
	tip, err := git(dir, "rev-parse", "refs/heads/"+c.Branch)
	if err != nil {
		return 0, err
	}
	old, err := git(dir, "rev-parse", "refs/heads/"+c.Base)
	if err != nil {
		return 0, err
	}
	if _, err := git(dir, "merge-base", "--is-ancestor", old, tip); err != nil {
		return 0, &NotFastForwardError{Branch: c.Branch, Base: c.Base}
	}
	n, err := countAhead(dir, c.Base, c.Branch)
	if err != nil {
		return 0, err
	}

	// Two ways to move a branch, and which is right depends on whether it is
	// the branch you are standing on. update-ref would move the ref out from
	// under a checked-out worktree, leaving every landed file staged as a
	// deletion; merge --ff-only carries the worktree along and refuses when
	// uncommitted work would be overwritten.
	current, _ := git(dir, "symbolic-ref", "--quiet", "--short", "HEAD")
	if current == c.Base {
		if _, err := git(dir, "merge", "--ff-only", c.Branch); err != nil {
			return 0, err
		}
	} else if _, err := git(dir, "update-ref", "refs/heads/"+c.Base, tip, old); err != nil {
		return 0, err
	}
	return n, nil
}
