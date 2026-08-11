package agentd

import (
	"os"
	"unsafe"
)

const (
	tcGets = 0x5401
	tcSets = 0x5402
)

// Termios is the kernel's struct, laid out for linux. The trailing speed
// fields are longer than what TCGETS fills in on some arches; the extra bytes
// are simply left zero, which is harmless in both directions.
type Termios struct {
	Iflag, Oflag, Cflag, Lflag uint32
	Line                       uint8
	Cc                         [19]uint8
	_                          [3]byte
	Ispeed, Ospeed             uint32
}

const (
	iIgnBrk = 0x1
	iBrkInt = 0x2
	iParMrk = 0x8
	iIStrip = 0x20
	iInlCr  = 0x40
	iIgnCr  = 0x80
	iICrNl  = 0x100
	iIxOn   = 0x400

	oOPost = 0x1

	lISig    = 0x1
	lICanon  = 0x2
	lEcho    = 0x8
	lEchoNl  = 0x40
	lIExtern = 0x8000

	cCSize  = 0x30
	cParEnb = 0x100
	cCS8    = 0x30

	vTime = 5
	vMin  = 6
)

func GetTermios(f *os.File) (Termios, error) {
	var t Termios
	err := ioctl(f.Fd(), tcGets, uintptr(unsafe.Pointer(&t)))
	return t, err
}

func SetTermios(f *os.File, t Termios) error {
	return ioctl(f.Fd(), tcSets, uintptr(unsafe.Pointer(&t)))
}

// MakeRaw hands every keystroke straight through, including the ones a shell
// would otherwise interpret: the agent on the far end is the thing that
// should decide what ^C means, not the terminal in front of it. It returns
// the previous settings for the caller to restore.
func MakeRaw(f *os.File) (Termios, error) {
	old, err := GetTermios(f)
	if err != nil {
		return old, err
	}
	raw := old
	raw.Iflag &^= iIgnBrk | iBrkInt | iParMrk | iIStrip | iInlCr | iIgnCr | iICrNl | iIxOn
	raw.Oflag &^= oOPost
	raw.Lflag &^= lEcho | lEchoNl | lICanon | lISig | lIExtern
	raw.Cflag &^= cCSize | cParEnb
	raw.Cflag |= cCS8
	raw.Cc[vMin] = 1
	raw.Cc[vTime] = 0
	return old, SetTermios(f, raw)
}

func IsTerminal(f *os.File) bool {
	_, err := GetTermios(f)
	return err == nil
}
