package main

import (
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
)

// Whether the guest is running older code than the checkout, and in which of
// the three ways it can be.
//
// The record is a commit, with "-dirty" appended when the build's inputs --
// guest/ and cave-vm/ -- had uncommitted changes at the time. Comparing two of
// those is not a string comparison, which is what it was: when the only
// difference was dirtiness, `was` and `now` differed as strings while their
// commits were the same, and the message stripped "-dirty" from both. So it
// printed the same short sha twice and told you to deploy to catch up -- true,
// but it read as the tool being confused.

const dirtySuffix = "-dirty"

func split(rev string) (commit string, dirty bool) {
	if c, ok := strings.CutSuffix(rev, dirtySuffix); ok {
		return c, true
	}
	return rev, false
}

func short(commit string) string {
	if len(commit) > 7 {
		return commit[:7]
	}
	return commit
}

// stalenessLines is what to say, or nothing when there is nothing to say.
func stalenessLines(was, now string) []string {
	if was == "" {
		// Nothing was ever recorded. A permanent warning about a one-off state
		// is worse than the state.
		return nil
	}
	wasCommit, wasDirty := split(was)
	nowCommit, nowDirty := split(now)

	if wasDirty {
		// What it was built from cannot be named, whatever the checkout is at
		// now: the changes were never committed, so nothing records them.
		return []string{
			"the guest was built from uncommitted changes on " + short(wasCommit),
			"  nothing records what those were; vmcav deploy to be sure",
		}
	}
	if wasCommit == nowCommit {
		if nowDirty {
			// The commit is the same and the tree is not. Saying "built from
			// abc, your checkout is at abc" was true and unreadable.
			return []string{
				"the guest was built from " + short(wasCommit) +
					", which is where your checkout is",
				"  but guest/ or cave-vm/ has uncommitted changes since;" +
					" vmcav deploy to include them",
			}
		}
		return nil
	}
	// A missing `now` means the checkout could not be read. Saying the guest
	// is behind something unnameable is worse than saying nothing.
	if nowCommit == "" {
		return nil
	}
	return []string{
		"the guest was built from " + short(wasCommit),
		"  your checkout is at " + short(nowCommit) + "; vmcav deploy to catch it up",
	}
}

func renderStaleness(w io.Writer, was, now string) {
	for _, line := range stalenessLines(was, now) {
		fmt.Fprintln(w, line)
	}
}

// `cave-vm staleness --was X --now Y`, where the caller reads the record and
// asks git, because both are its own state.
func stalenessCommand(args []string) {
	fs := flag.NewFlagSet("staleness", flag.ExitOnError)
	was := fs.String("was", "", "the revision the guest was built from")
	now := fs.String("now", "", "the revision the checkout is at")
	fs.Parse(args)
	renderStaleness(os.Stdout, *was, *now)
}
