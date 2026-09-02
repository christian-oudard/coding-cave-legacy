package guestpath

import "testing"

func TestResolve(t *testing.T) {
	l := Default
	for _, tc := range []struct {
		name, spec, id, project, want string
		ok                            bool
	}{
		// The bug this package was written for. The worktree is
		// <root>/<id>/projects/<slug>, and leaving the slug out put every
		// relative copy one directory too high -- in the directory that holds
		// worktrees rather than in one.
		{"a relative path is relative to the worktree", "notes.md", "2", "proj",
			"/var/lib/coding-cave/agents/2/projects/proj/notes.md", true},
		{"empty is the worktree itself", "", "2", "proj",
			"/var/lib/coding-cave/agents/2/projects/proj", true},
		{"a subdirectory keeps its shape", "docs/x.md", "2", "proj",
			"/var/lib/coding-cave/agents/2/projects/proj/docs/x.md", true},

		{"~ is the agent's home, not the worktree", "~", "2", "proj",
			"/home/agent2", true},
		{"and under it", "~/.claude/settings.json", "2", "proj",
			"/home/agent2/.claude/settings.json", true},
		// The root agent is root, so its home is /root and not /home/agent0.
		{"the root agent's home is /root", "~", "0", "proj", "/root", true},
		{"and under that too", "~/x", "0", "proj", "/root/x", true},

		{"an absolute path is the guest as written", "/etc/os-release", "2", "proj",
			"/etc/os-release", true},
		// Absolute and ~ need no project, so an agent whose project the guest
		// cannot name is still reachable by them.
		{"absolute needs no project", "/etc/os-release", "2", "", "/etc/os-release", true},
		{"nor does ~", "~/x", "2", "-", "/home/agent2/x", true},

		// A dash is how the wire form spells a field it has nothing for, so it
		// is absence rather than a project called "-".
		{"a relative path with no project has nothing to be relative to",
			"notes.md", "2", "", "", false},
		{"and a dash is not a project name", "notes.md", "2", "-", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := l.Resolve(tc.spec, tc.id, tc.project)
			if got != tc.want || ok != tc.ok {
				t.Errorf("Resolve(%q, %q, %q) = %q,%v; want %q,%v",
					tc.spec, tc.id, tc.project, got, ok, tc.want, tc.ok)
			}
		})
	}
}

// The layout is stated rather than assumed, so a guest that keeps things
// elsewhere is a different Layout and not a second copy of these rules.
func TestLayoutIsNotHardcoded(t *testing.T) {
	l := Layout{Worktrees: "/srv/agents", RootAgentID: "9"}
	if got, _ := l.Resolve("x", "3", "p"); got != "/srv/agents/3/projects/p/x" {
		t.Errorf("got %q", got)
	}
	if got := l.Home("9"); got != "/root" {
		t.Errorf("the reserved id is whichever one is reserved: %q", got)
	}
	if got := l.Home("0"); got != "/home/agent0" {
		t.Errorf("and 0 is ordinary when it is not the reserved one: %q", got)
	}
}
