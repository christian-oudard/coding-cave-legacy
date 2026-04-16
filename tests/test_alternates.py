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
from cav.constants import HOST_SHARED_REPO_DIR


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
        ws, _ = workspace.create(self.user_repo, "1")

        alternates = ws / ".git" / "objects" / "info" / "alternates"
        alt_path = alternates.read_text().strip()

        self.assertFalse(alt_path.startswith("/"),
                         f"alternates should be relative: {alt_path}")
        self.assertIn(f".coding-cave/{HOST_SHARED_REPO_DIR}/objects", alt_path)

    def test_alternates_resolves_on_host(self):
        """Relative alternates resolve to the shared repo objects on the host."""
        ws, _ = workspace.create(self.user_repo, "1")
        shared = config.shared_repo_path(self.user_repo)

        # The mount-point directory should have a symlink to real objects.
        mount_point = ws / ".coding-cave" / HOST_SHARED_REPO_DIR
        self.assertTrue(mount_point.is_dir())
        objects_link = mount_point / "objects"
        self.assertTrue(objects_link.is_symlink())
        self.assertEqual(objects_link.resolve(), (shared / "objects").resolve())

    def test_objects_accessible_via_alternates(self):
        """Git operations in the shared clone can access objects from the bare repo."""
        ws, _ = workspace.create(self.user_repo, "1")

        # The clone should see the same history as the bare repo
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=ws,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertIn("init", log)

    def test_coding_cave_dir_excluded(self):
        """The .coding-cave mount-point directory is excluded from git tracking."""
        ws, _ = workspace.create(self.user_repo, "1")

        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws,
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn(".coding-cave", result.stdout)

    def test_bwrap_mount_point_exists(self):
        """The HOST_SHARED_REPO_DIR dir exists as a bwrap mount target."""
        ws, _ = workspace.create(self.user_repo, "1")

        mount_point = ws / ".coding-cave" / HOST_SHARED_REPO_DIR
        self.assertTrue(mount_point.is_dir())
        # Must be a real directory (not a symlink) for bwrap --ro-bind.
        self.assertFalse(mount_point.is_symlink())


class TestStaleAlternatesMigration(unittest.TestCase):
    """Old workspaces with alternates pointing at cave-shared.git must
    be refused at launch and fixed by re-running cav init."""

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

        from cav.init import init
        init(str(self.user_repo))

        # Simulate an old pre-split workspace: alternates pointing at
        # cave-shared.git (the old name for the host's shared repo).
        self.alt = self.user_repo / ".git" / "objects" / "info" / "alternates"
        self.alt.parent.mkdir(parents=True, exist_ok=True)
        self.alt.write_text("../../.coding-cave/cave-shared.git/objects\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_require_init_refuses_stale_alternates(self):
        """require_init refuses a workspace whose alternates are stale."""
        from cav import cli
        with self.assertRaises(SystemExit) as cm:
            cli.require_init(self.user_repo)
        self.assertIn("cav init", str(cm.exception))

    def test_init_fixes_stale_alternates(self):
        """cav init rewrites stale alternates and creates mount point."""
        from cav.init import init
        init(str(self.user_repo))

        expected = f"../../.coding-cave/{HOST_SHARED_REPO_DIR}/objects"
        self.assertEqual(self.alt.read_text().strip(), expected)

        mount_point = self.user_repo / ".coding-cave" / HOST_SHARED_REPO_DIR
        self.assertTrue(mount_point.is_dir())
        self.assertTrue((mount_point / "objects").exists())

    def test_require_init_accepts_after_fix(self):
        """After cav init migrates, require_init passes without error."""
        from cav.init import init
        from cav import cli
        init(str(self.user_repo))
        cli.require_init(self.user_repo)  # no raise


class TestInsideCaveInit(unittest.TestCase):
    """Inside a cave, cav init does not create a local cave-shared.git:
    the parent's shared repo is bind-mounted at host-cave-shared.git."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_repo = Path(self.tmpdir) / "project"
        self.data_dir = Path(self.tmpdir) / "data"

        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        self._saved_cave_version = os.environ.get("CODING_CAVE_VERSION")
        os.environ["CODING_CAVE_VERSION"] = "test"
        config.DATA_DIR = self.data_dir

        self.user_repo.mkdir()
        git("init", "-b", "main", cwd=self.user_repo)
        git("config", "user.email", "test@test.com", cwd=self.user_repo)
        git("config", "user.name", "Test", cwd=self.user_repo)
        (self.user_repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.user_repo)
        git("commit", "-m", "init", cwd=self.user_repo)

        # Simulate the parent cave's shared repo bind-mounted into this cave.
        host_shared = self.user_repo / ".coding-cave" / HOST_SHARED_REPO_DIR
        host_shared.mkdir(parents=True)
        (host_shared / "objects").mkdir()
        (host_shared / "HEAD").write_text("ref: refs/heads/main\n")

    def tearDown(self):
        if self._saved_cave_version is None:
            del os.environ["CODING_CAVE_VERSION"]
        else:
            os.environ["CODING_CAVE_VERSION"] = self._saved_cave_version
        shutil.rmtree(self.tmpdir)

    def test_init_inside_cave_does_not_create_cave_shared(self):
        """cav init inside a cave must not create a local cave-shared.git."""
        from cav.init import init
        init(str(self.user_repo))
        self.assertFalse(
            (self.user_repo / ".coding-cave" / "cave-shared.git").exists())

    def test_init_inside_cave_removes_pre_split_stub(self):
        """cav init inside a cave cleans up a pre-split cave-shared.git stub
        (directory with only a broken objects symlink, no HEAD/config)."""
        stub = self.user_repo / ".coding-cave" / "cave-shared.git"
        stub.mkdir()
        (stub / "objects").symlink_to("/does/not/exist")

        from cav.init import init
        init(str(self.user_repo))
        self.assertFalse(stub.exists())

    def test_init_inside_cave_preserves_real_bare_repo(self):
        """A real cave-shared.git (from a subcave setup) must not be removed."""
        real = self.user_repo / ".coding-cave" / "cave-shared.git"
        real.mkdir()
        (real / "HEAD").write_text("ref: refs/heads/main\n")
        (real / "config").write_text("[core]\n")

        from cav.init import init
        init(str(self.user_repo))
        self.assertTrue(real.exists())
        self.assertTrue((real / "HEAD").exists())

    def test_init_inside_cave_requires_host_shared(self):
        """cav init inside a cave errors if host-cave-shared.git is missing."""
        import shutil as _shutil
        _shutil.rmtree(self.user_repo / ".coding-cave" / HOST_SHARED_REPO_DIR)

        from cav.init import init
        with self.assertRaises(SystemExit) as cm:
            init(str(self.user_repo))
        self.assertIn("host-cave-shared.git", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
