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


// renderGuards warns about a branch guard that is not ours, which is the
// host-side fact most likely to be quietly wrong: a foreign guard refuses
// pushes in a way that reads exactly like the guard working, so nothing about
// the symptom points at the cause.
func renderGuards(w io.Writer, all []projects.Project) {
	for _, p := range all {
		switch p.Guard() {
		case projects.GuardOurs:
		case projects.GuardNoProject:
			// The shell reported this as a missing guard, because resolving
			// the dead link gave it an empty path to look under. The guard is
			// not the problem; the project is not there.
			fmt.Fprintf(w, "%s is registered but %s is not there, so it has no guard\n",
				p.Slug, orNone(p.Target))
			fmt.Fprintf(w, "  re-register it where it is now:  vmcav init <dir>\n")
		case projects.GuardMissing:
			fmt.Fprintf(w, "%s has no branch guard; an agent could push to a base branch\n", p.Slug)
			fmt.Fprintf(w, "  install it:  vmcav init %s\n", p.CheckoutDir())
		case projects.GuardForeign:
			fmt.Fprintf(w, "%s's branch guard was installed by another tool\n", p.Slug)
			fmt.Fprintln(w, "  it refuses any agent whose id does not match the one this guest")
			fmt.Fprintln(w, "  shares, so every agent but one cannot publish at all")
			fmt.Fprintf(w, "  take it over:  vmcav init %s\n", p.CheckoutDir())
		}
	}
}

func projectsCommand(args []string) {
	v := vmFlagSet("projects")
	// The daemon's whitelist rather than the listing: each registration and
	// what its link resolves to, one per line.
	serveArgs := v.fs.Bool("serve-args", false, "print the paths the git daemon may serve")
	guards := v.fs.Bool("guards", false, "warn about branch guards that are not ours")
	v.parse(args)
	dir := projects.Dir(v.state)

	if *guards {
		all, err := projects.List(dir)
		if err != nil {
			fail(err)
		}
		renderGuards(os.Stdout, all)
		return
	}

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
