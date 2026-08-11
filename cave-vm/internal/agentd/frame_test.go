package agentd

import (
	"bytes"
	"testing"
)

func TestFrameRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteFrame(&buf, FrameData, []byte("hello")); err != nil {
		t.Fatal(err)
	}
	if err := WriteResize(&buf, 31, 101); err != nil {
		t.Fatal(err)
	}

	tp, payload, err := ReadFrame(&buf)
	if err != nil {
		t.Fatal(err)
	}
	if tp != FrameData || string(payload) != "hello" {
		t.Fatalf("got %c %q", tp, payload)
	}

	tp, payload, err = ReadFrame(&buf)
	if err != nil {
		t.Fatal(err)
	}
	if tp != FrameResize {
		t.Fatalf("got type %c", tp)
	}
	rows, cols, err := ParseResize(payload)
	if err != nil {
		t.Fatal(err)
	}
	if rows != 31 || cols != 101 {
		t.Fatalf("got %dx%d", rows, cols)
	}
}

// A short read must not be mistaken for a complete frame: a keystroke split
// across two packets is the normal case, not an error.
func TestReadFrameRejectsTruncatedInput(t *testing.T) {
	var buf bytes.Buffer
	WriteFrame(&buf, FrameData, []byte("hello"))
	truncated := bytes.NewReader(buf.Bytes()[:6])
	if _, _, err := ReadFrame(truncated); err == nil {
		t.Fatal("truncated frame read as complete")
	}
}

func TestParseResizeRejectsWrongLength(t *testing.T) {
	if _, _, err := ParseResize([]byte{1, 2}); err == nil {
		t.Fatal("accepted a 2-byte resize")
	}
}
