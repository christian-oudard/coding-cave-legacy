"""Integration tests for the git daemon remote isolation.

Tests that agents can fetch/push through the daemon, and that the
pre-receive hook correctly identifies agents via CAV_AGENT_ID.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cav import config, daemon, workspace

# Use a high base port for tests to avoid conflicts with running agents.
TEST_DAEMON_BASE_PORT = 19400


def git(*args, cwd=None, check=True, env=None):
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, check=check,
        capture_output=True, text=True, env=merged,
    )


def git_out(*args, cwd=None):
    return git(*args, cwd=cwd).stdout.strip()


class TestDaemon(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_repo = Path(self.tmpdir) / "project"
        self.data_dir = Path(self.tmpdir) / "data"

        os.environ["CAV_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir
        self._orig_base_port = config.DAEMON_BASE_PORT
        config.DAEMON_BASE_PORT = TEST_DAEMON_BASE_PORT

        self.user_repo.mkdir()
        git("init", "-b", "main", cwd=self.user_repo)
        git("config", "user.email", "test@test.com", cwd=self.user_repo)
        git("config", "user.name", "Test", cwd=self.user_repo)
        (self.user_repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.user_repo)
        git("commit", "-m", "init", cwd=self.user_repo)

        from cav.init import init
        init(str(self.user_repo))

        self.shared = config.shared_repo_path(self.user_repo)
        self.slug = config.project_slug(self.user_repo)

        # Start daemon for agent 1
        self.daemon_proc, self.daemon_dir = daemon.start(
            "1", [(self.shared, self.slug)])

    def tearDown(self):
        daemon.stop(self.daemon_proc, self.daemon_dir)
        config.DAEMON_BASE_PORT = self._orig_base_port
        shutil.rmtree(self.tmpdir)

    def _ws_with_daemon_remote(self, agent_id):
        """Create workspace and point remote to daemon."""
        ws = workspace.create(self.user_repo, agent_id)
        git("config", "user.email", f"{agent_id}@test.com", cwd=ws)
        git("config", "user.name", f"Agent {agent_id}", cwd=ws)
        url = daemon.remote_url(agent_id, self.slug)
        git("remote", "set-url", config.HOST_REMOTE, url, cwd=ws)
        return ws

    def test_fetch_via_daemon(self):
        """Agent can fetch from the bare repo through the daemon."""
        ws = self._ws_with_daemon_remote("1")
        result = git("fetch", "host-cave", cwd=ws)
        self.assertEqual(result.returncode, 0)

    def test_push_own_branch_via_daemon(self):
        """Agent can push to own branch through the daemon."""
        ws = self._ws_with_daemon_remote("1")
        (ws / "new.txt").write_text("new\n")
        git("add", "new.txt", cwd=ws)
        git("commit", "-m", "agent work", cwd=ws)
        result = git("push", "host-cave", "agent-1/main", cwd=ws)
        self.assertEqual(result.returncode, 0)

    def test_ff_push_shared_rejected_via_daemon(self):
        """Agent cannot ff-push to shared branch through daemon."""
        ws = self._ws_with_daemon_remote("1")
        git("checkout", "main", cwd=ws)
        (ws / "fix.txt").write_text("fix\n")
        git("add", "fix.txt", cwd=ws)
        git("commit", "-m", "fix", cwd=ws)
        result = git("push", "host-cave", "main", cwd=ws, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)

    def test_non_ff_push_rejected_via_daemon(self):
        """Agent non-ff push rejected by hook running through daemon."""
        ws = self._ws_with_daemon_remote("1")
        git("checkout", "main", cwd=ws)
        (ws / "bad.txt").write_text("bad\n")
        git("add", "bad.txt", cwd=ws)
        git("commit", "--amend", "-m", "rewritten", cwd=ws)
        result = git("push", "--force", "host-cave", "main", cwd=ws, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)

    def test_cross_agent_push_rejected_via_daemon(self):
        """Agent 1 cannot push to agent 2's branch through daemon."""
        ws = self._ws_with_daemon_remote("1")
        (ws / "x.txt").write_text("x\n")
        git("add", "x.txt", cwd=ws)
        git("commit", "-m", "work", cwd=ws)
        result = git(
            "push", "host-cave", "HEAD:refs/heads/agent-2/main",
            cwd=ws, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)

    def test_delete_shared_rejected_via_daemon(self):
        """Agent cannot delete shared branch through daemon."""
        ws = self._ws_with_daemon_remote("1")
        result = git("push", "host-cave", "--delete", "main",
                      cwd=ws, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)

    def test_daemon_identity_unforgeable(self):
        """Agent cannot override CAV_AGENT_ID via client-side env."""
        ws = self._ws_with_daemon_remote("1")
        (ws / "x.txt").write_text("x\n")
        git("add", "x.txt", cwd=ws)
        git("commit", "-m", "work", cwd=ws)
        # Try pushing to agent-2's branch while setting CAV_AGENT_ID=2
        # on the client side. The daemon sets CAV_AGENT_ID=1 server-side,
        # so this should still be rejected.
        result = git(
            "push", "host-cave", "HEAD:refs/heads/agent-2/main",
            cwd=ws, check=False, env={"CAV_AGENT_ID": "2"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)


if __name__ == "__main__":
    unittest.main()
