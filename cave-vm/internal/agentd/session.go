package agentd

import (
	"errors"
	"io"
	"os"
	"os/exec"
	"sync"
	"syscall"
)

// Session is one agent: a process on a pty, the recent output of that pty,
// and every client currently watching it.
//
// The pty outlives any client, which is the whole point: the agent keeps
// working while nobody is attached, and a client that arrives later replays
// the ring instead of finding a blank screen. More than one client can be
// attached at once, all seeing the same stream, which is what makes an agent
// something two people (or a person and a script) can watch together.
type Session struct {
	ID   string
	cmd  *exec.Cmd
	pty  *os.File
	ring *Ring

	// How to run this agent again, kept so an agent that has exited can be
	// brought back under the same name.
	argv, env []string
	dir       string

	mu      sync.Mutex
	clients map[chan []byte]struct{}
	done    chan struct{}
	exitErr error
}

const ringBytes = 256 << 10

// StartSession runs argv on a new pty. rows and cols size it before the
// process starts, so a program that reads its size once does not see zero.
func StartSession(id string, argv []string, env []string, dir string, rows, cols uint16) (*Session, error) {
	if len(argv) == 0 {
		return nil, errors.New("agentd: empty command")
	}
	master, slavePath, err := OpenPty()
	if err != nil {
		return nil, err
	}
	slave, err := os.OpenFile(slavePath, os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		master.Close()
		return nil, err
	}
	defer slave.Close()

	if rows == 0 || cols == 0 {
		rows, cols = 40, 120
	}
	if err := SetSize(master, rows, cols); err != nil {
		master.Close()
		return nil, err
	}

	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = slave, slave, slave
	cmd.Env = env
	cmd.Dir = dir
	// Setsid plus Setctty: the child needs the pty to be its controlling
	// terminal, else job control, SIGWINCH delivery and ^C all go nowhere.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true}
	if err := cmd.Start(); err != nil {
		master.Close()
		return nil, err
	}

	s := &Session{
		ID:      id,
		cmd:     cmd,
		pty:     master,
		argv:    argv,
		env:     env,
		dir:     dir,
		ring:    NewRing(ringBytes),
		clients: make(map[chan []byte]struct{}),
		done:    make(chan struct{}),
	}
	go s.pump()
	return s, nil
}

// pump is the only reader of the pty. Output goes to the ring first so a
// client attaching mid-write still gets a consistent replay, then to every
// attached client.
func (s *Session) pump() {
	buf := make([]byte, 32<<10)
	for {
		n, err := s.pty.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			s.ring.Write(chunk)
			s.broadcast(chunk)
		}
		if err != nil {
			break
		}
	}
	s.exitErr = s.cmd.Wait()
	s.mu.Lock()
	for ch := range s.clients {
		close(ch)
		delete(s.clients, ch)
	}
	s.mu.Unlock()
	close(s.done)
}

// broadcast never blocks on a slow client: a client that cannot keep up
// loses bytes rather than stalling the agent everyone else is watching.
func (s *Session) broadcast(chunk []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for ch := range s.clients {
		select {
		case ch <- chunk:
		default:
		}
	}
}

// Attach returns the replay and a channel of subsequent output. Both come
// from under one lock, so nothing written between them is lost or doubled.
func (s *Session) Attach() ([]byte, <-chan []byte, func()) {
	ch := make(chan []byte, 64)
	s.mu.Lock()
	replay := s.ring.Snapshot()
	s.clients[ch] = struct{}{}
	s.mu.Unlock()
	return replay, ch, func() {
		s.mu.Lock()
		if _, ok := s.clients[ch]; ok {
			delete(s.clients, ch)
			close(ch)
		}
		s.mu.Unlock()
	}
}

func (s *Session) Write(p []byte) (int, error) { return s.pty.Write(p) }

func (s *Session) Resize(rows, cols uint16) error { return SetSize(s.pty, rows, cols) }

func (s *Session) Size() (WinSize, error) { return GetSize(s.pty) }

func (s *Session) Running() bool {
	select {
	case <-s.done:
		return false
	default:
		return true
	}
}

// Close stops the agent. Used when a VM is shutting down; detaching a client
// deliberately does not come here.
func (s *Session) Close() error {
	if s.cmd.Process != nil {
		_ = s.cmd.Process.Signal(syscall.SIGHUP)
	}
	err := s.pty.Close()
	<-s.done
	if err != nil && !errors.Is(err, os.ErrClosed) {
		return err
	}
	return nil
}

var _ io.Writer = (*Session)(nil)
