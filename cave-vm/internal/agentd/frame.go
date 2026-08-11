package agentd

import (
	"encoding/binary"
	"fmt"
	"io"
)

// The two directions of an attached connection are deliberately asymmetric.
//
// Server to client is raw pty output: it is already a byte stream with no
// structure worth preserving, and framing it would only cost a copy. The end
// of the stream is the end of the connection.
//
// Client to server carries two kinds of thing, keystrokes and window sizes,
// so it is framed. A resize is not data the agent should ever see typed at
// it, which is the whole reason the direction needs a type byte at all.
type FrameType byte

const (
	FrameData   FrameType = 'd'
	FrameResize FrameType = 'w'
)

const maxFrame = 1 << 20

func WriteFrame(w io.Writer, t FrameType, payload []byte) error {
	if len(payload) > maxFrame {
		return fmt.Errorf("agentd: frame of %d bytes exceeds %d", len(payload), maxFrame)
	}
	var hdr [5]byte
	hdr[0] = byte(t)
	binary.BigEndian.PutUint32(hdr[1:], uint32(len(payload)))
	if _, err := w.Write(hdr[:]); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}

func ReadFrame(r io.Reader) (FrameType, []byte, error) {
	var hdr [5]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return 0, nil, err
	}
	n := binary.BigEndian.Uint32(hdr[1:])
	if n > maxFrame {
		return 0, nil, fmt.Errorf("agentd: frame of %d bytes exceeds %d", n, maxFrame)
	}
	payload := make([]byte, n)
	if _, err := io.ReadFull(r, payload); err != nil {
		return 0, nil, err
	}
	return FrameType(hdr[0]), payload, nil
}

func EncodeResize(rows, cols uint16) []byte {
	p := make([]byte, 4)
	binary.BigEndian.PutUint16(p[0:], rows)
	binary.BigEndian.PutUint16(p[2:], cols)
	return p
}

func WriteResize(w io.Writer, rows, cols uint16) error {
	return WriteFrame(w, FrameResize, EncodeResize(rows, cols))
}

func ParseResize(payload []byte) (rows, cols uint16, err error) {
	if len(payload) != 4 {
		return 0, 0, fmt.Errorf("agentd: resize payload is %d bytes, want 4", len(payload))
	}
	return binary.BigEndian.Uint16(payload[0:]), binary.BigEndian.Uint16(payload[2:]), nil
}
