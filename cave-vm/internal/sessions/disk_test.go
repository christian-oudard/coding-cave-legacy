package sessions

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// diskImage builds a small ext4 image holding the given files, relative to the
// image root. Skips rather than fails where the tools are absent: Disk exists
// to read an ext4 image with debugfs, so a machine without them cannot
// exercise it at all, and pretending otherwise would make the suite red for a
// reason that is not about this code.
func diskImage(t *testing.T, files map[string]string) string {
	t.Helper()
	for _, tool := range []string{"mke2fs", "debugfs"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("%s is not available, so the disk reader cannot be exercised", tool)
		}
	}
	root := t.TempDir()
	for name, body := range files {
		p := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	img := filepath.Join(t.TempDir(), "disk.img")
	out, err := exec.Command("mke2fs", "-q", "-t", "ext4", "-d", root,
		"-b", "1024", img, "4096").CombinedOutput()
	if err != nil {
		t.Fatalf("building the image: %v: %s", err, out)
	}
	return img
}

// A project directory whose name contains a space must still be read.
//
// debugfs takes a whole command as one string and re-tokenizes it, so a name
// reaching that string is a name debugfs interprets. A space turned
// `ls -p <dir>` into an `ls` with two arguments, which is a usage error --
// and debugfs answers a usage error by printing its usage and exiting 0. The
// caller saw success with no output and read it as an empty directory.
//
// It needs no attacker: EncodeProject builds these names from a working
// directory, so any project whose path has a space in it lost its history
// here. This is also the reader used when the guest is too broken to answer,
// immediately before a reformat destroys the disk being read.
func TestDiskReadsAProjectWhoseNameHasASpace(t *testing.T) {
	const plain = "11111111-2222-4333-8444-555555555555"
	const spaced = "66666666-7777-4888-8999-aaaaaaaaaaaa"
	img := diskImage(t, map[string]string{
		"root/.claude/projects/-projects-coding-cave/" + plain + ".jsonl":   "{}\n",
		"root/.claude/projects/-projects-with a space/" + spaced + ".jsonl": "one\ntwo\n",
	})
	d := Disk{Image: img, ClaudeDir: "/root/.claude"}

	recs, err := d.List()
	if err != nil {
		t.Fatal(err)
	}
	found := map[string]Record{}
	for _, r := range recs {
		found[r.ID] = r
	}
	if len(recs) != 2 {
		t.Fatalf("listed %d records, want 2: %+v", len(recs), recs)
	}
	if found[spaced].Project != "-projects-with a space" {
		t.Fatalf("the spaced project is %q", found[spaced].Project)
	}

	// And readable, not merely listed: Read used to rebuild a path out of the
	// same name, so it broke in the same place for the same reason.
	rc, err := d.Read(spaced)
	if err != nil {
		t.Fatalf("reading the record under the spaced name: %v", err)
	}
	defer rc.Close()
	body, err := io.ReadAll(rc)
	if err != nil || string(body) != "one\ntwo\n" {
		t.Fatalf("read %q, %v", body, err)
	}
}

// An id that is not in the image is not found, rather than found empty. The
// inode walk has to fail the same way the path lookup did.
func TestDiskFindRefusesAnAbsentRecord(t *testing.T) {
	img := diskImage(t, map[string]string{
		"root/.claude/projects/-p/11111111-2222-4333-8444-555555555555.jsonl": "{}\n",
	})
	d := Disk{Image: img, ClaudeDir: "/root/.claude"}
	if _, err := d.Read("99999999-8888-4777-8666-555555555555"); err == nil {
		t.Fatal("reading a record the image does not have succeeded")
	}
}

// A guest that never ran an agent has no projects directory, which is an
// answer rather than a failure: it has no sessions.
func TestDiskWithNoProjectsDirectoryListsNothing(t *testing.T) {
	img := diskImage(t, map[string]string{"root/.claude/settings.json": "{}\n"})
	d := Disk{Image: img, ClaudeDir: "/root/.claude"}
	recs, err := d.List()
	if err != nil || len(recs) != 0 {
		t.Fatalf("got %+v, %v", recs, err)
	}
}
