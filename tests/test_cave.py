"""Cave environment integration tests.

Requires running inside a Coding Cave with GPG socket forwarded
and git identity configured. Run with:
    python3 -m unittest -v tests.test_cave
"""

import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path


def git(*args, cwd=None):
    return subprocess.run(
        ["git"] + list(args), cwd=cwd,
        capture_output=True, text=True, check=True,
    )


def git_out(*args, cwd=None):
    return git(*args, cwd=cwd).stdout.strip()


class TestGPGSigning(unittest.TestCase):
    """Verify GPG commit signing works inside the cave."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "repo"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        # Inherit global git config (identity + gpgsign)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_signed_commit(self):
        """Commits are GPG-signed when commit.gpgsign is true."""
        gpgsign = git_out("config", "--global", "commit.gpgsign", cwd=self.repo)
        if gpgsign != "true":
            self.skipTest("commit.gpgsign not enabled")

        key = git_out("config", "--global", "user.signingkey", cwd=self.repo)

        (self.repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.repo)
        git("commit", "-m", "Signed commit", cwd=self.repo)

        # Verify the commit has a signature
        log = git_out("log", "-1", "--format=%G?", cwd=self.repo)
        self.assertIn(log, ("G", "U", "E"),
                      f"Expected signed commit, got verification status '{log}'")

        # Verify it used the correct key
        signer = git_out("log", "-1", "--format=%GK", cwd=self.repo)
        self.assertIn(key[-16:], signer)


if __name__ == "__main__":
    unittest.main()
