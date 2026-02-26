"""Tests for overlay writability.

Unit tests reproduce the kernel overlayfs EOVERFLOW bug (CVE-2023-0386):
when kernel overlayfs is mounted in a user namespace with limited UID/GID
mappings, copy-up of lower-layer files with unmapped UIDs/GIDs fails with
EOVERFLOW.  fuse-overlayfs avoids this by handling copy-up in userspace.

Integration tests (requires_cave) verify that the actual cave project
overlays are writable in all subdirectories.
"""
import errno
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Unit tests: reproduce kernel overlayfs EOVERFLOW from UID/GID mapping
# ---------------------------------------------------------------------------

# Helper script run in a child process with its own user namespace.
# It mounts a kernel overlay and tries to write to a specific subdir.
_OVERLAY_HELPER = r"""
import os, subprocess, errno, sys

uid, gid = int(sys.argv[1]), int(sys.argv[2])
lower, upper, work, merged = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
target_subdir = sys.argv[7] if len(sys.argv) > 7 else ""

os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
with open("/proc/self/uid_map", "w") as f:
    f.write(f"0 {uid} 1")
with open("/proc/self/setgroups", "w") as f:
    f.write("deny")
with open("/proc/self/gid_map", "w") as f:
    f.write(f"0 {gid} 1")

r = subprocess.run(
    ["mount", "-t", "overlay", "overlay",
     "-o", f"lowerdir={lower},upperdir={upper},workdir={work},userxattr",
     merged],
    capture_output=True, text=True,
)
if r.returncode != 0:
    print(f"MOUNT_FAIL:{r.stderr.strip()}")
    sys.exit(0)

target = os.path.join(merged, target_subdir) if target_subdir else merged
testfile = os.path.join(target, ".test-copyup")
try:
    fd = os.open(testfile, os.O_CREAT | os.O_WRONLY, 0o644)
    os.close(fd)
    os.unlink(testfile)
    print("WRITE_OK")
except OSError as e:
    print(f"WRITE_FAIL:{e.errno}")
"""


def _run_overlay_test(lower, target_subdir=""):
    """Mount a kernel overlay in a user namespace and try to write."""
    uid, gid = os.getuid(), os.getgid()
    with tempfile.TemporaryDirectory() as tmp:
        upper = os.path.join(tmp, "upper")
        work = os.path.join(tmp, "work")
        merged = os.path.join(tmp, "merged")
        for d in (upper, work, merged):
            os.makedirs(d)
        r = subprocess.run(
            [sys.executable, "-c", _OVERLAY_HELPER,
             str(uid), str(gid), lower, upper, work, merged, target_subdir],
            capture_output=True, text=True,
        )
        return r.stdout.strip()


def _find_unmapped_gid_dir():
    """Find a directory with a GID that doesn't match the current user's GID.

    Inside a cave, project overlays expose the host filesystem's real GIDs
    through the overlay.  Directories whose GID appears as 65534 (overflow)
    have an unmapped GID — exactly the condition that triggers the kernel
    overlayfs EOVERFLOW.
    """
    projects = Path("/projects")
    if not projects.is_dir():
        return None, None
    skip = {".gitdirs", ".shared-objects"}
    my_gid = os.getgid()
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir() or proj.name in skip:
            continue
        for sub in sorted(proj.iterdir()):
            if sub.is_dir() and not sub.is_symlink():
                try:
                    if sub.stat().st_gid != my_gid:
                        return str(proj), sub.name
                except OSError:
                    pass
    return None, None


def _find_mapped_gid_dir():
    """Find a directory with a GID matching the current user's GID."""
    projects = Path("/projects")
    if not projects.is_dir():
        return None, None
    skip = {".gitdirs", ".shared-objects"}
    my_gid = os.getgid()
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir() or proj.name in skip:
            continue
        for sub in sorted(proj.iterdir()):
            if sub.is_dir() and not sub.is_symlink():
                try:
                    if sub.stat().st_gid == my_gid:
                        return str(proj), sub.name
                except OSError:
                    pass
    return None, None


def is_inside_cave():
    return bool(os.environ.get("CODING_CAVE_VERSION"))


requires_cave = pytest.mark.skipif(
    not is_inside_cave(),
    reason="must run inside a cave session",
)


@requires_cave
def test_kernel_overlay_eoverflow_unmapped_gid():
    """Kernel overlayfs copy-up MUST fail with EOVERFLOW for unmapped GIDs.

    This reproduces the bug: mounting kernel overlayfs in a user namespace
    with only 1 GID mapped, then writing to a subdir whose GID is not
    mapped.  The kernel's CVE-2023-0386 check in ovl_copy_up_one() returns
    -EOVERFLOW because kgid_has_mapping() fails.
    """
    lower, subdir = _find_unmapped_gid_dir()
    if lower is None:
        pytest.skip("no directory with unmapped GID found")
    result = _run_overlay_test(lower, subdir)
    assert result == f"WRITE_FAIL:{errno.EOVERFLOW}", (
        f"Expected EOVERFLOW on unmapped-GID dir, got: {result}"
    )


@requires_cave
def test_kernel_overlay_ok_mapped_gid():
    """Kernel overlayfs copy-up should succeed when GIDs are mapped."""
    lower, subdir = _find_mapped_gid_dir()
    if lower is None:
        pytest.skip("no directory with mapped GID found")
    result = _run_overlay_test(lower, subdir)
    assert result == "WRITE_OK", (
        f"Expected write to succeed on mapped-GID dir, got: {result}"
    )


# ---------------------------------------------------------------------------
# Integration tests: verify cave project overlays are all writable
# ---------------------------------------------------------------------------

def project_subdirs():
    """Yield (project_slug, subdir_path) for all mounted project subdirectories."""
    projects = Path("/projects")
    if not projects.is_dir():
        return
    skip = {".gitdirs", ".shared-objects"}
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir() or proj.name in skip:
            continue
        for sub in sorted(proj.iterdir()):
            if sub.is_dir() and not sub.is_symlink():
                yield proj.name, sub


@requires_cave
@pytest.mark.parametrize(
    "slug,subdir",
    list(project_subdirs()),
    ids=[f"{s}/{d.name}" for s, d in project_subdirs()],
)
def test_overlay_writable(slug, subdir):
    """Creating a file in a project subdirectory must not fail with EOVERFLOW."""
    testfile = subdir / f".cave-overlay-test-{os.getpid()}"
    try:
        testfile.touch()
        testfile.unlink()
    except OSError as e:
        if e.errno == errno.EOVERFLOW:
            pytest.fail(
                f"EOVERFLOW writing to {subdir} (inode {subdir.stat().st_ino}): "
                f"overlay copy-up failed (UID/GID mapping or inode overflow)"
            )
        raise
