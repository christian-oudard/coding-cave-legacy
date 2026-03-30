"""Tests for CLI usability: friendly errors, defaults."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def git(*args, cwd=None):
    return subprocess.run(
        ["git"] + list(args), cwd=cwd,
        capture_output=True, text=True, check=True,
    )


def cav_cli(*args, cwd=None, check=True):
    return subprocess.run(
        ["python3", "-m", "cav.cli"] + list(args),
        cwd=cwd, check=check,
        capture_output=True, text=True,
    )


class TestRequireInit(unittest.TestCase):
    """cav commands give a friendly error when not initialized."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "project"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@test.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.repo)
        git("commit", "-m", "init", cwd=self.repo)
        os.symlink(PROJECT_ROOT / "cav", self.repo / "cav")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_agent_without_init(self):
        """Running cav (agent) without init gives a friendly message."""
        result = cav_cli(cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)
        self.assertIn("cav init", result.stderr)
        # Should NOT be a Python traceback
        self.assertNotIn("Traceback", result.stderr)

    def test_status_without_init(self):
        result = cav_cli("status", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_pull_without_init(self):
        result = cav_cli("pull", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_push_without_init(self):
        result = cav_cli("push", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)


class TestRequireInitWithRegistry(unittest.TestCase):
    """require_init checks the projects table, not just .coding-cave."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "project"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@test.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.repo)
        git("commit", "-m", "init", cwd=self.repo)
        os.symlink(PROJECT_ROOT / "cav", self.repo / "cav")
        self.data_dir = Path(self.tmpdir) / "data"
        os.environ["CAV_DATA_DIR"] = str(self.data_dir)

    def tearDown(self):
        os.environ.pop("CAV_DATA_DIR", None)
        shutil.rmtree(self.tmpdir)

    def test_agent_without_registry_entry(self):
        """.coding-cave exists but project not in DB gives friendly error."""
        cav_cli("init", cwd=self.repo)
        # Wipe the database
        (self.data_dir / "coding-cave.db").unlink()
        result = cav_cli(cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not registered", result.stderr)
        self.assertIn("cav init", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_resolve_project_by_slug(self):
        """-p accepts a registered project slug instead of a path."""
        # Set up a second project
        other = Path(self.tmpdir) / "other"
        other.mkdir()
        git("init", "-b", "main", cwd=other)
        git("config", "user.email", "test@test.com", cwd=other)
        git("config", "user.name", "Test", cwd=other)
        (other / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=other)
        git("commit", "-m", "init", cwd=other)
        os.symlink(PROJECT_ROOT / "cav", other / "cav")
        cav_cli("init", cwd=self.repo)
        cav_cli("init", cwd=other)

        slug = other.name  # project_slug uses the directory name
        result = subprocess.run(
            ["python3", "-c",
             f"from cav import config; print(config.project_path({slug!r}) or '')"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(Path(result.stdout.strip()).resolve(), other.resolve())


class TestInitDefaults(unittest.TestCase):
    """cav init defaults to current directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "project"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@test.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.repo)
        git("commit", "-m", "init", cwd=self.repo)
        os.symlink(PROJECT_ROOT / "cav", self.repo / "cav")
        os.environ["CAV_DATA_DIR"] = str(Path(self.tmpdir) / "data")

    def tearDown(self):
        os.environ.pop("CAV_DATA_DIR", None)
        shutil.rmtree(self.tmpdir)

    def test_init_no_args(self):
        """cav init with no path argument initializes the current directory."""
        result = cav_cli("init", cwd=self.repo)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Initialized", result.stdout)
        self.assertTrue((self.repo / ".coding-cave" / "cave-shared.git").exists())


class TestTopLevelHelp(unittest.TestCase):
    """cav --help shows top-level usage, not agent subcommand."""

    def test_help_flag_shows_all_subcommands(self):
        result = cav_cli("--help", cwd=PROJECT_ROOT, check=False)
        self.assertIn("init", result.stdout)
        self.assertIn("agent", result.stdout)
        self.assertIn("pull", result.stdout)
        self.assertIn("push", result.stdout)
        self.assertIn("status", result.stdout)

    def test_short_help_flag(self):
        result = cav_cli("-h", cwd=PROJECT_ROOT, check=False)
        self.assertIn("init", result.stdout)
        self.assertIn("agent", result.stdout)


class TestPullForceGuard(unittest.TestCase):
    """cav pull --force is only allowed on agent branches."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "project"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@test.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.repo)
        git("commit", "-m", "init", cwd=self.repo)
        os.symlink(PROJECT_ROOT / "cav", self.repo / "cav")
        os.environ["CAV_DATA_DIR"] = str(Path(self.tmpdir) / "data")
        cav_cli("init", cwd=self.repo)

    def tearDown(self):
        os.environ.pop("CAV_DATA_DIR", None)
        shutil.rmtree(self.tmpdir)

    def test_pull_force_blocked_on_main(self):
        """cav pull --force refuses on shared branches like main."""
        result = cav_cli("pull", "--force", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent branch", result.stderr.lower())

    def test_pull_force_blocked_on_non_agent_branch(self):
        """cav pull --force refuses on any non-agent branch."""
        git("checkout", "-b", "develop", cwd=self.repo)
        result = cav_cli("pull", "--force", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent branch", result.stderr.lower())

    def test_pull_force_allowed_on_agent_branch(self):
        """cav pull --force is allowed on agent branches."""
        git("checkout", "-b", "agent-1/main", cwd=self.repo)
        # Will fail (no remote tracking branch), but should NOT fail on the guard
        result = cav_cli("pull", "--force", cwd=self.repo, check=False)
        self.assertNotIn("agent branch", result.stderr.lower())

    def test_pull_force_with_branch_arg_blocked_on_shared(self):
        """cav pull --force main refuses even when given as argument."""
        git("checkout", "-b", "agent-1/main", cwd=self.repo)
        result = cav_cli("pull", "--force", "main", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent branch", result.stderr.lower())

    def test_pull_branch_arg_accepted(self):
        """cav pull <branch> is a valid argument."""
        result = cav_cli("pull", "main", cwd=self.repo, check=False)
        self.assertNotIn("unrecognized arguments", result.stderr)


class TestPushForceAllowed(unittest.TestCase):
    """cav push --force is allowed on any branch (user is source of truth)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "project"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@test.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.repo)
        git("commit", "-m", "init", cwd=self.repo)
        os.symlink(PROJECT_ROOT / "cav", self.repo / "cav")
        os.environ["CAV_DATA_DIR"] = str(Path(self.tmpdir) / "data")
        cav_cli("init", cwd=self.repo)

    def tearDown(self):
        os.environ.pop("CAV_DATA_DIR", None)
        shutil.rmtree(self.tmpdir)

    def test_push_force_not_blocked_on_main(self):
        """cav push --force on main is allowed (user corrects the cave)."""
        result = cav_cli("push", "--force", cwd=self.repo, check=False)
        # May fail for other reasons (no remote tracking), but NOT the guard
        self.assertNotIn("agent branch", result.stderr.lower())


class TestUnknownCommand(unittest.TestCase):
    """Misspelled subcommands produce a clear error, not agent launch."""

    def test_typo_gives_error(self):
        """cav pul (typo) should error, not try to launch an agent."""
        result = cav_cli("pul", cwd=PROJECT_ROOT, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown command", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_nonsense_gives_error(self):
        result = cav_cli("foobar", cwd=PROJECT_ROOT, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown command", result.stderr)

    def test_numeric_id_rejected(self):
        """cav 3 should error, not silently route to agent."""
        result = cav_cli("3", cwd=PROJECT_ROOT, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown command", result.stderr)


class TestContinueFlag(unittest.TestCase):
    """cav -c / cav --continue parses correctly."""

    def test_continue_flag_recognized(self):
        """cav -c is a valid argument (doesn't error on parse)."""
        result = cav_cli("-c", cwd="/tmp", check=False)
        # It will fail (not in a git repo), but should NOT fail on argument parsing
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_continue_long_flag_recognized(self):
        """cav --continue is a valid argument."""
        result = cav_cli("--continue", cwd="/tmp", check=False)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_resume_flag_recognized(self):
        """cav -r <uuid> is a valid argument."""
        result = cav_cli("-r", "some-uuid", cwd="/tmp", check=False)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_resume_long_flag_recognized(self):
        """cav --resume <uuid> is a valid argument."""
        result = cav_cli("--resume", "some-uuid", cwd="/tmp", check=False)
        self.assertNotIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
