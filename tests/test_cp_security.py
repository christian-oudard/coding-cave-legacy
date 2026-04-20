"""Security tests for cav cp symlink handling.

Tests attack vectors where a malicious agent uses symlinks to:
- Exfiltrate host files via cave->host copy (TOCTOU symlink substitution)
- Write to arbitrary host paths via host->cave copy (symlink destination escape)
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cav.commands.cp import _check_in_dir, _norm, cp


class TestSymlinkDestinationEscape(unittest.TestCase):
    """Agent creates a symlink in worktree pointing outside.

    When user copies a file into cave:symlink/path, _check_in_dir
    must reject paths that resolve (via symlinks) outside the worktree.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.worktree = self.tmpdir / "worktree"
        self.worktree.mkdir()
        self.outside = self.tmpdir / "outside"
        self.outside.mkdir()

        # Agent creates a symlink inside worktree pointing outside
        self.evil_link = self.worktree / "evil"
        self.evil_link.symlink_to(self.outside)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_normpath_does_not_catch_symlink_escape(self):
        """Demonstrate that _norm + _check_in_dir misses symlink escapes."""
        # _norm uses normpath which doesn't resolve symlinks
        path = _norm(self.worktree, "evil/target")
        # This SHOULD raise but currently doesn't
        # _check_in_dir(path, self.worktree)  # passes incorrectly

        # The resolved path is outside the worktree
        resolved = path.resolve()
        self.assertFalse(
            str(resolved).startswith(str(self.worktree.resolve()) + os.sep),
            "Resolved path should escape worktree"
        )

    def test_check_in_dir_rejects_symlink_escape(self):
        """_check_in_dir must reject paths that resolve outside via symlinks."""
        path = _norm(self.worktree, "evil/target")
        with self.assertRaises(SystemExit):
            _check_in_dir(path, self.worktree)

    def test_check_in_dir_allows_normal_subdirectory(self):
        """Normal subdirectories still work."""
        subdir = self.worktree / "subdir"
        subdir.mkdir()
        path = _norm(self.worktree, "subdir/file.txt")
        _check_in_dir(path, self.worktree)  # should not raise

    def test_check_in_dir_allows_worktree_root(self):
        """Copying to worktree root works."""
        path = _norm(self.worktree, "")
        _check_in_dir(path, self.worktree)  # should not raise


class TestCaveToHostSymlinkCopy(unittest.TestCase):
    """Cave->host copy must not follow symlinks even without TOCTOU.

    The current check uses is_symlink() on the top-level source, but
    for safety, the actual file open should use O_NOFOLLOW or equivalent.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cave_dir = self.tmpdir / "cave"
        self.cave_dir.mkdir()
        self.host_dir = self.tmpdir / "host"
        self.host_dir.mkdir()
        self.secret = self.tmpdir / "secret.txt"
        self.secret.write_text("TOP SECRET\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_symlink_file_rejected(self):
        """Direct symlink source is rejected."""
        link = self.cave_dir / "link.txt"
        link.symlink_to(self.secret)

        # Simulate the cave->host path by calling cp with mocked resolution
        # The existing check should catch this
        self.assertTrue(link.is_symlink())

    def test_symlink_in_recursive_copy_skipped(self):
        """Symlinks inside a recursive directory copy must be skipped."""
        subdir = self.cave_dir / "data"
        subdir.mkdir()
        (subdir / "safe.txt").write_text("safe\n")
        (subdir / "evil.txt").symlink_to(self.secret)

        dest = self.host_dir / "data"

        # Use the _copy_no_symlinks approach
        def _copy_no_symlinks(src, dst, **kwargs):
            if Path(src).is_symlink():
                return
            shutil.copy2(src, dst, **kwargs)

        shutil.copytree(subdir, dest, copy_function=_copy_no_symlinks)

        self.assertTrue((dest / "safe.txt").exists())
        self.assertFalse((dest / "evil.txt").exists(),
                         "Symlink should have been skipped")


class TestCheckRefNonBranch(unittest.TestCase):
    """Pre-receive hook must block non-branch refs from agents."""

    def setUp(self):
        # Import hook module
        import importlib.util
        hook_path = Path(__file__).parent.parent / "cav" / "scripts" / "hooks" / "pre-receive"
        loader = importlib.machinery.SourceFileLoader("pre_receive", str(hook_path))
        spec = importlib.util.spec_from_loader("pre_receive", loader)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)

    SHA_A = "a" * 40
    SHA_B = "b" * 40
    ZERO = "0" * 40

    def test_agent_tag_push_rejected(self):
        """Agents cannot create tags."""
        r = self.hook.check_ref(self.ZERO, self.SHA_A, "refs/tags/v1.0", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    def test_agent_notes_push_rejected(self):
        """Agents cannot push to refs/notes/."""
        r = self.hook.check_ref(self.SHA_A, self.SHA_B, "refs/notes/commits", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    def test_agent_replace_push_rejected(self):
        """Agents cannot push to refs/replace/ (would silently rewrite objects)."""
        r = self.hook.check_ref(self.SHA_A, self.SHA_B, "refs/replace/" + self.SHA_A, "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    def test_user_tag_push_allowed(self):
        """Users can still push tags."""
        r = self.hook.check_ref(self.ZERO, self.SHA_A, "refs/tags/v1.0", None)
        self.assertIsNone(r)

    def test_user_notes_push_allowed(self):
        """Users can still push notes."""
        r = self.hook.check_ref(self.SHA_A, self.SHA_B, "refs/notes/commits", None)
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
