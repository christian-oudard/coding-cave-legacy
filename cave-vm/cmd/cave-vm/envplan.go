package main

import (
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
)

// What `vmcav env` should do, given what each side has.
//
// The shell asked the guest for its spec with `$(ssh ... cat ...)` and kept the
// exit status in a variable it never read. So a guest that could not be
// reached and a guest with no spec were the same empty string, and every
// branch below treated the first as the second.
//
// That is not cosmetic here. An empty capture is caught by the "nothing to
// pull" guard, so the harmless case refuses. The damaging one is a capture
// that is not empty and is not a spec either -- an error line that reached
// stdout, which is what happened on the host this was reported from: `env
// pull` announced that the local cave.nix "differs from the guest's and would
// be overwritten" while the guest was unreachable, and offered `--force` to
// take the guest's anyway. Forcing would have written that over the only copy
// of a hundred and fifty lines of configuration, using the command documented
// as the way to rescue it.
//
// Comparing bytes cannot tell a spec from an error. Whether the guest answered
// can, so that is what is asked first.
//
// So reachability is an input here, and it is the first thing every action
// looks at.

// EnvState is what is known about the two sides.
type EnvState struct {
	HostHas   bool
	Reachable bool
	GuestHas  bool
	Same      bool
	Force     bool
}

// EnvPlan is what to do about it.
type EnvPlan struct {
	// Do is "show", "write-host", "write-guest", or "" for nothing.
	Do    string
	Lines []string
	// OK is false when the command should fail.
	OK bool
}

func envPlan(action string, s EnvState) EnvPlan {
	// Nothing can be decided against a guest that has not answered, including
	// -- especially -- a forced one. Force means "I know the two differ and I
	// want that one"; it cannot mean "overwrite with whatever silence
	// deserialises to".
	if !s.Reachable {
		switch action {
		case "show":
			return EnvPlan{Do: "show", OK: true, Lines: []string{
				"the guest is not answering, so what it has is unknown",
				"  this side is unchanged either way",
			}}
		default:
			return EnvPlan{OK: false, Lines: []string{
				"the guest is not answering, so " + action + " has nothing to work with",
				"  --force cannot help: there is no answer to force, only silence",
			}}
		}
	}

	switch action {
	case "show":
		switch {
		case !s.HostHas && !s.GuestHas:
			return EnvPlan{Do: "show", OK: true, Lines: []string{
				"neither side has one; the cave defaults apply"}}
		case s.Same:
			return EnvPlan{Do: "show", OK: true, Lines: []string{"they agree"}}
		default:
			return EnvPlan{Do: "show", OK: true, Lines: []string{
				"they differ: vmcav env pull (keep the guest's) or push (keep this one)"}}
		}

	case "pull":
		if !s.GuestHas {
			return EnvPlan{OK: false, Lines: []string{"the guest has no spec to pull"}}
		}
		if s.Same {
			return EnvPlan{OK: true, Lines: []string{"already the same; nothing to pull"}}
		}
		if s.HostHas && !s.Force {
			return EnvPlan{OK: false, Lines: []string{
				"this side differs from the guest's and would be overwritten",
				"  keep it and send it instead:  vmcav env push",
				"  take the guest's anyway:      vmcav env pull --force",
			}}
		}
		return EnvPlan{Do: "write-host", OK: true, Lines: []string{
			"it is configuration; commit it wherever your dotfiles live"}}

	case "push":
		if !s.HostHas {
			return EnvPlan{OK: false, Lines: []string{"there is no spec here to push"}}
		}
		if s.Same {
			return EnvPlan{OK: true, Lines: []string{"already the same; nothing to push"}}
		}
		if s.GuestHas && !s.Force {
			return EnvPlan{OK: false, Lines: []string{
				"the guest's spec differs from this one and would be overwritten",
				"  keep the guest's instead:  vmcav env pull",
				"  overwrite it anyway:       vmcav env push --force",
			}}
		}
		return EnvPlan{Do: "write-guest", OK: true}
	}
	return EnvPlan{OK: false, Lines: []string{"usage: vmcav env [show|pull|push] [--force]"}}
}

func renderEnvPlan(w io.Writer, p EnvPlan) {
	for _, l := range p.Lines {
		fmt.Fprintln(w, l)
	}
}

// `cave-vm env-plan` prints the decision on the first line and the lines to
// say after it, so the shell does the reading and writing and none of the
// deciding.
func envPlanCommand(args []string) {
	fs := flag.NewFlagSet("env-plan", flag.ExitOnError)
	action := fs.String("action", "show", "show, pull or push")
	hostHas := fs.Bool("host-has", false, "this side has a spec")
	reachable := fs.Bool("reachable", false, "the guest answered")
	guestHas := fs.Bool("guest-has", false, "the guest has a spec")
	same := fs.Bool("same", false, "the two are byte for byte the same")
	force := fs.Bool("force", false, "--force was given")
	fs.Parse(args)

	p := envPlan(strings.TrimSpace(*action), EnvState{
		HostHas: *hostHas, Reachable: *reachable,
		GuestHas: *guestHas, Same: *same, Force: *force,
	})
	fmt.Println("do:" + p.Do)
	renderEnvPlan(os.Stdout, p)
	if !p.OK {
		os.Exit(1)
	}
}
