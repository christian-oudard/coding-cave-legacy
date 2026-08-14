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
	case "pending", "review", "diff", "approve":
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
	case "vsock-connect":
		vmVsockConnect(os.Args[2:])
	default:
		usage()
	}
}

func gitCommand(cmd string, args []string) {
	// -C, so the caller decides which checkout this is about. vmcav passes
	// the repo it already resolved; a bare invocation means the cwd's.
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	dir := fs.String("C", ".", "repository to work in")
	yes := fs.Bool("y", false, "skip the confirmation (approve)")
	fs.Parse(args)
	agent := fs.Arg(0)

	switch cmd {
	case "pending":
		pending(*dir)
	case "review":
		review(*dir, agent)
	case "diff":
		showDiff(*dir, agent)
	case "approve":
		land(*dir, agent, *yes)
	}
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: cave-vm [pending|review|diff|approve|version]")
	fmt.Fprintln(os.Stderr, "       cave-vm [start|stop|serve|running|socket|cache-key|vsock-connect] --state DIR ...")
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

func resolve(dir, agent string) approve.Candidate {
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

// review is what the agent did at the granularity you read first: subjects,
// then which files and how much. `diff` is the same range in full.
func review(dir, agent string) {
	c := resolve(dir, agent)
	fmt.Fprintf(os.Stderr, "%s -> %s\n", c.Branch, c.Base)
	out, err := approve.Log(dir, c)
	if err != nil {
		die(err)
	}
	fmt.Println(out)
	if stat, err := approve.Stat(dir, c); err == nil && stat != "" {
		fmt.Println(stat)
	}
}

func showDiff(dir, agent string) {
	out, err := approve.Diff(dir, resolve(dir, agent))
	if err != nil {
		die(err)
	}
	fmt.Println(out)
}

func land(dir, agent string, yes bool) {
	c := resolve(dir, agent)
	out, err := approve.Log(dir, c)
	if err != nil {
		die(err)
	}
	fmt.Println(out)
	if !yes && !confirm(fmt.Sprintf("Land %d commit%s on %s?", c.Ahead, plural(c.Ahead), c.Base)) {
		fmt.Fprintln(os.Stderr, "not landed")
		os.Exit(1)
	}
	n, err := approve.Land(dir, c)
	if err != nil {
		die(err)
	}
	fmt.Fprintf(os.Stderr, "landed %d commit%s on %s\n", n, plural(n), c.Base)
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
