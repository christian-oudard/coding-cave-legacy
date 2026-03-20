"""End-to-end integration tests: identity forwarding, GPG signing.

These tests exercise the full commit→pull cycle using real git repos and the
actual pull_project code path. They verify that:
- Commits with the host identity are preserved exactly (hash preserved)
- GPG signing works via the forwarded agent socket
"""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import wrapper
from coding_cave import _state
from coding_cave.sync import pull_project
from tests.conftest import write_projects


GIT_HOST_ENV = {
    "GIT_AUTHOR_NAME": "Host User",
    "GIT_AUTHOR_EMAIL": "host@test.com",
    "GIT_COMMITTER_NAME": "Host User",
    "GIT_COMMITTER_EMAIL": "host@test.com",
}


def _run(*args, env=None, check=True):
    return subprocess.run(list(args), capture_output=True, text=True,
                          env=env, check=check)


def _head(path, gitdir=False):
    if gitdir:
        return _run("git", "--git-dir", str(path), "rev-parse", "HEAD").stdout.strip()
    return _run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


def _author_email(path):
    return _run("git", "-C", str(path), "log", "-1", "--format=%ae").stdout.strip()


def _author_name(path):
    return _run("git", "-C", str(path), "log", "-1", "--format=%an").stdout.strip()


@pytest.fixture
def e2e_env(tmp_path):
    """Full pull environment with host identity configured."""
    slug = "e2e-project"

    # Host repo with known identity
    host = tmp_path / "host"
    host.mkdir()
    _run("git", "-C", str(host), "init", "-b", "main")
    _run("git", "-C", str(host), "config", "user.email", "host@test.com")
    _run("git", "-C", str(host), "config", "user.name", "Host User")
    (host / "file.txt").write_text("initial\n")
    _run("git", "-C", str(host), "add", ".")
    env = {**os.environ, **GIT_HOST_ENV}
    _run("git", "-C", str(host), "commit", "-m", "initial", env=env)

    # Create gitdir (simulates cave's per-session gitdir)
    tmpclone = tmp_path / "_tmpclone"
    _run("git", "clone", "--no-checkout", "--local", str(host), str(tmpclone))
    projects_dir = tmp_path / "projects"
    proj_state = projects_dir / slug
    proj_state.mkdir(parents=True)
    gitdir = proj_state / "gitdir"
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)
    _run("git", "--git-dir", str(gitdir), "read-tree", "HEAD")

    # Cave working directory
    cave = tmp_path / "cave"
    cave.mkdir()
    _run("git", "--git-dir", str(gitdir), "--work-tree", str(cave),
         "checkout", "HEAD", "--", ".")

    # Wire up cave data + project registry
    _state.set_cave_data(tmp_path)
    write_projects(tmp_path, {slug: str(host)})
    wrapper.ensure_cave_remote(host, gitdir)
    host_head = _head(host)
    wrapper.sync_refs(gitdir, host, host_head, "main")

    # Clear env vars so tests control identity explicitly
    saved_env = {}
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        saved_env[var] = os.environ.pop(var, None)

    yield SimpleNamespace(host=host, gitdir=gitdir, cave=cave, slug=slug)

    for var, val in saved_env.items():
        if val is not None:
            os.environ[var] = val


def _cave_commit(env, msg, filename="file.txt", content="changed\n", git_env=None):
    """Make a commit in the cave with the specified identity."""
    (env.cave / filename).write_text(content)
    run_env = {**os.environ, **(git_env or GIT_HOST_ENV)}
    _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(env.cave),
         "add", filename, env=run_env)
    _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(env.cave),
         "commit", "-m", msg, env=run_env)


class TestIdentityForwarding:
    """Verify that identity forwarding eliminates re-authoring on pull."""

    def test_host_identity_no_reauthoring(self, e2e_env):
        """Cave commits with host identity → pull preserves hash exactly."""
        env = e2e_env
        _cave_commit(env, "cave work", git_env=GIT_HOST_ENV)
        cave_head = _head(env.gitdir, gitdir=True)

        pull_project(env.slug)

        host_head = _head(env.host)
        assert host_head == cave_head, \
            "Pull should preserve commit hash when identity matches"
        assert _author_email(env.host) == "host@test.com"
        assert _author_name(env.host) == "Host User"

    def test_multiple_commits_host_identity(self, e2e_env):
        """Multiple cave commits with host identity → all hashes preserved."""
        env = e2e_env
        _cave_commit(env, "commit 1", filename="a.txt", content="a\n",
                      git_env=GIT_HOST_ENV)
        _cave_commit(env, "commit 2", filename="b.txt", content="b\n",
                      git_env=GIT_HOST_ENV)
        cave_head = _head(env.gitdir, gitdir=True)

        pull_project(env.slug)

        assert _head(env.host) == cave_head
        assert (env.host / "a.txt").read_text() == "a\n"
        assert (env.host / "b.txt").read_text() == "b\n"


class TestGpgSigning:
    """Verify GPG signing works with a temporary test key."""

    @pytest.fixture
    def gpg_env(self, e2e_env, tmp_path):
        """E2E env with a temporary GPG key for signing."""
        import shutil, tempfile

        # Use a short path to avoid socket name length limits (108 chars)
        gpghome = Path(tempfile.mkdtemp(prefix="gpg", dir="/tmp"))
        gpghome.chmod(0o700)

        # Configure loopback pinentry so gpg never prompts interactively
        (gpghome / "gpg-agent.conf").write_text("allow-loopback-pinentry\n")
        (gpghome / "gpg.conf").write_text("pinentry-mode loopback\n")

        old_gnupghome = os.environ.get("GNUPGHOME")
        os.environ["GNUPGHOME"] = str(gpghome)

        # Generate a throwaway key (no passphrase).
        # Setting GNUPGHOME before this ensures the auto-started agent
        # uses the temp dir (with loopback pinentry config).
        keygen = subprocess.run(
            ["gpg", "--batch", "--passphrase", "", "--quick-gen-key",
             "Test User <test@test.com>", "default", "default", "0"],
            capture_output=True, text=True, timeout=15,
        )
        if keygen.returncode != 0:
            subprocess.run(["gpgconf", "--kill", "gpg-agent"],
                           capture_output=True)
            shutil.rmtree(gpghome, ignore_errors=True)
            if old_gnupghome is not None:
                os.environ["GNUPGHOME"] = old_gnupghome
            else:
                os.environ.pop("GNUPGHOME", None)
            pytest.skip(f"gpg key generation failed: {keygen.stderr}")

        # Extract the key fingerprint
        keys = subprocess.run(
            ["gpg", "--list-keys", "--with-colons", "test@test.com"],
            capture_output=True, text=True,
        )
        fpr = ""
        for line in keys.stdout.splitlines():
            if line.startswith("fpr:"):
                fpr = line.split(":")[9]
                break
        assert fpr, "Could not find generated key fingerprint"

        env = e2e_env
        # Configure signing in the cave gitdir
        _run("git", "--git-dir", str(env.gitdir), "config",
             "user.signingkey", fpr)
        _run("git", "--git-dir", str(env.gitdir), "config",
             "commit.gpgsign", "true")
        # Configure host repo to verify signatures
        _run("git", "-C", str(env.host), "config",
             "user.signingkey", fpr)

        yield env

        # Cleanup: kill agent, restore env, remove temp dir
        subprocess.run(["gpgconf", "--kill", "gpg-agent"],
                       capture_output=True)
        shutil.rmtree(gpghome, ignore_errors=True)
        if old_gnupghome is not None:
            os.environ["GNUPGHOME"] = old_gnupghome
        else:
            os.environ.pop("GNUPGHOME", None)

    def test_gpg_signed_commit(self, gpg_env):
        """Cave can create a GPG-signed commit."""
        env = gpg_env
        _cave_commit(env, "signed commit", git_env=GIT_HOST_ENV)

        # Verify the commit has a signature
        result = _run("git", "--git-dir", str(env.gitdir),
                       "log", "-1", "--format=%G?")
        sig_status = result.stdout.strip()
        assert sig_status in ("G", "U", "E"), \
            f"Commit should be GPG-signed, got status '{sig_status}'"

    def test_gpg_signed_commit_pull_preserves_signature(self, gpg_env):
        """GPG-signed commit with host identity → pull preserves signature."""
        env = gpg_env
        _cave_commit(env, "signed commit", git_env=GIT_HOST_ENV)
        cave_head = _head(env.gitdir, gitdir=True)

        pull_project(env.slug)

        # Hash preserved (no re-authoring)
        assert _head(env.host) == cave_head

        # Signature preserved
        result = _run("git", "-C", str(env.host),
                       "log", "-1", "--format=%G?")
        sig_status = result.stdout.strip()
        assert sig_status in ("G", "U", "E"), \
            f"Pulled commit should have GPG signature, got '{sig_status}'"

