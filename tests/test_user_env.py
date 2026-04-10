"""Tests for user_env build logic."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cav.user_env import _do_build


class TestDoBuild(unittest.TestCase):
    def test_failed_build_returns_none_even_with_stale_result(self):
        """A failed nix build must return None, not a stale result link."""
        with tempfile.TemporaryDirectory() as d:
            user_env = Path(d)
            # Create a stale result symlink pointing to a fake old path.
            stale_target = user_env / "old-store-path"
            stale_target.mkdir()
            result_link = user_env / "result"
            result_link.symlink_to(stale_target)

            fake_run = MagicMock()
            fake_run.returncode = 1
            fake_run.stdout = ""

            with patch("cav.user_env.subprocess.run", return_value=fake_run):
                out = _do_build(user_env)

            self.assertIsNone(out)

    def test_successful_build_returns_path(self):
        """A successful nix build returns the new output path."""
        with tempfile.TemporaryDirectory() as d:
            user_env = Path(d)
            new_path = user_env / "new-store-path"
            new_path.mkdir()

            fake_run = MagicMock()
            fake_run.returncode = 0
            fake_run.stdout = str(new_path) + "\n"

            with patch("cav.user_env.subprocess.run", return_value=fake_run):
                out = _do_build(user_env)

            self.assertEqual(out, new_path)
            self.assertTrue((user_env / "result").is_symlink())
            self.assertEqual((user_env / "result").resolve(), new_path)


if __name__ == "__main__":
    unittest.main()
