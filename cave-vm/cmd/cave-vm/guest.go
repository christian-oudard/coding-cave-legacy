package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"time"

	"codingcave.dev/cave-vm/internal/control"
	"codingcave.dev/cave-vm/internal/sessions"
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
	// Correcting only past a threshold is what lets `time` be called on every
	// host-to-guest command rather than only by hand.
	ifOffBy := v.fs.Duration("if-off-by", 0, "only correct the clock when it is off by more than this")
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
		// <id> <state> <project> <base>, with the terminal size dropped: it is
		// the pty's shape and belongs to whoever attaches, not to a human
		// asking what is running. The first two fields are unchanged because
		// callers read them by position.
		for _, ag := range a {
			fmt.Printf("%s %s %s %s\n", ag.ID, ag.State, dashIfEmpty(ag.Project), dashIfEmpty(ag.Base))
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
		skew := now.Sub(time.Unix(0, before.Now))
		// Only when it matters, so this can be called on every host-to-guest
		// command without saying anything most of the time. The guest has no
		// NTP and no NIC, so the host is its only clock; the point is to
		// correct it often rather than to report a few milliseconds of round
		// trip as though it were drift.
		if *ifOffBy > 0 && skew.Abs() < *ifOffBy {
			return
		}
		if err := c.SetTime(now); err != nil {
			fail(err)
		}
		fmt.Fprintf(os.Stderr, "guest clock was %s off; set to %s\n",
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

// controlGuest is sessions.Guest over the control plane, which is the third
// way the host reaches transcripts and now the usual one: ssh answers whether
// or not a daemon is up but costs a process and ~85ms a call, and debugfs
// reads a stopped image. The mapping is one-to-one, so this is conversions
// rather than logic.
type controlGuest struct{ c *control.Client }

func (g controlGuest) List() ([]sessions.Record, error) {
	recs, err := g.c.ListSessions()
	if err != nil {
		return nil, err
	}
	out := make([]sessions.Record, 0, len(recs))
	for _, r := range recs {
		// Agent included, and it is the field this conversion existed to drop:
		// a sync over the control plane is the usual path, so leaving it out
		// meant every record arrived unattributed exactly when the VM was
		// running, while the ssh and disk paths carried it.
		out = append(out, sessions.Record{
			ID: r.ID, Size: r.Size, Project: r.Project, Agent: r.Agent,
		})
	}
	return out, nil
}

func (g controlGuest) Read(id string) (io.ReadCloser, error) { return g.c.ReadSession(id) }

func (g controlGuest) Write(id string, r io.Reader, dest sessions.Dest, agent string) error {
	return g.c.WriteSession(id, r, dest == sessions.Shared, agent)
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

// dashIfEmpty keeps the agent line's columns aligned when the guest could not
// say what an agent was created for. A blank field would shift every column
// after it, and these lines are read by position.
func dashIfEmpty(s string) string {
	if s == "" {
		return "-"
	}
	return s
}
