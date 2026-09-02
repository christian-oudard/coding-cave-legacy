package vmm

import (
	"os"
	"syscall"
	"testing"
)

// allocatedBytes is what the filesystem actually gave the file, as `du`
// reports it, rather than how long it claims to be.
func allocatedBytes(t *testing.T, path string) int64 {
	t.Helper()
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		t.Skip("no block count on this platform")
	}
	return st.Blocks * 512
}
