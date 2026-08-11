package agentd

import (
	"bytes"
	"strings"
	"testing"
)

func TestRingKeepsNewestBytes(t *testing.T) {
	r := NewRing(8)
	r.Write([]byte("abcdef"))
	if got := string(r.Snapshot()); got != "abcdef" {
		t.Fatalf("under capacity: got %q", got)
	}
	r.Write([]byte("ghij"))
	if got := string(r.Snapshot()); got != "cdefghij" {
		t.Fatalf("over capacity: got %q, want the last 8 bytes", got)
	}
	if r.Len() != 8 {
		t.Fatalf("len %d, want 8", r.Len())
	}
}

func TestRingWriteLargerThanCapacity(t *testing.T) {
	r := NewRing(4)
	r.Write([]byte("0123456789"))
	if got := string(r.Snapshot()); got != "6789" {
		t.Fatalf("got %q, want the tail", got)
	}
}

func TestRingSnapshotIsACopy(t *testing.T) {
	r := NewRing(8)
	r.Write([]byte("abcd"))
	snap := r.Snapshot()
	r.Write([]byte("efgh"))
	if string(snap) != "abcd" {
		t.Fatalf("snapshot changed under the caller: %q", snap)
	}
}

func TestRingManySmallWrites(t *testing.T) {
	r := NewRing(16)
	for i := 0; i < 1000; i++ {
		r.Write([]byte("xy"))
	}
	if got := r.Snapshot(); !bytes.Equal(got, []byte(strings.Repeat("xy", 8))) {
		t.Fatalf("got %q", got)
	}
}
