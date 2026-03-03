"""Tests for pull_project: normal pull, auto-force, and edge cases."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import wrapper
from coding_cave import _state
from tests.conftest import write_projects


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
def pull_env(tmp_path):
    """Pull testing environment: synced host repo + cave gitdir."""
    slug = "test-project"

    host = tmp_path / "host"
    host.mkdir()
    _run("git", "-C", str(host), "init", "-b", "main")
    _run("git", "-C", str(host), "config", "user.email", "host@test.com")
    _run("git", "-C", str(host), "config", "user.name", "Host User")
    (host / "file.txt").write_text("initial\n")
    _run("git", "-C", str(host), "add", ".")
    _run("git", "-C", str(host), "commit", "-m", "initial")

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

    cave = tmp_path / "cave"
    cave.mkdir()
    _run("git", "--git-dir", str(gitdir), "--work-tree", str(cave),
         "checkout", "HEAD", "--", ".")

    _state.set_cave_data(tmp_path)
    write_projects(tmp_path, {slug: str(host)})

    saved_env = {}
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        saved_env[var] = os.environ.pop(var, None)

    wrapper.ensure_cave_remote(host, gitdir)
    host_head = _head(host)
    wrapper.sync_refs(gitdir, host, host_head, "main")

    yield SimpleNamespace(host=host, gitdir=gitdir, cave=cave, slug=slug)

    for var, val in saved_env.items():
        if val is not None:
            os.environ[var] = val


class TestPullForceRequired:
    """History rewrites require --force."""

    def test_amend_without_force_is_noop(self, pull_env):
        """Cave amends a synced commit -> plain pull refuses."""
        env = pull_env

        cave_commit(env, "cave work", content="v1\n")
        wrapper.pull_project(env.slug)
        head_v1 = _head(env.host)
        assert (env.host / "file.txt").read_text() == "v1\n"

        # Cave amends the synced commit
        cave_commit(env, "amended work", content="v2\n", amend=True)

        # Plain pull (no --force) should refuse
        wrapper.pull_project(env.slug)
        assert _head(env.host) == head_v1
        assert (env.host / "file.txt").read_text() == "v1\n"

    def test_amend_with_force(self, pull_env):
        """Cave amends a synced commit -> pull --force adopts."""
        env = pull_env

        cave_commit(env, "cave work", content="v1\n")
        wrapper.pull_project(env.slug)
        head_v1 = _head(env.host)

        cave_commit(env, "amended work", content="v2\n", amend=True)

        wrapper.pull_project(env.slug, force=True)
        assert _head(env.host) != head_v1
        assert (env.host / "file.txt").read_text() == "v2\n"

    def test_squash_with_force(self, pull_env):
        """Cave squashes synced commits -> pull --force adopts."""
        env = pull_env

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

        wrapper.pull_project(env.slug, force=True)
        assert (env.host / "file.txt").read_text() == "second\n"
        assert _log_count(env.host) == 2  # initial + squashed

    def test_force_then_normal_commit(self, pull_env):
        """After force-pull, normal commits still pull correctly."""
        env = pull_env

        cave_commit(env, "first", content="v1\n")
        wrapper.pull_project(env.slug)
        cave_commit(env, "amended", content="v2\n", amend=True)
        wrapper.pull_project(env.slug, force=True)

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

    def test_pull_into_empty_host(self, tmp_path):
        """Pull into a host repo with no commits initializes from cave.

        Regression: 0a7e904 — pull crashed on empty host repos because
        rev-parse HEAD failed with check=True.
        """
        slug = "test-project"

        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")

        source = tmp_path / "source"
        source.mkdir()
        _run("git", "-C", str(source), "init", "-b", "main")
        _run("git", "-C", str(source), "config", "user.email", "cave@test.com")
        _run("git", "-C", str(source), "config", "user.name", "cave-coder")
        (source / "file.txt").write_text("cave work\n")
        _run("git", "-C", str(source), "add", ".")
        _run("git", "-C", str(source), "commit", "-m", "cave commit")

        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        tmpclone = tmp_path / "_tmpclone"
        _run("git", "clone", "--no-checkout", "--local", str(source), str(tmpclone))
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)

        _state.set_cave_data(tmp_path)
        write_projects(tmp_path, {slug: str(host)})

        wrapper.pull_project(slug)

        # Host should now have the commit and a branch
        assert _log_count(host) == 1
        branch = _run("git", "-C", str(host), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "main"
        assert (host / "file.txt").read_text() == "cave work\n"

    def test_detached_head_uses_main_branch(self, tmp_path):
        """Pull from detached HEAD gitdir creates 'main' branch, not 'HEAD'.

        Regression: e5e228a — git rev-parse --abbrev-ref HEAD returns literal
        'HEAD' when detached, which was used as a branch name.
        """
        slug = "test-project"

        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")

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

        head_hash = _run("git", "--git-dir", str(gitdir), "rev-parse", "HEAD").stdout.strip()
        (gitdir / "HEAD").write_text(head_hash + "\n")
        abbrev = _run("git", "--git-dir", str(gitdir),
                       "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert abbrev == "HEAD"

        _state.set_cave_data(tmp_path)
        write_projects(tmp_path, {slug: str(host)})

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


class TestPullMidSession:
    """Pull picks up commits from running sessions."""

    def test_pull_sees_session_commits(self, pull_env, tmp_path):
        """Commits in a per-session gitdir are visible to pull_project."""
        env = pull_env
        sid = "test-session-123"

        # Create session overlay area and per-session gitdir
        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)

        session_gitdir = wrapper.create_session_gitdir(
            env.slug, sid, overlay_base,
        )
        assert session_gitdir is not None

        # Register a fake running session (use our own PID so it looks alive)
        import os
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        session_info = {
            "sid": sid,
            "project": env.slug,
            "bwrap_pid": os.getpid(),
            "started": 0,
        }
        (sessions_dir / f"{sid}.json").write_text(json.dumps(session_info))

        # Commit in the session gitdir (not the shared one)
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "cave@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "cave-coder")
        session_cave = tmp_path / "session-cave"
        session_cave.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "checkout", "HEAD", "--", ".", check=False)
        (session_cave / "file.txt").write_text("from session\n")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "add", "file.txt")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "commit", "-m", "session commit")

        # Verify shared gitdir does NOT have this commit yet
        shared_head = _head(env.gitdir, gitdir=True)
        session_head = _run("git", "--git-dir", str(session_gitdir),
                            "rev-parse", "HEAD").stdout.strip()
        assert shared_head != session_head

        # Pull should pick up the session commit
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "from session\n"

    def test_cross_project_session_commits(self, pull_env, tmp_path):
        """Pull project B sees commits from a session started for project A.

        Reproduction: start a session in folder A, agent edits and commits to
        project B, `cav pull B` from host should get the commit.  Bug: the
        session's "project" field is A, so sync_live_sessions("B") skipped it.
        """
        env = pull_env  # project B (slug="test-project", has host+gitdir)
        sid = "test-session-cross"

        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)

        # Create per-session gitdir for project B within this session
        session_gitdir = wrapper.create_session_gitdir(
            env.slug, sid, overlay_base,
        )
        assert session_gitdir is not None

        # Register session with a DIFFERENT primary project
        import os
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        session_info = {
            "sid": sid,
            "project": "other-project",  # <-- not env.slug
            "bwrap_pid": os.getpid(),
            "started": 0,
        }
        (sessions_dir / f"{sid}.json").write_text(json.dumps(session_info))

        # Commit in the session gitdir
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "cave@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "cave-coder")
        session_cave = tmp_path / "session-cave"
        session_cave.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "checkout", "HEAD", "--", ".", check=False)
        (session_cave / "file.txt").write_text("cross-project\n")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "add", "file.txt")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "commit", "-m", "cross-project commit")

        # Pull project B — should find the commit even though session is for A
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "cross-project\n"
