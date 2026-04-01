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


class TestRequireInitInsideCave(unittest.TestCase):
    """Commands require cav init inside a cave.

    Inside a cave, host-cave points to the parent's bare repo but cave
    (CAVE_REMOTE) does not exist until cav init is run.  require_init
    must reject commands that need the local bare repo.
    """

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
        # Add host-cave remote like the sandbox does (points to parent).
        bare = Path(self.tmpdir) / "host-bare.git"
        bare.mkdir()
        git("init", "--bare", cwd=bare)
        git("remote", "add", "host-cave", str(bare), cwd=self.repo)
        # Simulate being inside a cave.
        os.environ["CODING_CAVE_DEPTH"] = "1"

    def tearDown(self):
        os.environ.pop("CODING_CAVE_DEPTH", None)
        shutil.rmtree(self.tmpdir)

    def test_agent_blocked_inside_cave(self):
        """cav agent inside a cave without init is rejected."""
        result = cav_cli(cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_status_blocked_inside_cave(self):
        result = cav_cli("status", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_approve_blocked_inside_cave(self):
        result = cav_cli("approve", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_push_blocked_inside_cave(self):
        result = cav_cli("push", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_pull_blocked_inside_cave(self):
        result = cav_cli("pull", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_checkout_blocked_inside_cave(self):
        result = cav_cli("checkout", "main", cwd=self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)

    def test_version_still_works_inside_cave(self):
        """version does not need init and must always work."""
        result = cav_cli("version", cwd=self.repo, check=False)
        self.assertEqual(result.returncode, 0)


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
             f"from cav import registry; print(registry.project_path({slug!r}) or '')"],
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


class TestInitNewProject(unittest.TestCase):
    """cav init handles empty directories and empty git repos."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["CAV_DATA_DIR"] = str(Path(self.tmpdir) / "data")

    def tearDown(self):
        os.environ.pop("CAV_DATA_DIR", None)
        shutil.rmtree(self.tmpdir)

    def test_init_creates_git_repo(self):
        """cav init on an empty plain directory creates git but errors on no commits."""
        project = Path(self.tmpdir) / "newproject"
        project.mkdir()
        from cav.init import init
        with self.assertRaises(SystemExit) as ctx:
            init(str(project))
        self.assertTrue((project / ".git").exists())
        self.assertIn("No commits yet", str(ctx.exception))

    def test_init_empty_git_repo(self):
        """cav init on a git repo with no commits errors."""
        project = Path(self.tmpdir) / "emptyrepo"
        project.mkdir()
        git("init", "-b", "main", cwd=project)
        from cav.init import init
        with self.assertRaises(SystemExit) as ctx:
            init(str(project))
        self.assertIn("No commits yet", str(ctx.exception))

    def test_init_repo_with_commits(self):
        """cav init on a repo with commits pushes branches to cave."""
        project = Path(self.tmpdir) / "withcommits"
        project.mkdir()
        git("init", "-b", "main", cwd=project)
        git("config", "user.email", "test@test.com", cwd=project)
        git("config", "user.name", "Test", cwd=project)
        (project / "f.txt").write_text("hi\n")
        git("add", "f.txt", cwd=project)
        git("commit", "-m", "init", cwd=project)
        from cav.init import init
        init(str(project))
        shared = project / ".coding-cave" / "cave-shared.git"
        # Verify the branch was pushed
        refs = subprocess.run(
            ["git", "branch"], cwd=shared,
            capture_output=True, text=True).stdout
        self.assertIn("main", refs)

    def test_init_no_commits_errors(self):
        """cav init on a repo with no commits gives a helpful error."""
        project = Path(self.tmpdir) / "nocommits"
        project.mkdir()
        (project / "app.py").write_text("print('hello')\n")
        from cav.init import init
        with self.assertRaises(SystemExit) as ctx:
            init(str(project))
        self.assertIn("No commits yet", str(ctx.exception))
        self.assertIn("git add", str(ctx.exception))

    def test_init_uncommitted_warns(self):
        """cav init on a repo with uncommitted changes warns."""
        import io
        project = Path(self.tmpdir) / "dirty"
        project.mkdir()
        git("init", "-b", "main", cwd=project)
        git("config", "user.email", "test@test.com", cwd=project)
        git("config", "user.name", "Test", cwd=project)
        (project / "a.txt").write_text("committed\n")
        git("add", "a.txt", cwd=project)
        git("commit", "-m", "init", cwd=project)
        # Leave an uncommitted file
        (project / "b.txt").write_text("uncommitted\n")
        from cav.init import init
        from cav import config
        config.DATA_DIR = Path(self.tmpdir) / "data"
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            init(str(project))
        finally:
            sys.stderr = old_stderr
        self.assertIn("uncommitted changes", captured.getvalue())

    def test_approve_first_agent_commit(self):
        """cav approve works when agent adds work on top of first commit."""
        project = Path(self.tmpdir) / "firstcommit"
        project.mkdir()
        git("init", "-b", "main", cwd=project)
        git("config", "user.email", "test@test.com", cwd=project)
        git("config", "user.name", "Test", cwd=project)
        (project / "seed.txt").write_text("seed\n")
        git("add", "seed.txt", cwd=project)
        git("commit", "-m", "seed commit", cwd=project)
        from cav.init import init
        from cav import config, workspace
        from cav.approve import approve
        config.DATA_DIR = Path(self.tmpdir) / "data"
        init(str(project))
        ws = workspace.create(project, "1")
        # Agent adds a file
        git("config", "user.email", "test@test.com", cwd=ws)
        git("config", "user.name", "Test", cwd=ws)
        (ws / "README.md").write_text("# Hello\n")
        git("add", "README.md", cwd=ws)
        git("commit", "-m", "add readme", cwd=ws)
        # Push agent branch to cave (simulating post-commit hook)
        shared = config.shared_repo_path(project)
        git("push", str(shared), "agent-1/main", cwd=ws)
        # Fetch so user repo sees the agent branch
        git("fetch", "cave", cwd=project)
        # Approve
        approve(project, agent_id="1", yes=True)
        # Verify main has the agent's commit
        log = subprocess.run(
            ["git", "log", "--oneline", "main"], cwd=project,
            capture_output=True, text=True).stdout
        self.assertIn("add readme", log)


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
        """cav -r @3 is a valid argument."""
        result = cav_cli("-r", "@3", cwd="/tmp", check=False)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_resume_long_flag_recognized(self):
        """cav --resume coding-cave@3 is a valid argument."""
        result = cav_cli("--resume", "coding-cave@3", cwd="/tmp", check=False)
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
        from tests import find_agent_by_session
        self.assertIsNone(find_agent_by_session("deadbeef-dead-dead-dead-deadbeefcafe"))

    def test_resume_accepts_session_uuid(self):
        """cav --resume with a non-numeric ID is treated as a session UUID."""
        from cav.cli import cli
        from click.testing import CliRunner
        from unittest.mock import patch
        with patch("cav.cli.find_repo_root"), \
             patch("cav.cli.require_init"), \
             patch("cav.cli.git.current_branch", return_value="main"), \
             patch("cav.cli.registry.find_agent_by_session",
                   return_value=None), \
             patch("cav.cli.registry.acquire_agent_id",
                   return_value="1"), \
             patch("cav.cli.registry.write_session_id"), \
             patch("cav.cli.registry.read_session_id",
                   return_value="some-session-uuid"), \
             patch("cav.cli.registry.read_agent_launch_opts",
                   return_value=([], False)), \
             patch("cav.cli._sweep_stale_agents"), \
             patch("cav.sandbox.launch", return_value=0) as mock_launch, \
             patch("cav.cli.registry.mark_agent_exited"), \
             patch("cav.cli._auto_cleanup_if_clean"):
            result = CliRunner().invoke(
                cli, ["agent", "--resume", "some-session-uuid"])
        # Should have attempted to launch (exit via sys.exit)
        self.assertNotIn("Invalid agent", result.output)

    def test_write_then_find_session(self):
        """write_session_id + find_agent_by_session round-trip."""
        from cav import config, registry
        from tests import find_agent_by_session
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        db = registry._open()
        db.execute("INSERT INTO agents (agent_id, project, session_id) VALUES ('5', 'proj', NULL)")
        db.commit()
        db.close()
        registry.write_session_id("5", "test-session-uuid")
        self.assertEqual(find_agent_by_session("test-session-uuid"), "5")


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
        from cav import registry
        aid = registry.acquire_agent_id(self.base)
        sid = registry.read_session_id(aid)
        self.assertIsNotNone(sid)
        # Must look like a UUID (contains dashes, 36 chars)
        self.assertEqual(len(sid), 36)
        self.assertIn("-", sid)

    def test_continue_sees_pre_generated_session(self):
        """continue_agent_id returns an agent whose pre-generated session is intact."""
        from cav import registry
        aid = registry.acquire_agent_id(self.base)
        sid = registry.read_session_id(aid)
        registry.mark_agent_exited(aid)

        continued = registry.continue_agent_id(self.base)
        self.assertEqual(continued, aid)
        self.assertEqual(registry.read_session_id(continued), sid)

    def test_continue_returns_none_when_nothing_to_continue(self):
        """When no exited agent exists, continue_agent_id returns None."""
        from cav import registry
        self.assertIsNone(registry.continue_agent_id(self.base))

    def test_clear_session_id(self):
        """clear_session_id sets session to NULL."""
        from cav import registry
        aid = registry.acquire_agent_id(self.base)
        self.assertIsNotNone(registry.read_session_id(aid))
        registry.clear_session_id(aid)
        self.assertIsNone(registry.read_session_id(aid))

    def test_sweep_then_continue_does_not_resume(self):
        """Exact repro: sweep removes agent, cav -c must not --resume the fresh UUID.

        Sequence that triggered the original bug:
        1. Agent 1 runs, exits. Registry has agent 1 + session UUID.
        2. User approves work. Agent branches deleted from remote.
        3. _sweep_stale_agents removes agent 1 from registry.
        4. cav -c calls continue_agent_id, finds nothing, old code fell
           through to acquire_agent_id which created a NEW agent 1 with
           a new pre-generated UUID.
        5. resuming = bool(session_id) was True because the new UUID is
           non-None. Claude got --resume with a UUID it never saw.
        """
        from cav import registry
        from cav.registry import resolve_continue

        # Step 1: agent runs and exits.
        aid = registry.acquire_agent_id(self.base)
        old_sid = registry.read_session_id(aid)
        registry.mark_agent_exited(aid)

        # Step 2-3: simulate sweep removing the agent entirely.
        registry.remove_agent(aid)

        # Step 4-5: cav -c resolve logic.
        new_aid, session_id, resuming = resolve_continue(self.base)

        self.assertFalse(resuming,
                         "must not --resume after sweep created a fresh agent")
        self.assertNotEqual(session_id, old_sid,
                            "session_id should be a fresh UUID, not the swept one")

    def test_continue_with_valid_session_resumes(self):
        """cav -c resumes when the session file exists on disk."""
        from cav import config, registry
        from cav.registry import resolve_continue

        aid = registry.acquire_agent_id(self.base)
        sid = registry.read_session_id(aid)
        registry.mark_agent_exited(aid)

        # Simulate Claude having written the session file.
        self._make_session_file(config.project_slug(self.base), sid)

        _, session_id, resuming = resolve_continue(self.base)

        self.assertTrue(resuming)
        self.assertEqual(session_id, sid)

    def test_continue_with_missing_session_file_does_not_resume(self):
        """cav -c with agent in registry but session file gone starts fresh."""
        from cav import config, registry
        from cav.registry import resolve_continue

        aid = registry.acquire_agent_id(self.base)
        old_sid = registry.read_session_id(aid)
        registry.mark_agent_exited(aid)

        # Agent still in registry, but no session file on disk.
        slug = config.project_slug(self.base)
        self.assertFalse(config.session_file_exists(slug, old_sid))

        returned_aid, session_id, resuming = resolve_continue(self.base)

        self.assertFalse(resuming,
                         "must not --resume when session file is missing")
        self.assertEqual(returned_aid, aid,
                         "should reuse the same agent slot")
        self.assertNotEqual(session_id, old_sid,
                            "should generate a fresh session UUID")

    def test_find_agent_by_session_after_capture(self):
        """find_agent_by_session works after post-exit capture overwrites the UUID."""
        from cav import registry
        from tests import find_agent_by_session
        aid = registry.acquire_agent_id(self.base)
        pre_generated = registry.read_session_id(aid)

        # Overwrite with real UUID (simulates post-exit capture)
        real_uuid = "after-capture-real-uuid-sessionfile"
        registry.write_session_id(aid, real_uuid)

        # Old UUID no longer resolves
        self.assertIsNone(find_agent_by_session(pre_generated))
        # New UUID resolves to the agent
        self.assertEqual(find_agent_by_session(real_uuid), aid)


class TestFormatAll(unittest.TestCase):
    """Unit tests for multi-project status formatting."""

    def setUp(self):
        from cav.status import format_all
        self.format_all = format_all

    def test_no_agents_anywhere(self):
        entries = [
            (0.0, "alpha", ""),
            (0.0, "beta", ""),
        ]
        self.assertEqual(self.format_all(entries),
                         "No running agents in any project.")

    def test_active_shown_inactive_summarized(self):
        entries = [
            (1000.0, "onecli", "onecli@3  (running)  1h ago\n"),
            (0.0, "coding-cave", ""),
            (0.0, "dotfiles", ""),
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
            (1000.0, "zoo", "zoo@1  (running)\n"),
            (0.0, "delta", ""),
            (0.0, "alpha", ""),
            (0.0, "charlie", ""),
        ]
        out = self.format_all(entries)
        self.assertIn("No running agents in: alpha, charlie, delta", out)

    def test_all_active(self):
        entries = [
            (2000.0, "proj-a", "a@1  (running)\n"),
            (1000.0, "proj-b", "b@2  (exited)\n"),
        ]
        out = self.format_all(entries)
        self.assertIn("=== proj-a ===", out)
        self.assertIn("=== proj-b ===", out)
        self.assertNotIn("No running agents", out)

    def test_single_inactive(self):
        entries = [
            (1000.0, "active", "active@1  (running)\n"),
            (0.0, "lonely", ""),
        ]
        out = self.format_all(entries)
        self.assertIn("No running agents in: lonely", out)

    def test_projects_sorted_by_most_recent_activity(self):
        """Projects with more recent activity appear first."""
        entries = [
            (1000.0, "oldest", "oldest@1  (running)\n"),
            (3000.0, "newest", "newest@2  (running)\n"),
            (2000.0, "middle", "middle@3  (running)\n"),
            (0.0, "inactive", ""),
        ]
        out = self.format_all(entries)
        newest_pos = out.index("=== newest ===")
        middle_pos = out.index("=== middle ===")
        oldest_pos = out.index("=== oldest ===")
        self.assertLess(newest_pos, middle_pos)
        self.assertLess(middle_pos, oldest_pos)


class TestAgentSorting(unittest.TestCase):
    """Unit tests for agent sorting and formatting (no git needed)."""

    def setUp(self):
        from cav.status import sort_agents, format_agents
        self.sort_agents = sort_agents
        self.format_agents = format_agents

    def _agent(self, ts, aid, name, running, state="running", unapproved=None):
        return (ts, aid, name, running, state, unapproved or [])

    def test_sort_most_recent_first(self):
        agents = [
            self._agent(1000.0, "1", "proj@1", True),
            self._agent(3000.0, "3", "proj@3", True),
            self._agent(2000.0, "2", "proj@2", True),
        ]
        result = self.sort_agents(agents)
        ids = [a[1] for a in result]
        self.assertEqual(ids, ["3", "2", "1"])

    def test_sort_orphaned_last(self):
        agents = [
            self._agent(2000.0, "1", "proj@1", True),
            self._agent(3000.0, "2", "proj@2", False, "orphaned"),
            self._agent(1000.0, "3", "proj@3", False, "exited",
                        [("agent-3/main", 1, ["abc feat"])]),
        ]
        result = self.sort_agents(agents)
        ids = [a[1] for a in result]
        self.assertEqual(ids, ["1", "3", "2"])

    def test_format_agents_order_preserved(self):
        agents = [
            self._agent(3000.0, "2", "proj@2", True),
            self._agent(2000.0, "3", "proj@3", True),
            self._agent(1000.0, "1", "proj@1", True),
        ]
        out = self.format_agents(agents)
        pos2 = out.index("proj@2")
        pos3 = out.index("proj@3")
        pos1 = out.index("proj@1")
        self.assertLess(pos2, pos3)
        self.assertLess(pos3, pos1)

    def test_format_agents_unapproved_commits(self):
        agents = [
            self._agent(1000.0, "1", "proj@1", False, "exited",
                        [("agent-1/main", 2, ["abc feat: add X", "def fix: Y"])]),
        ]
        out = self.format_agents(agents)
        self.assertIn("2 commits on agent-1/main", out)
        self.assertIn("abc feat: add X", out)
        self.assertIn("def fix: Y", out)

    def test_format_agents_truncates_long_commit_lists(self):
        lines = [f"{chr(97+i)*7} commit {i}" for i in range(5)]
        agents = [
            self._agent(1000.0, "1", "proj@1", False, "exited",
                        [("agent-1/main", 5, lines)]),
        ]
        out = self.format_agents(agents, max_commits=3)
        self.assertIn("... and 2 more", out)
        self.assertNotIn("commit 3", out)
        self.assertNotIn("commit 4", out)


class TestLaunchOpts(unittest.TestCase):
    """Launch options (extra projects, recursive) persist for cav -c."""

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

    def test_save_and_read_round_trip(self):
        """save_agent_launch_opts round-trips through read."""
        from cav import registry
        aid = registry.acquire_agent_id(self.base)
        registry.save_agent_launch_opts(aid, ["coding-cave", "other"], True)
        slugs, recursive = registry.read_agent_launch_opts(aid)
        self.assertEqual(slugs, ["coding-cave", "other"])
        self.assertTrue(recursive)

    def test_read_defaults_without_save(self):
        """read_agent_launch_opts returns defaults when nothing saved."""
        from cav import registry
        aid = registry.acquire_agent_id(self.base)
        slugs, recursive = registry.read_agent_launch_opts(aid)
        self.assertEqual(slugs, [])
        self.assertFalse(recursive)

    def test_opts_survive_exit_and_continue(self):
        """Launch opts persist through exit and are available on continue."""
        from cav import registry
        aid = registry.acquire_agent_id(self.base)
        registry.save_agent_launch_opts(aid, ["extra-proj"], True)
        registry.mark_agent_exited(aid)

        continued = registry.continue_agent_id(self.base)
        self.assertEqual(continued, aid)
        slugs, recursive = registry.read_agent_launch_opts(continued)
        self.assertEqual(slugs, ["extra-proj"])
        self.assertTrue(recursive)

    def test_nonexistent_agent_returns_defaults(self):
        """read_agent_launch_opts returns defaults for unknown agent."""
        from cav import registry
        slugs, recursive = registry.read_agent_launch_opts("999")
        self.assertEqual(slugs, [])
        self.assertFalse(recursive)


if __name__ == "__main__":
    unittest.main()
