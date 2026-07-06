// Command cave-vm is the host CLI for the single-VM cave (see
// ../../../vm-cave-design.md). Phase-0 skeleton: only `version` for now;
// start/stop/status/shell/agent land in later phases.
package main

import (
	"fmt"
	"os"
)

const version = "dev"

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: cave-vm <command>")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version":
		fmt.Println(version)
	default:
		fmt.Fprintf(os.Stderr, "cave-vm: unknown command %q\n", os.Args[1])
		os.Exit(2)
	}
}
