"""Tests for push_project: normal push, refuse on unpulled, force push."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import wrapper
from coding_cave import _state
from tests.conftest import write_projects, write_session


def _run(*args, check=True):
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def _head(path, gitdir=False):
    if gitdir:
        return _run("git", "--git-dir", str(path), "rev-parse", "HEAD").stdout.strip()
    return _run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def push_env(tmp_path):
    """Synced host repo + cave gitdir for push testing."""
    slug = "test-project"

    host = tmp_path / "host"
    host.mkdir()
    _run("git", "-C", str(host), "init", "-b", "main")
    _run("git", "-C", str(host), "config", "user.email", "host@test.com")
    _run("git", "-C", str(host), "config", "user.name", "Host User")
    (host / "file.txt").write_text("initial\n")
    _run("git", "-C", str(host), "add", ".")
    _run("git", "-C", str(host), "commit", "-m", "initial")

    projects_dir = tmp_path / "projects"
    proj_state = projects_dir / slug
    proj_state.mkdir(parents=True)
    tmpclone = tmp_path / "_tmpclone"
    _run("git", "clone", "--no-checkout", "--local", str(host), str(tmpclone))
    gitdir = proj_state / "gitdir"
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)
    _run("git", "--git-dir", str(gitdir), "config", "user.email", "cave@test.com")
    _run("git", "--git-dir", str(gitdir), "config", "user.name", "cave-coder")

    _state.set_cave_data(tmp_path)
    write_projects(tmp_path, {slug: str(host)})

    saved_env = {}
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        saved_env[var] = os.environ.pop(var, None)

    wrapper.ensure_cave_remote(host, gitdir)
    host_head = _head(host)
    wrapper.sync_refs(gitdir, host, host_head, "main")

    yield SimpleNamespace(host=host, gitdir=gitdir, slug=slug,
                           projects_dir=projects_dir)

    for var, val in saved_env.items():
        if val is not None:
            os.environ[var] = val


class TestPushNormal:
    def test_push_new_commit(self, push_env):
        env = push_env
        (env.host / "file.txt").write_text("updated\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host change")
        wrapper.push_project(env.slug)
        assert _head(env.gitdir, gitdir=True) == _head(env.host)

    def test_push_multiple_commits(self, push_env):
        env = push_env
        (env.host / "file.txt").write_text("one\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "first")
        (env.host / "file.txt").write_text("two\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "second")
        wrapper.push_project(env.slug)
        assert _head(env.gitdir, gitdir=True) == _head(env.host)

    def test_push_noop_when_synced(self, push_env):
        env = push_env
        head_before = _head(env.gitdir, gitdir=True)
        wrapper.push_project(env.slug)
        assert _head(env.gitdir, gitdir=True) == head_before

    def test_push_updates_cave_sync(self, push_env):
        env = push_env
        (env.host / "file.txt").write_text("new\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host work")
        wrapper.push_project(env.slug)
        cave_sync = _run("git", "--git-dir", str(env.gitdir),
                         "rev-parse", "refs/cave-sync").stdout.strip()
        assert cave_sync == _head(env.host)


class TestPushRefuse:
    def test_refuses_with_unpulled_commits(self, push_env):
        env = push_env
        cave = push_env.host.parent / "cave-work"
        cave.mkdir()
        _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(cave),
             "checkout", "HEAD", "--", ".")
        (cave / "file.txt").write_text("cave work\n")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(cave),
             "add", "file.txt")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(cave),
             "commit", "-m", "cave commit")
        (env.host / "file.txt").write_text("host work\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host commit")
        with pytest.raises(SystemExit):
            wrapper.push_project(env.slug)

    def test_force_push_overwrites(self, push_env):
        env = push_env
        cave = push_env.host.parent / "cave-work"
        cave.mkdir()
        _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(cave),
             "checkout", "HEAD", "--", ".")
        (cave / "file.txt").write_text("cave work\n")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(cave),
             "add", "file.txt")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(cave),
             "commit", "-m", "cave commit")
        (env.host / "file.txt").write_text("host work\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host commit")
        wrapper.push_project(env.slug, force=True)
        assert _head(env.gitdir, gitdir=True) == _head(env.host)


class TestPushSyncsLiveSessions:
    def test_push_refuses_when_session_has_commits(self, push_env, tmp_path):
        env = push_env
        sid = "test-session-456"

        # Create session overlay + per-session gitdir
        overlay_base = _state.cave_data() / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(env.slug, sid, overlay_base)
        assert session_gitdir is not None

        write_session(_state.cave_data(), sid, env.slug)

        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "cave@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "cave-coder")
        session_work = tmp_path / "session-work"
        session_work.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_work), "checkout", "HEAD", "--", ".", check=False)
        (session_work / "session.txt").write_text("session work\n")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_work), "add", "session.txt")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_work), "commit", "-m", "session commit")

        (env.host / "file.txt").write_text("host push\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host commit")

        with pytest.raises(SystemExit):
            wrapper.push_project(env.slug)

    def test_push_succeeds_when_session_synced(self, push_env, tmp_path):
        env = push_env
        sid = "test-session-789"

        overlay_base = _state.cave_data() / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        wrapper.create_session_gitdir(env.slug, sid, overlay_base)

        write_session(_state.cave_data(), sid, env.slug)

        (env.host / "file.txt").write_text("host push\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host commit")

        wrapper.push_project(env.slug)
        assert _head(env.gitdir, gitdir=True) == _head(env.host)


class TestPushNotFound:
    def test_unknown_project_exits(self, push_env):
        with pytest.raises(SystemExit):
            wrapper.push_project("nonexistent")
