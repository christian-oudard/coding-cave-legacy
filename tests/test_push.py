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

    def test_push_updates_host_ref(self, push_env):
        env = push_env
        (env.host / "file.txt").write_text("new\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host work")
        wrapper.push_project(env.slug)
        host_ref = _run("git", "--git-dir", str(env.gitdir),
                        "rev-parse", "refs/remotes/host/main").stdout.strip()
        assert host_ref == _head(env.host)


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


class TestPushEmptyHost:
    def test_push_empty_host_does_not_crash(self, tmp_path):
        """push_project handles an empty host repo (no commits) gracefully."""
        slug = "empty-host"
        _state.set_cave_data(tmp_path)

        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")

        projects_dir = _state.projects_dir()
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        # Create a minimal gitdir (also empty)
        gitdir = proj_state / "gitdir"
        _run("git", "init", "--bare", "-q", str(gitdir))

        write_projects(tmp_path, {slug: str(host)})
        # Should not raise
        wrapper.push_project(slug)


class TestPushNotFound:
    def test_unknown_project_exits(self, push_env):
        with pytest.raises(SystemExit):
            wrapper.push_project("nonexistent")


class TestPushMultiBranch:
    """Push is branch-aware: each host branch pushes to the matching cave branch."""

    def test_push_multiple_branches(self, push_env):
        """Commits on multiple host branches all push to correct cave branches."""
        env = push_env

        # Create develop on both sides
        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")
        wrapper.sync_refs(env.gitdir, env.host, _head(env.host), "develop")

        # Commit on main
        (env.host / "file.txt").write_text("main update\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "main work")
        main_head = _head(env.host)

        # Commit on develop
        _run("git", "-C", str(env.host), "checkout", "develop")
        (env.host / "dev.txt").write_text("develop work\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "develop work")
        develop_head = _head(env.host)

        _run("git", "-C", str(env.host), "checkout", "main")

        wrapper.push_project(env.slug)

        # Both branches should be updated in the cave gitdir
        cave_main = _run("git", "--git-dir", str(env.gitdir),
                         "rev-parse", "main").stdout.strip()
        cave_develop = _run("git", "--git-dir", str(env.gitdir),
                            "rev-parse", "develop").stdout.strip()
        assert cave_main == main_head
        assert cave_develop == develop_head

    def test_push_does_not_cross_branches(self, push_env):
        """Host commit on develop does NOT affect cave's main."""
        env = push_env

        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")

        main_before = _run("git", "--git-dir", str(env.gitdir),
                           "rev-parse", "main").stdout.strip()

        # Only commit on develop
        _run("git", "-C", str(env.host), "checkout", "develop")
        (env.host / "dev.txt").write_text("develop\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "develop work")
        _run("git", "-C", str(env.host), "checkout", "main")

        wrapper.push_project(env.slug)

        # Main should be unchanged
        cave_main = _run("git", "--git-dir", str(env.gitdir),
                         "rev-parse", "main").stdout.strip()
        assert cave_main == main_before

    def test_force_only_pushes_current_branch(self, push_env):
        """--force only pushes the current branch, not others."""
        env = push_env

        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")
        wrapper.sync_refs(env.gitdir, env.host, _head(env.host), "develop")

        # Make cave commit on main (creates unpulled state)
        cave_work = push_env.host.parent / "cave-work"
        cave_work.mkdir()
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "checkout", "HEAD", "--", ".")
        (cave_work / "cave.txt").write_text("cave\n")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "add", "cave.txt")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "commit", "-m", "cave commit on main")
        cave_main = _run("git", "--git-dir", str(env.gitdir),
                         "rev-parse", "main").stdout.strip()

        # Host commits on both branches
        (env.host / "file.txt").write_text("host main\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host main")

        _run("git", "-C", str(env.host), "checkout", "develop")
        (env.host / "dev.txt").write_text("host develop\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host develop")

        # Force push from develop — should only push develop
        wrapper.push_project(env.slug, force=True)

        # Develop should be updated
        cave_develop = _run("git", "--git-dir", str(env.gitdir),
                            "rev-parse", "develop").stdout.strip()
        assert cave_develop == _head(env.host)

        # Main should still have the cave commit (NOT overwritten)
        cave_main_after = _run("git", "--git-dir", str(env.gitdir),
                               "rev-parse", "main").stdout.strip()
        assert cave_main_after == cave_main

    def test_unpulled_on_one_branch_blocks_only_that_branch(self, push_env):
        """Unpulled commits on develop don't block pushing main."""
        env = push_env

        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")
        wrapper.sync_refs(env.gitdir, env.host, _head(env.host), "develop")

        # Make cave commit on develop (creates unpulled state)
        cave_work = push_env.host.parent / "cave-work"
        cave_work.mkdir()
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "checkout", "develop")
        (cave_work / "cave.txt").write_text("cave develop\n")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "add", "cave.txt")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "commit", "-m", "cave commit on develop")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "checkout", "main")

        # Host commits on both branches
        (env.host / "file.txt").write_text("host main\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host main")

        _run("git", "-C", str(env.host), "checkout", "develop")
        (env.host / "dev.txt").write_text("host develop\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host develop")
        _run("git", "-C", str(env.host), "checkout", "main")

        # Push — develop is blocked, main should still push
        wrapper.push_project(env.slug)

        # Main should be updated
        cave_main = _run("git", "--git-dir", str(env.gitdir),
                         "rev-parse", "main").stdout.strip()
        host_main = _run("git", "-C", str(env.host),
                         "rev-parse", "main").stdout.strip()
        assert cave_main == host_main


class TestPushUpdatesHead:
    def test_push_updates_shared_gitdir_head(self, push_env):
        """Push updates shared gitdir HEAD to match host's current branch."""
        env = push_env

        # Create develop branch on host
        _run("git", "-C", str(env.host), "checkout", "-b", "develop")
        (env.host / "dev.txt").write_text("develop\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "develop work")

        # Gitdir HEAD should point to main before push
        head_before = _run("git", "--git-dir", str(env.gitdir),
                           "symbolic-ref", "HEAD").stdout.strip()
        assert head_before == "refs/heads/main"

        # Push while host is on develop
        wrapper.push_project(env.slug)

        # Gitdir HEAD should now point to develop
        head_after = _run("git", "--git-dir", str(env.gitdir),
                          "symbolic-ref", "HEAD").stdout.strip()
        assert head_after == "refs/heads/develop"

    def test_force_push_updates_shared_gitdir_head(self, push_env):
        """Force push also updates shared gitdir HEAD."""
        env = push_env

        _run("git", "-C", str(env.host), "checkout", "-b", "develop")
        (env.host / "dev.txt").write_text("develop\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "develop work")

        wrapper.push_project(env.slug, force=True)

        head_after = _run("git", "--git-dir", str(env.gitdir),
                          "symbolic-ref", "HEAD").stdout.strip()
        assert head_after == "refs/heads/develop"


class TestForcePushClearsSyncedPerBranch:
    """Force-push main clears refs/synced/main but not refs/synced/develop."""

    def test_force_push_clears_only_target_branch_synced(self, push_env, tmp_path):
        env = push_env
        sid = "test-session-synced"

        # Create develop branch on both sides
        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")
        wrapper.sync_refs(env.gitdir, env.host, _head(env.host), "develop")

        # Create a session with refs/synced for both branches
        overlay_base = _state.cave_data() / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(
            env.slug, sid, overlay_base)
        write_session(_state.cave_data(), sid, env.slug)

        # Simulate refs/synced for both branches
        synced_main = (session_gitdir / "refs" / "synced" / "main")
        synced_develop = (session_gitdir / "refs" / "synced" / "develop")
        synced_main.parent.mkdir(parents=True, exist_ok=True)
        synced_develop.parent.mkdir(parents=True, exist_ok=True)
        synced_main.write_text(_head(env.host) + "\n")
        synced_develop.write_text(_head(env.host) + "\n")

        # Make cave commit on main so force push has something to overwrite
        cave_work = tmp_path / "cave-work"
        cave_work.mkdir()
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "checkout", "HEAD", "--", ".")
        (cave_work / "cave.txt").write_text("cave\n")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "add", "cave.txt")
        _run("git", "--git-dir", str(env.gitdir), "--work-tree",
             str(cave_work), "commit", "-m", "cave commit")

        # Host commits on main
        (env.host / "file.txt").write_text("force push\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host force")

        # Force push main
        wrapper.push_project(env.slug, force=True)

        # refs/synced/main should be cleared
        assert not synced_main.exists(), \
            "refs/synced/main should be cleared after force push"

        # refs/synced/develop should still exist
        assert synced_develop.exists(), \
            "refs/synced/develop should NOT be cleared by force push of main"
