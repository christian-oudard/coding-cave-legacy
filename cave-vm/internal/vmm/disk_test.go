package vmm

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestMaterializeDiskCopiesAndGrows(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "nixos.img")
	body := append([]byte("boot sector"), make([]byte, 200<<10)...)
	body = append(body, []byte("and something at the end")...)
	if err := os.WriteFile(src, body, 0o444); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(dir, "guest.img")

	const cap = 8 << 20
	if err := MaterializeDisk(src, dst, cap); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Size() != cap {
		t.Fatalf("disk is %d bytes, want the %d byte cap", fi.Size(), cap)
	}
	// Writable, whatever the built image's mode was: the source comes out of
	// the nix store, which is read-only, and the VM writes to this.
	if fi.Mode().Perm()&0o200 == 0 {
		t.Fatalf("disk is %v, which the VM cannot write", fi.Mode().Perm())
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got[:len(body)], body) {
		t.Fatal("the image did not survive the copy")
	}
	if !bytes.Equal(got[len(body):], make([]byte, cap-len(body))) {
		t.Fatal("the growth is not zeroes")
	}
}

// The holes stay holes. Writing them would turn a mostly-empty image into its
// whole cap on disk, which is the difference between an elastic guest disk and
// one that costs 100 GB the day it is made.
func TestMaterializeDiskLeavesHolesUnallocated(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "nixos.img")
	body := make([]byte, 4<<20)
	copy(body, "data at the front")
	if err := os.WriteFile(src, body, 0o644); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(dir, "guest.img")
	if err := MaterializeDisk(src, dst, 64<<20); err != nil {
		t.Fatal(err)
	}
	// Blocks actually allocated, which is what `du` reports and what a sparse
	// copy is for. A dense copy of this would be at least the 64 MiB cap.
	var st os.FileInfo
	st, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}
	if st.Size() != 64<<20 {
		t.Fatalf("apparent size is %d", st.Size())
	}
	used := allocatedBytes(t, dst)
	if used > 8<<20 {
		t.Fatalf("a 64 MiB disk holding one block of data uses %d bytes", used)
	}
}

// An image bigger than the cap is a configuration error, not something to
// truncate into: the guest would boot into a filesystem cut off partway.
func TestMaterializeDiskRefusesACapSmallerThanTheImage(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "nixos.img")
	if err := os.WriteFile(src, make([]byte, 4<<20), 0o644); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(dir, "guest.img")
	if err := MaterializeDisk(src, dst, 1<<20); err == nil {
		t.Fatal("accepted a cap smaller than the image")
	}
	if _, err := os.Stat(dst); !os.IsNotExist(err) {
		t.Fatal("a refused materialize left a disk behind")
	}
}

// Never over an existing guest: that file is somebody's VM.
func TestMaterializeDiskRefusesToReplace(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "nixos.img")
	if err := os.WriteFile(src, []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(dir, "guest.img")
	if err := os.WriteFile(dst, []byte("an existing guest"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := MaterializeDisk(src, dst, 1<<20); err == nil {
		t.Fatal("overwrote an existing disk")
	}
	if got, _ := os.ReadFile(dst); string(got) != "an existing guest" {
		t.Fatalf("the existing disk is now %q", got)
	}
}
