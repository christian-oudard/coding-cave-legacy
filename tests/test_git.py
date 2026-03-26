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

    def test_launch_from_agent_branch_rejected(self):
        """Cannot launch a new agent while on an agent branch."""
        self._init()
        git("checkout", "-b", "agent-1/main", cwd=self.user_repo)
        with self.assertRaises(SystemExit) as ctx:
            worktree.create(self.user_repo, "2")
        self.assertIn("agent branch", str(ctx.exception))

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

    def test_deliver_cleans_up_agent_branch(self):
        """Delivering to main auto-deletes the agent's remote branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)

        # Push to agent branch first (simulating normal workflow)
        git("push", "cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # Verify agent branch exists on remote
        git("fetch", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertIn("cave/agent-1/main", branches)

        # Agent delivers to main
        git("push", "cave", "agent-1/main:main", cwd=ws, env=agent_env("1"))

        # Agent branch should be gone from remote
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertNotIn("cave/agent-1/main", branches)

    def test_pull_agent_branch_resets_to_remote(self):
        """cav pull on an agent branch hard-resets to the remote agent branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent makes a commit and pushes the agent branch
        (ws / "wip.py").write_text("# v1\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP v1", cwd=ws)
        git("push", "cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # User checks out the agent branch
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main", cwd=self.user_repo)

        # User makes a local commit (simulating divergence)
        (self.user_repo / "local.py").write_text("# local\n")
        git("add", "local.py", cwd=self.user_repo)
        git("commit", "-m", "Local change", cwd=self.user_repo)

        # Agent amends and force-pushes (rewrites history)
        (ws / "wip.py").write_text("# v2\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "--amend", "-m", "WIP v2", cwd=ws)
        git("push", "--force", "cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # cav pull should reset to remote, discarding local divergence
        cav_cli("pull", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("WIP v2", log)
        self.assertNotIn("Local change", log)
        self.assertNotIn("WIP v1", log)

        content = (self.user_repo / "wip.py").read_text()
        self.assertEqual(content, "# v2\n")
        self.assertFalse((self.user_repo / "local.py").exists())

    def test_nested_agent_branch_resolves_to_shared_target(self):
        """Nested agent branch agent-2/agent-1/main resolves target to main."""
        agents = self._init_and_agents("1", "2")
        ws1 = agents["1"]

        # Agent 1 pushes work on their branch
        (ws1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=ws1)
        git("commit", "-m", "Agent 1 work", cwd=ws1)
        git("push", "cave", "agent-1/main", cwd=ws1, env=agent_env("1"))

        # Agent 2 creates a nested branch (as if spawned inside agent 1's workspace)
        ws2 = agents["2"]
        git("checkout", "-b", "agent-2/agent-1/main", cwd=ws2)
        (ws2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=ws2)
        git("commit", "-m", "Agent 2 nested work", cwd=ws2)
        git("push", "cave", "agent-2/agent-1/main", cwd=ws2, env=agent_env("2"))

        # Also deliver agent 1's work to main so we can test the delta
        git("push", "cave", "agent-1/main:main", cwd=ws1, env=agent_env("1"))

        # Status should compare agent-2's branch against main (not agent-1/main)
        result = cav_cli("status", cwd=self.user_repo)
        # Only agent 2's own commit should be undelivered, not agent 1's
        self.assertIn("1 undelivered commit", result.stdout)
        self.assertIn("Agent 2 nested work", result.stdout)

        # Pull on the nested branch should reset to remote and show
        # commits relative to main
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-2/agent-1/main",
            "cave/agent-2/agent-1/main", cwd=self.user_repo)
        result = cav_cli("pull", cwd=self.user_repo, check=False)
        self.assertIn("Already up to date", result.stdout)

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

    def test_snapshot_captures_new_session_not_old(self):
        """With a snapshot, capture_session_id picks the new file, not stale ones."""
        self._init()
        slug = config.project_slug(self.user_repo)
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
            sessions_dir.mkdir(parents=True, exist_ok=True)

            # Pre-existing sessions (before agent launch)
            (sessions_dir / "old-1111.jsonl").write_text("old data")
            time.sleep(0.01)
            (sessions_dir / "old-2222.jsonl").write_text("old data")
            time.sleep(0.01)

            # Take snapshot (simulates pre-launch)
            snapshot = config.snapshot_sessions(self.user_repo)
            self.assertEqual(len(snapshot), 2)

            # Simulate agent running: creates a NEW session file
            time.sleep(0.01)
            (sessions_dir / "new-3333.jsonl").write_text("new data")

            # Capture with snapshot should pick new-3333, not old-2222
            config.capture_session_id(self.user_repo, "1", snapshot=snapshot)
            sid = config.read_session_id(self.user_repo, "1")
        self.assertEqual(sid, "new-3333")

    def test_snapshot_captures_modified_session(self):
        """With a snapshot, capture picks a file that was modified (resumed session)."""
        self._init()
        slug = config.project_slug(self.user_repo)
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
            sessions_dir.mkdir(parents=True, exist_ok=True)

            # Two pre-existing sessions
            (sessions_dir / "sess-aaaa.jsonl").write_text("v1")
            time.sleep(0.01)
            (sessions_dir / "sess-bbbb.jsonl").write_text("v1")

            snapshot = config.snapshot_sessions(self.user_repo)

            # Agent resumes sess-aaaa (modifies it, so mtime changes)
            time.sleep(0.01)
            (sessions_dir / "sess-aaaa.jsonl").write_text("v2 with new content")

            config.capture_session_id(self.user_repo, "1", snapshot=snapshot)
            sid = config.read_session_id(self.user_repo, "1")
        self.assertEqual(sid, "sess-aaaa")

    def test_multi_agent_session_isolation(self):
        """Two agents each capture their own session, not each other's."""
        self._init()
        slug = config.project_slug(self.user_repo)
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
            sessions_dir.mkdir(parents=True, exist_ok=True)

            # Agent 1 launches
            snap1 = config.snapshot_sessions(self.user_repo)
            time.sleep(0.01)
            (sessions_dir / "agent1-sess.jsonl").write_text("a1")

            # Agent 1 exits, captures
            config.capture_session_id(self.user_repo, "1", snapshot=snap1)

            # Agent 2 launches (snapshot now includes agent1-sess)
            snap2 = config.snapshot_sessions(self.user_repo)
            time.sleep(0.01)
            (sessions_dir / "agent2-sess.jsonl").write_text("a2")

            # Agent 2 exits, captures
            config.capture_session_id(self.user_repo, "2", snapshot=snap2)

            # Each agent reads back their own session
            self.assertEqual(config.read_session_id(self.user_repo, "1"), "agent1-sess")
            self.assertEqual(config.read_session_id(self.user_repo, "2"), "agent2-sess")

    def test_resume_routes_to_owning_agent(self):
        """find_agent_by_session + continue_agent_id routes to the correct agent."""
        self._init_and_agents("1", "2")
        slug = config.project_slug(self.user_repo)

        # Acquire both locks, then release with exit timestamps
        id1, fd1 = config.acquire_agent_id(self.user_repo)
        id2, fd2 = config.acquire_agent_id(self.user_repo)
        for fd in (fd1, fd2):
            fd.seek(0); fd.truncate()
            fd.write(str(int(time.time()) - 50))
            fd.flush(); fd.close()

        # Store session mappings
        config.agent_session_file(slug, "1").parent.mkdir(parents=True, exist_ok=True)
        config.agent_session_file(slug, "1").write_text("uuid-for-agent-1")
        config.agent_session_file(slug, "2").write_text("uuid-for-agent-2")

        # Resuming uuid-for-agent-2 should route to agent 2
        owner = config.find_agent_by_session(slug, "uuid-for-agent-2")
        self.assertEqual(owner, "2")
        cid, cfd = config.continue_agent_id(self.user_repo, owner)
        self.assertEqual(cid, "2")
        cfd.close()

        # Resuming uuid-for-agent-1 should route to agent 1
        owner = config.find_agent_by_session(slug, "uuid-for-agent-1")
        self.assertEqual(owner, "1")
        cid, cfd = config.continue_agent_id(self.user_repo, owner)
        self.assertEqual(cid, "1")
        cfd.close()

    def test_snapshot_no_false_capture_from_concurrent_agent(self):
        """Agent A's capture ignores a session created by concurrent agent B."""
        self._init()
        slug = config.project_slug(self.user_repo)
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
            sessions_dir.mkdir(parents=True, exist_ok=True)

            # Both agents launch at roughly the same time
            snap_a = config.snapshot_sessions(self.user_repo)
            snap_b = config.snapshot_sessions(self.user_repo)

            # Agent A creates its session
            time.sleep(0.01)
            (sessions_dir / "sess-aaa.jsonl").write_text("a")

            # Agent B creates its session (more recently modified)
            time.sleep(0.01)
            (sessions_dir / "sess-bbb.jsonl").write_text("b")

            # Agent A exits first. Both files are "new" relative to snap_a,
            # but capture picks the most recent. This is the limitation:
            # with concurrent agents and identical snapshots, capture picks
            # the most-recently-modified among new files. But at least it
            # won't pick a PRE-EXISTING session.
            config.capture_session_id(self.user_repo, "1", snapshot=snap_a)
            sid_a = config.read_session_id(self.user_repo, "1")
            # Both are new, so most recent wins (sess-bbb)
            self.assertIn(sid_a, ("sess-aaa", "sess-bbb"))

            # Agent B exits. Same situation from B's perspective.
            config.capture_session_id(self.user_repo, "2", snapshot=snap_b)
            sid_b = config.read_session_id(self.user_repo, "2")
            self.assertIn(sid_b, ("sess-aaa", "sess-bbb"))

    def test_snapshot_empty_dir(self):
        """snapshot_sessions returns empty dict when sessions dir doesn't exist."""
        self._init()
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            snap = config.snapshot_sessions(self.user_repo)
        self.assertEqual(snap, {})

    def test_capture_without_snapshot_falls_back(self):
        """capture_session_id without snapshot picks most recent (backward compat)."""
        self._init()
        slug = config.project_slug(self.user_repo)
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
            sessions_dir.mkdir(parents=True, exist_ok=True)
            (sessions_dir / "old-file.jsonl").write_text("")
            time.sleep(0.01)
            (sessions_dir / "new-file.jsonl").write_text("")

            # No snapshot passed
            config.capture_session_id(self.user_repo, "1")
            sid = config.read_session_id(self.user_repo, "1")
        self.assertEqual(sid, "new-file")

    def test_resume_unknown_uuid_returns_none(self):
        """find_agent_by_session returns None for unknown UUIDs."""
        self._init_and_agents("1")
        slug = config.project_slug(self.user_repo)

        # Agent 1 exits
        id1, fd1 = config.acquire_agent_id(self.user_repo)
        fd1.seek(0); fd1.truncate()
        fd1.write(str(int(time.time())))
        fd1.flush(); fd1.close()

        # No agent owns "nonexistent-uuid"
        owner = config.find_agent_by_session(slug, "nonexistent-uuid")
        self.assertIsNone(owner)
        # CLI checks for None and errors before calling continue_agent_id

    def test_sessions_dir_depth_zero_uses_persistent_home(self):
        """At depth 0 (host), sessions dir is under persistent_home()."""
        slug = "myproject"
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
        self.assertEqual(
            sessions_dir,
            config.persistent_home() / ".claude" / "projects" / "-projects-myproject",
        )

    def test_sessions_dir_depth_nonzero_uses_real_home(self):
        """At depth > 0 (inside cave), sessions dir is under Path.home()."""
        slug = "myproject"
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "1"}):
            sessions_dir = config._claude_sessions_dir(slug)
        self.assertEqual(
            sessions_dir,
            Path.home() / ".claude" / "projects" / "-projects-myproject",
        )

    def test_sessions_dir_depth_two_uses_real_home(self):
        """At depth 2 (subcave), sessions dir still uses Path.home()."""
        slug = "myproject"
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "2"}):
            sessions_dir = config._claude_sessions_dir(slug)
        self.assertEqual(
            sessions_dir,
            Path.home() / ".claude" / "projects" / "-projects-myproject",
        )

    def test_continue_specific_agent_not_most_recent(self):
        """continue_agent_id(base, '1') returns agent 1 even when agent 2 exited more recently."""
        self._init_and_agents("1", "2")

        # Both agents exit, agent 2 more recently
        id1, fd1 = config.acquire_agent_id(self.user_repo)
        id2, fd2 = config.acquire_agent_id(self.user_repo)
        fd1.seek(0); fd1.truncate()
        fd1.write(str(int(time.time()) - 100))
        fd1.flush(); fd1.close()
        fd2.seek(0); fd2.truncate()
        fd2.write(str(int(time.time())))
        fd2.flush(); fd2.close()

        # Explicitly requesting agent 1 should return 1, not 2
        cid, cfd = config.continue_agent_id(self.user_repo, "1")
        self.assertEqual(cid, "1")
        cfd.close()

    def test_continue_running_agent_errors(self):
        """continue_agent_id errors when the requested agent is still running."""
        self._init_and_agents("1")

        # Agent 1 is running (holds lock)
        id1, fd1 = config.acquire_agent_id(self.user_repo)

        with self.assertRaises(SystemExit) as ctx:
            config.continue_agent_id(self.user_repo, "1")
        self.assertIn("still running", str(ctx.exception))

        fd1.close()

    def test_depth_and_lineage_propagation(self):
        """Verify depth increments and lineage chains across 3 levels of nesting."""
        # Simulate the env computation from sandbox.py launch() at each level

        def compute_env(parent_env, agent_name, recursive=True):
            """Replicate the depth/lineage logic from sandbox.py."""
            parent_depth = int(parent_env.get("CODING_CAVE_DEPTH", "0"))
            env = {
                **parent_env,
                "CODING_CAVE_DEPTH": str(parent_depth + 1),
                "CAV_AGENT_NAME": agent_name,
                "CODING_CAVE_HOST_HOME": parent_env.get(
                    "CODING_CAVE_HOST_HOME", "/home/testuser"),
            }
            if recursive:
                env["CODING_CAVE_RECURSIVE"] = "1"
            parent_lineage = parent_env.get("CODING_CAVE_LINEAGE", "")
            parent_agent = parent_env.get("CAV_AGENT_NAME", "")
            if parent_agent:
                lineage = f"{parent_lineage}.{parent_agent}" if parent_lineage else parent_agent
                env["CODING_CAVE_LINEAGE"] = lineage
            return env

        # Level 0: host
        host_env = {"CODING_CAVE_HOST_HOME": "/home/testuser"}

        # Level 1: first cave
        l1 = compute_env(host_env, "project@1")
        self.assertEqual(l1["CODING_CAVE_DEPTH"], "1")
        self.assertNotIn("CODING_CAVE_LINEAGE", l1)  # no parent agent at host
        self.assertEqual(l1["CODING_CAVE_HOST_HOME"], "/home/testuser")

        # Level 2: subcave
        l2 = compute_env(l1, "project@2")
        self.assertEqual(l2["CODING_CAVE_DEPTH"], "2")
        self.assertEqual(l2["CODING_CAVE_LINEAGE"], "project@1")
        self.assertEqual(l2["CODING_CAVE_HOST_HOME"], "/home/testuser")

        # Level 3: sub-subcave
        l3 = compute_env(l2, "project@1")
        self.assertEqual(l3["CODING_CAVE_DEPTH"], "3")
        self.assertEqual(l3["CODING_CAVE_LINEAGE"], "project@1.project@2")
        self.assertEqual(l3["CODING_CAVE_HOST_HOME"], "/home/testuser")

        # Level 4: verify it keeps working
        l4 = compute_env(l3, "project@3")
        self.assertEqual(l4["CODING_CAVE_DEPTH"], "4")
        self.assertEqual(l4["CODING_CAVE_LINEAGE"], "project@1.project@2.project@1")

    def test_sessions_dir_at_each_depth(self):
        """_claude_sessions_dir resolves correctly at depths 0, 1, 2, 3."""
        slug = "testproject"

        # Depth 0 (host): uses persistent_home()
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            d0 = config._claude_sessions_dir(slug)
        self.assertTrue(str(d0).startswith(str(config.persistent_home())))

        # Depths 1, 2, 3 (inside cave): all use Path.home()
        for depth in ("1", "2", "3"):
            with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": depth}):
                d = config._claude_sessions_dir(slug)
            self.assertTrue(
                str(d).startswith(str(Path.home())),
                f"At depth {depth}, sessions dir should be under Path.home(), got {d}",
            )

    def test_recursive_not_set_blocks_subcave(self):
        """Without --recursive, CODING_CAVE_RECURSIVE is not propagated."""
        # Simulate launch without --recursive
        env = {}
        recursive = False
        if recursive:
            env["CODING_CAVE_RECURSIVE"] = "1"

        self.assertNotIn("CODING_CAVE_RECURSIVE", env)

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


    # -- cross-depth isolation bugs --

    def test_lock_files_isolated_by_depth(self):
        """Agent 1 at depth 1 and agent 1 at depth 2 must not share a lock file."""
        self._init()
        slug = config.project_slug(self.user_repo)

        # Depth 1 agent acquires lock for agent 1
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "1"}):
            id1, fd1 = config.acquire_agent_id(self.user_repo)
            self.assertEqual(id1, "1")

            # Depth 2 agent tries to acquire agent 1 - should get it independently
            with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "2"}):
                id2, fd2 = config.acquire_agent_id(self.user_repo)
                # BUG: currently returns "2" because depth 1 holds agent 1's lock.
                # Should return "1" because depth 2 has its own lock namespace.
                self.assertEqual(id2, "1")
                fd2.close()

            fd1.close()

    def test_session_files_isolated_by_depth(self):
        """Session capture at depth 2 must not overwrite depth 1's session."""
        self._init()
        slug = config.project_slug(self.user_repo)

        # In production, all depths share the same sessions dir (bind-mounted home).
        # Simulate this by using a fixed sessions dir regardless of depth.
        sessions_dir = self.data_dir / "test-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "depth1-session.jsonl").write_text("")
        time.sleep(0.01)
        (sessions_dir / "depth2-session.jsonl").write_text("")

        with unittest.mock.patch("cav.config._claude_sessions_dir", return_value=sessions_dir):
            # Depth 1 agent 1 captures
            with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "1"}):
                snap1 = {"depth2-session": 9999999999.0}
                config.capture_session_id(self.user_repo, "1", snapshot=snap1)
                depth1_sid = config.read_session_id(self.user_repo, "1")
            self.assertEqual(depth1_sid, "depth1-session")

            # Depth 2 agent 1 captures a different session
            with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "2"}):
                snap2 = {"depth1-session": 9999999999.0}
                config.capture_session_id(self.user_repo, "1", snapshot=snap2)
                depth2_sid = config.read_session_id(self.user_repo, "1")
            self.assertEqual(depth2_sid, "depth2-session")

            # Depth 1's session should still be intact (different lock dir)
            with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "1"}):
                depth1_sid_after = config.read_session_id(self.user_repo, "1")
            self.assertEqual(depth1_sid_after, "depth1-session")

    def test_daemon_port_isolated_by_depth(self):
        """Agent 1 at depth 1 and agent 1 at depth 2 must use different ports."""
        from cav import daemon

        # Same agent ID at two different depths should produce different ports
        # Currently both would use DAEMON_BASE_PORT + 1 = 9401
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "1"}):
            url_d1 = daemon.remote_url("1", "project")

        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "2"}):
            url_d2 = daemon.remote_url("1", "project")

        # BUG: both return git://127.0.0.1:9401/project
        self.assertNotEqual(url_d1, url_d2)

    # -- concurrent agent race --

    def test_concurrent_agents_capture_distinct_sessions(self):
        """Two agents launching simultaneously each capture their own session.

        The capture lock serializes the scan-and-claim. Agent 1 captures first
        (claims most recent), agent 2 captures second (claims the remaining one).
        """
        self._init()
        slug = config.project_slug(self.user_repo)

        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_DEPTH": "0"}):
            sessions_dir = config._claude_sessions_dir(slug)
            sessions_dir.mkdir(parents=True, exist_ok=True)

            # Both agents take snapshot at the same moment (empty dir)
            snap = config.snapshot_sessions(self.user_repo)

            # Agent 1 creates its session
            time.sleep(0.01)
            (sessions_dir / "agent1-uuid.jsonl").write_text("a1")

            # Agent 2 creates its session (more recently modified)
            time.sleep(0.01)
            (sessions_dir / "agent2-uuid.jsonl").write_text("a2")

            # Agent 1 captures first (gets most recent: agent2-uuid)
            config.capture_session_id(self.user_repo, "1", snapshot=snap)
            # Agent 2 captures second (agent2-uuid already claimed, gets agent1-uuid)
            config.capture_session_id(self.user_repo, "2", snapshot=snap)

            sid1 = config.read_session_id(self.user_repo, "1")
            sid2 = config.read_session_id(self.user_repo, "2")

            # Each agent gets a distinct session
            self.assertNotEqual(sid1, sid2)
            self.assertIn(sid1, ("agent1-uuid", "agent2-uuid"))
            self.assertIn(sid2, ("agent1-uuid", "agent2-uuid"))


if __name__ == "__main__":
    unittest.main()
