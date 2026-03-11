"""End-to-end integration tests: identity forwarding, pull re-authoring, GPG signing.

These tests exercise the full commit→pull cycle using real git repos and the
actual pull_project code path. They verify that:
- Commits with the host identity don't need re-authoring (hash preserved)
- Commits with the throwaway identity get re-authored on pull
- GPG signing works via the forwarded agent socket
"""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import wrapper
from coding_cave import _state
from coding_cave.sync import _needs_reauthoring, pull_project
from tests.conftest import write_projects


GIT_HOST_ENV = {
    "GIT_AUTHOR_NAME": "Host User",
    "GIT_AUTHOR_EMAIL": "host@test.com",
    "GIT_COMMITTER_NAME": "Host User",
    "GIT_COMMITTER_EMAIL": "host@test.com",
}

GIT_THROWAWAY_ENV = {
    "GIT_AUTHOR_NAME": "coding-cave",
    "GIT_AUTHOR_EMAIL": "coding-cave@noreply",
    "GIT_COMMITTER_NAME": "coding-cave",
    "GIT_COMMITTER_EMAIL": "coding-cave@noreply",
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

    def test_throwaway_identity_reauthored(self, e2e_env):
        """Cave commits with throwaway identity → pull re-authors."""
        env = e2e_env
        _cave_commit(env, "cave work", git_env=GIT_THROWAWAY_ENV)
        cave_head = _head(env.gitdir, gitdir=True)

        pull_project(env.slug)

        host_head = _head(env.host)
        assert host_head != cave_head, \
            "Pull should re-author (different hash) for throwaway identity"
        assert _author_email(env.host) == "host@test.com", \
            "Re-authored commit should have host email"
        assert _author_name(env.host) == "Host User", \
            "Re-authored commit should have host name"

    def test_needs_reauthoring_host_identity(self, e2e_env):
        """_needs_reauthoring returns False for host identity."""
        env = e2e_env
        _cave_commit(env, "cave work", git_env=GIT_HOST_ENV)
        assert not _needs_reauthoring(env.gitdir, env.host)

    def test_needs_reauthoring_throwaway_identity(self, e2e_env):
        """_needs_reauthoring returns True for throwaway identity."""
        env = e2e_env
        _cave_commit(env, "cave work", git_env=GIT_THROWAWAY_ENV)
        assert _needs_reauthoring(env.gitdir, env.host)

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

    def test_multiple_commits_throwaway_all_reauthored(self, e2e_env):
        """Multiple throwaway commits → all get re-authored on pull."""
        env = e2e_env
        _cave_commit(env, "commit 1", filename="a.txt", content="a\n",
                      git_env=GIT_THROWAWAY_ENV)
        _cave_commit(env, "commit 2", filename="b.txt", content="b\n",
                      git_env=GIT_THROWAWAY_ENV)

        pull_project(env.slug)

        # Verify all commits have host identity
        log = _run("git", "-C", str(env.host), "log", "--format=%ae")
        emails = log.stdout.strip().splitlines()
        assert all(e == "host@test.com" for e in emails), \
            f"All commits should be re-authored: {emails}"


class TestGpgSigning:
    """Verify GPG signing works via forwarded agent socket.

    These tests require a GPG agent with a signing key configured.
    They are skipped when GPG infrastructure is not available.
    """

    @staticmethod
    def _gpg_available():
        """Check if GPG signing works (agent socket + usable key)."""
        key = os.environ.get("CODING_CAVE_GIT_SIGNING_KEY", "")
        if not key:
            return False
        # Check if agent is reachable and key is usable
        try:
            result = subprocess.run(
                ["gpg", "--list-keys", key],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @pytest.fixture
    def gpg_env(self, e2e_env):
        """E2E env with GPG signing configured."""
        if not self._gpg_available():
            pytest.skip("GPG agent/key not available")
        env = e2e_env
        key = os.environ["CODING_CAVE_GIT_SIGNING_KEY"]
        # Configure signing in the cave gitdir
        _run("git", "--git-dir", str(env.gitdir), "config",
             "user.signingkey", key)
        _run("git", "--git-dir", str(env.gitdir), "config",
             "commit.gpgsign", "true")
        # Configure host repo to verify signatures
        _run("git", "-C", str(env.host), "config",
             "user.signingkey", key)
        return env

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

    def test_gpg_signed_throwaway_reauthored_gets_host_signature(self, gpg_env):
        """GPG-signed throwaway commit → re-authoring re-signs with host key."""
        env = gpg_env
        # Enable signing in host repo so re-authoring produces a new signature
        key = os.environ["CODING_CAVE_GIT_SIGNING_KEY"]
        _run("git", "-C", str(env.host), "config", "commit.gpgsign", "true")

        _cave_commit(env, "signed throwaway", git_env=GIT_THROWAWAY_ENV)
        cave_head = _head(env.gitdir, gitdir=True)

        pull_project(env.slug)

        # Re-authored (different hash, host identity)
        host_head = _head(env.host)
        assert host_head != cave_head
        assert _author_email(env.host) == "host@test.com"

        # Re-authored commit is re-signed by host
        result = _run("git", "-C", str(env.host),
                       "log", "-1", "--format=%G? %GK")
        parts = result.stdout.strip().split()
        sig_status = parts[0]
        assert sig_status in ("G", "U"), \
            f"Re-authored commit should be re-signed, got '{sig_status}'"
