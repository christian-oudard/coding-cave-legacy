package main

import (
	"flag"
	"fmt"
	"os"

	"codingcave.dev/cave-vm/internal/guestpath"
)

// `cave-vm guest-path <spec>` says where a cave: address lands in the guest,
// so `vmcav cp` does not have to know the guest's layout.
//
// Exit 1 with nothing on stdout when a relative address has no project to be
// relative to. The caller reports it, because the remedy is theirs: an
// absolute path or a ~ needs no project, so an agent whose project the guest
// cannot name is still reachable.
func guestPathCommand(args []string) {
	fs := flag.NewFlagSet("guest-path", flag.ExitOnError)
	id := fs.String("id", "", "the agent whose tree and home these are")
	project := fs.String("project", "", "the project its worktree holds")
	root := fs.String("root", guestpath.Default.RootAgentID, "the id reserved for the root agent")
	worktrees := fs.String("worktrees", guestpath.Default.Worktrees, "where agents' worktrees live")
	fs.Parse(args)

	l := guestpath.Layout{Worktrees: *worktrees, RootAgentID: *root}
	got, ok := l.Resolve(fs.Arg(0), *id, *project)
	if !ok {
		os.Exit(1)
	}
	fmt.Println(got)
}
