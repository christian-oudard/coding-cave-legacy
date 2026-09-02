package main

import (
	"fmt"
	"io"
	"os"

	"codingcave.dev/cave-vm/internal/projects"
)

// `cave-vm projects`, in two forms: the listing a person reads, and the
// argument list the git daemon is started with.
//
// One source for both, because they disagreed once. The listing resolved a
// registration with `readlink -f` and printed a blank column for one whose
// target had moved; the daemon's whitelist was built separately and handed
// that same dead path over, so the guest got "repository not found" -- which
// from inside is what never having registered the project looks like.

// renderProjects writes the listing, and returns whether any registration is
// pointing at nothing.
func renderProjects(w io.Writer, all []projects.Project) (missing bool) {
	for _, p := range all {
		if p.Present {
			fmt.Fprintf(w, "%-24s %s\n", p.Slug, p.Target)
			continue
		}
		// Still says where it pointed. That path is the half you need to
		// register it where it is now.
		fmt.Fprintf(w, "%-24s %s (missing)\n", p.Slug, orNone(p.Target))
		missing = true
	}
	return missing
}

func projectsCommand(args []string) {
	v := vmFlagSet("projects")
	// The daemon's whitelist rather than the listing: each registration and
	// what its link resolves to, one per line.
	serveArgs := v.fs.Bool("serve-args", false, "print the paths the git daemon may serve")
	v.parse(args)
	dir := projects.Dir(v.state)

	if *serveArgs {
		serve, missing, err := projects.ServePaths(dir)
		if err != nil {
			fail(err)
		}
		for _, p := range missing {
			// On stderr, because stdout here is an argument list.
			fmt.Fprintf(os.Stderr, "not serving '%s': %s is not there any more\n",
				p.Slug, orNone(p.Target))
			fmt.Fprintln(os.Stderr, "  (vmcav init <dir> to re-register it, vmcav projects to see them all)")
		}
		for _, p := range serve {
			fmt.Println(p)
		}
		return
	}

	all, err := projects.List(dir)
	if err != nil {
		fail(err)
	}
	if renderProjects(os.Stdout, all) {
		fmt.Fprintln(os.Stderr, "a project marked (missing) has moved or been deleted. The guest cannot")
		fmt.Fprintln(os.Stderr, "  clone it and says only that the repository does not exist:")
		fmt.Fprintln(os.Stderr, "    register it where it is now:  vmcav init <dir>")
		fmt.Fprintln(os.Stderr, "    or forget it:                 vmcav remove-project <slug>")
	}
}
