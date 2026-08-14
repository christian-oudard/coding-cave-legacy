package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"codingcave.dev/cave-vm/internal/sessions"
)

// Session records are host state: they outlive the guest disk, a reformat and
// the machine, and they are what `--resume` needs. `checkpoint` copies out what
// has grown, `restore` puts records back into a guest that does not have them.
// Both are one-shot, so the caller decides when: an agent exiting, a stop, and
// before a reformat, which is the one that may not be skipped.
func sessionsCommand(args []string) {
	fs := flag.NewFlagSet("sessions", flag.ExitOnError)
	archive := fs.String("archive", defaultArchive(), "the host's record directory")
	config := fs.String("ssh-config", "", "ssh_config naming the guest")
	host := fs.String("host", "cave-guest", "the guest's alias in that config")
	fs.Parse(args)

	if *config == "" {
		fatal(fmt.Errorf("sessions: -ssh-config is required"))
	}
	guest := sessions.SSH{Config: *config, Host: *host}
	a := sessions.Archive{Dir: *archive}

	switch fs.Arg(0) {
	case "checkpoint":
		report(sessions.Checkpoint(guest, a))
	case "restore":
		ids := fs.Args()[1:]
		if len(ids) == 0 {
			fatal(fmt.Errorf("sessions restore: which session?"))
		}
		report(sessions.Restore(guest, a, ids))
	case "list":
		records, err := a.List()
		if err != nil {
			fatal(err)
		}
		for _, r := range records {
			fmt.Printf("%s %d\n", r.ID, r.Size)
		}
	default:
		fmt.Fprintln(os.Stderr,
			"usage: cave-vm sessions -ssh-config F [checkpoint|restore <id>...|list]")
		os.Exit(2)
	}
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
