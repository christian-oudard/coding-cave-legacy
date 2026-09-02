// Package guestpath turns a cave: address into the path it names in the guest.
//
// An agent's worktree is at <root>/<id>/projects/<slug>, which is a layout
// nobody should have to hold in their head, and inside its own mount namespace
// that same tree is /projects/<slug> -- a path the host cannot use. So the
// host says `cave:notes.md` and this says where that is.
//
// Written in the shell first, and the project was left out of the worktree
// path, so every relative copy landed one directory too high. The test agreed,
// because it was written from the same wrong picture of the layout; only
// listing the real directory settled it. Here the pieces are arguments, and a
// missing one is a compile error rather than an empty string that concatenates
// into a plausible path.
package guestpath

import (
	"path"
	"strings"
)

// Layout is where the guest keeps things. Fields rather than constants so a
// test states them, and so the two callers cannot each remember their own.
type Layout struct {
	// Worktrees is the directory holding one directory per agent.
	Worktrees string
	// RootAgentID is the id whose home is /root rather than /home/agent<id>,
	// because that agent is root.
	RootAgentID string
}

// Default is the guest's own layout, and matches guest/configuration.nix.
var Default = Layout{Worktrees: "/var/lib/coding-cave/agents", RootAgentID: "0"}

// Home is where agent <id> keeps its own files.
func (l Layout) Home(id string) string {
	if id == l.RootAgentID {
		return "/root"
	}
	return "/home/agent" + id
}

// Worktree is agent <id>'s checkout of <project>, which is what it sees as
// /projects/<project> from inside its namespace.
func (l Layout) Worktree(id, project string) string {
	return path.Join(l.Worktrees, id, "projects", project)
}

// Resolve turns the part after `cave:` into a guest path.
//
//   - "~" or "~/..."  the agent's home
//   - "/..."          the guest as written, which is how anything outside the
//     agent's own two directories is reached
//   - ""              the worktree itself, so `cp x cave:` lands in it
//   - anything else   relative to the worktree
//
// A relative address needs a project to be relative to. Reporting that is the
// caller's job, because the remedy is theirs: an absolute path needs no
// project, so an agent whose project the guest cannot name is still reachable.
func (l Layout) Resolve(spec, id, project string) (string, bool) {
	switch {
	case spec == "~":
		return l.Home(id), true
	case strings.HasPrefix(spec, "~/"):
		return path.Join(l.Home(id), spec[2:]), true
	case strings.HasPrefix(spec, "/"):
		return spec, true
	case project == "" || project == "-":
		// Everything below is relative to a worktree, and there is not one.
		return "", false
	case spec == "":
		return l.Worktree(id, project), true
	default:
		return path.Join(l.Worktree(id, project), spec), true
	}
}
