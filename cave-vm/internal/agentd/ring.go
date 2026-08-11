package agentd

import "sync"

// Ring is a fixed-size byte buffer holding the most recent output of an
// agent's terminal. It is what a reattaching client replays to get its
// scrollback back: the pty itself remembers nothing, and neither does any
// detach tool we have used, so the last window of output has to live
// somewhere between the program and the next client.
//
// Bytes, not lines: the stream carries escape sequences that span lines and
// mean nothing when cut at a line boundary. Truncating at an arbitrary byte
// can still split one sequence, which costs at most a smudged first line on
// replay, where splitting on lines would corrupt the whole screen state.
type Ring struct {
	mu   sync.Mutex
	buf  []byte
	size int
}

func NewRing(size int) *Ring {
	if size <= 0 {
		panic("agentd: ring size must be positive")
	}
	return &Ring{size: size}
}

func (r *Ring) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := len(p)
	// A write larger than the whole ring keeps only its tail: the newest
	// bytes are the ones a client needs to paint the current screen.
	if n >= r.size {
		r.buf = append(r.buf[:0], p[n-r.size:]...)
		return n, nil
	}
	if len(r.buf)+n > r.size {
		drop := len(r.buf) + n - r.size
		r.buf = append(r.buf[:0], r.buf[drop:]...)
	}
	r.buf = append(r.buf, p...)
	return n, nil
}

// Snapshot copies what the ring holds, oldest first. A copy, because the
// caller writes it to a socket while the agent keeps producing output.
func (r *Ring) Snapshot() []byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]byte, len(r.buf))
	copy(out, r.buf)
	return out
}

func (r *Ring) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.buf)
}
