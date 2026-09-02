package main

import (
	"fmt"
	"io"
	"os"

	"codingcave.dev/cave-vm/internal/control"
)

// The agent roster as a person reads it, rather than as a caller parses it.
//
// This was in the shell, and it worked by re-parsing the machine-readable form
// this program had just printed: `awk '$2 == "running"'` over lines built from
// a []Agent that had the same information as fields. Two bugs came out of that
// round trip and both reached a person.
//
// The first was `grep ' running '`, which needs a space on each side, so an
// agent printed with no project and no base fell out of the list of running
// agents entirely. The second is worse and is the reason this moved: the shell
// wrote `roster=$(guest_rpc agents || true)`, which turns "the guest did not
// answer" and "the guest has nothing" into the same empty string. A cave with
// a live agent in it reported "agents: none running (vmcav agent starts one)"
// to the agent reading the output.
//
// Neither is a mistake you can make here. Reachability is a separate argument
// from the list, and an Agent is a struct rather than four positional fields
// that have to survive a shell word split.

// renderAgents writes the roster section of `vmcav status`.
//
// Pure, and takes what it needs rather than fetching it, so the interesting
// cases -- an unreachable guest, an agent with nothing recorded, a stopped
// agent still holding its id -- are a table rather than a live VM.
func renderAgents(w io.Writer, agents []control.Agent, reachable bool) {
	if !reachable {
		// Not "none". Nothing has been learned about this guest, and an agent
		// may well be running in it -- possibly the one reading this.
		fmt.Fprintln(w, "agents: unknown (the guest is not answering)")
		return
	}

	var running, stopped []control.Agent
	for _, a := range agents {
		switch a.State {
		case "running":
			running = append(running, a)
		default:
			// Anything that is not running is holding its id: its worktree and
			// its home are on the guest's disk and outlive the session.
			stopped = append(stopped, a)
		}
	}

	if len(running) == 0 {
		fmt.Fprintln(w, "agents: none running (vmcav agent starts one)")
	} else {
		fmt.Fprintf(w, "agents: %d\n", len(running))
		for _, a := range running {
			// What it is working on, not its id and the shape of its terminal.
			// With one agent the number identified it; with several it says
			// nothing, and the project and branch is the question being asked.
			fmt.Fprintf(w, "  agent %s: %s @ %s (pushes land on agent-%s/%s)\n",
				a.ID, orUnknown(a.Project), orUnknown(a.Base), a.ID, orUnknown(a.Base))
		}
	}

	for _, a := range stopped {
		fmt.Fprintf(w, "  agent %s: stopped, still holding its worktree and home\n", a.ID)
		// The session's environment file is on a tmpfs and the agent outlives
		// it, so a stopped agent often has nothing left to say about itself.
		// It is listed either way: holding an id is the point of the line.
		if p := orUnknown(a.Project); p != "?" {
			fmt.Fprintf(w, "    was working on: %s @ %s\n", p, orUnknown(a.Base))
		}
	}
	if len(stopped) > 0 {
		fmt.Fprintln(w, "  these hold their ids (vmcav agent remove <id>, or vmcav gc)")
	}
}

// orUnknown turns the two ways of having nothing -- an empty field and the
// dash the wire form uses for one -- into the same visible answer.
func orUnknown(s string) string {
	if s == "" || s == "-" {
		return "?"
	}
	return s
}


// renderAfterStart is what `vmcav start` says once the guest is up: not the
// roster, but what to do next, which is a different sentence depending on
// whether the guest is holding anything.
//
// A stop leaves an agent's worktree and home behind -- they are what hold its
// id -- so a guest that has run before comes back holding agents. Saying "no
// agents yet" there is the sentence that makes somebody create a second agent
// beside the one that has their conversation in it.
func renderAfterStart(w io.Writer, agents []control.Agent, reachable bool) {
	if !reachable {
		// The same distinction the roster makes. A guest that cannot answer
		// has not told us it is empty.
		fmt.Fprintln(w, "cannot tell what this guest holds; it is not answering")
		return
	}
	var held []control.Agent
	for _, a := range agents {
		if a.State != "running" {
			held = append(held, a)
		}
	}
	if len(held) == 0 {
		fmt.Fprintln(w, "no agents yet; start one:  vmcav agent [project] [branch]")
		return
	}
	fmt.Fprintln(w, "this guest still holds:")
	for _, a := range held {
		if p := orUnknown(a.Project); p != "?" {
			fmt.Fprintf(w, "  agent %s: %s @ %s\n", a.ID, p, orUnknown(a.Base))
		} else {
			fmt.Fprintf(w, "  agent %s\n", a.ID)
		}
	}
	fmt.Fprintln(w, "bring one back:  vmcav attach <id>")
	fmt.Fprintln(w, "or start another:  vmcav agent [project] [branch]")
}

// agentsCommand is `cave-vm agents`, the roster section on stdout.
//
// Exit status says whether the guest answered, so the shell can still tell the
// two apart without parsing the prose back.
func agentsCommand(args []string) {
	v := vmFlagSet("agents")
	// What `start` says when it has finished, which is about what to do next
	// rather than about what is running.
	afterStart := v.fs.Bool("after-start", false, "say what to do next, as `start` does")
	v.parse(args)

	render := renderAgents
	if *afterStart {
		render = renderAfterStart
	}

	tok, err := control.EnsureToken(controlToken(v.state))
	if err != nil {
		fail(err)
	}
	c := control.NewClient(guestDialer(v.paths()), tok)
	agents, err := c.Agents()
	if err != nil {
		render(os.Stdout, nil, false)
		os.Exit(3)
	}
	render(os.Stdout, agents, true)
}
