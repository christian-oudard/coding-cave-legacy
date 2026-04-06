"""Test that shared clones have correct alternates for bwrap mounting."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cav import config, workspace


def git(*args, cwd=None):
    return subprocess.run(
        ["git"] + list(args), cwd=cwd,
        capture_output=True, text=True, check=True,
    )


class TestAlternates(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_repo = Path(self.tmpdir) / "project"
        self.data_dir = Path(self.tmpdir) / "data"

        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir

        self.user_repo.mkdir()
        git("init", "-b", "main", cwd=self.user_repo)
        git("config", "user.email", "test@test.com", cwd=self.user_repo)
        git("config", "user.name", "Test", cwd=self.user_repo)
        (self.user_repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.user_repo)
        git("commit", "-m", "init", cwd=self.user_repo)

        # Init Coding Cave
        from cav.init import init
        init(str(self.user_repo))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_alternates_is_relative(self):
        """Alternates uses a relative path so it works on both host and cave."""
        ws = workspace.create(self.user_repo, "1")

        alternates = ws / ".git" / "objects" / "info" / "alternates"
        alt_path = alternates.read_text().strip()

        self.assertFalse(alt_path.startswith("/"),
                         f"alternates should be relative: {alt_path}")
        self.assertIn(".coding-cave/cave-shared.git/objects", alt_path)

    def test_alternates_resolves_on_host(self):
        """Relative alternates resolve to the shared repo objects on the host."""
        ws = workspace.create(self.user_repo, "1")
        shared = config.shared_repo_path(self.user_repo)

        # The mount-point directory should have a symlink to real objects.
        mount_point = ws / ".coding-cave" / "cave-shared.git"
        self.assertTrue(mount_point.is_dir())
        objects_link = mount_point / "objects"
        self.assertTrue(objects_link.is_symlink())
        self.assertEqual(objects_link.resolve(), (shared / "objects").resolve())

    def test_objects_accessible_via_alternates(self):
        """Git operations in the shared clone can access objects from the bare repo."""
        ws = workspace.create(self.user_repo, "1")

        # The clone should see the same history as the bare repo
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=ws,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertIn("init", log)

    def test_coding_cave_dir_excluded(self):
        """The .coding-cave mount-point directory is excluded from git tracking."""
        ws = workspace.create(self.user_repo, "1")

        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws,
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn(".coding-cave", result.stdout)

    def test_bwrap_mount_point_exists(self):
        """The .coding-cave/cave-shared.git dir exists as a bwrap mount target."""
        ws = workspace.create(self.user_repo, "1")

        mount_point = ws / ".coding-cave" / "cave-shared.git"
        self.assertTrue(mount_point.is_dir())
        # Must be a real directory (not a symlink) for bwrap --ro-bind.
        self.assertFalse(mount_point.is_symlink())


if __name__ == "__main__":
    unittest.main()
