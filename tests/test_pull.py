"""Tests for pull_project: normal pull, auto-force, and edge cases."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import wrapper


def _run(*args, check=True):
    """Run a command, return CompletedProcess."""
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def _head(path, gitdir=False):
    """Get HEAD commit hash."""
    if gitdir:
        return _run("git", "--git-dir", str(path), "rev-parse", "HEAD").stdout.strip()
    return _run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


def _log_count(path):
    """Count commits on HEAD."""
    return int(_run("git", "-C", str(path), "rev-list", "--count", "HEAD").stdout.strip())


def _last_author(path):
    """Get author name of HEAD commit."""
    return _run("git", "-C", str(path), "log", "-1", "--format=%an").stdout.strip()


def _cave_git(env, *args):
    """Run git against the cave gitdir + working tree."""
    cmd = ["git", "--git-dir", str(env.gitdir), "--work-tree", str(env.cave)]
    return _run(*cmd, *args)


def cave_commit(env, msg, filename="file.txt", content=None, amend=False):
    """Make a commit in the cave."""
    if content is not None:
        (env.cave / filename).write_text(content)
        _cave_git(env, "add", filename)
    args = ["commit", "-m", msg]
    if amend:
        args.append("--amend")
    _cave_git(env, *args)


@pytest.fixture
def pull_env(tmp_path, monkeypatch):
    """Pull testing environment: synced host repo + cave gitdir.

    After setup:
    - Host has 1 commit (file.txt = "initial")
    - Gitdir mirrors host (same commit, cave-sync set)
    - Cave working dir matches
    - wrapper globals patched for pull_project
    """
    slug = "test-project"

    # Host repo
    host = tmp_path / "host"
    host.mkdir()
    _run("git", "-C", str(host), "init", "-b", "main")
    _run("git", "-C", str(host), "config", "user.email", "host@test.com")
    _run("git", "-C", str(host), "config", "user.name", "Host User")
    (host / "file.txt").write_text("initial\n")
    _run("git", "-C", str(host), "add", ".")
    _run("git", "-C", str(host), "commit", "-m", "initial")

    # Gitdir (simulates initialize_gitdir)
    tmpclone = tmp_path / "_tmpclone"
    _run("git", "clone", "--no-checkout", "--local", str(host), str(tmpclone))
    projects_dir = tmp_path / "projects"
    proj_state = projects_dir / slug
    proj_state.mkdir(parents=True)
    gitdir = proj_state / "gitdir"
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)
    _run("git", "--git-dir", str(gitdir), "config", "user.email", "cave-coder@cave")
    _run("git", "--git-dir", str(gitdir), "config", "user.name", "cave-coder")
    _run("git", "--git-dir", str(gitdir), "read-tree", "HEAD")

    # Cave working dir
    cave = tmp_path / "cave"
    cave.mkdir()
    _run("git", "--git-dir", str(gitdir), "--work-tree", str(cave),
         "checkout", "HEAD", "--", ".")

    # Patch wrapper globals
    monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(wrapper, "load_projects", lambda: {slug: str(host)})

    # Unset cave git identity env vars so --reset-author uses repo config
    # (on the real host these aren't set; in the cave they override everything)
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)

    # Set up cave remote + initial sync
    wrapper.ensure_cave_remote(host, gitdir)
    host_head = _head(host)
    wrapper.sync_refs(gitdir, host, host_head, "main")

    return SimpleNamespace(host=host, gitdir=gitdir, cave=cave, slug=slug)


class TestPullAutoForce:
    """Auto-force: cave amends/squashes adopted without --force."""

    def test_amend_after_sync(self, pull_env):
        """Cave amends a synced commit -> pull auto-adopts."""
        env = pull_env

        # Cave adds a commit, pull it
        cave_commit(env, "cave work", content="v1\n")
        wrapper.pull_project(env.slug)
        head_v1 = _head(env.host)
        assert (env.host / "file.txt").read_text() == "v1\n"

        # Cave amends the synced commit
        cave_commit(env, "amended work", content="v2\n", amend=True)

        # Plain pull (no --force) should succeed
        wrapper.pull_project(env.slug)
        assert _head(env.host) != head_v1
        assert (env.host / "file.txt").read_text() == "v2\n"

    def test_squash_after_sync(self, pull_env):
        """Cave squashes synced commits -> pull auto-adopts."""
        env = pull_env

        # Cave adds two commits, pull each
        cave_commit(env, "first", content="first\n")
        wrapper.pull_project(env.slug)
        cave_commit(env, "second", content="second\n")
        wrapper.pull_project(env.slug)
        assert _log_count(env.host) == 3  # initial + 2

        # Cave squashes via commit-tree: same tree, parent=initial
        initial = _run("git", "--git-dir", str(env.gitdir),
                        "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        tree = _run("git", "--git-dir", str(env.gitdir),
                     "rev-parse", "HEAD^{tree}").stdout.strip()
        squashed = _run("git", "--git-dir", str(env.gitdir),
                         "commit-tree", tree, "-p", initial,
                         "-m", "squashed").stdout.strip()
        _run("git", "--git-dir", str(env.gitdir),
             "update-ref", "refs/heads/main", squashed)

        # Pull should auto-adopt (2 host commits replaced with 1)
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "second\n"
        assert _log_count(env.host) == 2  # initial + squashed

    def test_amend_then_new_commit(self, pull_env):
        """After auto-force amend, normal commits still pull correctly."""
        env = pull_env

        # Amend cycle
        cave_commit(env, "first", content="v1\n")
        wrapper.pull_project(env.slug)
        cave_commit(env, "amended", content="v2\n", amend=True)
        wrapper.pull_project(env.slug)

        # Now a fresh commit should pull normally
        cave_commit(env, "next", content="v3\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "v3\n"
        assert _log_count(env.host) == 3  # initial + amended + next


class TestPullNormal:
    """Normal pull: fresh commits without history rewriting."""

    def test_new_commit(self, pull_env):
        """Fresh cave commit is pulled."""
        env = pull_env
        cave_commit(env, "new work", content="new\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "new\n"
        assert _log_count(env.host) == 2

    def test_multiple_commits(self, pull_env):
        """Multiple cave commits pulled in one go."""
        env = pull_env
        cave_commit(env, "first", content="one\n")
        cave_commit(env, "second", content="two\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "two\n"
        assert _log_count(env.host) == 3

    def test_reauthors(self, pull_env):
        """Pulled commits have host author, not cave author."""
        env = pull_env
        cave_commit(env, "cave work", content="work\n")
        wrapper.pull_project(env.slug)
        assert _last_author(env.host) == "Host User"

    def test_noop_when_synced(self, pull_env):
        """No changes -> noop."""
        env = pull_env
        head_before = _head(env.host)
        wrapper.pull_project(env.slug)
        assert _head(env.host) == head_before

    def test_incremental_pulls(self, pull_env):
        """Successive pulls each add new commits."""
        env = pull_env
        cave_commit(env, "first", content="one\n")
        wrapper.pull_project(env.slug)
        assert _log_count(env.host) == 2

        cave_commit(env, "second", content="two\n")
        wrapper.pull_project(env.slug)
        assert _log_count(env.host) == 3
        assert (env.host / "file.txt").read_text() == "two\n"


class TestPullEdgeCases:
    """Edge cases: empty repos, stale state, detached HEAD."""

    def test_pull_into_empty_host(self, tmp_path, monkeypatch):
        """Pull into a host repo with no commits initializes from cave.

        Regression: 0a7e904 — pull crashed on empty host repos because
        rev-parse HEAD failed with check=True.
        """
        slug = "test-project"

        # Empty host repo (no commits)
        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")

        # Gitdir with commits (simulate cave that already has work)
        source = tmp_path / "source"
        source.mkdir()
        _run("git", "-C", str(source), "init", "-b", "main")
        _run("git", "-C", str(source), "config", "user.email", "cave@test.com")
        _run("git", "-C", str(source), "config", "user.name", "cave-coder")
        (source / "file.txt").write_text("cave work\n")
        _run("git", "-C", str(source), "add", ".")
        _run("git", "-C", str(source), "commit", "-m", "cave commit")

        # Create gitdir from source
        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        tmpclone = tmp_path / "_tmpclone"
        _run("git", "clone", "--no-checkout", "--local", str(source), str(tmpclone))
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)

        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(wrapper, "load_projects", lambda: {slug: str(host)})
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            monkeypatch.delenv(var, raising=False)

        wrapper.pull_project(slug)

        # Host should now have the commit and a branch
        assert _log_count(host) == 1
        branch = _run("git", "-C", str(host), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "main"
        assert (host / "file.txt").read_text() == "cave work\n"

    def test_detached_head_uses_main_branch(self, tmp_path, monkeypatch):
        """Pull from detached HEAD gitdir creates 'main' branch, not 'HEAD'.

        Regression: e5e228a — git rev-parse --abbrev-ref HEAD returns literal
        'HEAD' when detached, which was used as a branch name.
        """
        slug = "test-project"

        # Empty host repo
        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")

        # Create gitdir with a commit, then detach HEAD
        source = tmp_path / "source"
        source.mkdir()
        _run("git", "-C", str(source), "init", "-b", "main")
        _run("git", "-C", str(source), "config", "user.email", "cave@test.com")
        _run("git", "-C", str(source), "config", "user.name", "cave-coder")
        (source / "file.txt").write_text("work\n")
        _run("git", "-C", str(source), "add", ".")
        _run("git", "-C", str(source), "commit", "-m", "initial")

        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        tmpclone = tmp_path / "_tmpclone"
        _run("git", "clone", "--no-checkout", "--local", str(source), str(tmpclone))
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)

        # Detach HEAD by writing commit hash directly to HEAD file
        # (git checkout --detach doesn't work cleanly on bare-ish gitdirs)
        head_hash = _run("git", "--git-dir", str(gitdir), "rev-parse", "HEAD").stdout.strip()
        (gitdir / "HEAD").write_text(head_hash + "\n")

        # Verify it's detached
        abbrev = _run("git", "--git-dir", str(gitdir),
                       "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert abbrev == "HEAD"

        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(wrapper, "load_projects", lambda: {slug: str(host)})
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            monkeypatch.delenv(var, raising=False)

        wrapper.pull_project(slug)

        branch = _run("git", "-C", str(host), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "main", f"Expected 'main', got '{branch}'"

    def test_stale_cave_pull_branch(self, pull_env):
        """Stale _cave_pull branch from interrupted pull doesn't crash.

        Regression: 68dbc83 — git branch -d fails on _cave_pull when it has
        rewritten commits not fully merged into main. Changed to -D.
        """
        env = pull_env

        # Create a diverged _cave_pull branch (simulates interrupted pull)
        _run("git", "-C", str(env.host), "checkout", "-b", "_cave_pull")
        (env.host / "stale.txt").write_text("from interrupted pull\n")
        _run("git", "-C", str(env.host), "add", "stale.txt")
        _run("git", "-C", str(env.host), "commit", "-m", "stale rebase work")
        _run("git", "-C", str(env.host), "checkout", "main")

        # Make a cave commit
        cave_commit(env, "new work", content="new\n")

        # Pull should succeed despite stale _cave_pull
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "new\n"

        # _cave_pull should be cleaned up
        branches = _run("git", "-C", str(env.host), "branch",
                         "--list", "_cave_pull").stdout.strip()
        assert branches == ""
