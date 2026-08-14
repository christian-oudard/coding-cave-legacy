package agentd

import (
	"bytes"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// DetachKey is ^\, the same key dtach used before the daemon existed, so the
// habit carries over. In raw mode it is an ordinary byte rather than SIGQUIT.
const DetachKey = 0x1c

// Detaching leaves the terminal however the agent's TUI left it: cursor
// hidden, colours set, bracketed paste and mouse reporting on, and a scroll
// region covering part of the screen. Restoring the termios settings does not
// undo any of that, because none of it is a termios setting -- it is state
// the remote program set by writing escape sequences, and the shell you land
// back in never asked for it. So put it back:
//
//	[0m     attributes
//	[?25h   show the cursor (this is the one you notice)
//	[?2004l bracketed paste
//	[?1000l [?1002l [?1003l [?1006l  mouse reporting
//	[r      scroll region back to the whole screen
//
// Deliberately nothing that touches the screen buffer: no clear, no leaving
// the alternate screen. The point of detaching is to leave the agent's output
// sitting in the scrollback.
const restoreTerminal = "\x1b[0m\x1b[?25h\x1b[?2004l" +
	"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[r"

// SocketPath is where the session called id listens. The name is the whole
// of the addressing: no daemon keeps a table, so a session exists exactly
// when something answers here.
func SocketPath(dir, id string) string { return filepath.Join(dir, id+".sock") }

func dialAgentd(sock string) (net.Conn, error) {
	conn, err := net.Dial("unix", sock)
	if err != nil {
		return nil, fmt.Errorf("agentd: no session at %s: %w", sock, err)
	}
	return conn, nil
}

// EnsureSocket returns the path to a listening session, calling start (which
// is systemd, in the guest) if nothing answers yet and waiting for it to bind.
//
// Starting through systemd rather than forking here is the point of the whole
// arrangement: the session must not be a child of whoever attached, or hanging
// up an ssh connection would take the agent with it. A socket file left behind
// by a killed session is not a session; dialing is what decides, and the new
// one unlinks the corpse before it binds.
func EnsureSocket(dir, id string, start func() error, timeout time.Duration) (string, error) {
	sock := SocketPath(dir, id)
	if listening(sock) {
		return sock, nil
	}
	if err := start(); err != nil {
		return "", fmt.Errorf("agentd: starting session %q: %w", id, err)
	}
	deadline := time.Now().Add(timeout)
	for {
		if listening(sock) {
			return sock, nil
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("agentd: session %q never listened at %s", id, sock)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func listening(sock string) bool {
	conn, err := net.Dial("unix", sock)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// frameWriter serialises the two things that send frames, keystrokes and
// window sizes. Without it a resize can land between a frame's header and its
// payload and desynchronise the stream for good.
type frameWriter struct {
	mu sync.Mutex
	w  io.Writer
}

func (fw *frameWriter) write(t FrameType, payload []byte) error {
	fw.mu.Lock()
	defer fw.mu.Unlock()
	return WriteFrame(fw.w, t, payload)
}

func (fw *frameWriter) resize(rows, cols uint16) error {
	return fw.write(FrameResize, EncodeResize(rows, cols))
}

// Info asks one session to describe itself: "id state rows cols".
func Info(sock string) (string, error) {
	conn, err := dialAgentd(sock)
	if err != nil {
		return "", err
	}
	defer conn.Close()
	if _, err := io.WriteString(conn, "INFO\n"); err != nil {
		return "", err
	}
	body, err := io.ReadAll(conn)
	if err != nil {
		return "", err
	}
	line := strings.TrimSpace(string(body))
	if rest, ok := strings.CutPrefix(line, "error "); ok {
		return "", fmt.Errorf("agentd: %s", rest)
	}
	return line, nil
}

// List returns one line per session, by asking each socket in dir. Sockets
// nobody is listening on are skipped rather than reported: they are what a
// killed session leaves behind, and the next start of that session unlinks
// one. Glob sorts, so the order does not reshuffle between runs.
func List(dir string) ([]string, error) {
	socks, err := filepath.Glob(filepath.Join(dir, "*.sock"))
	if err != nil {
		return nil, err
	}
	var out []string
	for _, sock := range socks {
		line, err := Info(sock)
		if err != nil {
			continue
		}
		out = append(out, line)
	}
	return out, nil
}

// AttachTo connects this terminal to an agent and stays until the user
// detaches or the agent exits. The terminal goes into raw mode for the
// duration: the agent is a full-screen program and needs the keystrokes and
// escape sequences that a cooked terminal would otherwise eat.
func AttachTo(sock string, in, out *os.File) error {
	conn, err := dialAgentd(sock)
	if err != nil {
		return err
	}
	defer conn.Close()

	var rows, cols uint16
	if ws, err := GetSize(out); err == nil {
		rows, cols = ws.Rows, ws.Cols
	}
	if _, err := fmt.Fprintf(conn, "ATTACH %d %d\n", rows, cols); err != nil {
		return err
	}

	if IsTerminal(in) {
		old, err := MakeRaw(in)
		if err != nil {
			return err
		}
		defer SetTermios(in, old)
	}

	fw := &frameWriter{w: conn}

	winch := make(chan os.Signal, 1)
	signal.Notify(winch, syscall.SIGWINCH)
	defer signal.Stop(winch)
	go func() {
		for range winch {
			if ws, err := GetSize(out); err == nil {
				if err := fw.resize(ws.Rows, ws.Cols); err != nil {
					return
				}
			}
		}
	}()

	// A read of the keyboard cannot be cancelled, so this goroutine stays
	// blocked when the agent exits; closing the connection under it is what
	// lets AttachTo return without waiting for one more keystroke.
	detached := make(chan struct{})
	go func() {
		buf := make([]byte, 4096)
		for {
			n, err := in.Read(buf)
			if n > 0 {
				if i := bytes.IndexByte(buf[:n], DetachKey); i >= 0 {
					fw.write(FrameData, buf[:i])
					close(detached)
					conn.Close()
					return
				}
				if err := fw.write(FrameData, buf[:n]); err != nil {
					return
				}
			}
			if err != nil {
				return
			}
		}
	}()

	_, err = io.Copy(out, conn)
	// Both ways out land here: the agent that painted the screen is no longer
	// the one driving this terminal, whether it exited or you walked away.
	fmt.Fprint(out, restoreTerminal)
	select {
	case <-detached:
		fmt.Fprint(out, "\r\n[detached]\r\n")
		return nil
	default:
		return err
	}
}
