package vmm

import (
	"bytes"
	"fmt"
	"io"
	"os"
)

// blockSize is the unit a sparse copy decides about. 64 KiB is large enough
// that the zero test costs little per byte and small enough that a run of
// zeros shorter than a filesystem's own allocation unit is not worth writing.
const blockSize = 64 << 10

// MaterializeDisk copies the built guest image into the VM's own disk and
// grows it to its configured cap.
//
// This was `cp --sparse=always` followed by `truncate -s`, which is two GNU
// coreutils and neither is on a Mac: BSD cp has no --sparse and macOS ships no
// truncate at all. It is one of the places docs/vm-cave-cross-platform.md
// lists as still breaking there.
//
// Sparseness is the point rather than a detail. The built image is mostly
// empty and the cap is large -- 100 GB by default -- so a copy that wrote
// every zero would turn a few hundred megabytes into the whole cap on disk,
// and do it twice: once for the zeros already in the image and once for the
// growth. Seeking over a hole leaves the filesystem to represent it, which is
// what makes the guest disk elastic instead of merely large.
//
// Writing nothing at all for a hole is also why the truncate at the end is
// required rather than tidy: a file whose last block is a hole ends where the
// last write ended, so without it the disk would be short by however much of
// its tail was empty.
func MaterializeDisk(src, dst string, sizeBytes int64) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	// O_EXCL: the caller decides whether replacing an existing disk is
	// allowed, and doing it here by accident would destroy a guest.
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	defer out.Close()

	if err := copySparse(in, out); err != nil {
		os.Remove(dst)
		return err
	}
	size, err := out.Seek(0, io.SeekCurrent)
	if err != nil {
		os.Remove(dst)
		return err
	}
	if sizeBytes < size {
		os.Remove(dst)
		return fmt.Errorf("vmm: a %d byte cap cannot hold a %d byte image", sizeBytes, size)
	}
	if err := out.Truncate(sizeBytes); err != nil {
		os.Remove(dst)
		return err
	}
	return out.Close()
}

// copySparse writes everything but the holes, seeking over blocks that are
// entirely zero so the filesystem never allocates them.
func copySparse(in io.Reader, out *os.File) error {
	buf := make([]byte, blockSize)
	zero := make([]byte, blockSize)
	for {
		n, err := io.ReadFull(in, buf)
		if n > 0 {
			if bytes.Equal(buf[:n], zero[:n]) {
				if _, serr := out.Seek(int64(n), io.SeekCurrent); serr != nil {
					return serr
				}
			} else if _, werr := out.Write(buf[:n]); werr != nil {
				return werr
			}
		}
		switch err {
		case nil:
		case io.EOF, io.ErrUnexpectedEOF:
			return nil
		default:
			return err
		}
	}
}
