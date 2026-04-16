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


class TestBrokenWorkspaceReconfigure(unittest.TestCase):
    """A workspace with wrong alternates must be refused at launch and
    reconfigured by cav init --reinitialize on the host.

    Host-side workspaces live under DATA_DIR/worktrees/<slug>/; the
    user repo itself is a full clone with no alternates.
    """

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

        # Create a real workspace, then rewrite it to the pre-split
        # layout: alternates pointing at cave-shared.git and a stub dir
        # where host-cave-shared.git would live.
        self.ws, _ = workspace.create(self.user_repo, "1")
        self.alt = self.ws / ".git" / "objects" / "info" / "alternates"
        self.alt.write_text("../../.coding-cave/cave-shared.git/objects\n")
        # Remove the new-style mount point to simulate pre-split state.
        new_mount = self.ws / ".coding-cave" / HOST_SHARED_REPO_DIR
        if new_mount.exists():
            shutil.rmtree(new_mount)
        # Recreate the pre-split stub (symlink to the host's real objects,
        # which will become a broken symlink inside a cave namespace).
        self.stub = self.ws / ".coding-cave" / "cave-shared.git"
        self.stub.mkdir(parents=True, exist_ok=True)
        real_objects = config.shared_repo_path(self.user_repo) / "objects"
        (self.stub / "objects").symlink_to(real_objects)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_require_init_refuses_stale_workspace(self):
        """require_init refuses on the host when a workspace is stale."""
        from cav import cli
        with self.assertRaises(SystemExit) as cm:
            cli.require_init(self.user_repo)
        self.assertIn("cav init --reinitialize", str(cm.exception))

    def test_init_without_reinitialize_is_noop(self):
        """Plain cav init on an already-init'd project does not touch
        workspaces."""
        from cav.init import init
        init(str(self.user_repo))  # no reinitialize
        # Stale alternates untouched.
        self.assertEqual(
            self.alt.read_text().strip(),
            "../../.coding-cave/cave-shared.git/objects",
        )
        # Stub still there.
        self.assertTrue(self.stub.exists())

    def test_init_reinitialize_reconfigures_workspace(self):
        """cav init --reinitialize restores the correct workspace layout."""
        from cav.init import init
        init(str(self.user_repo), reinitialize=True)

        expected = f"../../.coding-cave/{HOST_SHARED_REPO_DIR}/objects"
        self.assertEqual(self.alt.read_text().strip(), expected)

        mount_point = self.ws / ".coding-cave" / HOST_SHARED_REPO_DIR
        self.assertTrue(mount_point.is_dir())
        self.assertTrue((mount_point / "objects").exists())

    def test_require_init_accepts_after_fix(self):
        """After cav init --reinitialize migrates, require_init passes."""
        from cav.init import init
        from cav import cli
        init(str(self.user_repo), reinitialize=True)
        cli.require_init(self.user_repo)  # no raise


class TestInsideCaveInit(unittest.TestCase):
    """Inside a cave, cav init creates cave-shared.git just like on the
    host. The parent's shared repo (bind-mounted at host-cave-shared.git)
    supplies the base branches."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_repo = Path(self.tmpdir) / "project"
        self.data_dir = Path(self.tmpdir) / "data"

        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        self._saved_cave_version = os.environ.get("CODING_CAVE_VERSION")
        os.environ["CODING_CAVE_VERSION"] = "test"
        config.DATA_DIR = self.data_dir

        # The parent cave's shared repo. In production this is bind-mounted
        # into user_repo/.coding-cave/host-cave-shared.git. Build it
        # externally, then move into place after cloning.
        external_bare = Path(self.tmpdir) / "parent-cave-shared.git"
        git("init", "--bare", "-b", "main", str(external_bare))

        # Seed main with a commit by using a scratch working repo.
        scratch = Path(self.tmpdir) / "scratch"
        scratch.mkdir()
        git("init", "-b", "main", cwd=scratch)
        git("config", "user.email", "test@test.com", cwd=scratch)
        git("config", "user.name", "Test", cwd=scratch)
        (scratch / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=scratch)
        git("commit", "-m", "init", cwd=scratch)
        git("push", str(external_bare), "main", cwd=scratch)

        # Simulate the agent workspace: cloned from the parent's shared repo,
        # remote renamed to host-cave, checked out on an agent dev branch.
        git("clone", str(external_bare), str(self.user_repo))
        git("config", "user.email", "test@test.com", cwd=self.user_repo)
        git("config", "user.name", "Test", cwd=self.user_repo)
        git("remote", "rename", "origin", "host-cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-7/main", cwd=self.user_repo)

        # Now stage the bind-mount appearance: host-cave-shared.git inside
        # .coding-cave. The remote URL on host-cave still points at the
        # external bare, which is fine for the test.
        self.host_shared = self.user_repo / ".coding-cave" / HOST_SHARED_REPO_DIR
        self.host_shared.parent.mkdir(parents=True)
        shutil.copytree(external_bare, self.host_shared)

    def tearDown(self):
        if self._saved_cave_version is None:
            del os.environ["CODING_CAVE_VERSION"]
        else:
            os.environ["CODING_CAVE_VERSION"] = self._saved_cave_version
        shutil.rmtree(self.tmpdir)

    def test_init_inside_cave_creates_cave_shared(self):
        """cav init inside a cave creates cave-shared.git as a real bare repo
        and configures a 'cave' remote for down-link commands."""
        from cav.init import init
        init(str(self.user_repo))

        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        self.assertTrue((shared / "HEAD").exists())
        self.assertTrue((shared / "config").exists())
        self.assertTrue((shared / "hooks" / "pre-receive").exists())

        remotes = subprocess.run(
            ["git", "remote"], cwd=self.user_repo,
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertIn("cave", remotes)

    def test_init_inside_cave_publishes_host_cave_bases(self):
        """Base branches from host-cave land on cave-shared.git for subagents."""
        from cav.init import init
        init(str(self.user_repo))

        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            cwd=shared, capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertIn("main", refs)

    def test_init_inside_cave_rebuilds_pre_split_stub(self):
        """A pre-split cave-shared.git stub (no HEAD/config) is rebuilt as a
        real bare repo rather than preserved."""
        stub = self.user_repo / ".coding-cave" / "cave-shared.git"
        stub.mkdir()
        (stub / "objects").symlink_to("/does/not/exist")

        from cav.init import init
        init(str(self.user_repo))
        self.assertTrue((stub / "HEAD").exists())
        self.assertTrue((stub / "config").exists())

    def test_init_inside_cave_idempotent_on_real_repo(self):
        """Re-running init on a real cave-shared.git must preserve its refs."""
        from cav.init import init
        init(str(self.user_repo))

        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        main_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=shared,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        init(str(self.user_repo))
        main_sha_after = subprocess.run(
            ["git", "rev-parse", "main"], cwd=shared,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(main_sha, main_sha_after)

    def test_init_inside_cave_requires_host_shared(self):
        """cav init inside a cave errors if host-cave-shared.git is missing."""
        shutil.rmtree(self.host_shared)

        from cav.init import init
        with self.assertRaises(SystemExit) as cm:
            init(str(self.user_repo))
        self.assertIn("host-cave-shared.git", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
