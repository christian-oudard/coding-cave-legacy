"""Integration tests for Coding Cave.

Tests the git protocol: hooks, branch protection, pull, push, status.
Workspace creation uses the Python API directly. CLI commands (init, pull,
push, status) are tested via subprocess.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

# Add project root to path for direct imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cav import config, worktree


def run(cmd, cwd=None, check=True, env=None):
    """Run a command, return CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd, cwd=cwd, check=check,
        capture_output=True, text=True,
        env=merged_env,
    )


def git(*args, cwd=None, check=True, env=None):
    return run(["git"] + list(args), cwd=cwd, check=check, env=env)


def agent_env(agent_id):
    """Env dict that identifies pushes as coming from an agent."""
    return {"CAV_AGENT_ID": agent_id}


def git_out(*args, cwd=None):
    return git(*args, cwd=cwd).stdout.strip()


def cav_cli(*args, cwd=None, check=True):
    """Run cav CLI via python -m."""
    return run(
        ["python3", "-m", "cav.cli"] + list(args),
        cwd=cwd, check=check,
    )


class TestCAV(unittest.TestCase):

    # Create the blank repo once, copy it for each test.
    _template_dir = None

    @classmethod
    def setUpClass(cls):
        cls._template_dir = tempfile.mkdtemp()
        repo = Path(cls._template_dir) / "project"
        repo.mkdir()
        git("init", "-b", "main", cwd=repo)
        git("config", "user.email", "test@test.com", cwd=repo)
        git("config", "user.name", "Test User", cwd=repo)
        (repo / "README.md").write_text("# Project\n")
        git("add", "README.md", cwd=repo)
        git("commit", "-m", "Initial commit", cwd=repo)
        os.symlink(PROJECT_ROOT / "cav", repo / "cav")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._template_dir)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_repo = Path(self.tmpdir) / "project"
        self.data_dir = Path(self.tmpdir) / "data"

        # Copy the template repo
        shutil.copytree(
            Path(self._template_dir) / "project", self.user_repo,
            symlinks=True,
        )

        os.environ["CAV_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _init(self):
        """Initialize Coding Cave in the user repo."""
        cav_cli("init", str(self.user_repo), cwd=self.user_repo)

    def _create_agent(self, name):
        """Create agent workspace, configure git identity. Returns workspace path."""
        ws = worktree.create(self.user_repo, name)
        git("config", "user.email", f"{name}@test.com", cwd=ws)
        git("config", "user.name", name, cwd=ws)
        return ws

    def _init_and_agents(self, *names):
        """Init + create agents. Returns dict of name -> workspace path."""
        self._init()
        return {name: self._create_agent(name) for name in names}

    # -- init --

    def test_init(self):
        """cav init creates shared repo with hook."""
        result = cav_cli("init", str(self.user_repo), cwd=self.user_repo)
        self.assertIn("Initialized Coding Cave", result.stdout)

        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        self.assertTrue(shared.exists())
        self.assertTrue((shared / "hooks" / "pre-receive").exists())

        remotes = git_out("remote", cwd=self.user_repo)
        self.assertIn("cave", remotes)

    # -- workspace creation --

    def test_workspace_creation(self):
        """Workspace has correct branch and remote."""
        agents = self._init_and_agents("1", "2")

        for name, ws in agents.items():
            self.assertTrue(ws.exists())

            remotes = git_out("remote", cwd=ws)
            self.assertIn("cave", remotes)

            branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=ws)
            self.assertEqual(branch, f"agent-{name}/main")

    def test_invalid_agent_id(self):
        """Non-numeric agent IDs are rejected."""
        self._init()
        with self.assertRaises(SystemExit):
            worktree.create(self.user_repo, "bad_name")

    def test_auto_agent_id(self):
        """acquire_agent_id auto-assigns 1, 2, 3 when all are locked."""
        self._init()

        id1, fd1 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        self._create_agent(id1)

        id2, fd2 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id2, "2")
        self._create_agent(id2)

        id3, fd3 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id3, "3")

        fd1.close()
        fd2.close()
        fd3.close()

    def test_auto_agent_id_reuse(self):
        """acquire_agent_id reuses IDs of exited agents."""
        self._init()

        id1, fd1 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        self._create_agent(id1)

        id2, fd2 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id2, "2")
        self._create_agent(id2)

        # Agent 1 exits (release lock)
        fd1.close()

        # Next agent should reuse ID 1
        id3, fd3 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id3, "1")

        fd2.close()
        fd3.close()

    def test_auto_agent_id_gaps(self):
        """acquire_agent_id fills gaps in the ID sequence."""
        self._init()
        # Simulate agents 1 and 3 existing (gap at 2), neither locked
        slug = config.project_slug(self.user_repo)
        for aid in ("1", "3"):
            (self.data_dir / "worktrees" / slug / aid).mkdir(parents=True)

        next_id, fd = config.acquire_agent_id(self.user_repo)
        self.assertEqual(next_id, "1")
        fd.close()

    def test_paused_session_protected(self):
        """Agent with undelivered work keeps its ID reserved."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent 1 does work and pushes to its agent branch (not main)
        (ws / "wip.py").write_text("work in progress\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP", cwd=ws)
        git("push", "cave", "HEAD", cwd=ws, env=agent_env("1"))

        # Agent 1 exits with a recent timestamp
        import time
        id1, fd1 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        fd1.seek(0)
        fd1.truncate()
        fd1.write(str(int(time.time())))
        fd1.flush()
        fd1.close()

        # Next agent should skip 1 (paused, undelivered work) and get 2
        def available(aid):
            shared = config.shared_repo_path(self.user_repo)
            try:
                from cav.git import git_output
                refs = git_output(
                    "for-each-ref", "--format=%(refname:short)",
                    f"refs/heads/agent-{aid}/*", cwd=shared,
                )
            except Exception:
                return True
            for branch in refs.splitlines():
                parsed = config.parse_agent_branch(branch)
                if not parsed:
                    continue
                target, _ = parsed
                try:
                    ahead = int(git_output(
                        "rev-list", "--count", f"{target}..{branch}",
                        cwd=shared,
                    ))
                    if ahead > 0 and not config.is_session_expired(self.user_repo, aid):
                        return False
                except Exception:
                    pass
            return True

        id2, fd2 = config.acquire_agent_id(self.user_repo, available=available)
        self.assertEqual(id2, "2")
        fd2.close()

    # -- agent work + pull --

    def test_agent_work_and_pull(self):
        """Agent delivers work to main, user can pull and see it."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)

        (ws / "feature.py").write_text("print('hello world')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Update feature", cwd=ws)

        # Agent delivers to main (ff push)
        git("push", "cave", "agent-1/main:main", cwd=ws, env=agent_env("1"))

        cav_cli("pull", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Add feature", log)
        self.assertIn("Update feature", log)

    # -- merge + push --

    def test_user_pulls_and_merges(self):
        """Agent delivers to main, user pulls and merges."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)
        git("push", "cave", "agent-1/main:main", cwd=ws, env=agent_env("1"))

        cav_cli("pull", cwd=self.user_repo)
        git("merge", "cave/main", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Add feature", log)

    # -- hook enforcement --

    def test_agent_ff_push_succeeds(self):
        """Agent fast-forward push to shared branch succeeds."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        git("checkout", "main", cwd=ws)
        (ws / "fix.py").write_text("# fix\n")
        git("add", "fix.py", cwd=ws)
        git("commit", "-m", "Quick fix", cwd=ws)

        result = git("push", "cave", "main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

    def test_agent_non_ff_push_rejected(self):
        """Agent non-fast-forward push to shared branch is rejected."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        git("checkout", "main", cwd=ws)
        (ws / "bad.py").write_text("# bad\n")
        git("add", "bad.py", cwd=ws)
        git("commit", "--amend", "-m", "Rewritten commit", cwd=ws)

        result = git("push", "--force", "cave", "main", cwd=ws, check=False, env=agent_env("1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("Non-fast-forward", result.stderr)

    def test_agent_cross_push_rejected(self):
        """Agent pushing to another agent's branch is rejected."""
        agents = self._init_and_agents("1", "2")
        ws1 = agents["1"]

        (ws1 / "file.py").write_text("# file\n")
        git("add", "file.py", cwd=ws1)
        git("commit", "-m", "Agent 1 work", cwd=ws1)

        result = git(
            "push", "cave", "HEAD:refs/heads/agent-2/main",
            cwd=ws1, check=False, env=agent_env("1"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("cannot push", result.stderr)

    def test_agent_cannot_delete_shared_branch(self):
        """Agent push --delete on a shared branch is rejected."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        result = git("push", "cave", "--delete", "main", cwd=ws, check=False, env=agent_env("1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("cannot delete", result.stderr)

    def test_agent_pushes_own_branch(self):
        """Agent can push and force-push its own branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "wip.py").write_text("# wip\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP", cwd=ws)
        result = git("push", "cave", "agent-1/main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

        (ws / "wip.py").write_text("# wip v2\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "--amend", "-m", "WIP v2", cwd=ws)
        result = git("push", "--force", "cave", "agent-1/main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

    # -- multi-agent --

    def test_multi_agent_deliver_and_pull(self):
        """Two agents deliver work to main, pull shows all commits."""
        agents = self._init_and_agents("1", "2")

        # Agent 1 delivers
        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 work", cwd=a1)
        git("push", "cave", "agent-1/main:main", cwd=a1, env=agent_env("1"))

        # Agent 2 rebases and delivers
        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("fetch", "cave", cwd=a2)
        git("rebase", "cave/main", "agent-2/main", cwd=a2)
        git("push", "cave", "agent-2/main:main", cwd=a2, env=agent_env("2"))

        cav_cli("pull", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

    def test_multi_agent_ff_contention(self):
        """Two agents both ff-push to shared. Second rejected, rebases, retries."""
        agents = self._init_and_agents("1", "2")

        for ws in agents.values():
            git("checkout", "main", cwd=ws)

        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 merge", cwd=a1)
        git("push", "cave", "main", cwd=a1, env=agent_env("1"))

        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 merge", cwd=a2)
        result = git("push", "cave", "main", cwd=a2, check=False, env=agent_env("2"))
        self.assertNotEqual(result.returncode, 0)

        git("fetch", "cave", cwd=a2)
        git("rebase", "cave/main", cwd=a2)
        result = git("push", "cave", "main", cwd=a2, env=agent_env("2"))
        self.assertEqual(result.returncode, 0)

        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/main", cwd=self.user_repo)
        self.assertIn("Agent 1 merge", log)
        self.assertIn("Agent 2 merge", log)

    def test_multi_agent_status(self):
        """cav status shows commits ahead after agents deliver to main."""
        agents = self._init_and_agents("1", "2")

        # Agent 1 delivers 2 commits
        a1 = agents["1"]
        for i in range(2):
            (a1 / f"w{i}.py").write_text(f"# {i}\n")
            git("add", f"w{i}.py", cwd=a1)
            git("commit", "-m", f"agent 1 commit {i}", cwd=a1)
        git("push", "cave", "agent-1/main:main", cwd=a1, env=agent_env("1"))

        # Agent 2 rebases and delivers 1 commit
        a2 = agents["2"]
        (a2 / "api.py").write_text("# api\n")
        git("add", "api.py", cwd=a2)
        git("commit", "-m", "agent 2 commit", cwd=a2)
        git("fetch", "cave", cwd=a2)
        git("rebase", "cave/main", "agent-2/main", cwd=a2)
        git("push", "cave", "agent-2/main:main", cwd=a2, env=agent_env("2"))

        # Fetch so status can see it (status doesn't fetch)
        git("fetch", "cave", cwd=self.user_repo)

        result = cav_cli("status", cwd=self.user_repo)
        self.assertIn("3 commits", result.stdout)
        self.assertIn("behind", result.stdout)

    def test_status_dedup_multi_branch_agent(self):
        """cav status shows one line per agent even when agent has multiple branches."""
        agents = self._init_and_agents("1")
        a1 = agents["1"]

        # Agent 1 pushes work on their main branch
        (a1 / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=a1)
        git("commit", "-m", "agent 1 work", cwd=a1)
        git("push", "cave", "HEAD", cwd=a1, env=agent_env("1"))

        # Agent 1 also has a second branch (e.g. from red team or experimental)
        git("checkout", "-b", "agent-1/experiment", cwd=a1)
        git("push", "cave", "HEAD", cwd=a1, env=agent_env("1"))
        git("checkout", "agent-1/main", cwd=a1)

        # Fetch so status can see both branches
        git("fetch", "cave", cwd=self.user_repo)

        result = cav_cli("status", cwd=self.user_repo)
        # Should appear exactly once (slug varies, but @1 suffix is stable)
        count = result.stdout.count("@1")
        self.assertEqual(count, 1, f"Expected @1 once, got {count}:\n{result.stdout}")

    # -- reject workflow --

    def test_continue_reuses_paused_agent(self):
        """continue_agent_id reacquires the most recently exited agent."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent 1 does work and pushes to agent branch
        (ws / "wip.py").write_text("work in progress\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP", cwd=ws)
        git("push", "cave", "HEAD", cwd=ws, env=agent_env("1"))

        # Agent 1 exits: acquire lock, write timestamp, release
        import time
        id1, fd1 = config.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        fd1.seek(0)
        fd1.truncate()
        fd1.write(str(int(time.time())))
        fd1.flush()
        fd1.close()

        # continue_agent_id should return ID 1
        cid, cfd = config.continue_agent_id(self.user_repo)
        self.assertEqual(cid, "1")
        cfd.close()

    def test_continue_picks_most_recent(self):
        """continue_agent_id picks the most recently exited agent."""
        self._init_and_agents("1", "2")

        import time

        # Hold both locks simultaneously
        id1, fd1 = config.acquire_agent_id(self.user_repo)
        id2, fd2 = config.acquire_agent_id(self.user_repo)

        # Agent 1 exits first (older timestamp)
        fd1.seek(0); fd1.truncate()
        fd1.write(str(int(time.time()) - 100))
        fd1.flush(); fd1.close()

        # Agent 2 exits later (newer timestamp)
        fd2.seek(0); fd2.truncate()
        fd2.write(str(int(time.time())))
        fd2.flush(); fd2.close()

        cid, cfd = config.continue_agent_id(self.user_repo)
        self.assertEqual(cid, "2")
        cfd.close()

    def test_continue_skips_running(self):
        """continue_agent_id skips agents that are still running."""
        self._init_and_agents("1", "2")

        import time

        # Agent 1 is running (holds lock)
        id1, fd1 = config.acquire_agent_id(self.user_repo)

        # Agent 2 exited
        id2, fd2 = config.acquire_agent_id(self.user_repo)
        fd2.seek(0); fd2.truncate()
        fd2.write(str(int(time.time())))
        fd2.flush(); fd2.close()

        cid, cfd = config.continue_agent_id(self.user_repo)
        self.assertEqual(cid, "2")

        fd1.close()
        cfd.close()

    def test_continue_no_agents(self):
        """continue_agent_id raises SystemExit when no agents exist."""
        self._init()
        with self.assertRaises(SystemExit):
            config.continue_agent_id(self.user_repo)

    def test_session_id_captured_and_read(self):
        """capture_session_id finds most recent session; read_session_id returns it."""
        self._init()
        slug = config.project_slug(self.user_repo)
        # Force depth=0 so sessions_dir resolves inside the test data dir, not real ~
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
            sessions_dir.mkdir(parents=True, exist_ok=True)
            (sessions_dir / "aaaa-1111.jsonl").write_text("")
            time.sleep(0.01)
            (sessions_dir / "bbbb-2222.jsonl").write_text("")
            config.capture_session_id(self.user_repo, "1")
            sid = config.read_session_id(self.user_repo, "1")
        self.assertEqual(sid, "bbbb-2222")

    def test_session_id_missing_returns_none(self):
        """read_session_id returns None when no session file exists."""
        self._init()
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            self.assertIsNone(config.read_session_id(self.user_repo, "99"))

    def test_session_id_missing_file_returns_none(self):
        """read_session_id returns None when stored UUID has no matching session file."""
        self._init()
        slug = config.project_slug(self.user_repo)
        p = config.agent_session_file(slug, "1")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("nonexistent-uuid")
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            self.assertIsNone(config.read_session_id(self.user_repo, "1"))

    def test_find_agent_by_session(self):
        """find_agent_by_session returns the agent that owns a given UUID."""
        self._init()
        slug = config.project_slug(self.user_repo)
        config.agent_session_file(slug, "3").parent.mkdir(parents=True, exist_ok=True)
        config.agent_session_file(slug, "3").write_text("my-uuid")
        config.agent_session_file(slug, "5").write_text("other-uuid")
        self.assertEqual(config.find_agent_by_session(slug, "my-uuid"), "3")
        self.assertIsNone(config.find_agent_by_session(slug, "unknown-uuid"))

    def test_reject_workflow(self):
        """Agent delivers v1, user rejects via rollback, agent delivers v2."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent delivers v1
        (ws / "feature.py").write_text("# v1\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Feature v1", cwd=ws)
        git("push", "cave", "agent-1/main:main", cwd=ws, env=agent_env("1"))

        cav_cli("pull", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Feature v1", log)

        # User rejects: force-push main back to original
        cav_cli("push", "--force", cwd=self.user_repo)

        # Agent fetches, sees main was rolled back, amends and redelivers
        git("fetch", "cave", cwd=ws)
        git("reset", "--hard", "cave/main", cwd=ws)
        git("checkout", "agent-1/main", cwd=ws)
        git("reset", "--hard", "cave/main", cwd=ws)
        (ws / "feature.py").write_text("# v2 (fixed)\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Feature v2", cwd=ws)
        git("push", "cave", "agent-1/main:main", cwd=ws, env=agent_env("1"))

        cav_cli("pull", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Feature v2", log)

    # -- rollback workflows --

    def test_user_force_push_shared_branch_rollback(self):
        """User force-pushes shared branch, agent rebases."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        git("checkout", "main", cwd=ws)
        (ws / "oops.py").write_text("# oops\n")
        git("add", "oops.py", cwd=ws)
        git("commit", "-m", "Bad commit", cwd=ws)
        git("push", "cave", "main", cwd=ws, env=agent_env("1"))

        # User makes a different commit (diverges)
        (self.user_repo / "rollback.py").write_text("# replacement\n")
        git("add", "rollback.py", cwd=self.user_repo)
        git("commit", "-m", "Replacement commit", cwd=self.user_repo)
        cav_cli("push", "--force", cwd=self.user_repo)

        # Agent's push is now rejected (non-ff)
        (ws / "oops2.py").write_text("# oops2\n")
        git("add", "oops2.py", cwd=ws)
        git("commit", "-m", "Another commit", cwd=ws)
        result = git("push", "cave", "main", cwd=ws, check=False, env=agent_env("1"))
        self.assertNotEqual(result.returncode, 0)

        # Agent rebases and retries
        git("fetch", "cave", cwd=ws)
        git("reset", "--hard", "cave/main", cwd=ws)
        (ws / "good.py").write_text("# good\n")
        git("add", "good.py", cwd=ws)
        git("commit", "-m", "Good commit", cwd=ws)
        result = git("push", "cave", "main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

    def test_user_force_pushes_agent_branch(self):
        """User force-pushes an agent branch to roll back agent work."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "bad.py").write_text("# bad\n")
        git("add", "bad.py", cwd=ws)
        git("commit", "-m", "Bad agent work", cwd=ws)
        git("push", "cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # User rolls back agent branch to main
        git("fetch", "cave", cwd=self.user_repo)
        git(
            "push", "--force", "cave",
            "cave/main:refs/heads/agent-1/main",
            cwd=self.user_repo,
        )

        # Agent discovers rollback, resets
        git("fetch", "cave", cwd=ws)
        git("reset", "--hard", "cave/agent-1/main", cwd=ws)

        files = git_out("ls-files", cwd=ws)
        self.assertNotIn("bad.py", files)


if __name__ == "__main__":
    unittest.main()
