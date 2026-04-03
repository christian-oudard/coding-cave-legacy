"""Unit tests for the pre-receive hook's check_ref logic.

Tests the security boundary in isolation, without needing git repos.
"""

import os
import unittest
from unittest.mock import patch

# Import hook as a module (it's a script with no .py extension)
import importlib.util
import types
from pathlib import Path

hook_path = Path(__file__).parent.parent / "cav" / "hooks" / "pre-receive"
loader = importlib.machinery.SourceFileLoader("pre_receive", str(hook_path))
spec = importlib.util.spec_from_loader("pre_receive", loader)
hook = importlib.util.module_from_spec(spec)
# Reload to pick up regex changes
spec.loader.exec_module(hook)

ZERO = "0" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40


class TestCheckRef(unittest.TestCase):
    """Test check_ref directly with all code paths."""

    # -- User (agent=None) can do anything --

    def test_user_push_shared(self):
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/main", None)
        self.assertIsNone(r)

    def test_user_force_push_shared(self):
        # Even non-ff is allowed for user
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/main", None)
        self.assertIsNone(r)

    def test_user_delete_shared(self):
        r = hook.check_ref(SHA_A, ZERO, "refs/heads/main", None)
        self.assertIsNone(r)

    def test_user_push_agent_branch(self):
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-1/main", None)
        self.assertIsNone(r)

    def test_user_create_branch(self):
        r = hook.check_ref(ZERO, SHA_A, "refs/heads/develop", None)
        self.assertIsNone(r)

    # -- Agent: own branch --

    def test_agent_push_own_branch(self):
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-1/main", "1")
        self.assertIsNone(r)

    def test_agent_create_own_branch(self):
        r = hook.check_ref(ZERO, SHA_A, "refs/heads/agent-1/develop", "1")
        self.assertIsNone(r)

    def test_agent_force_push_own_branch(self):
        # Force push own branch is allowed (no ff check for dev branches)
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-1/main", "1")
        self.assertIsNone(r)

    # -- Agent: other agent's branch --

    def test_agent_push_other_agent_branch(self):
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-2/main", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)
        self.assertIn("cannot push", r)
        self.assertIn("1", r)

    def test_agent_create_other_agent_branch(self):
        r = hook.check_ref(ZERO, SHA_A, "refs/heads/agent-2/main", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    # -- Agent: base branch (all pushes rejected) --

    def test_agent_push_base_rejected(self):
        """Agents cannot push to base branches at all."""
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/main", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)
        self.assertIn("cannot push", r)
        self.assertIn("base branch", r)

    def test_agent_delete_shared_rejected(self):
        r = hook.check_ref(SHA_A, ZERO, "refs/heads/main", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)
        self.assertIn("cannot push", r)

    def test_agent_create_shared_rejected(self):
        """Agents cannot create new base branches."""
        r = hook.check_ref(ZERO, SHA_A, "refs/heads/develop", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)
        self.assertIn("cannot push", r)

    # -- HEAD branch trap (diagnostic for spurious agent-N/HEAD branches) --

    def test_agent_push_head_branch_rejected(self):
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-1/HEAD", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)
        self.assertIn("HEAD", r)

    def test_user_push_head_branch_rejected(self):
        """Even users cannot create HEAD branches (it's always a bug)."""
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-1/HEAD", None)
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    def test_bare_head_branch_rejected(self):
        r = hook.check_ref(ZERO, SHA_A, "refs/heads/HEAD", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    # -- Non-branch refs (tags, notes, replace, etc.) --

    def test_tag_push_by_agent_rejected(self):
        r = hook.check_ref(ZERO, SHA_A, "refs/tags/v1.0", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    def test_notes_ref_by_agent_rejected(self):
        r = hook.check_ref(SHA_A, SHA_B, "refs/notes/commits", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    def test_replace_ref_by_agent_rejected(self):
        r = hook.check_ref(ZERO, SHA_A, "refs/replace/" + SHA_A, "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    def test_tag_push_by_user_allowed(self):
        r = hook.check_ref(ZERO, SHA_A, "refs/tags/v1.0", None)
        self.assertIsNone(r)

    # -- Agent ID edge cases --

    def test_agent_high_id(self):
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-42/main", "42")
        self.assertIsNone(r)

    def test_agent_id_mismatch(self):
        """Agent 1 cannot push to agent 10's branch."""
        r = hook.check_ref(SHA_A, SHA_B, "refs/heads/agent-10/main", "1")
        self.assertIsNotNone(r)
        self.assertIn("REJECTED", r)

    # -- Multiple base branches --

    def test_agent_own_branch_different_targets(self):
        for base in ("main", "develop", "feature/foo"):
            r = hook.check_ref(SHA_A, SHA_B, f"refs/heads/agent-1/{base}", "1")
            self.assertIsNone(r), f"Failed for base: {base}"


class TestGetAgentName(unittest.TestCase):
    """Test agent identity extraction."""

    def _clean_env(self):
        """Env dict with no agent identity vars."""
        return {k: v for k, v in os.environ.items() if k != "CODING_CAVE_AGENT_ID"}

    def test_no_identity(self):
        with patch.dict(os.environ, self._clean_env(), clear=True):
            self.assertIsNone(hook.get_agent_name())

    def test_cav_agent_id(self):
        with patch.dict(os.environ, {**self._clean_env(), "CODING_CAVE_AGENT_ID": "3"}, clear=True):
            self.assertEqual(hook.get_agent_name(), "3")

    def test_push_options_ignored(self):
        """Push options must not be used for identity."""
        with patch.dict(os.environ, {
            **self._clean_env(),
            "GIT_PUSH_OPTION_COUNT": "1",
            "GIT_PUSH_OPTION_0": "agent=1",
        }, clear=True):
            self.assertIsNone(hook.get_agent_name())

    def test_empty_string_is_no_identity(self):
        with patch.dict(os.environ, {**self._clean_env(), "CODING_CAVE_AGENT_ID": ""}, clear=True):
            self.assertIsNone(hook.get_agent_name())


if __name__ == "__main__":
    unittest.main()
