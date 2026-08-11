package agentd

import (
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

// Pty allocation without cgo and without x/sys, because the module has no
// dependencies and this is the only reason it would gain one. All three
// steps are plain ioctls on /dev/ptmx.
const (
	tiocSPtLck = 0x40045431 // unlock the slave side
	tiocGPtN   = 0x80045430 // get the slave's number
	tiocSWinSz = 0x5414     // set window size
	tiocGWinSz = 0x5413     // get window size
	tiocSCtty  = 0x540E     // make it the controlling terminal
)

// WinSize is the kernel's struct winsize. The pixel fields are unused by
// everything here but must be present for the ioctl to be the right size.
type WinSize struct {
	Rows, Cols     uint16
	XPixel, YPixel uint16
}

func ioctl(fd uintptr, req uint, arg uintptr) error {
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, fd, uintptr(req), arg); e != 0 {
		return e
	}
	return nil
}

// OpenPty returns the master and the path of its slave.
func OpenPty() (*os.File, string, error) {
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		return nil, "", err
	}
	var unlock int32
	if err := ioctl(master.Fd(), tiocSPtLck, uintptr(unsafe.Pointer(&unlock))); err != nil {
		master.Close()
		return nil, "", fmt.Errorf("unlock pty: %w", err)
	}
	var n uint32
	if err := ioctl(master.Fd(), tiocGPtN, uintptr(unsafe.Pointer(&n))); err != nil {
		master.Close()
		return nil, "", fmt.Errorf("pty number: %w", err)
	}
	return master, fmt.Sprintf("/dev/pts/%d", n), nil
}

// SetSize resizes the pty. The kernel sends SIGWINCH to the foreground
// process group as a side effect, which is what makes a full-screen program
// re-read the size and repaint; nothing else here asks it to.
func SetSize(f *os.File, rows, cols uint16) error {
	ws := WinSize{Rows: rows, Cols: cols}
	return ioctl(f.Fd(), tiocSWinSz, uintptr(unsafe.Pointer(&ws)))
}

func GetSize(f *os.File) (WinSize, error) {
	var ws WinSize
	err := ioctl(f.Fd(), tiocGWinSz, uintptr(unsafe.Pointer(&ws)))
	return ws, err
}
