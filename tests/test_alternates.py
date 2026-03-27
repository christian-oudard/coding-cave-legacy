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

        os.environ["CAV_DATA_DIR"] = str(self.data_dir)
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

    def test_alternates_points_to_bare_repo(self):
        """Shared clone's alternates file points to the bare repo's objects dir."""
        ws = workspace.create(self.user_repo, "1")

        alternates = ws / ".git" / "objects" / "info" / "alternates"
        self.assertTrue(alternates.exists(), "alternates file missing")

        alt_path = alternates.read_text().strip()
        bare_objects = str(config.shared_repo_path(self.user_repo) / "objects")

        self.assertEqual(alt_path, bare_objects)
        self.assertTrue(Path(alt_path).exists(), f"alternates target missing: {alt_path}")

    def test_alternates_path_is_absolute(self):
        """Alternates must be absolute for bwrap bind-mount to work."""
        ws = workspace.create(self.user_repo, "1")

        alternates = ws / ".git" / "objects" / "info" / "alternates"
        alt_path = alternates.read_text().strip()

        self.assertTrue(alt_path.startswith("/"), f"alternates not absolute: {alt_path}")

    def test_objects_accessible_via_alternates(self):
        """Git operations in the shared clone can access objects from the bare repo."""
        ws = workspace.create(self.user_repo, "1")

        # The clone should see the same history as the bare repo
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=ws,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertIn("init", log)

    def test_bwrap_mount_path_matches_alternates(self):
        """The bare repo path in alternates is what sandbox.py would bind-mount."""
        ws = workspace.create(self.user_repo, "1")

        alternates = ws / ".git" / "objects" / "info" / "alternates"
        alt_path = Path(alternates.read_text().strip())

        # The parent of objects/ is the bare repo root
        bare_from_alternates = alt_path.parent
        bare_expected = config.shared_repo_path(self.user_repo)

        self.assertEqual(bare_from_alternates, bare_expected)


if __name__ == "__main__":
    unittest.main()
