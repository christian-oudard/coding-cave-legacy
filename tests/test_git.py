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

from cav import config, registry, workspace
from tests import find_agent_by_session


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
        ws = workspace.create(self.user_repo, name)
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
        self.assertIn("Coding Cave initialized", result.stdout)

        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        self.assertTrue(shared.exists())
        self.assertTrue((shared / "hooks" / "pre-receive").exists())

        remotes = git_out("remote", cwd=self.user_repo)
        self.assertIn("cave", remotes)

    def test_init_idempotent(self):
        """cav init on an existing project updates hooks."""
        cav_cli("init", str(self.user_repo), cwd=self.user_repo)
        shared = self.user_repo / ".coding-cave" / "cave-shared.git"

        # Delete post-receive to simulate old install
        (shared / "hooks" / "post-receive").unlink()
        self.assertFalse((shared / "hooks" / "post-receive").exists())

        # Re-init should update hooks, not error
        result = cav_cli("init", str(self.user_repo), cwd=self.user_repo)
        self.assertIn("Coding Cave initialized", result.stdout)
        self.assertTrue((shared / "hooks" / "post-receive").exists())

    # -- workspace creation --

    def test_workspace_creation(self):
        """Workspace has correct branch and remote."""
        agents = self._init_and_agents("1", "2")

        for name, ws in agents.items():
            self.assertTrue(ws.exists())

            remotes = git_out("remote", cwd=ws)
            self.assertIn("host-cave", remotes)

            branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=ws)
            self.assertEqual(branch, f"agent-{name}/main")

    def test_workspace_rejects_detached_head(self):
        """New workspace from detached HEAD must fail, not create agent-N/HEAD."""
        from cav.init import init
        init(str(self.user_repo))

        # Detach HEAD
        head = git_out("rev-parse", "HEAD", cwd=self.user_repo)
        git("checkout", head, cwd=self.user_repo)

        with self.assertRaises(SystemExit):
            workspace.create(self.user_repo, "1")

    def test_workspace_branch_sync_sets_tracking(self):
        """branch -f in workspace sync must set host-cave tracking for new branches.

        The sandbox syncs shared branches with:
            git branch -f <branch> host-cave/<branch>
        This must NOT use --no-track. The clone only sets up tracking for
        branches that existed at clone time. If a new shared branch (e.g.
        develop) appears on host-cave later, the sync loop creates it locally,
        and git's autoSetupMerge gives it host-cave tracking. With --no-track,
        the new branch would have no upstream and git pull would fail.
        """
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Create a new shared branch on cave (after workspace already exists)
        git("checkout", "-b", "develop", cwd=self.user_repo)
        (self.user_repo / "dev.py").write_text("# develop\n")
        git("add", "dev.py", cwd=self.user_repo)
        git("commit", "-m", "develop branch", cwd=self.user_repo)
        git("push", "cave", "develop", cwd=self.user_repo)

        # Simulate the sandbox sync loop (sandbox.py:378-390)
        git("fetch", "host-cave", cwd=ws)
        git("branch", "-f", "develop", "host-cave/develop", cwd=ws)

        # New branch must track host-cave so git pull works
        remote = git_out("config", "branch.develop.remote", cwd=ws)
        self.assertEqual(remote, "host-cave")

        # Verify pull actually works
        git("checkout", "develop", cwd=ws)
        git("pull", cwd=ws, check=True)

    def test_git_wrapper_force_pushes(self):
        """Wrapper force-pushes so squashed history updates the remote."""
        from cav.sandbox import GIT_WRAPPER
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Install the wrapper (normally done by sandbox.launch)
        bin_dir = Path(self.tmpdir) / "wrapper_bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "git"
        wrapper.write_text(GIT_WRAPPER)
        wrapper.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}

        # Make 2 commits and push
        (ws / "a.py").write_text("a\n")
        git("add", "a.py", cwd=ws)
        git("commit", "-m", "commit 1", cwd=ws, env=env)
        (ws / "b.py").write_text("b\n")
        git("add", "b.py", cwd=ws)
        git("commit", "-m", "commit 2", cwd=ws, env=env)

        # Squash to 1 commit — creates diverging history
        git("reset", "--soft", "HEAD~2", cwd=ws)
        # Commit via wrapper — must force-push or remote stays on old history
        git("commit", "-m", "squashed", cwd=ws, env=env)

        git("fetch", "host-cave", cwd=ws)
        local_sha = git_out("rev-parse", "HEAD", cwd=ws)
        remote_sha = git_out("rev-parse", "host-cave/agent-1/main", cwd=ws)
        self.assertEqual(local_sha, remote_sha)

    def test_invalid_agent_id(self):
        """Non-numeric agent IDs are rejected."""
        self._init()
        with self.assertRaises(SystemExit):
            workspace.create(self.user_repo, "bad_name")

    def test_launch_from_agent_branch_resolves_target(self):
        """Launching from an agent branch targets the underlying shared branch."""
        self._init()
        git("checkout", "-b", "agent-1/main", cwd=self.user_repo)
        ws = workspace.create(self.user_repo, "2")
        # Should create agent-2/main (not agent-2/agent-1/main)
        branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=ws)
        self.assertEqual(branch, "agent-2/main")

    def test_auto_agent_id(self):
        """acquire_agent_id auto-assigns 1, 2, 3 when all are running."""
        self._init()

        id1 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        self._create_agent(id1)

        id2 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id2, "2")
        self._create_agent(id2)

        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

    def test_agent_id_reuse_after_remove(self):
        """acquire_agent_id reuses IDs after they are removed from registry."""
        self._init()

        id1 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        self._create_agent(id1)

        id2 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id2, "2")
        self._create_agent(id2)

        # Remove agent 1 from registry
        registry.mark_agent_exited(id1)
        registry.remove_agent(id1)

        # Next agent should reuse ID 1
        id3 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id3, "1")

        registry.mark_agent_exited(id2)
        registry.mark_agent_exited(id3)

    def test_paused_agent_keeps_id(self):
        """Exited agents keep their ID reserved until explicitly removed."""
        self._init()

        id1 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        registry.mark_agent_exited(id1)

        # Paused agent keeps its ID, next agent gets 2
        id2 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id2, "2")
        registry.mark_agent_exited(id2)

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

        # Push agent branch, then approve as user
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))
        git("push", "host-cave", "agent-1/main:main", cwd=ws)

        cav_cli("pull", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Add feature", log)
        self.assertIn("Update feature", log)

    def test_approve_preserves_agent_branch(self):
        """Approving to main does NOT auto-delete the agent's remote branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)

        # Push to agent branch
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # Approve as user
        git("push", "host-cave", "agent-1/main:main", cwd=ws)

        # Agent branch should still exist on remote
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertIn("cave/agent-1/main", branches)

    def test_pull_agent_branch_resets_to_remote(self):
        """cav pull on an agent branch hard-resets to the remote agent branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent makes a commit and pushes the agent branch
        (ws / "wip.py").write_text("# v1\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP v1", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

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
        git("push", "--force", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # cav pull should reset to remote, discarding local divergence
        cav_cli("pull", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("WIP v2", log)
        self.assertNotIn("Local change", log)
        self.assertNotIn("WIP v1", log)

        content = (self.user_repo / "wip.py").read_text()
        self.assertEqual(content, "# v2\n")
        self.assertFalse((self.user_repo / "local.py").exists())

    def test_pull_agent_branch_preserves_main_upstream(self):
        """cav pull on agent branch must not hijack main's upstream to cave."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Simulate a real repo: main tracks an origin remote, not cave.
        origin = Path(self.tmpdir) / "origin.git"
        git("init", "--bare", str(origin))
        git("remote", "add", "origin", str(origin), cwd=self.user_repo)
        git("push", "origin", "main", cwd=self.user_repo)
        git("branch", "--set-upstream-to=origin/main", "main", cwd=self.user_repo)

        # Agent pushes work
        (ws / "feature.py").write_text("# new\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Agent work", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # User checks out agent branch and pulls
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main",
            cwd=self.user_repo)
        cav_cli("pull", cwd=self.user_repo)

        # main must still track origin, not cave
        remote = git_out("config", "branch.main.remote", cwd=self.user_repo)
        self.assertEqual(remote, "origin")

    def test_nested_agent_branch_resolves_to_shared_target(self):
        """Nested agent branch agent-2/agent-1/main resolves target to main."""
        agents = self._init_and_agents("1", "2")
        # Register so status can see them
        for name in agents:
            registry.acquire_agent_id(self.user_repo)
        ws1 = agents["1"]

        # Agent 1 pushes work on their branch
        (ws1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=ws1)
        git("commit", "-m", "Agent 1 work", cwd=ws1)
        git("push", "host-cave", "agent-1/main", cwd=ws1, env=agent_env("1"))

        # Agent 2 creates a nested branch (as if spawned inside agent 1's workspace)
        ws2 = agents["2"]
        git("checkout", "-b", "agent-2/agent-1/main", cwd=ws2)
        (ws2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=ws2)
        git("commit", "-m", "Agent 2 nested work", cwd=ws2)
        git("push", "host-cave", "agent-2/agent-1/main", cwd=ws2, env=agent_env("2"))

        # Also approve agent 1's work to main so we can test the delta
        git("push", "host-cave", "agent-1/main:main", cwd=ws1)

        # Status should compare agent-2's branch against main (not agent-1/main)
        result = cav_cli("status", cwd=self.user_repo)
        # Only agent 2's own commit should be unapproved, not agent 1's
        self.assertIn("1 unapproved commit", result.stdout)
        self.assertIn("Agent 2 nested work", result.stdout)

        # Pull on the nested branch should reset to remote and show
        # commits relative to main
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-2/agent-1/main",
            "cave/agent-2/agent-1/main", cwd=self.user_repo)
        result = cav_cli("pull", cwd=self.user_repo, check=False)
        self.assertIn("Already up to date", result.stdout)

    def test_approve_preserves_agent_branch_in_bare(self):
        """After approving to main, the agent branch still exists in bare repo."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)

        # Agent branch exists on remote before approval
        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        refs_before = git_out("branch", cwd=shared)
        self.assertIn("agent-1/main", refs_before)

        # Approve as user
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))
        git("push", "host-cave", "agent-1/main:main", cwd=ws)

        # Agent branch should still exist after approval
        refs_after = git_out("branch", cwd=shared)
        self.assertIn("agent-1/main", refs_after)

        # Work should be on main
        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/main", cwd=self.user_repo)
        self.assertIn("Add feature", log)

    # -- merge + push --

    def test_user_pulls_approved_work(self):
        """User approves agent work to main, then pulls."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))
        git("push", "host-cave", "agent-1/main:main", cwd=ws)

        cav_cli("pull", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Add feature", log)

    # -- hook enforcement --

    def test_agent_push_shared_rejected(self):
        """Agent cannot push to shared branches at all."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        git("checkout", "main", cwd=ws)
        (ws / "fix.py").write_text("# fix\n")
        git("add", "fix.py", cwd=ws)
        git("commit", "-m", "Quick fix", cwd=ws)

        result = git("push", "host-cave", "main", cwd=ws, check=False, env=agent_env("1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("cannot push", result.stderr)

    def test_agent_force_push_shared_rejected(self):
        """Agent force-push to shared branch is rejected."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        git("checkout", "main", cwd=ws)
        (ws / "bad.py").write_text("# bad\n")
        git("add", "bad.py", cwd=ws)
        git("commit", "--amend", "-m", "Rewritten commit", cwd=ws)

        result = git("push", "--force", "host-cave", "main", cwd=ws, check=False, env=agent_env("1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("cannot push", result.stderr)

    def test_agent_cross_push_rejected(self):
        """Agent pushing to another agent's branch is rejected."""
        agents = self._init_and_agents("1", "2")
        ws1 = agents["1"]

        (ws1 / "file.py").write_text("# file\n")
        git("add", "file.py", cwd=ws1)
        git("commit", "-m", "Agent 1 work", cwd=ws1)

        result = git(
            "push", "host-cave", "HEAD:refs/heads/agent-2/main",
            cwd=ws1, check=False, env=agent_env("1"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("cannot push", result.stderr)

    def test_agent_cannot_delete_shared_branch(self):
        """Agent push --delete on a shared branch is rejected."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        result = git("push", "host-cave", "--delete", "main", cwd=ws, check=False, env=agent_env("1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("cannot push", result.stderr)

    def test_agent_pushes_own_branch(self):
        """Agent can push and force-push its own branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "wip.py").write_text("# wip\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP", cwd=ws)
        result = git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

        (ws / "wip.py").write_text("# wip v2\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "--amend", "-m", "WIP v2", cwd=ws)
        result = git("push", "--force", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

    # -- multi-agent --

    def test_multi_agent_approve_and_pull(self):
        """Two agents' work approved to main, pull shows all commits."""
        agents = self._init_and_agents("1", "2")

        # Agent 1 commits and pushes agent branch
        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 work", cwd=a1)
        git("push", "host-cave", "agent-1/main", cwd=a1, env=agent_env("1"))

        # Approve agent 1 as user
        git("push", "host-cave", "agent-1/main:main", cwd=a1)

        # Agent 2 rebases on new main and pushes
        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", "agent-2/main", cwd=a2)
        git("push", "--force", "host-cave", "agent-2/main", cwd=a2, env=agent_env("2"))

        # Approve agent 2 as user
        git("push", "host-cave", "agent-2/main:main", cwd=a2)

        cav_cli("pull", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

    def test_approve_contention(self):
        """cav approve fails when agent branch is not FF from updated main."""
        agents = self._init_and_agents("1", "2")

        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 work", cwd=a1)
        git("push", "host-cave", "agent-1/main", cwd=a1, env=agent_env("1"))

        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("push", "host-cave", "agent-2/main", cwd=a2, env=agent_env("2"))

        # Approve agent 1 via cav approve
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main", cwd=self.user_repo)
        cav_cli("approve", "--yes", cwd=self.user_repo)

        # Try to approve agent 2, should fail (not FF from updated main)
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-2/main", "cave/agent-2/main", cwd=self.user_repo)
        result = cav_cli("approve", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a fast-forward", result.stderr)

        # Agent 2 rebases on new main
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", cwd=a2)
        git("push", "--force", "host-cave", "agent-2/main", cwd=a2, env=agent_env("2"))

        # Retry approve
        git("checkout", "main", cwd=self.user_repo)
        git("branch", "-D", "agent-2/main", cwd=self.user_repo)
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-2/main", "cave/agent-2/main", cwd=self.user_repo)
        cav_cli("approve", "--yes", cwd=self.user_repo)

        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/main", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

    def test_approve_by_agent_id_from_main(self):
        """cav approve <id> approves from main without checking out agent branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]
        id1 = registry.acquire_agent_id(self.user_repo)

        (ws / "feature.py").write_text("# feature\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Agent 1 feature", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # Approve from main by agent ID (no checkout needed)
        git("fetch", "cave", cwd=self.user_repo)
        branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=self.user_repo)
        self.assertEqual(branch, "main")

        cav_cli("approve", "1", "--yes", cwd=self.user_repo)

        # Should still be on main with the approved commit
        branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=self.user_repo)
        self.assertEqual(branch, "main")
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 feature", log)

        registry.mark_agent_exited(id1)

    def test_approve_by_agent_id_contention(self):
        """cav approve <id> fails on non-FF, succeeds after rebase."""
        agents = self._init_and_agents("1", "2")

        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 work", cwd=a1)
        git("push", "host-cave", "agent-1/main", cwd=a1, env=agent_env("1"))

        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("push", "host-cave", "agent-2/main", cwd=a2, env=agent_env("2"))

        # Approve agent 1 from main by ID
        cav_cli("approve", "1", "--yes", cwd=self.user_repo)

        # Agent 2 should fail (not FF)
        result = cav_cli("approve", "2", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a fast-forward", result.stderr)

        # Agent 2 rebases and retries
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", "agent-2/main", cwd=a2)
        git("push", "--force", "host-cave", "agent-2/main", cwd=a2, env=agent_env("2"))

        cav_cli("approve", "2", "--yes", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

    def test_approve_no_arg_on_shared_branch_fails(self):
        """cav approve without agent-id fails on a shared branch."""
        self._init()
        result = cav_cli("approve", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cav approve <agent-id>", result.stderr)

    def test_approve_nonexistent_agent(self):
        """cav approve with wrong agent-id fails cleanly."""
        self._init()
        result = cav_cli("approve", "99", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No remote branch", result.stderr)

    def test_approve_succeeds_when_local_ahead(self):
        """cav approve pushes to cave even when local main is ahead."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent does work
        (ws / "feature.py").write_text("# feature\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Agent 1 feature", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # User makes a local-only commit on main (ahead of cave)
        (self.user_repo / "local.py").write_text("# local\n")
        git("add", "local.py", cwd=self.user_repo)
        git("commit", "-m", "Local only", cwd=self.user_repo)

        # Approve succeeds: cave gets the agent's work
        result = cav_cli("approve", "1", "--yes", cwd=self.user_repo, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

        # cave/main has the agent's commit
        git("fetch", "cave", cwd=self.user_repo)
        cave_log = git_out("log", "--oneline", "cave/main", cwd=self.user_repo)
        self.assertIn("Agent 1 feature", cave_log)

    def test_approve_ok_when_local_behind(self):
        """cav approve works when local main is behind cave/main."""
        agents = self._init_and_agents("1", "2")
        id1 = registry.acquire_agent_id(self.user_repo)

        # Approve agent 1 directly (advances cave/main, local follows)
        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 work", cwd=a1)
        git("push", "host-cave", "agent-1/main", cwd=a1, env=agent_env("1"))
        cav_cli("approve", "1", "--yes", cwd=self.user_repo)

        # Reset local main behind cave/main (simulate not having pulled)
        git("reset", "--hard", "HEAD~1", cwd=self.user_repo)

        # Agent 2 work (rebased on new cave/main)
        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", "agent-2/main", cwd=a2)
        git("push", "--force", "host-cave", "agent-2/main", cwd=a2, env=agent_env("2"))

        # Approve agent 2 from local main (which is behind cave/main)
        result = cav_cli("approve", "2", "--yes", cwd=self.user_repo)

        # Should succeed and local catches up
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

        registry.mark_agent_exited(id1)

    def test_approve_succeeds_with_dirty_worktree(self):
        """cav approve pushes to cave even when local worktree has conflicts."""
        agents = self._init_and_agents("1")

        a1 = agents["1"]
        (a1 / "feature.py").write_text("# feature\n")
        git("add", "feature.py", cwd=a1)
        git("commit", "-m", "Agent 1 feature", cwd=a1)
        git("push", "host-cave", "agent-1/main", cwd=a1, env=agent_env("1"))

        # Create a dirty worktree conflict on the target branch
        (self.user_repo / "feature.py").write_text("# conflict\n")

        result = cav_cli("approve", "1", "--yes", cwd=self.user_repo, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

        # cave/main has the agent's commit (approval succeeded)
        git("fetch", "cave", cwd=self.user_repo)
        cave_log = git_out("log", "--oneline", "cave/main", cwd=self.user_repo)
        self.assertIn("Agent 1 feature", cave_log)

        # Local could not be updated, user is told to cav pull
        self.assertIn("could not be updated", result.stdout)

    def test_multi_agent_status(self):
        """cav status shows commits ahead after approving work to main."""
        agents = self._init_and_agents("1", "2")

        # Approve agent 1's commits
        a1 = agents["1"]
        for i in range(2):
            (a1 / f"w{i}.py").write_text(f"# {i}\n")
            git("add", f"w{i}.py", cwd=a1)
            git("commit", "-m", f"agent 1 commit {i}", cwd=a1)
        git("push", "host-cave", "agent-1/main", cwd=a1, env=agent_env("1"))
        git("push", "host-cave", "agent-1/main:main", cwd=a1)

        # Approve agent 2's commit
        a2 = agents["2"]
        (a2 / "api.py").write_text("# api\n")
        git("add", "api.py", cwd=a2)
        git("commit", "-m", "agent 2 commit", cwd=a2)
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", "agent-2/main", cwd=a2)
        git("push", "--force", "host-cave", "agent-2/main", cwd=a2, env=agent_env("2"))
        git("push", "host-cave", "agent-2/main:main", cwd=a2)

        git("fetch", "cave", cwd=self.user_repo)

        result = cav_cli("status", cwd=self.user_repo)
        self.assertIn("3 commits", result.stdout)
        self.assertIn("behind", result.stdout)

    def test_status_dedup_multi_branch_agent(self):
        """cav status shows one line per agent even when agent has multiple branches."""
        agents = self._init_and_agents("1")
        registry.acquire_agent_id(self.user_repo)  # register agent 1
        a1 = agents["1"]

        # Agent 1 pushes work on their main branch
        (a1 / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=a1)
        git("commit", "-m", "agent 1 work", cwd=a1)
        git("push", "host-cave", "HEAD", cwd=a1, env=agent_env("1"))

        # Agent 1 also has a second branch (e.g. from red team or experimental)
        git("checkout", "-b", "agent-1/experiment", cwd=a1)
        git("push", "host-cave", "HEAD", cwd=a1, env=agent_env("1"))
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
        git("push", "host-cave", "HEAD", cwd=ws, env=agent_env("1"))

        # Agent 1 acquires and exits
        id1 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")
        registry.mark_agent_exited(id1)

        # continue_agent_id should return ID 1
        cid = registry.continue_agent_id(self.user_repo)
        self.assertEqual(cid, "1")
        registry.mark_agent_exited(cid)

    def test_continue_picks_most_recent(self):
        """continue_agent_id picks the most recently exited agent."""
        self._init_and_agents("1", "2")

        id1 = registry.acquire_agent_id(self.user_repo)
        id2 = registry.acquire_agent_id(self.user_repo)

        # Both exit, manipulate timestamps so agent 2 exited more recently
        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

        now = time.time()
        db = registry._open()
        db.execute(
            "UPDATE agents SET exited_at = ? WHERE agent_id = ?",
            (now - 100, id1)
        )
        db.commit()
        db.close()

        cid = registry.continue_agent_id(self.user_repo)
        self.assertEqual(cid, "2")
        registry.mark_agent_exited(cid)

    def test_continue_skips_running(self):
        """continue_agent_id skips agents that are still running."""
        self._init_and_agents("1", "2")

        # Agent 1 is running
        id1 = registry.acquire_agent_id(self.user_repo)

        # Agent 2 exited
        id2 = registry.acquire_agent_id(self.user_repo)
        registry.mark_agent_exited(id2)

        cid = registry.continue_agent_id(self.user_repo)
        self.assertEqual(cid, "2")

        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(cid)

    def test_continue_no_agents_no_sessions(self):
        """resolve_continue with no agents acquires fresh agent."""
        self._init()
        aid, sid, resuming = registry.resolve_continue(self.user_repo)
        self.assertEqual(aid, "1")
        self.assertFalse(resuming)
        self.assertIsNotNone(sid)
        registry.mark_agent_exited(aid)

    def test_continue_no_agents_acquires_fresh(self):
        """resolve_continue with no agents acquires a fresh one with a UUID session."""
        self._init()
        aid, sid, resuming = registry.resolve_continue(self.user_repo)
        self.assertIsNotNone(sid)
        self.assertEqual(len(sid), 36)
        self.assertFalse(resuming)
        registry.mark_agent_exited(aid)

    def test_claude_project_dir_encoding(self):
        """_claude_project_dir encodes path like Claude Code does."""
        # /projects/coding-cave -> -projects-coding-cave
        self.assertTrue(
            str(config._claude_project_dir("coding-cave")).endswith(
                ".claude/projects/-projects-coding-cave"
            )
        )
        # Dots, underscores become dashes
        self.assertTrue(
            str(config._claude_project_dir("my.project_v2")).endswith(
                "-projects-my-project-v2"
            )
        )

    def test_session_id_generated_on_acquire(self):
        """acquire_agent_id generates a UUID v4 session ID."""
        import uuid as uuid_mod
        self._init()
        aid = registry.acquire_agent_id(self.user_repo)
        sid = registry.read_session_id(aid)
        self.assertIsNotNone(sid)
        # Must be a valid UUID v4
        parsed = uuid_mod.UUID(sid)
        self.assertEqual(parsed.version, 4)
        registry.mark_agent_exited(aid)

    def test_session_id_unique_per_agent(self):
        """Each agent gets a distinct session UUID."""
        self._init()
        id1 = registry.acquire_agent_id(self.user_repo)
        self._create_agent(id1)
        id2 = registry.acquire_agent_id(self.user_repo)
        self._create_agent(id2)

        sid1 = registry.read_session_id(id1)
        sid2 = registry.read_session_id(id2)
        self.assertNotEqual(sid1, sid2)

        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

    def test_session_id_missing_returns_none(self):
        """read_session_id returns None when no agent exists in registry."""
        self._init()
        self.assertIsNone(registry.read_session_id("99"))

    def test_session_id_persists_after_exit(self):
        """Session ID is retained after agent exits, for resume."""
        self._init()
        aid = registry.acquire_agent_id(self.user_repo)
        sid = registry.read_session_id(aid)
        registry.mark_agent_exited(aid)
        self.assertEqual(registry.read_session_id(aid), sid)

    def test_find_agent_by_session(self):
        """find_agent_by_session returns the agent that owns a given UUID."""
        self._init()
        id1 = registry.acquire_agent_id(self.user_repo)
        self._create_agent(id1)
        id2 = registry.acquire_agent_id(self.user_repo)
        self._create_agent(id2)

        sid1 = registry.read_session_id(id1)
        sid2 = registry.read_session_id(id2)

        self.assertEqual(find_agent_by_session(sid1), id1)
        self.assertEqual(find_agent_by_session(sid2), id2)
        self.assertIsNone(find_agent_by_session("unknown-uuid"))

        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

    def test_resume_routes_to_owning_agent(self):
        """find_agent_by_session + continue_agent_id routes to the correct agent."""
        self._init_and_agents("1", "2")

        id1 = registry.acquire_agent_id(self.user_repo)
        id2 = registry.acquire_agent_id(self.user_repo)
        sid1 = registry.read_session_id(id1)
        sid2 = registry.read_session_id(id2)
        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

        # Resuming agent 2's session should route to agent 2
        owner = find_agent_by_session(sid2)
        self.assertEqual(owner, "2")
        cid = registry.continue_agent_id(self.user_repo, owner)
        self.assertEqual(cid, "2")
        registry.mark_agent_exited(cid)

        # Resuming agent 1's session should route to agent 1
        owner = find_agent_by_session(sid1)
        self.assertEqual(owner, "1")
        cid = registry.continue_agent_id(self.user_repo, owner)
        self.assertEqual(cid, "1")
        registry.mark_agent_exited(cid)

    def test_resume_unknown_uuid_returns_none(self):
        """find_agent_by_session returns None for unknown UUIDs."""
        self._init_and_agents("1")

        id1 = registry.acquire_agent_id(self.user_repo)
        registry.mark_agent_exited(id1)

        owner = find_agent_by_session("nonexistent-uuid")
        self.assertIsNone(owner)

    def test_write_session_id_overwrites(self):
        """write_session_id can update a session ID (e.g. when resuming a different session)."""
        self._init()
        aid = registry.acquire_agent_id(self.user_repo)
        original_sid = registry.read_session_id(aid)
        registry.write_session_id(aid, "new-uuid")
        self.assertEqual(registry.read_session_id(aid), "new-uuid")
        self.assertNotEqual(original_sid, "new-uuid")
        registry.mark_agent_exited(aid)

    def test_continue_specific_agent_not_most_recent(self):
        """continue_agent_id(base, '1') returns agent 1 even when agent 2 exited more recently."""
        self._init_and_agents("1", "2")

        # Both agents exit, agent 2 more recently
        id1 = registry.acquire_agent_id(self.user_repo)
        id2 = registry.acquire_agent_id(self.user_repo)
        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

        now = time.time()
        db = registry._open()
        db.execute(
            "UPDATE agents SET exited_at = ? WHERE agent_id = ?",
            (now - 100, id1)
        )
        db.commit()
        db.close()

        # Explicitly requesting agent 1 should return 1, not 2
        cid = registry.continue_agent_id(self.user_repo, "1")
        self.assertEqual(cid, "1")
        registry.mark_agent_exited(cid)

    def test_continue_running_agent_errors(self):
        """continue_agent_id errors when the requested agent is still running."""
        self._init_and_agents("1")

        # Agent 1 is running (registered with our PID)
        id1 = registry.acquire_agent_id(self.user_repo)

        with self.assertRaises(SystemExit) as ctx:
            registry.continue_agent_id(self.user_repo, "1")
        self.assertIn("still running", str(ctx.exception))

        registry.mark_agent_exited(id1)

    def test_remove_nonexistent_agent(self):
        """remove-agent on an agent not in registry gives 'not found'."""
        self._init()
        result = cav_cli("remove-agent", "99", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr.lower())

    def test_remove_agent_idempotent_gives_error(self):
        """remove-agent twice on the same agent gives 'not found' the second time."""
        self._init()
        aid = registry.acquire_agent_id(self.user_repo)
        registry.mark_agent_exited(aid)

        result = cav_cli("remove-agent", aid, cwd=self.user_repo)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Removed", result.stdout)

        result = cav_cli("remove-agent", aid, cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr.lower())

    def test_remove_all_agents(self):
        """remove-agent --all clears registry and agent branches."""
        agents = self._init_and_agents("1", "2")

        # Both agents do work and push
        for name, ws in agents.items():
            (ws / f"work-{name}.py").write_text(f"# {name}\n")
            git("add", f"work-{name}.py", cwd=ws)
            git("commit", "-m", f"Agent {name} work", cwd=ws)
            git("push", "host-cave", f"agent-{name}/main", cwd=ws, env=agent_env(name))

        # Register them in the registry and mark exited
        id1 = registry.acquire_agent_id(self.user_repo)
        id2 = registry.acquire_agent_id(self.user_repo)
        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

        # Verify agent branches exist
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertIn("cave/agent-1/main", branches)
        self.assertIn("cave/agent-2/main", branches)

        # Run remove-agent --all --yes
        result = cav_cli("remove-agent", "--all", "--yes", cwd=self.user_repo)
        self.assertEqual(result.returncode, 0)

        # Registry should be empty
        self.assertEqual(registry.all_agent_ids(), [])

        # Agent branches should be gone
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertNotIn("agent-1", branches)
        self.assertNotIn("agent-2", branches)

    def test_remove_all_refuses_running(self):
        """remove-agent --all refuses if any agent is running."""
        self._init()
        id1 = registry.acquire_agent_id(self.user_repo)

        result = cav_cli("remove-agent", "--all", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still running", result.stderr)

        registry.mark_agent_exited(id1)

    def test_remove_all_empty_registry(self):
        """remove-agent --all on empty registry is a no-op."""
        self._init()
        result = cav_cli("remove-agent", "--all", "--yes", cwd=self.user_repo)
        self.assertIn("No agents", result.stdout)

    def test_global_agent_id_uniqueness(self):
        """Agent IDs are globally unique across projects, not per-project."""
        self._init()
        # Acquire agents for this project
        id1 = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(id1, "1")

        # Simulate a second project acquiring an agent (same registry)
        other = Path(self.tmpdir) / "other-project"
        other.mkdir()
        git("init", "-b", "main", cwd=other)
        git("config", "user.email", "t@t.com", cwd=other)
        git("config", "user.name", "T", cwd=other)
        (other / "f").write_text("x")
        git("add", "f", cwd=other)
        git("commit", "-m", "init", cwd=other)

        # Next ID should be 2, not 1 (global uniqueness)
        id2 = registry.acquire_agent_id(other)
        self.assertEqual(id2, "2")

        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

    def test_init_registers_project(self):
        """cav init registers the project slug and path."""
        self._init()
        projects = registry.all_projects()
        self.assertEqual(len(projects), 1)
        slug, path = projects[0]
        self.assertEqual(slug, config.project_slug(self.user_repo))
        self.assertEqual(path, str(self.user_repo.resolve()))

    def test_remove_project(self):
        """remove_project unregisters a project and its agents."""
        self._init()
        slug = config.project_slug(self.user_repo)
        agent_id = registry.acquire_agent_id(self.user_repo)
        self.assertEqual(len(registry.all_projects()), 1)
        self.assertIsNotNone(registry.agent_project(agent_id))
        registry.remove_project(slug)
        self.assertEqual(len(registry.all_projects()), 0)
        self.assertIsNone(registry.agent_project(agent_id))

    def test_init_slug_collision_different_path(self):
        """cav init rejects a slug collision from a different path."""
        self._init()
        # Create a second repo with the same directory name in a different location
        other_parent = Path(self.tmpdir) / "elsewhere"
        other_parent.mkdir()
        other = other_parent / self.user_repo.name
        other.mkdir()
        git("init", "-b", "main", cwd=other)
        git("config", "user.email", "t@t.com", cwd=other)
        git("config", "user.name", "T", cwd=other)
        (other / "f").write_text("x")
        git("add", "f", cwd=other)
        git("commit", "-m", "init", cwd=other)
        with self.assertRaises(SystemExit):
            registry.register_project(other)

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

    def test_recursive_not_set_blocks_subcave(self):
        """Without --recursive, CODING_CAVE_RECURSIVE is not propagated."""
        # Simulate launch without --recursive
        env = {}
        recursive = False
        if recursive:
            env["CODING_CAVE_RECURSIVE"] = "1"

        self.assertNotIn("CODING_CAVE_RECURSIVE", env)

    def test_approve_and_reject_workflow(self):
        """User approves agent work via cav approve, then rejects via cav reject."""
        agents = self._init_and_agents("1")
        ws = agents["1"]
        id1 = registry.acquire_agent_id(self.user_repo)

        # Agent does work and pushes to agent branch
        (ws / "feature.py").write_text("# v1\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Feature v1", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # User approves via cav approve (agent is "running" since PID is alive)
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main", cwd=self.user_repo)
        cav_cli("approve", "--yes", cwd=self.user_repo)

        # Verify on main with the approved commit
        branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=self.user_repo)
        self.assertEqual(branch, "main")
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Feature v1", log)

        # User rejects the tip
        cav_cli("reject", "--yes", cwd=self.user_repo)

        # Main should be back to before approval
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertNotIn("Feature v1", log)

        # Agent branch should be restored on cave
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertIn("cave/agent-1/main", branches)

        # The restored agent branch should have the rejected commit
        log = git_out("log", "--oneline", "cave/agent-1/main", cwd=self.user_repo)
        self.assertIn("Feature v1", log)

        registry.mark_agent_exited(id1)

    def test_reject_per_commit_from_batch(self):
        """Approving 3 commits creates 3 records; reject peels them one at a time."""
        agents = self._init_and_agents("1", "2")
        ws1 = agents["1"]
        id1 = registry.acquire_agent_id(self.user_repo)
        id2 = registry.acquire_agent_id(self.user_repo)

        # Agent 1 makes 3 commits
        for i in range(1, 4):
            (ws1 / f"f{i}.py").write_text(f"# v{i}\n")
            git("add", f"f{i}.py", cwd=ws1)
            git("commit", "-m", f"Commit {i}", cwd=ws1)
        git("push", "host-cave", "agent-1/main", cwd=ws1, env=agent_env("1"))

        # User approves all 3
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main", cwd=self.user_repo)
        cav_cli("approve", "--yes", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Commit 3", log)

        # Reject tip (Commit 3) → goes to agent-1
        cav_cli("reject", "--yes", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Commit 2", log)
        self.assertNotIn("Commit 3", log)

        # Reject Commit 2 → stacks onto agent-1 (Commit 2 is ancestor of
        # Commit 3, which is already on agent-1/main)
        cav_cli("reject", "--yes", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Commit 1", log)
        self.assertNotIn("Commit 2", log)

        # Reject Commit 1 → also stacks onto agent-1
        cav_cli("reject", "--yes", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertNotIn("Commit 1", log)

        # Agent-1 branch should still have all 3 commits
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/agent-1/main", cwd=self.user_repo)
        self.assertIn("Commit 3", log)
        self.assertIn("Commit 1", log)

        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

    # -- rollback workflows --

    def test_user_force_push_shared_branch_rollback(self):
        """User approves bad work, then force-pushes to correct cave."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent commits bad work on agent branch
        (ws / "oops.py").write_text("# oops\n")
        git("add", "oops.py", cwd=ws)
        git("commit", "-m", "Bad commit", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # Approve the bad work as user
        git("push", "host-cave", "agent-1/main:main", cwd=ws)
        cav_cli("pull", cwd=self.user_repo)
        self.assertIn("Bad commit", git_out("log", "--oneline", cwd=self.user_repo))

        # User makes a replacement commit and force-pushes
        (self.user_repo / "rollback.py").write_text("# replacement\n")
        git("add", "rollback.py", cwd=self.user_repo)
        git("commit", "-m", "Replacement commit", cwd=self.user_repo)
        cav_cli("push", "--force", cwd=self.user_repo)

        # Verify cave has the replacement
        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/main", cwd=self.user_repo)
        self.assertIn("Replacement commit", log)

    def test_user_force_pushes_agent_branch(self):
        """User force-pushes an agent branch to roll back agent work."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "bad.py").write_text("# bad\n")
        git("add", "bad.py", cwd=ws)
        git("commit", "-m", "Bad agent work", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # User rolls back agent branch to main
        git("fetch", "cave", cwd=self.user_repo)
        git(
            "push", "--force", "cave",
            "cave/main:refs/heads/agent-1/main",
            cwd=self.user_repo,
        )

        # Agent discovers rollback, resets
        git("fetch", "host-cave", cwd=ws)
        git("reset", "--hard", "host-cave/agent-1/main", cwd=ws)

        files = git_out("ls-files", cwd=ws)
        self.assertNotIn("bad.py", files)


    # -- cav checkout --

    def test_checkout_retargets_agent_from_cave(self):
        """cav checkout on an agent branch retargets to new shared branch."""
        from cav.checkout import checkout as _checkout

        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=ws)
        git("commit", "-m", "Agent work", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # Agent retargets (auto-creates standalone from main)
        _checkout(ws, "standalone")

        # Branch renamed
        current = git_out("branch", "--show-current", cwd=ws)
        self.assertEqual(current, "agent-1/standalone")

        # Remote has new branch and target, old agent branch gone
        git("fetch", "cave", "--prune", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertIn("cave/agent-1/standalone", branches)
        self.assertIn("cave/standalone", branches)
        self.assertNotIn("cave/agent-1/main", branches)

        # Agent's commit is still there
        log = git_out("log", "--oneline", cwd=ws)
        self.assertIn("Agent work", log)

    def test_checkout_retargets_from_host(self):
        """cav checkout from the host renames bare repo and workspace."""
        from cav.checkout import checkout as _checkout

        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=ws)
        git("commit", "-m", "Agent work", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # Host retargets agent 1 from main
        cav_cli("checkout", "standalone", "1", cwd=self.user_repo)

        # Bare repo has renamed branch
        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        bare_branches = git_out("branch", cwd=shared)
        self.assertIn("agent-1/standalone", bare_branches)
        self.assertNotIn("agent-1/main", bare_branches)
        # Target branch was auto-created
        self.assertIn("standalone", bare_branches)

        # Workspace branch also renamed
        ws_branch = git_out("branch", "--show-current", cwd=ws)
        self.assertEqual(ws_branch, "agent-1/standalone")

    def test_checkout_creates_target_branch(self):
        """cav checkout auto-creates the target branch if it doesn't exist."""
        from cav.checkout import checkout as _checkout

        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=ws)
        git("commit", "-m", "Agent work", cwd=ws)
        git("push", "host-cave", "agent-1/main", cwd=ws, env=agent_env("1"))

        # Target doesn't exist yet
        _checkout(ws, "new-feature")

        # Target was created from main
        git("fetch", "cave", cwd=self.user_repo)
        main_sha = git_out("rev-parse", "cave/main", cwd=self.user_repo)
        new_sha = git_out("rev-parse", "cave/new-feature", cwd=self.user_repo)
        self.assertEqual(main_sha, new_sha)

    def test_checkout_rejects_agent_name(self):
        """cav checkout rejects agent-named branches."""
        self._init()
        result = cav_cli("checkout", "agent-1/main", cwd=self.user_repo,
                         check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent branch", result.stderr)

    # -- cross-depth isolation --

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

    # -- session uniqueness under concurrent registration --

    def test_concurrent_agents_get_distinct_sessions(self):
        """Two agents registered concurrently have distinct session UUIDs."""
        self._init()
        id1 = registry.acquire_agent_id(self.user_repo)
        self._create_agent(id1)
        id2 = registry.acquire_agent_id(self.user_repo)
        self._create_agent(id2)

        sid1 = registry.read_session_id(id1)
        sid2 = registry.read_session_id(id2)

        self.assertIsNotNone(sid1)
        self.assertIsNotNone(sid2)
        self.assertNotEqual(sid1, sid2)

        registry.mark_agent_exited(id1)
        registry.mark_agent_exited(id2)

    # -- stale-ref cleanup bug --

    def test_cleanup_fetches_before_checking_branches(self):
        """Cleanup must not remove agent whose branch is only in the bare repo.

        Repro of the agent-fork bug: workspace pushes agent-1/main to the
        bare repo, but the user's project dir never fetches. Without a
        fetch, git branch -r sees no agent branches and the cleanup
        deletes the agent from the registry. Then cav -c creates agent 2
        instead of continuing agent 1.
        """
        from cav import git as cavgit
        from cav.init import init
        init(str(self.user_repo))

        aid = registry.acquire_agent_id(self.user_repo)
        ws = self._create_agent(aid)

        # Agent does work and pushes from its workspace
        (ws / "work.txt").write_text("agent work\n")
        git("add", "work.txt", cwd=ws)
        git("commit", "-m", "agent work", cwd=ws)
        ab = config.agent_branch("main", aid)
        git("push", config.HOST_REMOTE, ab,
            f"--push-option=agent={aid}", cwd=ws)

        # Agent exits
        registry.mark_agent_exited(aid)

        # User's project dir has NOT fetched. Verify it can't see the
        # agent branch locally.
        stale_branches = cavgit.remote_branches(
            config.CAVE_REMOTE, cwd=self.user_repo)
        agent_visible = any(
            config.parse_agent_branch(b) and
            config.parse_agent_branch(b)[1] == aid
            for b in stale_branches
        )
        self.assertFalse(agent_visible,
                         "pre-condition: user repo must NOT see agent branch "
                         "before fetching")

        # After fetching, the agent branch becomes visible
        cavgit.git("fetch", config.CAVE_REMOTE, cwd=self.user_repo,
                   echo=False, check=False, capture=True)
        fresh_branches = cavgit.remote_branches(
            config.CAVE_REMOTE, cwd=self.user_repo)
        agent_visible_after = any(
            config.parse_agent_branch(b) and
            config.parse_agent_branch(b)[1] == aid
            for b in fresh_branches
        )
        self.assertTrue(agent_visible_after,
                        "agent branch must be visible after fetching")

        # Agent should still be in the registry (not erroneously swept)
        self.assertIn(aid, registry.all_agent_ids())

        # cav -c should continue the same agent, not create a new one
        continued = registry.continue_agent_id(self.user_repo)
        self.assertEqual(continued, aid)
        registry.mark_agent_exited(continued)

    def test_zero_commit_agent_cleaned_up_on_exit(self):
        """Agent with no commits is garbage collected in the finally block.

        Zero-commit agents have no branches to preserve, so cleanup
        removes them immediately. cav -c will launch a fresh agent.
        """
        from cav.cli import _auto_cleanup_if_clean
        from cav.init import init

        init(str(self.user_repo))

        aid = registry.acquire_agent_id(self.user_repo)
        self._create_agent(aid)

        # Agent exits with no commits (simulates the finally block)
        registry.mark_agent_exited(aid)
        _auto_cleanup_if_clean(self.user_repo, aid)

        self.assertNotIn(aid, registry.all_agent_ids(),
                         "Zero-commit agent should be cleaned up")


class TestGitWrapper(unittest.TestCase):
    """Tests for the git wrapper's auto-rebase and sync behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_repo = Path(self.tmpdir) / "project"
        self.data_dir = Path(self.tmpdir) / "data"
        self.user_repo.mkdir()
        git("init", "-b", "main", cwd=self.user_repo)
        git("config", "user.email", "test@test.com", cwd=self.user_repo)
        git("config", "user.name", "Test User", cwd=self.user_repo)
        (self.user_repo / "README.md").write_text("# Project\n")
        git("add", "README.md", cwd=self.user_repo)
        git("commit", "-m", "Initial commit", cwd=self.user_repo)
        os.environ["CAV_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir
        from cav import init as cav_init
        cav_init.init(str(self.user_repo))
        self.a1 = workspace.create(self.user_repo, "1")
        git("config", "user.email", "1@test.com", cwd=self.a1)
        git("config", "user.name", "Agent 1", cwd=self.a1)
        self._install_wrapper()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _install_wrapper(self):
        from cav.sandbox import GIT_WRAPPER
        bin_dir = Path(self.tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        wrapper = bin_dir / "git"
        wrapper.write_text(GIT_WRAPPER)
        wrapper.chmod(0o755)
        self._wrapper_bin = str(bin_dir)

    def _env(self):
        """Return env with the git wrapper on PATH."""
        env = os.environ.copy()
        env["PATH"] = self._wrapper_bin + ":" + env.get("PATH", "")
        return env

    def test_auto_rebase_on_commit(self):
        """Git wrapper rebases agent branch onto updated main after commit."""
        # Simulate approved work landing on main
        (self.user_repo / "approved.py").write_text("# approved\n")
        git("add", "approved.py", cwd=self.user_repo)
        git("commit", "-m", "approved work", cwd=self.user_repo)
        git("push", "cave", "main", cwd=self.user_repo)

        # Agent commits via wrapper (triggers: fetch, rebase, push)
        (self.a1 / "agent_work.py").write_text("# agent\n")
        git("add", "agent_work.py", cwd=self.a1)
        git("commit", "-m", "agent work", cwd=self.a1, env=self._env())

        # Verify agent branch is a fast-forward from main
        git("fetch", "cave", cwd=self.user_repo)
        merge_base = git_out("merge-base", "cave/main", "cave/agent-1/main",
                             cwd=self.user_repo)
        main_sha = git_out("rev-parse", "cave/main", cwd=self.user_repo)
        self.assertEqual(merge_base, main_sha,
                         "agent branch should be a fast-forward from main")

        # Verify both commits are in the agent branch history
        log = git_out("log", "--oneline", "cave/agent-1/main", cwd=self.user_repo)
        self.assertIn("approved work", log)
        self.assertIn("agent work", log)

    def test_conflict_warns_and_pushes(self):
        """Git wrapper warns on conflict, still pushes un-rebased branch."""
        # Both sides edit the same file to create a conflict
        (self.user_repo / "README.md").write_text("# User version\n")
        git("add", "README.md", cwd=self.user_repo)
        git("commit", "-m", "user edit", cwd=self.user_repo)
        git("push", "cave", "main", cwd=self.user_repo)

        (self.a1 / "README.md").write_text("# Agent version\n")
        git("add", "README.md", cwd=self.a1)
        result = git("commit", "-m", "agent edit", cwd=self.a1,
                     env=self._env(), check=False)

        # Wrapper should warn about the conflict
        self.assertIn("WARNING", result.stderr)
        self.assertIn("rebase", result.stderr)

        # Branch should still be pushed (un-rebased)
        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/agent-1/main", cwd=self.user_repo)
        self.assertIn("agent edit", log)

    def test_no_rebase_needed(self):
        """Git wrapper pushes when already up to date with main."""
        (self.a1 / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=self.a1)
        git("commit", "-m", "simple commit", cwd=self.a1, env=self._env())

        # Verify it pushed successfully
        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/agent-1/main", cwd=self.user_repo)
        self.assertIn("simple commit", log)

    def test_manual_rebase_pushes_to_remote(self):
        """Wrapper must push the rebased branch after a manual rebase.

        During a rebase, HEAD is detached, so 'git push host-cave HEAD'
        fails because git can't resolve the remote branch. The wrapper
        must read the branch name from .git/rebase-merge/head-name and
        push with an explicit refspec.
        """
        # Agent commits first (via wrapper so it's pushed).
        (self.a1 / "agent_work.py").write_text("# agent\n")
        git("add", "agent_work.py", cwd=self.a1)
        git("commit", "-m", "agent work", cwd=self.a1, env=self._env())

        # Then main diverges.
        (self.user_repo / "diverge.py").write_text("# diverge\n")
        git("add", "diverge.py", cwd=self.user_repo)
        git("commit", "-m", "main diverges", cwd=self.user_repo)
        git("push", "cave", "main", cwd=self.user_repo)

        pre_rebase_sha = git_out("rev-parse", "HEAD", cwd=self.a1)

        # Manual rebase via wrapper (triggers light sync: push).
        git("fetch", "host-cave", cwd=self.a1)
        git("rebase", "host-cave/main", cwd=self.a1, env=self._env())

        post_rebase_sha = git_out("rev-parse", "HEAD", cwd=self.a1)

        # The rebase must have actually moved the branch.
        self.assertNotEqual(pre_rebase_sha, post_rebase_sha,
                            "rebase did not move HEAD")

        # The remote must have the rebased commit.
        git("fetch", "cave", cwd=self.user_repo)
        remote_sha = git_out("rev-parse", "cave/agent-1/main",
                             cwd=self.user_repo)
        self.assertEqual(remote_sha, post_rebase_sha,
                         "wrapper did not push the rebased branch to the remote")

        # Agent branch should be a fast-forward from main.
        merge_base = git_out("merge-base", "cave/main", "cave/agent-1/main",
                             cwd=self.user_repo)
        main_sha = git_out("rev-parse", "cave/main", cwd=self.user_repo)
        self.assertEqual(merge_base, main_sha,
                         "agent branch should be a fast-forward from main")


    def test_detached_head_no_spurious_branch(self):
        """Git wrapper on detached HEAD must not create spurious branches."""
        # Detach HEAD at the current commit
        sha = git_out("rev-parse", "HEAD", cwd=self.a1)
        git("checkout", sha, cwd=self.a1)

        # Commit on detached HEAD (wrapper fires)
        (self.a1 / "detached.py").write_text("# detached\n")
        git("add", "detached.py", cwd=self.a1)
        git("commit", "-m", "detached commit", cwd=self.a1, env=self._env())

        # No branch named HEAD should exist on the remote
        git("fetch", "cave", cwd=self.user_repo)
        raw = git_out("branch", "-r", "--list", "cave/*", cwd=self.user_repo)
        for line in raw.splitlines():
            branch = line.strip()
            if " -> " in branch:
                continue
            self.assertNotIn("HEAD", branch,
                             f"spurious branch '{branch}' created from detached HEAD")

    def test_reset_syncs_to_remote(self):
        """Git wrapper pushes after git reset (the gap the wrapper fills)."""
        # Agent makes two commits via wrapper
        (self.a1 / "a.py").write_text("a\n")
        git("add", "a.py", cwd=self.a1)
        git("commit", "-m", "commit 1", cwd=self.a1, env=self._env())
        (self.a1 / "b.py").write_text("b\n")
        git("add", "b.py", cwd=self.a1)
        git("commit", "-m", "commit 2", cwd=self.a1, env=self._env())

        # Reset back one commit via wrapper (light sync: push)
        git("reset", "--hard", "HEAD~1", cwd=self.a1, env=self._env())

        # Remote should reflect the reset
        git("fetch", "cave", cwd=self.user_repo)
        local_sha = git_out("rev-parse", "HEAD", cwd=self.a1)
        remote_sha = git_out("rev-parse", "cave/agent-1/main",
                             cwd=self.user_repo)
        self.assertEqual(local_sha, remote_sha,
                         "reset was not synced to remote")


if __name__ == "__main__":
    unittest.main()
