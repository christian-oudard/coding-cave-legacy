package agentd

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
)

// Serve answers connections until ln is closed. The daemon holds the agents;
// a connection is only ever a window onto one, so nothing here owns state and
// a client crashing is not an event the agent notices.
func Serve(ln net.Listener, reg *Registry) error {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return err
		}
		go func() {
			defer conn.Close()
			if err := handle(conn, reg); err != nil && err != io.EOF {
				fmt.Fprintf(conn, "error %v\n", err)
			}
		}()
	}
}

func handle(conn net.Conn, reg *Registry) error {
	br := bufio.NewReader(conn)
	line, err := br.ReadString('\n')
	if err != nil {
		return err
	}
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return fmt.Errorf("empty request")
	}
	switch fields[0] {
	case "LIST":
		return doList(conn, reg)
	case "ATTACH":
		return doAttach(conn, br, reg, fields[1:])
	default:
		return fmt.Errorf("unknown request %q", fields[0])
	}
}

// LIST reports one agent per line. The connection closing is the end of the
// list; there is nothing to say afterwards.
func doList(w io.Writer, reg *Registry) error {
	for _, s := range reg.List() {
		state := "exited"
		if s.Running() {
			state = "running"
		}
		ws, err := s.Size()
		if err != nil {
			ws = WinSize{}
		}
		if _, err := fmt.Fprintf(w, "%s %s %d %d\n", s.ID, state, ws.Rows, ws.Cols); err != nil {
			return err
		}
	}
	return nil
}

// doAttach relays in both directions until the client goes away or the agent
// exits. The client's size wins on attach: a terminal that reconnects at a
// different size expects the agent to repaint at the size it can actually see.
func doAttach(conn net.Conn, br *bufio.Reader, reg *Registry, args []string) error {
	if len(args) != 3 {
		return fmt.Errorf("usage: ATTACH <id> <rows> <cols>")
	}
	rows, err := strconv.ParseUint(args[1], 10, 16)
	if err != nil {
		return fmt.Errorf("bad rows %q", args[1])
	}
	cols, err := strconv.ParseUint(args[2], 10, 16)
	if err != nil {
		return fmt.Errorf("bad cols %q", args[2])
	}
	s, ok := reg.Get(args[0])
	if !ok {
		return fmt.Errorf("no agent %q", args[0])
	}
	if !s.Running() {
		if s, err = reg.Restart(args[0]); err != nil {
			return err
		}
	}

	replay, out, detach := s.Attach()
	defer detach()
	if rows > 0 && cols > 0 {
		if err := s.Resize(uint16(rows), uint16(cols)); err != nil {
			return err
		}
	}
	if _, err := conn.Write(replay); err != nil {
		return err
	}

	// The read side ends the attach by closing the connection under the
	// writer, which is what turns a client hanging up into an unblocked
	// write error rather than a goroutine parked on a channel forever.
	go func() {
		defer conn.Close()
		for {
			t, payload, err := ReadFrame(br)
			if err != nil {
				return
			}
			switch t {
			case FrameData:
				if _, err := s.Write(payload); err != nil {
					return
				}
			case FrameResize:
				r, c, err := ParseResize(payload)
				if err != nil {
					return
				}
				if err := s.Resize(r, c); err != nil {
					return
				}
			}
		}
	}()

	for chunk := range out {
		if _, err := conn.Write(chunk); err != nil {
			return nil
		}
	}
	return nil
}
