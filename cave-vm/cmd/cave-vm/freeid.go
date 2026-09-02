package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"

	"codingcave.dev/cave-vm/internal/registry"
)

// `cave-vm free-id` picks the id a new ordinary agent gets, from the ids
// already in use on stdin, one per line.
//
// The gathering stays with the caller because it spans three places -- a
// session the guest is running, a branch in the shared repo, and a worktree or
// home on the guest's disk -- and each is reached differently. What comes here
// is the rule they add up to, which is the part that was wrong once: an id
// held only by leftovers on disk looked free, and the agent given it inherited
// a stranger's home and resumed a conversation it had never had.
//
// Exit 1 with nothing on stdout when the pool is full, so the caller can say
// so in its own words.
func freeIDCommand(args []string) {
	fs := flag.NewFlagSet("free-id", flag.ExitOnError)
	root := fs.String("root", "0", "the id reserved for the root agent")
	max := fs.Int("max", 8, "how many agents this guest holds at once")
	fs.Parse(args)

	var used []string
	sc := bufio.NewScanner(os.Stdin)
	for sc.Scan() {
		used = append(used, sc.Text())
	}
	n, ok := registry.FreeID(used, *root, *max)
	if !ok {
		os.Exit(1)
	}
	fmt.Println(n)
}
