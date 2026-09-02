package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/sessions"
	"codingcave.dev/cave-vm/internal/vmm"
)

// Session records are host state: they outlive the guest disk, a reformat and
// the machine, and they are what `--resume` needs.
//
//	sync      copy out what is new or has grown (ssh, or -disk for a stopped VM)
//	restore   put an agent's own records back where its picker will find them
//	share     copy every record into the guest's read-only view
//	list      what the archive holds
//
// All one-shot, so the caller decides when: an agent exiting, a stop, and
// before a reformat, which is the one that may not be skipped.
func sessionsCommand(args []string) {
	fs := flag.NewFlagSet("sessions", flag.ExitOnError)
	archive := fs.String("archive", defaultArchive(), "the host's record directory")
	config := fs.String("ssh-config", "", "ssh_config naming the guest")
	host := fs.String("host", "cave-guest", "the guest's alias in that config")
	// Reading a stopped image instead of asking a running guest. This is what
	// makes a reformat safe when the guest is too broken to answer, which is
	// exactly when a reformat happens.
	disk := fs.String("disk", "", "read a stopped guest image instead of asking the guest")
	state := fs.String("state", "", "state directory, to reach the guest over the control plane")
	// Restore only. A sync no longer takes an agent: which agent a record
	// belongs to is a fact about the home it was found in, so the guest says
	// it per record instead of the host asserting one for the whole batch.
	// Empty means every agent's, each into its own home. A name narrows it.
	agent := fs.String("agent", "", "whose records to restore (default: everyone)")
	fs.Parse(args)

	a := sessions.Archive{Dir: *archive}
	// Only the verbs that cross the boundary need a guest; listing what the
	// host holds is a host question and must answer with the VM stopped, gone,
	// or never created.
	guest := func() sessions.Guest {
		g, err := pickGuest(*disk, *state, *config, *host)
		if err != nil {
			fatal(err)
		}
		return g
	}

	switch fs.Arg(0) {
	case "sync":
		report(sessions.Sync(guest(), a))
	case "restore":
		ids := fs.Args()[1:]
		if len(ids) == 0 {
			// Everything the archive holds, each record into the home of the
			// agent that wrote it. That is what a VM start wants: it runs
			// before any agent exists, so restoring "the agent's own" had to
			// name one, and naming one meant every other agent's history
			// stayed on the host while that one's landed in a picker it did
			// not belong to. Naming an agent still asks for just its records.
			var err error
			if *agent == "" {
				var recs []sessions.Record
				if recs, err = a.List(); err == nil {
					for _, r := range recs {
						ids = append(ids, r.ID)
					}
				}
			} else {
				ids, err = a.Owned(*agent)
			}
			if err != nil {
				fatal(err)
			}
		}
		report(sessions.Restore(guest(), a, ids))
	case "share":
		report(sessions.Share(guest(), a))
	case "list":
		records, err := a.List()
		if err != nil {
			fatal(err)
		}
		for _, r := range records {
			m := a.ReadMeta(r.ID)
			fmt.Printf("%s %d %s %s\n", r.ID, r.Size, orDash(m.Agent), orDash(m.Project))
		}
	default:
		fmt.Fprintln(os.Stderr,
			"usage: cave-vm sessions [-ssh-config F | -disk IMG] [sync|restore [id...]|share|list]")
		os.Exit(2)
	}
}

// pickGuest is the one place that decides which side of the boundary answers.
// Three ways, and the order is the point:
//
//   - a stopped image, read with debugfs. First because it is asked for
//     explicitly, and because it is the only one that works for the guest most
//     likely to need reading, the one too broken to boot.
//   - a running guest over the control plane. The usual path: a call costs
//     about 2ms, which is what makes syncing on every `status` affordable.
//   - a running guest over ssh. Kept because it needs nothing in the guest to
//     be listening but cave-guestd's binary, so it still answers when the
//     control plane is the thing that is broken.
func pickGuest(disk, state, config, host string) (sessions.Guest, error) {
	if disk != "" {
		return sessions.Disk{Image: disk}, nil
	}
	if state != "" {
		tok, err := control.EnsureToken(controlToken(state))
		if err != nil {
			return nil, err
		}
		return controlGuest{control.NewClient(guestDialer(vmm.NewPaths(state)), tok)}, nil
	}
	if config == "" {
		return nil, fmt.Errorf("sessions: give -state (or -ssh-config) for a running guest, or -disk for a stopped one")
	}
	return sessions.SSH{Config: config, Host: host}, nil
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

// report says what moved and fails loudly on anything that did not, without
// hiding the records that did: a checkpoint runs when something is about to be
// destroyed, so eight of ten is worth having and worth knowing about.
func report(changes []sessions.Change, err error) {
	if err != nil {
		fatal(err)
	}
	bad := 0
	for _, c := range changes {
		if c.Err != nil {
			bad++
			fmt.Fprintf(os.Stderr, "%s: %v\n", c.ID, c.Err)
			continue
		}
		fmt.Printf("%s %s\n", c.ID, c.Outcome)
	}
	if bad > 0 {
		os.Exit(1)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "cave-vm:", err)
	os.Exit(1)
}

// The archive lives beside the rest of the data dir, flat, so a record is
// findable with ls and whatever backs up a home directory covers it.
func defaultArchive() string {
	dir := os.Getenv("CODING_CAVE_DATA_DIR")
	if dir == "" {
		if x := os.Getenv("XDG_DATA_HOME"); x != "" {
			dir = filepath.Join(x, "coding-cave")
		} else {
			dir = filepath.Join(os.Getenv("HOME"), ".local", "share", "coding-cave")
		}
	}
	return filepath.Join(dir, "sessions")
}
