"""Tests for overlay writability across all mounted projects.

These are integration tests that run INSIDE a cave session. They verify
that every project overlay is writable in all subdirectories — catching
the uuid=on EOVERFLOW bug where bwrap's --overlay forces uuid=on, which
remaps inodes and can overflow on ext4 filesystems with high inode numbers.

Run from the host with:  coding-cave shell -- pytest tests/test_overlay.py
"""
import errno
import os
import subprocess
import tempfile
from pathlib import Path

import pytest


def is_inside_cave():
    return bool(os.environ.get("CODING_CAVE_VERSION"))


requires_cave = pytest.mark.skipif(
    not is_inside_cave(),
    reason="must run inside a cave session",
)


def project_subdirs():
    """Yield (project_slug, subdir_path) for all mounted project subdirectories."""
    projects = Path("/projects")
    if not projects.is_dir():
        return
    # Skip internal dirs (gitdirs, shared-objects are not project overlays)
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
                f"bwrap overlay uuid=on is remapping inodes beyond representable range"
            )
        raise


@requires_cave
def test_nested_bwrap_overlay():
    """A nested bwrap overlay on a project dir must allow writes in all subdirs.

    This simulates starting a nested cave on an existing project mount.
    """
    # Pick a project with subdirectories
    projects = Path("/projects")
    source = None
    subdirs = []
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir():
            continue
        subs = [s for s in proj.iterdir() if s.is_dir() and not s.is_symlink()]
        if subs:
            source = proj
            subdirs = subs
            break

    if source is None:
        pytest.skip("no project with subdirectories")

    # Nest a bwrap overlay (simulating inner cave) using --tmp-overlay
    write_cmds = []
    for sub in subdirs:
        rel = sub.relative_to(source)
        write_cmds.append(
            f'touch /nested/{rel}/.nest-test-$$ 2>&1 || '
            f'echo "FAIL:{rel}"'
        )

    script = "; ".join(write_cmds)
    result = subprocess.run(
        ["bwrap", "--bind", "/", "/",
         "--overlay-src", str(source),
         "--tmp-overlay", "/nested",
         "--", "bash", "-c", script],
        capture_output=True, text=True,
    )

    failures = [
        line.removeprefix("FAIL:")
        for line in result.stdout.splitlines()
        if line.startswith("FAIL:")
    ]
    if failures:
        pytest.fail(
            f"Nested overlay writes failed in: {', '.join(failures)}. "
            f"uuid=on from outer overlay causes EOVERFLOW in nested mounts."
        )
