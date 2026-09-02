// Command cave-vm is the host CLI for the single-VM cave (see
// ../../../docs/vm-cave-design.md). Two things live here: host-side git, which
// is what `vmcav review|diff|approve` delegates to, and the VM lifecycle,
// which is the part that differs between a Linux host and an Apple silicon
// host. `vmcav` still owns everything above the VMM.
package main

import (
	"bufio"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"

	"codingcave.dev/cave-vm/internal/approve"
)

const version = "dev"

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch cmd := os.Args[1]; cmd {
	case "version":
		fmt.Println(version)
	case "pending", "review", "approve", "reject":
		gitCommand(cmd, os.Args[2:])
	case "start":
		vmStart(os.Args[2:])
	case "stop":
		vmStop(os.Args[2:])
	case "serve":
		vmServe(os.Args[2:])
	case "running":
		vmRunning(os.Args[2:])
	case "socket":
		vmSocket(os.Args[2:])
	case "cache-key":
		vmCacheKey(os.Args[2:])
	case "disk":
		vmDisk(os.Args[2:])
	case "vsock-connect":
		vmVsockConnect(os.Args[2:])
	case "guest":
		guestCommand(os.Args[2:])
	case "sessions":
		sessionsCommand(os.Args[2:])
	default:
		usage()
	}
}

func gitCommand(cmd string, args []string) {
	// -C, so the caller decides which checkout this is about: the human's
	// working checkout, not the shared bare repo. vmcav passes the repo it
	// already resolved; a bare invocation means the cwd's.
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	dir := fs.String("C", ".", "repository to work in")
	yes := fs.Bool("y", false, "skip the confirmation (approve)")
	// Partial approval: land the branch only this far and leave the rest
	// pending, for when the first commits are right and the last is not.
	upTo := fs.String("commit", "", "land only up to this commit (approve)")
	fs.Parse(args)
	agent := fs.Arg(0)

	switch cmd {
	case "pending":
		pending(*dir)
	case "review":
		review(*dir, agent)
	case "reject":
		unland(*dir, agent, *yes)
	case "approve":
		land(*dir, agent, *yes, *upTo)
	}
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: cave-vm [pending|review|approve|reject|version]")
	fmt.Fprintln(os.Stderr, "       cave-vm [start|stop|serve|running|socket|cache-key|vsock-connect] --state DIR ...")
	fmt.Fprintln(os.Stderr, "       cave-vm sessions -ssh-config F [checkpoint|restore <id>...|list]")
	os.Exit(2)
}

// die turns the package's errors into the sentence a person needs, which is
// usually what to do next rather than what went wrong.
func die(err error) {
	var amb *approve.AmbiguousError
	var nff *approve.NotFastForwardError
	switch {
	case errors.Is(err, approve.ErrNothingPending):
		fmt.Fprintln(os.Stderr, "nothing to approve")
	case errors.Is(err, approve.ErrNoRemote):
		fmt.Fprintf(os.Stderr, "this checkout has no '%s' remote, so there is no shared repo to read\n", approve.Remote)
		fmt.Fprintln(os.Stderr, "  (vmcav start creates it, and adds the remote)")
	case errors.As(err, &amb):
		fmt.Fprintln(os.Stderr, "more than one agent has work pending; name one (approve <agent>):")
		for _, c := range amb.Candidates {
			fmt.Fprintf(os.Stderr, "  %s (agent %s, %d commit%s)\n", c.Branch, c.Agent, c.Ahead, plural(c.Ahead))
		}
	case errors.As(err, &nff):
		fmt.Fprintln(os.Stderr, nff.Error())
		fmt.Fprintf(os.Stderr, "  (in the guest: git fetch origin && git rebase origin/%s)\n", nff.Base)
	default:
		fmt.Fprintln(os.Stderr, err)
	}
	os.Exit(1)
}

// resolve fetches first, always: the shared repo is where the agent pushes
// and nothing tells this checkout that it happened, so acting on
// remote-tracking refs without refreshing them is acting on a guess.
func resolve(dir, agent string) approve.Candidate {
	if err := approve.Fetch(dir); err != nil {
		die(err)
	}
	c, err := approve.Resolve(dir, agent)
	if err != nil {
		die(err)
	}
	return c
}

// pending is what `vmcav status` prints: nothing announces a push, so status
// is where you find out one arrived. Silent when there is nothing, so it can
// be called unconditionally.
func pending(dir string) {
	// Silent about a missing remote too: status runs on every start, and a
	// checkout with no shared repo yet has nothing to say, not a complaint.
	if err := approve.Fetch(dir); err != nil {
		return
	}
	all, err := approve.Pending(dir)
	if err != nil {
		die(err)
	}
	for _, c := range all {
		fmt.Printf("pending: %s, %d commit%s for %s\n", c.Branch, c.Ahead, plural(c.Ahead), c.Base)
	}
	if len(all) > 0 {
		fmt.Println("review: vmcav review   (land it: vmcav approve)")
	}
}

// review checks the agent's branch out in your working tree and says what is
// on it: subjects, then which files and how much. Reading the change itself
// is then your editor's job and your test runner's, which is why there is no
// `diff` subcommand -- `main` has none either, and a diff this program prints
// is a worse version of the one you already have tools for.
func review(dir, agent string) {
	c := resolve(dir, agent)
	if err := approve.Checkout(dir, c); err != nil {
		die(err)
	}
	fmt.Fprintf(os.Stderr, "%s -> %s (checked out)\n", c.Branch, c.Base)
	out, err := approve.Log(dir, c)
	if err != nil {
		die(err)
	}
	fmt.Println(out)
	if stat, err := approve.Stat(dir, c); err == nil && stat != "" {
		fmt.Println(stat)
	}
	fmt.Fprintf(os.Stderr, "land it: vmcav approve   (leave it: git checkout %s)\n", c.Base)
}

func land(dir, agent string, yes bool, upTo string) {
	c := resolve(dir, agent)
	out, err := approve.Log(dir, c)
	if err != nil {
		die(err)
	}
	fmt.Println(out)
	// How many are actually being landed, which is not c.Ahead when only part
	// of the branch is approved. Asking about the wrong number is how you get
	// a yes to a question nobody meant.
	count, err := approve.WouldLand(dir, c, upTo)
	if err != nil {
		die(err)
	}
	question := fmt.Sprintf("Land %d commit%s on %s?", count, plural(count), c.Base)
	if upTo != "" {
		question = fmt.Sprintf("Land %d of %d commit%s on %s, leaving the rest on %s?",
			count, c.Ahead, plural(c.Ahead), c.Base, c.Branch)
	}
	if !yes && !confirm(question) {
		fmt.Fprintln(os.Stderr, "not landed")
		os.Exit(1)
	}
	n, err := approve.Land(dir, c, upTo)
	if err != nil {
		die(err)
	}
	fmt.Fprintf(os.Stderr, "landed %d commit%s on %s, here and in the shared repo\n",
		n, plural(n), c.Base)
	if upTo != "" {
		fmt.Fprintf(os.Stderr, "%d left on %s\n", c.Ahead-n, c.Branch)
	}
}

// Default no, like every other destructive-ish prompt in the cave: an empty
// answer is not consent.
func confirm(question string) bool {
	fmt.Fprintf(os.Stderr, "%s [y/N] ", question)
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	if err != nil {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(line)) {
	case "y", "yes":
		return true
	}
	return false
}

// unland takes back the last landing on a base, which is the undo `approve`
// did not have. The agent argument names a base branch rather than an agent:
// what moved was the base, and more than one agent may have landed on it.
func unland(dir, base string, yes bool) {
	if err := approve.Fetch(dir); err != nil {
		die(err)
	}
	rec, err := approve.Undoable(dir, base)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	out, err := approve.Log(dir, approve.Candidate{Base: rec.Base, Branch: rec.Branch})
	if err == nil && out != "" {
		fmt.Println(out)
	}
	q := fmt.Sprintf("Take %d commit%s back off %s?", rec.Ahead, plural(rec.Ahead), rec.Base)
	if rec.Branch != "" {
		q = fmt.Sprintf("Take %d commit%s back off %s, onto %s?",
			rec.Ahead, plural(rec.Ahead), rec.Base, rec.Branch)
	}
	if !yes && !confirm(q) {
		fmt.Fprintln(os.Stderr, "not undone")
		os.Exit(1)
	}
	if err := approve.Reject(dir, rec); err != nil {
		die(err)
	}
	fmt.Fprintf(os.Stderr, "%s is back at %s", rec.Base, rec.From[:7])
	if rec.Branch != "" {
		fmt.Fprintf(os.Stderr, ", and the work is pending again on %s", rec.Branch)
	}
	fmt.Fprintln(os.Stderr)
}
