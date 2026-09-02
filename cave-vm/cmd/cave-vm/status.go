package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"codingcave.dev/cave-vm/internal/approve"
	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/projects"
	"codingcave.dev/cave-vm/internal/vmm"
)

// `vmcav status`, whole, rather than a shell function sequencing six of this
// program's own subcommands.
//
// It was the latter, and that shape cost the thing status exists for. Every
// question it asks touches something that can be missing -- a socket, a
// pidfile, a repo, a guest that stopped answering -- and under `set -euo
// pipefail` one unguarded non-zero ended the script before its first line of
// output. The report on a broken cave was an empty screen and an exit code.
//
// Here a step that fails is a step that failed. Nothing below stops the rest.

// statusCommand prints the report. -C names the checkout, because the git half
// is about the human's working copy rather than the state directory.
func statusCommand(args []string) {
	v := vmFlagSet("status")
	repo := v.fs.String("C", ".", "the checkout this cave is for")
	v.parse(args)
	statusReport(os.Stdout, v, *repo)
}

func statusReport(w io.Writer, v *vmFlags, repo string) {
	p := v.paths()

	// First, and whether or not the VM is up: a guard belonging to another
	// tool refuses pushes in a way that reads exactly like the guard working,
	// so nothing about the symptom points at the cause.
	if all, err := projects.List(projects.Dir(v.state)); err == nil {
		renderGuards(w, all)
	}

	pid, running := vmm.Running(backend(), v.spec(p), p)
	if !running {
		// "Stopped" answered two different questions with one word: a VM you
		// can start, and one that was never created.
		if _, err := os.Stat(v.disk); err == nil {
			fmt.Fprintln(w, "vm: stopped")
			// Staleness matters most here rather than least: `start` boots
			// what the disk says, so a stopped guest is the one about to run
			// old code.
			renderStaleness(w, deployedFrom(v.state), sourceRev(repo))
		} else {
			fmt.Fprintln(w, "vm: no image yet (vmcav image create)")
		}
		pending(repo)
		return
	}
	if pid <= 0 {
		// Held by a supervisor this side cannot see: another cave, or the
		// host. Its pid means nothing here and its sockets are not ours.
		fmt.Fprintln(w, "vm: running, started elsewhere on this machine (one cave VM per machine)")
		fmt.Fprintln(w, "  stop it where it was started; this side cannot reach its sockets")
		pending(repo)
		return
	}

	// The human's own commits, before anything asks what is pending: an agent
	// rebases onto the shared repo, and a base that has not caught up makes
	// its next rebase a conflict against work it cannot see.
	carryBase(w, repo)
	// The clock, before anything reads it: a guest whose clock is wrong fails
	// every TLS request an agent makes, while looking like nothing is wrong.
	correctClock(v)

	fmt.Fprintf(w, "vm: running (pid %d, since %s)\n", pid, started(pid))
	booted, _ := vmm.BootedSystem(p)
	current, reachable := guestSystem(v)
	renderSystem(w, booted, current, reachable)
	agents, ok := guestAgents(v)
	renderAgents(w, agents, ok)
	fmt.Fprintf(w, "console: %s\n", filepath.Join(v.state, "console.log"))
	renderStaleness(w, deployedFrom(v.state), sourceRev(repo))
	pending(repo)
}

// started is when the process began, which turns a pid into something a person
// can match against what they remember doing.
//
// From /proc, whose directory mtime is the process start. `ps -o lstart=` is
// what the shell used and is one more subprocess for one field; this is a stat.
// An answer that cannot be had is "?" rather than a missing line: the pid above
// is the useful half.
func started(pid int) string {
	st, err := os.Stat(fmt.Sprintf("/proc/%d", pid))
	if err != nil {
		return "?"
	}
	return st.ModTime().Format(time.ANSIC)
}

func deployedFrom(state string) string {
	b, err := os.ReadFile(filepath.Join(state, "deployed-from"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// sourceRev is what the guest would be built from now: the checkout's commit,
// marked dirty when the build's own inputs have uncommitted changes.
//
// Only guest/ and cave-vm/ count. Editing a doc does not change what a guest
// is, and a warning that stood while any file was dirty is one nobody reads.
func sourceRev(repo string) string {
	rev, err := git(repo, "rev-parse", "HEAD")
	if err != nil {
		return ""
	}
	if out, err := git(repo, "status", "--porcelain", "--", "guest", "cave-vm"); err == nil && out != "" {
		rev += "-dirty"
	}
	return rev
}

// carryBase moves the human's commits on the current base into the shared repo,
// which is what an agent clones and rebases onto.
//
// A fetch into the bare repo rather than a push out of the checkout: fetch does
// not run pre-receive at all, and refuses a non-fast-forward without a leading
// plus, so the safe case needs no flag and the unsafe one cannot happen by
// accident. Silent when it does nothing, which is almost always.
func carryBase(w io.Writer, repo string) {
	branch, err := git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
	if err != nil || branch == "" {
		return
	}
	// An agent branch belongs to the agent, and the shared repo's copy is the
	// one it pushed.
	if _, _, isAgent := parseAgentBranch(branch); isAgent {
		return
	}
	shared, err := approve.Shared(repo)
	if err != nil {
		return
	}
	if _, err := os.Stat(shared); err != nil {
		return
	}
	before, _ := git(shared, "rev-parse", "--verify", "--quiet", "refs/heads/"+branch)
	if _, err := git(shared, "fetch", "--quiet", repo,
		"refs/heads/"+branch+":refs/heads/"+branch); err != nil {
		return
	}
	after, _ := git(shared, "rev-parse", "--verify", "--quiet", "refs/heads/"+branch)
	if before != after && after != "" {
		fmt.Fprintf(w, "%s -> shared repo (%s), where agents clone it\n", branch, short(after))
	}
}

func parseAgentBranch(branch string) (id, base string, ok bool) {
	rest, found := strings.CutPrefix(branch, "agent-")
	if !found {
		return "", "", false
	}
	id, base, found = strings.Cut(rest, "/")
	if !found || id == "" {
		return "", "", false
	}
	return id, base, true
}

// correctClock nudges the guest's clock if it has drifted enough to matter.
//
// The guest has no NIC and no NTP; it takes the clock from the host at boot and
// nothing touches it after, so on a host that suspends it wakes behind by
// however long the lid was shut. A stale clock fails TLS certificate validation
// and therefore every request an agent makes, and nothing in that failure
// mentions the time.
//
// Quiet below the threshold, and quiet about its own failure: this runs on a
// command that is about to report on the guest anyway.
func correctClock(v *vmFlags) {
	tok, err := control.EnsureToken(controlToken(v.state))
	if err != nil {
		return
	}
	c := control.NewClient(guestDialer(v.paths()), tok)
	before, err := c.Health()
	if err != nil {
		return
	}
	now := time.Now()
	// Only past a threshold, so this can run on every command that reports on
	// the guest without calling a few milliseconds of round trip "drift".
	skew := now.Sub(time.Unix(0, before.Now))
	if skew < clockSlack && -skew < clockSlack {
		return
	}
	c.SetTime(now)
}

// How far out the guest's clock has to be before it is worth setting.
const clockSlack = 2 * time.Second

func guestSystem(v *vmFlags) (string, bool) {
	tok, err := control.EnsureToken(controlToken(v.state))
	if err != nil {
		return "", false
	}
	h, err := control.NewClient(guestDialer(v.paths()), tok).Health()
	if err != nil {
		return "", false
	}
	return h.System, true
}

func guestAgents(v *vmFlags) ([]control.Agent, bool) {
	tok, err := control.EnsureToken(controlToken(v.state))
	if err != nil {
		return nil, false
	}
	a, err := control.NewClient(guestDialer(v.paths()), tok).Agents()
	if err != nil {
		return nil, false
	}
	return a, true
}

// git runs one command in a repository and returns its trimmed stdout.
func git(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	out, err := cmd.Output()
	return strings.TrimSpace(string(out)), err
}
