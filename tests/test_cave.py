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


class TestGPGRestriction(unittest.TestCase):
    """Verify the GPG agent socket blocks dangerous operations."""

    def test_secret_key_export_blocked(self):
        """The restricted extra socket must deny secret key export."""
        result = subprocess.run(
            ["gpg", "--export-secret-keys", "--armor"],
            capture_output=True, text=True, timeout=10,
        )
        # Restricted socket returns no key material (empty output or error)
        self.assertEqual(result.stdout, "",
                         "Secret key export should produce no output through "
                         "the restricted extra socket")


class TestNixStore(unittest.TestCase):
    """Verify nix operations work inside the cave.

    The cave mounts a fuse-overlayfs on /nix/store and seeds the nix DB
    from the host so nix knows about existing store paths. Without DB
    seeding, nix tries to re-download and chmod host store paths, which
    fails because they're owned by uid 65534 (nix daemon).
    """

    def test_nix_develop(self):
        """nix develop must be able to evaluate and enter a dev shell."""
        result = subprocess.run(
            ["nix", "develop", "--command", "true"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"nix develop failed: {result.stderr}")

    def test_host_store_paths_registered(self):
        """Host store paths must be registered in the cave's nix DB."""
        # Pick a store path we know exists (nix itself)
        nix_paths = list(Path("/nix/store").glob("*-nix-2.*"))
        if not nix_paths:
            self.skipTest("no nix store path found")
        result = subprocess.run(
            ["nix-store", "--query", "--hash", str(nix_paths[0])],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                         f"Store path not registered in DB: {nix_paths[0]}\n"
                         f"This means nix DB seeding from host is not working.")


class TestSubcaveReadiness(unittest.TestCase):
    """Verify the cave environment supports launching subcaves.

    These checks confirm the recursive plumbing is in place. Actual
    subcave launches require API access (crinj proxy chain).
    """

    def test_recursive_env_set(self):
        """CODING_CAVE_RECURSIVE must be 1 for subcave support."""
        import os
        val = os.environ.get("CODING_CAVE_RECURSIVE")
        if val != "1":
            self.skipTest("not a recursive cave (--recursive not passed)")
        self.assertEqual(val, "1")

    def test_fuse_device_available(self):
        """/dev/fuse is required for nested nix store overlays."""
        import os
        if os.environ.get("CODING_CAVE_RECURSIVE") != "1":
            self.skipTest("not a recursive cave")
        self.assertTrue(Path("/dev/fuse").exists(),
                        "/dev/fuse must be available for fuse-overlayfs")

    def test_tun_device_available(self):
        """/dev/net/tun is required for subcave network isolation."""
        import os
        if os.environ.get("CODING_CAVE_RECURSIVE") != "1":
            self.skipTest("not a recursive cave")
        self.assertTrue(Path("/dev/net/tun").exists(),
                        "/dev/net/tun must be available for slirp4netns")

    def test_cav_binary_available(self):
        """cav binary must be on PATH for subcave launching."""
        self.assertIsNotNone(shutil.which("cav"),
                             "cav not found on PATH")

    def test_prompt_flag_accepted(self):
        """cav agent --prompt must be a valid option."""
        # Use the locally built binary if available (system cav may be outdated).
        cav = str(Path(__file__).parent.parent / "result" / "bin" / "cav")
        if not Path(cav).exists():
            cav = "cav"
        result = subprocess.run(
            [cav, "agent", "--help"],
            capture_output=True, text=True,
        )
        self.assertIn("--prompt", result.stdout)

    def test_inside_cave_detected(self):
        """config.inside_cave() must return True inside a cave."""
        import sys, os
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from cav import config
        self.assertTrue(config.inside_cave(),
                        "CODING_CAVE_VERSION not set")

    def test_subcave_home_is_isolated(self):
        """Subcave homes must be separate from the parent's persistent home."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from cav import config
        parent_home = config.persistent_home()
        subcave_home = config.DATA_DIR / "subcave-homes" / "test-agent"
        self.assertNotEqual(subcave_home, parent_home)
        self.assertFalse(
            str(subcave_home).startswith(str(parent_home) + "/"),
            "subcave home must not be under parent's persistent home")

    def test_nix_overlay_dirs_isolated_per_home(self):
        """Different homes produce non-overlapping nix overlay paths."""
        import sys, tempfile
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from cav.sandbox import _nix_overlay_dirs
        a = Path(tempfile.mkdtemp())
        b = Path(tempfile.mkdtemp())
        _, _, merged_a = _nix_overlay_dirs(a)
        _, _, merged_b = _nix_overlay_dirs(b)
        self.assertNotEqual(merged_a, merged_b)


if __name__ == "__main__":
    unittest.main()
