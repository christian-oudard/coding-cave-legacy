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
from cav.constants import CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR
from tests import find_agent_by_session


def _clean_env():
    """Base environment for test subprocesses.

    Strip CODING_CAVE_* vars (except DATA_DIR) and the cave's ~/bin
    (which contains a git wrapper) so test repos behave as host-side
    repos even when tests run inside a cave.
    """
    cave_bin = str(Path.home() / "bin")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CODING_CAVE_") or k == "CODING_CAVE_DATA_DIR"}
    env["PATH"] = ":".join(
        p for p in env.get("PATH", "").split(":") if p != cave_bin)
    return env


def run(cmd, cwd=None, check=True, env=None):
    """Run a command, return CompletedProcess."""
    merged_env = {**_clean_env(), **(env or {})}
    return subprocess.run(
        cmd, cwd=cwd, check=check,
        capture_output=True, text=True,
        env=merged_env,
    )


def git(*args, cwd=None, check=True, env=None):
    return run(["git"] + list(args), cwd=cwd, check=check, env=env)


def agent_env(agent_id):
    """Env dict that identifies pushes as coming from an agent.

    CODING_CAVE_AGENT_ID is read by the pre-receive hook (server side)
    to enforce per-agent branch ownership. CODING_CAVE_AGENT_NAME is
    read by the cave's git wrapper (client side) to know which
    agent-<id>/<base> ref to publish to upstream.
    """
    return {
        "CODING_CAVE_AGENT_ID": agent_id,
        "CODING_CAVE_AGENT_NAME": f"coding-cave@{agent_id}",
    }


def git_out(*args, cwd=None):
    return git(*args, cwd=cwd).stdout.strip()


def cav_cli(*args, cwd=None, check=True):
    """Run cav CLI via python -m."""
    return run(
        [sys.executable, "-m", "cav.cli"] + list(args),
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

        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _init(self):
        """Initialize Coding Cave in the user repo."""
        cav_cli("init", str(self.user_repo), cwd=self.user_repo)

    def _create_agent(self, name):
        """Create agent workspace, configure git identity. Returns workspace path."""
        ws, _ = workspace.create(self.user_repo, name)
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
        """Plain cav init on an already-init'd project is a no-op."""
        cav_cli("init", str(self.user_repo), cwd=self.user_repo)
        shared = self.user_repo / ".coding-cave" / "cave-shared.git"

        # Delete post-receive to simulate old install
        (shared / "hooks" / "post-receive").unlink()
        self.assertFalse((shared / "hooks" / "post-receive").exists())

        # Plain re-init does nothing: hooks are not touched.
        result = cav_cli("init", str(self.user_repo), cwd=self.user_repo)
        self.assertIn("already initialized", result.stdout)
        self.assertFalse((shared / "hooks" / "post-receive").exists())

    def test_init_reinitialize_updates_hooks(self):
        """cav init --reinitialize refreshes hooks on an existing project."""
        cav_cli("init", str(self.user_repo), cwd=self.user_repo)
        shared = self.user_repo / ".coding-cave" / "cave-shared.git"

        (shared / "hooks" / "post-receive").unlink()
        self.assertFalse((shared / "hooks" / "post-receive").exists())

        result = cav_cli("init", "--reinitialize", str(self.user_repo),
                         cwd=self.user_repo)
        self.assertIn("reinitialized", result.stdout)
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
            # Inside the workspace, the local branch is the base name;
            # agent-<id>/<base> is the upstream name (set via push refspec).
            self.assertEqual(branch, "main")

    def test_workspace_from_detached_at_branch_tip(self):
        """Workspace creation works when detached at a unique branch tip."""
        from cav.init import init
        init(str(self.user_repo))

        head = git_out("rev-parse", "HEAD", cwd=self.user_repo)
        git("checkout", head, cwd=self.user_repo)

        ws, _ = workspace.create(self.user_repo, "1")
        self.assertTrue(ws.exists())

    def test_workspace_rejects_unresolvable_detached_head(self):
        """Workspace from detached HEAD far from any branch must fail."""
        from cav.init import init
        init(str(self.user_repo))

        git("checkout", "--detach", cwd=self.user_repo)
        git("commit", "--allow-empty", "-m", "a", cwd=self.user_repo)
        git("commit", "--allow-empty", "-m", "b", cwd=self.user_repo)

        with self.assertRaises(SystemExit):
            workspace.create(self.user_repo, "1")

    def test_workspace_branch_sync_sets_tracking(self):
        """branch -f in workspace sync must set host-cave tracking for new branches.

        The sandbox syncs base branches with:
            git branch -f <branch> host-cave/<branch>
        This must NOT use --no-track. The clone only sets up tracking for
        branches that existed at clone time. If a new base branch (e.g.
        develop) appears on host-cave later, the sync loop creates it locally,
        and git's autoSetupMerge gives it host-cave tracking. With --no-track,
        the new branch would have no upstream and git pull would fail.
        """
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Create a new base branch on shared remote (after workspace already exists)
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
        env = {
            "PATH": f"{bin_dir}:{_clean_env()['PATH']}",
            "CODING_CAVE_AGENT_NAME": "coding-cave@1",
        }

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
        """Launching from a dev branch targets the underlying base branch."""
        self._init()
        git("checkout", "-b", "agent-1/main", cwd=self.user_repo)
        ws, _ = workspace.create(self.user_repo, "2")
        # Local branch in the workspace is the resolved base (main),
        # not the host's agent-1/main nor an agent-2/ prefixed name.
        branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=ws)
        self.assertEqual(branch, "main")

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

        # Push dev branch, then approve as user
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))
        git("push", "host-cave", "main:main", cwd=ws)

        git("fetch", "cave", cwd=self.user_repo)
        git("merge", "--ff-only", "cave/main", cwd=self.user_repo)
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

        # Push to dev branch
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Approve as user
        git("push", "host-cave", "main:main", cwd=ws)

        # Agent branch should still exist on remote
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertIn("cave/agent-1/main", branches)

    def test_pull_refuses_overwriting_local_commits(self):
        """cav pull refuses to overwrite local commits without --force."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent makes a commit and pushes the dev branch
        (ws / "wip.py").write_text("# v1\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP v1", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # User checks out the dev branch
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main", cwd=self.user_repo)

        # User makes a local commit
        (self.user_repo / "local.py").write_text("# local\n")
        git("add", "local.py", cwd=self.user_repo)
        git("commit", "-m", "Local change", cwd=self.user_repo)

        # Agent amends and force-pushes (rewrites history)
        (ws / "wip.py").write_text("# v2\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "--amend", "-m", "WIP v2", cwd=ws)
        git("push", "--force", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # cav pull without --force should refuse
        r = cav_cli("pull", cwd=self.user_repo, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Local change", r.stderr)
        self.assertIn("--force", r.stderr)

        # Local commit should still be there
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Local change", log)

        # cav pull --force should overwrite
        cav_cli("pull", "--force", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("WIP v2", log)
        self.assertNotIn("Local change", log)
        self.assertNotIn("WIP v1", log)

        content = (self.user_repo / "wip.py").read_text()
        self.assertEqual(content, "# v2\n")
        self.assertFalse((self.user_repo / "local.py").exists())

    def test_pull_accepts_agent_rewrite_without_force(self):
        """cav pull accepts agent history rewrites when no user commits exist."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent makes a commit and pushes
        (ws / "wip.py").write_text("# v1\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP v1", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # User pulls
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main",
            cwd=self.user_repo)

        # Agent amends and force-pushes
        (ws / "wip.py").write_text("# v2\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "--amend", "-m", "WIP v2", cwd=ws)
        git("push", "--force", "host-cave", "main:agent-1/main",
            cwd=ws, env=agent_env("1"))

        # cav pull should succeed without --force
        cav_cli("pull", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("WIP v2", log)
        self.assertNotIn("WIP v1", log)

        content = (self.user_repo / "wip.py").read_text()
        self.assertEqual(content, "# v2\n")

    def test_agent_force_push_rejects_over_user_commits(self):
        """Agent force-push must not clobber user commits on agent branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent pushes initial commit
        (ws / "wip.py").write_text("# v1\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP v1", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # User adds a commit on the agent branch and pushes to shared remote
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main",
            cwd=self.user_repo)
        (self.user_repo / "user.py").write_text("# user fix\n")
        git("add", "user.py", cwd=self.user_repo)
        git("commit", "-m", "User fix", cwd=self.user_repo)
        git("push", "cave", "agent-1/main", cwd=self.user_repo)

        # Agent amends and tries to force-push (should be rejected)
        (ws / "wip.py").write_text("# v2\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "--amend", "-m", "WIP v2", cwd=ws)
        r = git("push", "--force", "host-cave", "main:agent-1/main",
                cwd=ws, env=agent_env("1"), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REJECTED", r.stderr)

    def test_agent_can_amend_after_cav_push(self):
        """Agent can amend after cav push resets its worktree."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent pushes initial commit
        (ws / "wip.py").write_text("# v1\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP v1", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # User pushes to the dev branch (resets agent worktree)
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main",
            cwd=self.user_repo)
        (self.user_repo / "fix.py").write_text("# user fix\n")
        git("add", "fix.py", cwd=self.user_repo)
        git("commit", "-m", "User fix", cwd=self.user_repo)
        cav_cli("push", cwd=self.user_repo)

        # Agent should be able to amend (force-push) from the new state
        (ws / "fix.py").write_text("# improved fix\n")
        git("add", "fix.py", cwd=ws)
        git("commit", "--amend", "-m", "Improved fix", cwd=ws)
        r = git("push", "--force", "host-cave", "main:agent-1/main",
                cwd=ws, env=agent_env("1"), check=False)
        self.assertEqual(r.returncode, 0)

    def test_push_dev_preserves_agent_uncommitted_work(self):
        """cav push to dev branch preserves agent's uncommitted changes."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent pushes initial commit
        (ws / "feature.py").write_text("# v1\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Initial feature", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Agent starts working on something (uncommitted)
        (ws / "wip.py").write_text("# work in progress\n")
        git("add", "wip.py", cwd=ws)

        # User pushes a non-conflicting commit
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main",
            cwd=self.user_repo)
        (self.user_repo / "fix.py").write_text("# user fix\n")
        git("add", "fix.py", cwd=self.user_repo)
        git("commit", "-m", "User fix", cwd=self.user_repo)
        cav_cli("push", cwd=self.user_repo)

        # Agent's uncommitted work should still be there
        self.assertTrue((ws / "wip.py").exists())
        self.assertEqual((ws / "wip.py").read_text(), "# work in progress\n")

        # User's commit should be in the agent's history
        log = git_out("log", "--oneline", cwd=ws)
        self.assertIn("User fix", log)

    def test_pull_agent_branch_preserves_main_upstream(self):
        """cav pull on dev branch must not hijack main's upstream to shared remote."""
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
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # User checks out dev branch and pulls
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main",
            cwd=self.user_repo)
        cav_cli("pull", cwd=self.user_repo)

        # main must still track origin, not cave
        remote = git_out("config", "branch.main.remote", cwd=self.user_repo)
        self.assertEqual(remote, "origin")

    def test_nested_agent_branch_resolves_to_shared_target(self):
        """Nested dev branch agent-2/agent-1/main resolves base to main."""
        agents = self._init_and_agents("1", "2")
        # Register so status can see them
        for name in agents:
            registry.acquire_agent_id(self.user_repo)
        ws1 = agents["1"]

        # Agent 1 pushes work on their branch
        (ws1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=ws1)
        git("commit", "-m", "Agent 1 work", cwd=ws1)
        git("push", "host-cave", "main:agent-1/main", cwd=ws1, env=agent_env("1"))

        # Agent 2 creates a nested branch (as if spawned inside agent 1's workspace)
        ws2 = agents["2"]
        git("checkout", "-b", "agent-2/agent-1/main", cwd=ws2)
        (ws2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=ws2)
        git("commit", "-m", "Agent 2 nested work", cwd=ws2)
        git("push", "host-cave", "agent-2/agent-1/main", cwd=ws2, env=agent_env("2"))

        # Also approve agent 1's work to main so we can test the delta
        git("push", "host-cave", "main:main", cwd=ws1)

        # Status should compare agent-2's branch against main (not agent-1/main)
        result = cav_cli("status", cwd=self.user_repo)
        # Only agent 2's own commit should be unapproved, not agent 1's
        self.assertIn("1 commit on agent-2/agent-1/main", result.stdout)
        self.assertIn("Agent 2 nested work", result.stdout)

        # Pull on the nested branch should reset to remote and show
        # commits relative to main
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-2/agent-1/main",
            "cave/agent-2/agent-1/main", cwd=self.user_repo)
        result = cav_cli("pull", cwd=self.user_repo, check=False)
        self.assertIn("Already up to date", result.stdout)

    def test_approve_preserves_agent_branch_in_bare(self):
        """After approving to main, the dev branch still exists in bare repo."""
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
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))
        git("push", "host-cave", "main:main", cwd=ws)

        # Agent branch should still exist after approval
        refs_after = git_out("branch", cwd=shared)
        self.assertIn("agent-1/main", refs_after)

        # Work should be on main
        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/main", cwd=self.user_repo)
        self.assertIn("Add feature", log)

    # -- merge + push --

    def test_user_pulls_approved_work(self):
        """User approves agent work to main, then fetches it."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))
        git("push", "host-cave", "main:main", cwd=ws)

        git("fetch", "cave", cwd=self.user_repo)
        git("merge", "--ff-only", "cave/main", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Add feature", log)

    # -- hook enforcement --

    def test_agent_push_shared_rejected(self):
        """Agent cannot push to base branches at all."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "fix.py").write_text("# fix\n")
        git("add", "fix.py", cwd=ws)
        git("commit", "-m", "Quick fix", cwd=ws)

        # Explicit refspec bypasses the workspace's push refspec mapping,
        # exercising the pre-receive hook directly.
        result = git("push", "host-cave", "main:main", cwd=ws,
                     check=False, env=agent_env("1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)
        self.assertIn("cannot push", result.stderr)

    def test_agent_force_push_shared_rejected(self):
        """Agent force-push to base branch is rejected."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "bad.py").write_text("# bad\n")
        git("add", "bad.py", cwd=ws)
        git("commit", "--amend", "-m", "Rewritten commit", cwd=ws)

        result = git("push", "--force", "host-cave", "main:main", cwd=ws,
                     check=False, env=agent_env("1"))
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
        """Agent push --delete on a base branch is rejected."""
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
        result = git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

        (ws / "wip.py").write_text("# wip v2\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "--amend", "-m", "WIP v2", cwd=ws)
        result = git("push", "--force", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))
        self.assertEqual(result.returncode, 0)

    # -- multi-agent --

    def test_multi_agent_approve_and_pull(self):
        """Two agents' work approved to main, pull shows all commits."""
        agents = self._init_and_agents("1", "2")

        # Agent 1 commits and pushes dev branch
        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 work", cwd=a1)
        git("push", "host-cave", "main:agent-1/main", cwd=a1, env=agent_env("1"))

        # Approve agent 1 as user
        git("push", "host-cave", "main:main", cwd=a1)

        # Agent 2 rebases on new main and pushes
        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", cwd=a2)
        git("push", "--force", "host-cave", "main:agent-2/main", cwd=a2, env=agent_env("2"))

        # Approve agent 2 as user
        git("push", "host-cave", "main:main", cwd=a2)

        git("fetch", "cave", cwd=self.user_repo)
        git("merge", "--ff-only", "cave/main", cwd=self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

    def test_approve_contention(self):
        """cav approve fails when dev branch is not FF from updated main."""
        agents = self._init_and_agents("1", "2")

        a1 = agents["1"]
        (a1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=a1)
        git("commit", "-m", "Agent 1 work", cwd=a1)
        git("push", "host-cave", "main:agent-1/main", cwd=a1, env=agent_env("1"))

        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("push", "host-cave", "main:agent-2/main", cwd=a2, env=agent_env("2"))

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
        git("push", "--force", "host-cave", "main:agent-2/main", cwd=a2, env=agent_env("2"))

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
        """cav approve <id> approves from main without checking out dev branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]
        id1 = registry.acquire_agent_id(self.user_repo)

        (ws / "feature.py").write_text("# feature\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Agent 1 feature", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

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
        git("push", "host-cave", "main:agent-1/main", cwd=a1, env=agent_env("1"))

        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("push", "host-cave", "main:agent-2/main", cwd=a2, env=agent_env("2"))

        # Approve agent 1 from main by ID
        cav_cli("approve", "1", "--yes", cwd=self.user_repo)

        # Agent 2 should fail (not FF)
        result = cav_cli("approve", "2", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a fast-forward", result.stderr)

        # Agent 2 rebases and retries
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", cwd=a2)
        git("push", "--force", "host-cave", "main:agent-2/main", cwd=a2, env=agent_env("2"))

        cav_cli("approve", "2", "--yes", cwd=self.user_repo)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

    def test_approve_no_arg_no_agents_fails(self):
        """cav approve without agent-id fails when no agent branches exist."""
        self._init()
        result = cav_cli("approve", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No agent branches in", result.stderr)

    def test_approve_nonexistent_agent(self):
        """cav approve with wrong agent-id fails cleanly."""
        self._init()
        result = cav_cli("approve", "99", "--yes", cwd=self.user_repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No commits to approve", result.stderr)

    def test_approve_succeeds_when_local_ahead(self):
        """cav approve pushes to shared remote even when local main is ahead."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent does work
        (ws / "feature.py").write_text("# feature\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Agent 1 feature", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

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
        git("push", "host-cave", "main:agent-1/main", cwd=a1, env=agent_env("1"))
        cav_cli("approve", "1", "--yes", cwd=self.user_repo)

        # Reset local main behind cave/main (simulate not having pulled)
        git("reset", "--hard", "HEAD~1", cwd=self.user_repo)

        # Agent 2 work (rebased on new cave/main)
        a2 = agents["2"]
        (a2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=a2)
        git("commit", "-m", "Agent 2 work", cwd=a2)
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", cwd=a2)
        git("push", "--force", "host-cave", "main:agent-2/main", cwd=a2, env=agent_env("2"))

        # Approve agent 2 from local main (which is behind cave/main)
        result = cav_cli("approve", "2", "--yes", cwd=self.user_repo)

        # Should succeed and local catches up
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

        registry.mark_agent_exited(id1)

    def test_approve_succeeds_with_dirty_worktree(self):
        """cav approve pushes to shared remote even when local worktree has conflicts."""
        agents = self._init_and_agents("1")

        a1 = agents["1"]
        (a1 / "feature.py").write_text("# feature\n")
        git("add", "feature.py", cwd=a1)
        git("commit", "-m", "Agent 1 feature", cwd=a1)
        git("push", "host-cave", "main:agent-1/main", cwd=a1, env=agent_env("1"))

        # Create a dirty worktree conflict on the base branch
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
        git("push", "host-cave", "main:agent-1/main", cwd=a1, env=agent_env("1"))
        git("push", "host-cave", "main:main", cwd=a1)

        # Approve agent 2's commit
        a2 = agents["2"]
        (a2 / "api.py").write_text("# api\n")
        git("add", "api.py", cwd=a2)
        git("commit", "-m", "agent 2 commit", cwd=a2)
        git("fetch", "host-cave", cwd=a2)
        git("rebase", "host-cave/main", cwd=a2)
        git("push", "--force", "host-cave", "main:agent-2/main", cwd=a2, env=agent_env("2"))
        git("push", "host-cave", "main:main", cwd=a2)

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
        git("push", "host-cave", "main:agent-1/main", cwd=a1, env=agent_env("1"))

        # Agent 1 also has a second branch (e.g. from red team or experimental).
        # On the wire it lives under their agent-1/ prefix.
        git("checkout", "-b", "experiment", cwd=a1)
        git("push", "host-cave", "experiment:agent-1/experiment",
            cwd=a1, env=agent_env("1"))
        git("checkout", "main", cwd=a1)

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

        # Agent 1 does work and pushes to dev branch
        (ws / "wip.py").write_text("work in progress\n")
        git("add", "wip.py", cwd=ws)
        git("commit", "-m", "WIP", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

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

    def test_continue_no_agents_no_sessions_fails(self):
        """resolve_continue with nothing to continue raises SystemExit."""
        self._init()
        with self.assertRaises(SystemExit):
            registry.resolve_continue(self.user_repo)

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

    def test_version_and_lineage_propagation(self):
        """Verify version is set and lineage chains across 3 levels of nesting."""
        # Simulate the env computation from sandbox.py launch() at each level

        def compute_env(parent_env, agent_name):
            """Replicate the version/lineage logic from sandbox.py."""
            env = {
                **parent_env,
                "CODING_CAVE_VERSION": "test",
                "CODING_CAVE_AGENT_NAME": agent_name,
                "CODING_CAVE_HOST_HOME": parent_env.get(
                    "CODING_CAVE_HOST_HOME", "/home/testuser"),
            }
            parent_lineage = parent_env.get("CODING_CAVE_LINEAGE", "")
            parent_agent = parent_env.get("CODING_CAVE_AGENT_NAME", "")
            if parent_agent:
                lineage = f"{parent_lineage}.{parent_agent}" if parent_lineage else parent_agent
                env["CODING_CAVE_LINEAGE"] = lineage
            return env

        # Level 0: host
        host_env = {"CODING_CAVE_HOST_HOME": "/home/testuser"}

        # Level 1: first cave
        l1 = compute_env(host_env, "project@1")
        self.assertEqual(l1["CODING_CAVE_VERSION"], "test")
        self.assertNotIn("CODING_CAVE_LINEAGE", l1)  # no parent agent at host
        self.assertEqual(l1["CODING_CAVE_HOST_HOME"], "/home/testuser")

        # Level 2: subcave
        l2 = compute_env(l1, "project@2")
        self.assertEqual(l2["CODING_CAVE_VERSION"], "test")
        self.assertEqual(l2["CODING_CAVE_LINEAGE"], "project@1")
        self.assertEqual(l2["CODING_CAVE_HOST_HOME"], "/home/testuser")

        # Level 3: sub-subcave
        l3 = compute_env(l2, "project@1")
        self.assertEqual(l3["CODING_CAVE_VERSION"], "test")
        self.assertEqual(l3["CODING_CAVE_LINEAGE"], "project@1.project@2")
        self.assertEqual(l3["CODING_CAVE_HOST_HOME"], "/home/testuser")

        # Level 4: verify it keeps working
        l4 = compute_env(l3, "project@3")
        self.assertEqual(l4["CODING_CAVE_VERSION"], "test")
        self.assertEqual(l4["CODING_CAVE_LINEAGE"], "project@1.project@2.project@1")

    def test_approve_and_reject_workflow(self):
        """User approves agent work via cav approve, then rejects via cav reject."""
        agents = self._init_and_agents("1")
        ws = agents["1"]
        id1 = registry.acquire_agent_id(self.user_repo)

        # Agent does work and pushes to dev branch
        (ws / "feature.py").write_text("# v1\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Feature v1", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

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

        # Dev branch should be restored on shared remote
        git("fetch", "--prune", "cave", cwd=self.user_repo)
        branches = git_out("branch", "-r", cwd=self.user_repo)
        self.assertIn("cave/agent-1/main", branches)

        # The restored dev branch should have the rejected commit
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
        git("push", "host-cave", "main:agent-1/main", cwd=ws1, env=agent_env("1"))

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
        """User approves bad work, then force-pushes to correct shared remote."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        # Agent commits bad work on dev branch
        (ws / "oops.py").write_text("# oops\n")
        git("add", "oops.py", cwd=ws)
        git("commit", "-m", "Bad commit", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Approve the bad work as user
        git("push", "host-cave", "main:main", cwd=ws)
        git("fetch", "cave", cwd=self.user_repo)
        git("merge", "--ff-only", "cave/main", cwd=self.user_repo)
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
        """User force-pushes a dev branch to roll back agent work."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "bad.py").write_text("# bad\n")
        git("add", "bad.py", cwd=ws)
        git("commit", "-m", "Bad agent work", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # User rolls back dev branch to main
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
        """cav checkout on a dev branch retargets to new base branch."""
        from cav.checkout import checkout as _checkout

        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=ws)
        git("commit", "-m", "Agent work", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Agent retargets (auto-creates standalone from main).
        # Simulate running inside the cave: set CODING_CAVE_* env vars.
        with unittest.mock.patch.dict(os.environ, {
            "CODING_CAVE_VERSION": "test",
            "CODING_CAVE_AGENT_NAME": "coding-cave@1",
        }):
            _checkout(ws, "standalone")

        # Inside the cave the local branch is the base name.
        current = git_out("branch", "--show-current", cwd=ws)
        self.assertEqual(current, "standalone")

        # Remote has new branch and base, old dev branch gone
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
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Host retargets agent 1 from main
        cav_cli("checkout", "standalone", "1", cwd=self.user_repo)

        # Bare repo has renamed branch
        shared = self.user_repo / ".coding-cave" / "cave-shared.git"
        bare_branches = git_out("branch", cwd=shared)
        self.assertIn("agent-1/standalone", bare_branches)
        self.assertNotIn("agent-1/main", bare_branches)
        # Target branch was auto-created
        self.assertIn("standalone", bare_branches)

        # Workspace branch also renamed (uses base name inside cave).
        ws_branch = git_out("branch", "--show-current", cwd=ws)
        self.assertEqual(ws_branch, "standalone")

    def test_checkout_creates_base_branch(self):
        """cav checkout auto-creates the base branch if it doesn't exist."""
        from cav.checkout import checkout as _checkout

        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=ws)
        git("commit", "-m", "Agent work", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Target doesn't exist yet. Simulate running inside the cave.
        with unittest.mock.patch.dict(os.environ, {
            "CODING_CAVE_VERSION": "test",
            "CODING_CAVE_AGENT_NAME": "coding-cave@1",
        }):
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
        self.assertIn("dev branch", result.stderr)

    # -- version env var --

    def test_version_env_var_set_in_cave(self):
        """CODING_CAVE_VERSION is set when running inside a cave."""
        with unittest.mock.patch.dict(os.environ, {"CODING_CAVE_VERSION": "test"}):
            self.assertTrue(config.inside_cave())

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
        fetch, git branch -r sees no dev branches and the cleanup
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
        ab = config.dev_branch("main", aid)
        git("push", HOST_SHARED_REMOTE, f"main:{ab}",
            f"--push-option=agent={aid}", cwd=ws, env=agent_env(aid))

        # Agent exits
        registry.mark_agent_exited(aid)

        # User's project dir has NOT fetched. Verify it can't see the
        # dev branch locally.
        stale_branches = cavgit.remote_branches(
            CAVE_SHARED_REMOTE, cwd=self.user_repo)
        agent_visible = any(
            config.parse_dev_branch(b) and
            config.parse_dev_branch(b)[1] == aid
            for b in stale_branches
        )
        self.assertFalse(agent_visible,
                         "pre-condition: user repo must NOT see dev branch "
                         "before fetching")

        # After fetching, the dev branch becomes visible
        cavgit.git("fetch", CAVE_SHARED_REMOTE, cwd=self.user_repo,
                   echo=False, check=False, capture=True)
        fresh_branches = cavgit.remote_branches(
            CAVE_SHARED_REMOTE, cwd=self.user_repo)
        agent_visible_after = any(
            config.parse_dev_branch(b) and
            config.parse_dev_branch(b)[1] == aid
            for b in fresh_branches
        )
        self.assertTrue(agent_visible_after,
                        "dev branch must be visible after fetching")

        # Agent should still be in the registry (not erroneously swept)
        self.assertIn(aid, registry.all_agent_ids())

        # cav -c should continue the same agent, not create a new one
        continued = registry.continue_agent_id(self.user_repo)
        self.assertEqual(continued, aid)
        registry.mark_agent_exited(continued)

    def test_zero_commit_agent_cleaned_up_by_reclaim(self):
        """Agent with no commits is removed by _auto_cleanup_if_clean.

        Zero-commit agents have no branches to preserve, so reclaim
        removes them when a new agent needs an ID.
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

    def test_workspace_alternates_use_relative_path(self):
        """Workspace alternates use a relative path via a mount-point directory.

        The relative path resolves on the host (via symlink to shared repo
        objects) and inside the cave (via bwrap overlay on the mount point).
        """
        from cav.init import init

        init(str(self.user_repo))
        shared = config.shared_repo_path(self.user_repo)

        aid = registry.acquire_agent_id(self.user_repo)
        ws = self._create_agent(aid)

        # Alternates should be a relative path, not an absolute one.
        alt = ws / ".git" / "objects" / "info" / "alternates"
        content = alt.read_text().strip()
        self.assertFalse(content.startswith("/"),
                         f"alternates should be relative, got: {content}")
        self.assertIn(f".coding-cave/{HOST_SHARED_REPO_DIR}/objects", content)

        # The mount-point directory should exist with symlink to real objects.
        mount_point = ws / ".coding-cave" / HOST_SHARED_REPO_DIR
        self.assertTrue(mount_point.is_dir())
        objects_link = mount_point / "objects"
        self.assertTrue(objects_link.is_symlink())
        self.assertEqual(objects_link.resolve(), (shared / "objects").resolve())

        # git status should work on the host (relative path resolves via symlink).
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws,
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"git status failed: {result.stderr}")

        # .coding-cave should be excluded from git tracking.
        self.assertNotIn(".coding-cave", result.stdout)


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
        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir
        from cav import init as cav_init
        cav_init.init(str(self.user_repo))
        self.a1, _ = workspace.create(self.user_repo, "1")
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
        """Return env with the git wrapper on PATH and agent identity set."""
        return {
            "PATH": f"{self._wrapper_bin}:{_clean_env()['PATH']}",
            "CODING_CAVE_AGENT_NAME": "coding-cave@1",
        }

    def test_auto_rebase_on_commit(self):
        """Git wrapper rebases dev branch onto updated main after commit."""
        # Simulate approved work landing on main
        (self.user_repo / "approved.py").write_text("# approved\n")
        git("add", "approved.py", cwd=self.user_repo)
        git("commit", "-m", "approved work", cwd=self.user_repo)
        git("push", "cave", "main", cwd=self.user_repo)

        # Agent commits via wrapper (triggers: fetch, rebase, push)
        (self.a1 / "agent_work.py").write_text("# agent\n")
        git("add", "agent_work.py", cwd=self.a1)
        git("commit", "-m", "agent work", cwd=self.a1, env=self._env())

        # Verify dev branch is a fast-forward from main
        git("fetch", "cave", cwd=self.user_repo)
        merge_base = git_out("merge-base", "cave/main", "cave/agent-1/main",
                             cwd=self.user_repo)
        main_sha = git_out("rev-parse", "cave/main", cwd=self.user_repo)
        self.assertEqual(merge_base, main_sha,
                         "dev branch should be a fast-forward from main")

        # Verify both commits are in the dev branch history
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

    def test_failed_push_warns(self):
        """A failed auto-push must surface via stderr, not be swallowed."""
        # Point host-cave at a non-existent repo so push fails.
        git("remote", "set-url", "host-cave",
            str(Path(self.tmpdir) / "nope.git"), cwd=self.a1)

        (self.a1 / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=self.a1)
        result = git("commit", "-m", "commit without a reachable remote",
                     cwd=self.a1, env=self._env(), check=False)

        # Commit itself must succeed - the sync failure is secondary.
        self.assertEqual(result.returncode, 0)
        self.assertIn("cav-sync", result.stderr)
        self.assertIn("agent-1/main", result.stderr)

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
                         "dev branch should be a fast-forward from main")


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


    def test_commit_with_dash_C_syncs(self):
        """git -C <dir> commit must trigger auto-push (not skip it)."""
        (self.a1 / "work.py").write_text("# work\n")
        git("add", "work.py", cwd=self.a1)
        # Commit via -C flag from a different directory
        git("-C", str(self.a1), "commit", "-m", "via -C",
            cwd=self.tmpdir, env=self._env())

        git("fetch", "cave", cwd=self.user_repo)
        log = git_out("log", "--oneline", "cave/agent-1/main",
                      cwd=self.user_repo)
        self.assertIn("via -C", log)


class TestReview(TestCAV):
    """Tests for cav review: fetch and checkout agent branches."""

    def test_review_checks_out_agent_branch(self):
        """cav review N checks out agent-N/main with the agent's commits."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Review from main
        from cav.review import review
        review(self.user_repo, "1")

        branch = git_out("branch", "--show-current", cwd=self.user_repo)
        self.assertEqual(branch, "agent-1/main")
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Add feature", log)

    def test_review_resets_when_already_on_branch(self):
        """cav review N resets to remote when already on that agent branch."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "v1.py").write_text("v1\n")
        git("add", "v1.py", cwd=ws)
        git("commit", "-m", "v1", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        from cav.review import review
        review(self.user_repo, "1")

        # Agent adds more work
        (ws / "v2.py").write_text("v2\n")
        git("add", "v2.py", cwd=ws)
        git("commit", "-m", "v2", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # Review again while already on the branch
        review(self.user_repo, "1")
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("v2", log)

    def test_review_nonexistent_agent(self):
        """cav review with unknown agent ID gives a clear error."""
        self._init()
        from cav.review import review
        with self.assertRaises(SystemExit) as ctx:
            review(self.user_repo, "99")
        self.assertIn("No agent branch", str(ctx.exception))

    def test_review_shows_pending_commits(self):
        """cav review prints the number of commits ahead of target."""
        agents = self._init_and_agents("1")
        ws = agents["1"]

        (ws / "a.py").write_text("a\n")
        git("add", "a.py", cwd=ws)
        git("commit", "-m", "first", cwd=ws)
        (ws / "b.py").write_text("b\n")
        git("add", "b.py", cwd=ws)
        git("commit", "-m", "second", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        import io
        from cav.review import review
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            review(self.user_repo, "1")
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        self.assertIn("2 commits ahead of main", output)


class TestShallowClone(unittest.TestCase):
    """Verify that cav works on shallow-cloned repos (--depth=1 --single-branch)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self.tmpdir) / "data"

        # Create a "remote" repo with multiple commits (simulating GitHub).
        origin = Path(self.tmpdir) / "origin"
        origin.mkdir()
        git("init", "-b", "main", cwd=origin)
        git("config", "user.email", "test@test.com", cwd=origin)
        git("config", "user.name", "Test User", cwd=origin)
        (origin / "README.md").write_text("# Project\n")
        git("add", "README.md", cwd=origin)
        git("commit", "-m", "Initial commit", cwd=origin)
        (origin / "lib.py").write_text("# lib\n")
        git("add", "lib.py", cwd=origin)
        git("commit", "-m", "Add lib", cwd=origin)

        # Shallow clone (like `gh repo clone ... -- --depth=1 --single-branch`)
        self.user_repo = Path(self.tmpdir) / "project"
        git("clone", "--depth=1", "--single-branch",
            str(origin), str(self.user_repo))

        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _init(self):
        from cav.init import init
        init(str(self.user_repo))

    def _create_agent(self, name):
        ws, _ = workspace.create(self.user_repo, name)
        git("config", "user.email", f"{name}@test.com", cwd=ws)
        git("config", "user.name", name, cwd=ws)
        return ws

    def test_init_on_shallow_clone(self):
        """cav init works on a shallow clone."""
        self._init()
        shared = config.shared_repo_path(self.user_repo)
        self.assertTrue(shared.exists())
        val = git_out("config", "receive.shallowUpdate", cwd=shared)
        self.assertEqual(val, "true")

    def test_workspace_creation(self):
        """Workspace can be created from a shallow clone."""
        self._init()
        ws = self._create_agent("1")
        self.assertTrue(ws.exists())
        branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=ws)
        # Inside the cave, local branch is the base name. Push refspec
        # maps it to agent-1/main upstream.
        self.assertEqual(branch, "main")

    def test_agent_push_and_approve(self):
        """Agent can push work and user can approve on a shallow clone."""
        from cav.approve import approve
        self._init()
        ws = self._create_agent("1")

        (ws / "feature.py").write_text("print('hello')\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Add feature", cwd=ws)

        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        approve(self.user_repo, agent_id="1", yes=True)

        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Add feature", log)

    def test_agent_pull_after_approve(self):
        """Agent can fetch updated main after another agent's work is approved."""
        self._init()
        ws1 = self._create_agent("1")
        ws2 = self._create_agent("2")

        # Agent 1 pushes and gets approved
        (ws1 / "a1.py").write_text("# a1\n")
        git("add", "a1.py", cwd=ws1)
        git("commit", "-m", "Agent 1 work", cwd=ws1)
        git("push", "host-cave", "main:agent-1/main", cwd=ws1, env=agent_env("1"))
        git("push", "host-cave", "main:main", cwd=ws1)

        # Agent 2 fetches and rebases on updated main
        (ws2 / "a2.py").write_text("# a2\n")
        git("add", "a2.py", cwd=ws2)
        git("commit", "-m", "Agent 2 work", cwd=ws2)
        git("fetch", "host-cave", cwd=ws2)
        git("rebase", "host-cave/main", cwd=ws2)

        log = git_out("log", "--oneline", cwd=ws2)
        self.assertIn("Agent 1 work", log)
        self.assertIn("Agent 2 work", log)

    def test_status(self):
        """cav status works on a shallow clone."""
        from cav.status import status
        self._init()
        registry.acquire_agent_id(self.user_repo)  # register agent 1
        ws = self._create_agent("1")

        (ws / "feature.py").write_text("# feature\n")
        git("add", "feature.py", cwd=ws)
        git("commit", "-m", "Shallow feature", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        import io
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            status(self.user_repo)
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        self.assertIn("agent-1/main", output)
        self.assertIn("Shallow feature", output)

    def test_pull_on_dev_branch(self):
        """cav pull works on a shallow clone's dev branch."""
        from cav.pull import pull
        self._init()
        ws = self._create_agent("1")

        (ws / "v1.py").write_text("# v1\n")
        git("add", "v1.py", cwd=ws)
        git("commit", "-m", "Version 1", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # User checks out the dev branch
        git("fetch", "cave", cwd=self.user_repo)
        git("checkout", "-b", "agent-1/main", "cave/agent-1/main",
            cwd=self.user_repo)

        # Agent pushes more work
        (ws / "v2.py").write_text("# v2\n")
        git("add", "v2.py", cwd=ws)
        git("commit", "-m", "Version 2", cwd=ws)
        git("push", "host-cave", "main:agent-1/main", cwd=ws, env=agent_env("1"))

        # pull picks up the new work
        pull(self.user_repo)
        log = git_out("log", "--oneline", cwd=self.user_repo)
        self.assertIn("Version 2", log)

    def test_reinit_enables_shallow_update(self):
        """cav init --reinitialize sets shallowUpdate on an existing project."""
        from cav.init import init
        self._init()
        shared = config.shared_repo_path(self.user_repo)
        # Clear it, then re-init
        git("config", "--unset", "receive.shallowUpdate", cwd=shared)
        init(str(self.user_repo), reinitialize=True)
        val = git_out("config", "receive.shallowUpdate", cwd=shared)
        self.assertEqual(val, "true")


if __name__ == "__main__":
    unittest.main()
