package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"time"

	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/vmm"
)

// guestCommand is the host end of the control plane: everything routine that
// `cav` asks the guest, over one typed protocol instead of an ssh call per
// question. ssh remains for a human wanting a shell, and for nothing else.
//
//	cave-vm guest --state DIR health
//	cave-vm guest --state DIR agents
//	cave-vm guest --state DIR stop <id>
//	cave-vm guest --state DIR time
//	cave-vm guest --state DIR token
func guestCommand(args []string) {
	v := vmFlagSet("guest")
	asJSON := v.fs.Bool("json", false, "print the wire form rather than lines")
	v.parse(args)
	sub := v.fs.Arg(0)

	// The token is minted here rather than by `start`: whichever side needs it
	// first creates it, the same rule the cache signing key follows, and
	// `token` exists so the seed staging can ask for it without a call that
	// also talks to a VM that may not be running.
	tok, err := control.EnsureToken(controlToken(v.state))
	if err != nil {
		fail(err)
	}
	if sub == "token" {
		fmt.Println(tok)
		return
	}

	c := control.NewClient(guestDialer(v.paths()), tok)
	switch sub {
	case "health":
		h, err := c.Health()
		if err != nil {
			fail(err)
		}
		if *asJSON {
			emit(h)
			return
		}
		// Line-oriented by default, because the caller is a shell script and
		// the alternative is making jq a hard dependency of `status`.
		fmt.Printf("system %s\n", h.System)
		fmt.Printf("uptime %d\n", h.Uptime)
	case "agents":
		a, err := c.Agents()
		if err != nil {
			fail(err)
		}
		if *asJSON {
			emit(a)
			return
		}
		for _, ag := range a {
			fmt.Printf("%s %s %s\n", ag.ID, ag.State, ag.Info)
		}
	case "stop":
		id := v.fs.Arg(1)
		if id == "" {
			fail(fmt.Errorf("stop needs an agent id"))
		}
		if err := c.StopAgent(id); err != nil {
			fail(err)
		}
	case "time":
		// Read back what the guest thinks it is, so a caller can see the skew
		// that was corrected rather than being told nothing happened.
		before, err := c.Health()
		if err != nil {
			fail(err)
		}
		now := time.Now()
		if err := c.SetTime(now); err != nil {
			fail(err)
		}
		skew := now.Sub(time.Unix(0, before.Now))
		fmt.Printf("guest clock was %s off; set to %s\n",
			skew.Round(time.Millisecond), now.Format(time.RFC3339))
	default:
		fmt.Fprintln(os.Stderr, "usage: cave-vm guest --state DIR health|agents|stop <id>|time|token")
		os.Exit(2)
	}
}

// controlToken is the shared secret's home on the host. Beside the cache key
// and the ssh key, under the same owner-only permissions.
func controlToken(state string) string { return filepath.Join(state, "control-token") }

// guestDialer is the only place the transport appears. A cave hosted somewhere
// other than this machine replaces this function and nothing else.
func guestDialer(p vmm.Paths) func(context.Context) (net.Conn, error) {
	return func(context.Context) (net.Conn, error) {
		return backend().Dial(p, control.Port)
	}
}

func emit(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	enc.Encode(v)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "cave-vm:", err)
	os.Exit(1)
}
