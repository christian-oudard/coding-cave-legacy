"""Tests for overlay writability.

Integration tests verify that the actual cave project overlays (fuse-overlayfs)
are writable in all subdirectories.
"""
import errno
import os
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
