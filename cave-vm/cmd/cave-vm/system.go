package main

import (
	"fmt"
	"io"
	"os"

	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/vmm"
)

// Which system the guest is on, and whether it is the one the host booted.
//
// Two facts that are usually the same and matter when they are not: the host
// boots one toplevel, and an in-guest `cave-rebuild` moves the running system
// off it without touching what a restart would come back to. Equal is the
// normal case and says nothing twice; a difference gets two lines.
//
// Both halves used to be recovered by sed. The booted one came out of
// run-vm.sh -- a shell script this program writes -- with a pattern that had
// to know the flag spelling, the store prefix, and that `init=` points at
// <toplevel>/init. The current one came out of `health` with `sed -n 's/^system
// //p'`, over a struct field that was formatted into a line so it could be
// parsed back. Neither round trip has a reason to exist on this side.
func renderSystem(w io.Writer, booted, current string, reachable bool) {
	switch {
	case booted == "" && !reachable:
		// Nothing was recorded and nobody can be asked. Saying "system:" with
		// an empty value reads as a system called nothing.
		fmt.Fprintln(w, "system: unknown (no run script, and the guest is not answering)")
	case !reachable:
		fmt.Fprintf(w, "booted: %s\n", booted)
		fmt.Fprintln(w, "current: unreachable")
	case booted == current:
		fmt.Fprintf(w, "system: %s\n", booted)
	default:
		// A rebuild inside the guest is the usual reason, and it is worth
		// seeing: what the host boots is unchanged, so a stop and start comes
		// back to the first of these.
		fmt.Fprintf(w, "booted: %s\n", orNone(booted))
		fmt.Fprintf(w, "current: %s\n", orNone(current))
	}
}

func orNone(s string) string {
	if s == "" {
		return "(none recorded)"
	}
	return s
}

// systemCommand is `cave-vm system`, the generation lines of `vmcav status`.
func systemCommand(args []string) {
	v := vmFlagSet("system")
	v.parse(args)

	booted, err := vmm.BootedSystem(v.paths())
	if err != nil {
		fail(err)
	}
	current, reachable := "", false
	if tok, err := control.EnsureToken(controlToken(v.state)); err == nil {
		c := control.NewClient(guestDialer(v.paths()), tok)
		if h, err := c.Health(); err == nil {
			current, reachable = h.System, true
		}
	}
	renderSystem(os.Stdout, booted, current, reachable)
}
