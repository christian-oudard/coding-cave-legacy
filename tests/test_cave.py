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


def _cav_bin():
    """Return path to a cav binary with --prompt support."""
    built = Path(__file__).parent.parent / "result" / "bin" / "cav"
    if built.exists():
        return str(built)
    system = shutil.which("cav")
    if system:
        r = subprocess.run([system, "agent", "--help"],
                           capture_output=True, text=True)
        if "--prompt" in r.stdout:
            return system
    return None


def _subcave(prompt, timeout=120):
    """Launch a subcave with --prompt and return stdout."""
    cav = _cav_bin()
    if not cav:
        raise unittest.SkipTest("no cav binary with --prompt support")
    r = subprocess.run(
        [cav, "agent", "--prompt", prompt],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip()


class TestSubcaveReadiness(unittest.TestCase):
    """Verify the cave environment supports launching subcaves."""

    def test_fuse_device_available(self):
        """/dev/fuse is required for nested nix store overlays."""
        self.assertTrue(Path("/dev/fuse").exists())

    def test_tun_device_available(self):
        """/dev/net/tun is required for subcave network isolation."""
        self.assertTrue(Path("/dev/net/tun").exists())

    def test_cav_binary_available(self):
        """A cav binary with --prompt must be available."""
        self.assertIsNotNone(_cav_bin(), "no cav with --prompt found")


class TestSubcaveLaunch(unittest.TestCase):
    """Integration tests that launch a real subcave via --prompt.

    A single subcave launch (bwrap + slirp4netns + fuse-overlayfs + claude)
    runs all checks at once to avoid the ~30s startup cost per test.
    """

    @classmethod
    def setUpClass(cls):
        """Launch one subcave and cache the output for all assertions."""
        cls.out = _subcave(
            'Run these commands and reply with ONLY the raw output, '
            'one result per line, prefixed by the label:\n'
            'MATH: echo $((2+2))\n'
            'VERSION: echo $CODING_CAVE_VERSION\n'
            'LINEAGE: echo $CODING_CAVE_LINEAGE\n'
            'NIXHASH: nix-store --query --hash /nix/store/*-bash-5* 2>/dev/null | head -1\n'
            'PROJECT: ls /projects/coding-cave/CLAUDE.md 2>&1\n'
            'NIXDEV: nix develop /projects/coding-cave --command python3 -c "import click; print(click.__name__)" 2>&1 | tail -1\n'
        )

    def _get(self, label):
        """Extract the value for a label from the combined output."""
        for line in self.out.splitlines():
            if line.startswith(label + ":"):
                return line[len(label) + 1:].strip()
        return ""

    def test_api_connectivity(self):
        """Subcave can reach the API through the crinj proxy chain."""
        self.assertIn("4", self._get("MATH"))

    def test_cave_version_set(self):
        """Subcave has CODING_CAVE_VERSION set."""
        self.assertTrue(len(self._get("VERSION")) > 0,
                        "CODING_CAVE_VERSION is empty")

    def test_lineage_includes_parent(self):
        """Subcave CODING_CAVE_LINEAGE contains the parent agent name."""
        self.assertRegex(self._get("LINEAGE"), r'coding-cave@\d+',
                         "lineage should contain parent agent name")

    def test_nix_store_registered(self):
        """Subcave nix DB has registered store paths from parent."""
        self.assertRegex(self._get("NIXHASH"), r'sha256:',
                         "nix store query should return a hash")

    def test_project_mounted(self):
        """Subcave has the project directory mounted."""
        self.assertIn("CLAUDE.md", self._get("PROJECT"))

    def test_nix_develop(self):
        """Subcave can run nix develop and import devShell packages."""
        self.assertEqual(self._get("NIXDEV"), "click")


if __name__ == "__main__":
    unittest.main()
