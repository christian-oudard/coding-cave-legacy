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

    def test_resolve_project_unknown_slug(self):
        """Unrecognised slug gives 'No project named' error, not git error."""
        from cav.cli import resolve_project
        with self.assertRaises(SystemExit) as cm:
            resolve_project("no-such-project")
        self.assertIn("No project named", str(cm.exception))
        self.assertNotIn("Not a git repository", str(cm.exception))

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


class TestSessionResume(unittest.TestCase):
    """Session ID capture and --resume fallback."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import cav.config as cfg
        self._orig_data_dir = cfg.DATA_DIR
        cfg.DATA_DIR = Path(self.tmpdir) / "data"
        os.environ["CAV_DATA_DIR"] = str(cfg.DATA_DIR)

    def tearDown(self):
        import cav.config as cfg
        cfg.DATA_DIR = self._orig_data_dir
        os.environ.pop("CAV_DATA_DIR", None)
        shutil.rmtree(self.tmpdir)

    def _make_session_file(self, slug: str, session_id: str):
        from cav import config
        session_dir = config._claude_project_dir(slug)
        session_dir.mkdir(parents=True, exist_ok=True)
        f = session_dir / f"{session_id}.jsonl"
        f.write_text("{}\n")
        return f

    def test_session_file_exists(self):
        """session_file_exists returns True only when the .jsonl file is present."""
        from cav import config
        slug = "myproject"
        sid = "aaaaaaaa-0000-0000-0000-000000000001"
        self.assertFalse(config.session_file_exists(slug, sid))
        self._make_session_file(slug, sid)
        self.assertTrue(config.session_file_exists(slug, sid))
        self.assertFalse(config.session_file_exists(slug, "nonexistent"))

    def test_find_agent_by_session_unknown_returns_none(self):
        from cav import config
        self.assertIsNone(config.find_agent_by_session("deadbeef-dead-dead-dead-deadbeefcafe"))

    def test_resume_unknown_uuid_does_not_error_on_no_agent(self):
        """cav --resume <unknown-uuid> no longer errors with 'No agent owns session'."""
        result = cav_cli("--resume", "deadbeef-dead-dead-dead-deadbeefcafe",
                         cwd="/tmp", check=False)
        self.assertNotIn("No agent owns session", result.stderr)

    def test_write_then_find_session(self):
        """write_session_id + find_agent_by_session round-trip."""
        from cav import config
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        db = config._open_registry()
        db.execute("INSERT INTO agents (agent_id, project, session_id) VALUES ('5', 'proj', NULL)")
        db.commit()
        db.close()
        config.write_session_id("5", "test-session-uuid")
        self.assertEqual(config.find_agent_by_session("test-session-uuid"), "5")


class TestSessionLifecycle(unittest.TestCase):
    """Session ID flows through acquire, launch, and continue.

    The pre-generated UUID is passed to Claude via --session-id, so
    the session file name is deterministic.  Post-exit, we just check
    whether the file was written.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import cav.config as cfg
        self._orig_data_dir = cfg.DATA_DIR
        cfg.DATA_DIR = Path(self.tmpdir) / "data"
        os.environ["CAV_DATA_DIR"] = str(cfg.DATA_DIR)
        self.base = Path(self.tmpdir) / "project"
        self.base.mkdir()

    def tearDown(self):
        import cav.config as cfg
        cfg.DATA_DIR = self._orig_data_dir
        os.environ.pop("CAV_DATA_DIR", None)
        shutil.rmtree(self.tmpdir)

    def _make_session_file(self, slug, session_id):
        from cav import config
        d = config._claude_project_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{session_id}.jsonl"
        f.write_text("{}\n")
        return f

    def test_acquire_stores_readable_session_id(self):
        """acquire_agent_id pre-generates a UUID retrievable via read_session_id."""
        from cav import config
        aid = config.acquire_agent_id(self.base)
        sid = config.read_session_id(aid)
        self.assertIsNotNone(sid)
        # Must look like a UUID (contains dashes, 36 chars)
        self.assertEqual(len(sid), 36)
        self.assertIn("-", sid)

    def test_continue_sees_pre_generated_session(self):
        """continue_agent_id returns an agent whose pre-generated session is intact."""
        from cav import config
        aid = config.acquire_agent_id(self.base)
        sid = config.read_session_id(aid)
        config.mark_agent_exited(aid)

        continued = config.continue_agent_id(self.base)
        self.assertEqual(continued, aid)
        self.assertEqual(config.read_session_id(continued), sid)

    def test_continue_fallback_acquires_fresh(self):
        """When no exited agent exists, continue_agent_id acquires a fresh one."""
        from cav import config
        aid = config.continue_agent_id(self.base)
        # Should have a pre-generated session UUID
        sid = config.read_session_id(aid)
        self.assertIsNotNone(sid)
        self.assertEqual(len(sid), 36)

    def test_clear_session_id(self):
        """clear_session_id sets session to NULL."""
        from cav import config
        aid = config.acquire_agent_id(self.base)
        self.assertIsNotNone(config.read_session_id(aid))
        config.clear_session_id(aid)
        self.assertIsNone(config.read_session_id(aid))

    def test_find_agent_by_session_after_capture(self):
        """find_agent_by_session works after post-exit capture overwrites the UUID."""
        from cav import config
        aid = config.acquire_agent_id(self.base)
        pre_generated = config.read_session_id(aid)

        # Overwrite with real UUID (simulates post-exit capture)
        real_uuid = "after-capture-real-uuid-sessionfile"
        config.write_session_id(aid, real_uuid)

        # Old UUID no longer resolves
        self.assertIsNone(config.find_agent_by_session(pre_generated))
        # New UUID resolves to the agent
        self.assertEqual(config.find_agent_by_session(real_uuid), aid)


class TestFormatAll(unittest.TestCase):
    """Unit tests for multi-project status formatting."""

    def setUp(self):
        from cav.status import format_all
        self.format_all = format_all

    def test_no_agents_anywhere(self):
        entries = [
            (False, "alpha", ""),
            (False, "beta", ""),
        ]
        self.assertEqual(self.format_all(entries),
                         "No running agents in any project.")

    def test_active_shown_inactive_summarized(self):
        entries = [
            (True, "onecli", "onecli@3  (running)  1h ago\n"),
            (False, "coding-cave", ""),
            (False, "dotfiles", ""),
        ]
        out = self.format_all(entries)
        self.assertIn("=== onecli ===", out)
        self.assertIn("onecli@3", out)
        self.assertIn("No running agents in: coding-cave, dotfiles", out)
        # Inactive projects should not get their own headers
        self.assertNotIn("=== coding-cave ===", out)
        self.assertNotIn("=== dotfiles ===", out)

    def test_inactive_sorted_alphabetically(self):
        entries = [
            (True, "zoo", "zoo@1  (running)\n"),
            (False, "delta", ""),
            (False, "alpha", ""),
            (False, "charlie", ""),
        ]
        out = self.format_all(entries)
        self.assertIn("No running agents in: alpha, charlie, delta", out)

    def test_all_active(self):
        entries = [
            (True, "proj-a", "a@1  (running)\n"),
            (True, "proj-b", "b@2  (paused)\n"),
        ]
        out = self.format_all(entries)
        self.assertIn("=== proj-a ===", out)
        self.assertIn("=== proj-b ===", out)
        self.assertNotIn("No running agents", out)

    def test_single_inactive(self):
        entries = [
            (True, "active", "active@1  (running)\n"),
            (False, "lonely", ""),
        ]
        out = self.format_all(entries)
        self.assertIn("No running agents in: lonely", out)


if __name__ == "__main__":
    unittest.main()
