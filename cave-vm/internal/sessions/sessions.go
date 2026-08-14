// Package sessions moves claude's session transcripts out of a guest and back
// into one. See docs/vm-cave-design.md: the archive on the host is the record,
// the guest holds a working copy, and the host does all the naming so that a
// compromised guest cannot choose where bytes land.
package sessions

import (
	"io"
	"strings"
)

// Record is one transcript, as the side holding it reports it. The id is the
// session uuid and is also the file's stem, everywhere: claude finds resumable
// sessions by scanning a directory and reading stems, so that name has to
// survive the round trip or `--resume` and the picker stop working.
type Record struct {
	ID   string
	Size int64
}

// Guest is the side of the boundary holding live transcripts. Local
// implements it against a filesystem, SSH against a running VM, which is what
// lets the copying logic be tested without one.
type Guest interface {
	List() ([]Record, error)
	Read(id string) (io.ReadCloser, error)
	Write(id string, r io.Reader) error
}

// ValidID is the whole of the host's trust in what the guest says. Ids become
// filenames, so anything that is not a plain uuid is discarded rather than
// sanitized: there is no legitimate session id with a slash, a dot or a length
// other than this in it.
func ValidID(id string) bool {
	groups := strings.Split(id, "-")
	want := []int{8, 4, 4, 4, 12}
	if len(groups) != len(want) {
		return false
	}
	for i, g := range groups {
		if len(g) != want[i] {
			return false
		}
		for _, c := range g {
			if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F') {
				return false
			}
		}
	}
	return true
}

// EncodeProject is claude's directory name for a working directory: the path
// with separators turned into dashes, so /root/coding-cave becomes
// -root-coding-cave. Only the guest computes this, from the cwd a session will
// run in, which is why the host never needs to know guest paths.
func EncodeProject(dir string) string {
	return strings.ReplaceAll(dir, "/", "-")
}
