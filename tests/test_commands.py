"""Tests for CLI commands: add, remove, move, reset, status, and helpers."""
import json
import os
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import wrapper


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(*args, **kwargs):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(list(args), capture_output=True, text=True, env=env, **kwargs)


def _head(path):
    return git("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def cave_env(tmp_path, monkeypatch):
    """Minimal cave environment with patched globals."""
    cave_data = tmp_path / "cave-data"
    projects_dir = cave_data / "projects"
    projects_dir.mkdir(parents=True)
    sessions_dir = cave_data / "sessions"
    sessions_dir.mkdir(parents=True)
    projects_file = cave_data / "projects.json"

    monkeypatch.setattr(wrapper, "CAVE_DATA", cave_data)
    monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(wrapper, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(wrapper, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(wrapper, "CAVE_HOME", cave_data / "home")

    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)

    return SimpleNamespace(
        cave_data=cave_data, projects_dir=projects_dir,
        sessions_dir=sessions_dir, projects_file=projects_file,
        tmp_path=tmp_path,
    )


def make_repo(path, name="main"):
    """Create a git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    git("git", "-C", str(path), "init", "-b", name)
    git("git", "-C", str(path), "config", "user.email", "test@test.com")
    git("git", "-C", str(path), "config", "user.name", "Test")
    (path / "file.txt").write_text("initial\n")
    git("git", "-C", str(path), "add", ".")
    git("git", "-C", str(path), "commit", "-m", "initial")
    return path


# --- Slugify ---

class TestSlugify:
    def test_simple(self):
        assert wrapper.slugify("my-project") == "my-project"

    def test_uppercase(self):
        assert wrapper.slugify("My Project") == "my-project"

    def test_special_chars(self):
        assert wrapper.slugify("hello@world!") == "hello-world"

    def test_leading_trailing(self):
        assert wrapper.slugify("---foo---") == "foo"

    def test_max_len(self):
        assert len(wrapper.slugify("a" * 100)) <= 40

    def test_empty(self):
        assert wrapper.slugify("!!!") == ""


# --- Project registry ---

class TestProjectRegistry:
    def test_save_load_roundtrip(self, cave_env):
        projects = {"foo": "/path/to/foo", "bar": "/path/to/bar"}
        wrapper.save_projects(projects)
        loaded = wrapper.load_projects()
        assert loaded == projects

    def test_load_empty(self, cave_env):
        assert wrapper.load_projects() == {}

    def test_list_projects(self, cave_env):
        wrapper.save_projects({"a": "/a", "b": "/b"})
        result = wrapper.list_projects()
        assert set(dict(result).keys()) == {"a", "b"}


# --- Session tracking ---

class TestSessionTracking:
    def test_register_and_list(self, cave_env):
        wrapper.register_session("sid-1", "my-project", os.getpid())
        sessions = wrapper.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["sid"] == "sid-1"
        assert sessions[0]["project"] == "my-project"

    def test_list_filters_by_project(self, cave_env):
        wrapper.register_session("s1", "proj-a", os.getpid())
        wrapper.register_session("s2", "proj-b", os.getpid())
        assert len(wrapper.list_sessions(project="proj-a")) == 1
        assert len(wrapper.list_sessions(project="proj-b")) == 1
        assert len(wrapper.list_sessions()) == 2

    def test_stale_session_cleaned(self, cave_env):
        # Register with a dead PID
        wrapper.register_session("dead-session", "proj", 999999)
        sessions = wrapper.list_sessions()
        assert len(sessions) == 0
        # Session file should be cleaned up
        assert not (cave_env.sessions_dir / "dead-session.json").exists()

    def test_cleanup_session(self, cave_env):
        wrapper.register_session("s1", "proj", os.getpid())
        wrapper.cleanup_session("s1")
        assert not (cave_env.sessions_dir / "s1.json").exists()

    def test_session_id_format(self):
        sid = wrapper.session_id()
        parts = sid.split("-")
        assert len(parts) == 2
        assert parts[0].isdigit()
        assert parts[1].isdigit()


# --- cmd_add ---

class TestCmdAdd:
    def test_add_new_project(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "my-project")
        wrapper.cmd_add([str(repo)])
        projects = wrapper.load_projects()
        assert "my-project" in projects

    def test_add_creates_gitdir(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "my-project")
        wrapper.cmd_add([str(repo)])
        assert (cave_env.projects_dir / "my-project" / "gitdir").is_dir()

    def test_add_already_registered(self, cave_env, capsys):
        repo = make_repo(cave_env.tmp_path / "my-project")
        wrapper.cmd_add([str(repo)])
        wrapper.cmd_add([str(repo)])
        assert "already in the coding cave" in capsys.readouterr().err

    def test_add_nonexistent_dir(self, cave_env):
        with pytest.raises(SystemExit):
            wrapper.cmd_add(["/nonexistent/path"])

    def test_add_inits_git_if_needed(self, cave_env):
        plain_dir = cave_env.tmp_path / "no-git"
        plain_dir.mkdir()
        wrapper.cmd_add([str(plain_dir)])
        assert (plain_dir / ".git").is_dir()


# --- cmd_remove ---

class TestCmdRemove:
    def test_remove_project(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "my-project")
        wrapper.cmd_add([str(repo)])
        assert "my-project" in wrapper.load_projects()

        wrapper.cmd_remove(["my-project"])
        assert "my-project" not in wrapper.load_projects()
        assert not (cave_env.projects_dir / "my-project").exists()

    def test_remove_unknown_exits(self, cave_env):
        with pytest.raises(SystemExit):
            wrapper.cmd_remove(["nonexistent"])

    def test_remove_with_running_session_exits(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "my-project")
        wrapper.cmd_add([str(repo)])
        wrapper.register_session("s1", "my-project", os.getpid())

        with pytest.raises(SystemExit):
            wrapper.cmd_remove(["my-project"])
        # Project should still exist
        assert "my-project" in wrapper.load_projects()

    def test_remove_from_cwd(self, cave_env, monkeypatch):
        repo = make_repo(cave_env.tmp_path / "my-project")
        wrapper.cmd_add([str(repo)])
        monkeypatch.chdir(repo)
        wrapper.cmd_remove([])
        assert "my-project" not in wrapper.load_projects()


# --- cmd_move ---

class TestCmdMove:
    def test_move_project(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "old-name")
        wrapper.cmd_add([str(repo)])
        assert "old-name" in wrapper.load_projects()

        new_dir = cave_env.tmp_path / "new-name"
        repo.rename(new_dir)

        wrapper.cmd_move("old-name", str(new_dir))
        projects = wrapper.load_projects()
        assert "old-name" not in projects
        assert "new-name" in projects
        assert projects["new-name"] == str(new_dir)

    def test_move_renames_state_dir(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "old-name")
        wrapper.cmd_add([str(repo)])
        assert (cave_env.projects_dir / "old-name").exists()

        new_dir = cave_env.tmp_path / "new-name"
        repo.rename(new_dir)

        wrapper.cmd_move("old-name", str(new_dir))
        assert not (cave_env.projects_dir / "old-name").exists()
        assert (cave_env.projects_dir / "new-name").exists()

    def test_move_unknown_exits(self, cave_env):
        with pytest.raises(SystemExit):
            wrapper.cmd_move("nonexistent", "/tmp")

    def test_move_to_nonexistent_dir_exits(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        with pytest.raises(SystemExit):
            wrapper.cmd_move("proj", "/nonexistent/path")

    def test_move_slug_conflict_exits(self, cave_env):
        repo_a = make_repo(cave_env.tmp_path / "proj-a")
        repo_b = make_repo(cave_env.tmp_path / "proj-b")
        wrapper.cmd_add([str(repo_a)])
        wrapper.cmd_add([str(repo_b)])

        with pytest.raises(SystemExit):
            wrapper.cmd_move("proj-a", str(repo_b))

    def test_move_with_running_session_exits(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        wrapper.register_session("s1", "proj", os.getpid())

        with pytest.raises(SystemExit):
            wrapper.cmd_move("proj", str(cave_env.tmp_path / "new"))

    def test_move_same_slug(self, cave_env):
        """Move to new path with same dir name (only path changes)."""
        subdir = cave_env.tmp_path / "dir1"
        subdir.mkdir()
        repo = make_repo(subdir / "proj")
        wrapper.cmd_add([str(repo)])

        new_parent = cave_env.tmp_path / "dir2"
        new_parent.mkdir()
        new_dir = new_parent / "proj"
        repo.rename(new_dir)

        wrapper.cmd_move("proj", str(new_dir))
        projects = wrapper.load_projects()
        assert projects["proj"] == str(new_dir)


# --- cmd_reset ---

class TestCmdReset:
    def test_reset_recreates_gitdir(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        gitdir = cave_env.projects_dir / "proj" / "gitdir"
        assert gitdir.is_dir()

        # Add a marker file inside gitdir
        (gitdir / "marker").write_text("old")

        wrapper.cmd_reset(["proj"])
        assert gitdir.is_dir()
        assert not (gitdir / "marker").exists()

    def test_reset_with_running_session_exits(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        wrapper.register_session("s1", "proj", os.getpid())

        with pytest.raises(SystemExit):
            wrapper.cmd_reset(["proj"])

    def test_reset_unknown_exits(self, cave_env):
        with pytest.raises(SystemExit):
            wrapper.cmd_reset(["nonexistent"])

    def test_reset_warns_unpulled(self, cave_env, monkeypatch):
        """Reset with unpulled commits prompts for confirmation."""
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])

        # Make a cave commit that host doesn't have
        gitdir = cave_env.projects_dir / "proj" / "gitdir"
        cave_work = cave_env.tmp_path / "cave-work"
        cave_work.mkdir()
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave_work),
             "checkout", "HEAD", "--", "."],
            capture_output=True, env={**os.environ, **GIT_ENV},
        )
        (cave_work / "new.txt").write_text("cave only\n")
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave_work),
             "add", "new.txt"],
            capture_output=True, env={**os.environ, **GIT_ENV},
        )
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave_work),
             "commit", "-m", "cave work"],
            capture_output=True, env={**os.environ, **GIT_ENV},
        )

        # Decline confirmation
        monkeypatch.setattr("builtins.input", lambda _: "n")
        wrapper.cmd_reset(["proj"])

        # Gitdir should NOT be wiped (user declined)
        head = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        assert head.returncode == 0

    def test_reset_from_cwd(self, cave_env, monkeypatch):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        monkeypatch.chdir(repo)
        wrapper.cmd_reset([])
        # Should succeed without error


# --- project_sync_status ---

class TestProjectSyncStatus:
    def test_synced(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        status = wrapper.project_sync_status("proj", str(repo))
        assert status == ""

    def test_to_pull(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])

        # Make a cave commit
        gitdir = cave_env.projects_dir / "proj" / "gitdir"
        cave_work = cave_env.tmp_path / "cave-work"
        cave_work.mkdir()
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave_work),
             "checkout", "HEAD", "--", "."],
            capture_output=True, env={**os.environ, **GIT_ENV},
        )
        (cave_work / "new.txt").write_text("cave\n")
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave_work),
             "add", "new.txt"],
            capture_output=True, env={**os.environ, **GIT_ENV},
        )
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave_work),
             "commit", "-m", "cave commit"],
            capture_output=True, env={**os.environ, **GIT_ENV},
        )

        status = wrapper.project_sync_status("proj", str(repo))
        assert "to pull" in status

    def test_to_push(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])

        # Make a host commit
        (repo / "new.txt").write_text("host\n")
        git("git", "-C", str(repo), "add", ".")
        git("git", "-C", str(repo), "commit", "-m", "host commit")

        status = wrapper.project_sync_status("proj", str(repo))
        assert "to push" in status

    def test_missing_project_dir(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        import shutil
        shutil.rmtree(repo)
        status = wrapper.project_sync_status("proj", str(repo))
        assert status == "missing"

    def test_no_gitdir(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.cmd_add([str(repo)])
        import shutil
        shutil.rmtree(cave_env.projects_dir / "proj" / "gitdir")
        status = wrapper.project_sync_status("proj", str(repo))
        assert status == "no gitdir"


# --- _parse_oneline / _print_rewrite_summary ---

class TestRewriteHelpers:
    """Test the pull_project helper functions for rewrite messaging."""

    def _make_completed_process(self, stdout, returncode=0):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="",
        )

    def test_force_pull_shows_amend_label(self, tmp_path, monkeypatch, capsys):
        """Force pull output shows amended vs new commits."""
        slug = "test-project"

        host = tmp_path / "host"
        host.mkdir()
        subprocess.run(["git", "-C", str(host), "init", "-b", "main"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(host), "config", "user.email", "h@t"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(host), "config", "user.name", "Host"],
                       capture_output=True)
        (host / "file.txt").write_text("initial\n")
        subprocess.run(["git", "-C", str(host), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(host), "commit", "-m", "initial"],
                       capture_output=True)

        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        tmpclone = tmp_path / "_tmpclone"
        subprocess.run(["git", "clone", "--no-checkout", "--local",
                        str(host), str(tmpclone)], capture_output=True)
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        subprocess.run(["git", "--git-dir", str(gitdir), "remote", "remove",
                        "origin"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "config",
                        "user.email", "cave@t"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "config",
                        "user.name", "cave-coder"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "read-tree", "HEAD"],
                       capture_output=True)

        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(wrapper, "load_projects", lambda: {slug: str(host)})
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            monkeypatch.delenv(var, raising=False)

        wrapper.ensure_cave_remote(host, gitdir)
        host_head = subprocess.run(
            ["git", "-C", str(host), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        wrapper.sync_refs(gitdir, host, host_head, "main")

        # Cave: make a commit, pull it, then amend + add new
        cave = tmp_path / "cave"
        cave.mkdir()
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
                        "checkout", "HEAD", "--", "."], capture_output=True)
        (cave / "file.txt").write_text("v1\n")
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
                        "add", "file.txt"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
                        "commit", "-m", "cave work"], capture_output=True)
        wrapper.pull_project(slug)

        # Now amend the commit and add a new one
        (cave / "file.txt").write_text("v2\n")
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
                        "add", "file.txt"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
                        "commit", "--amend", "-m", "cave work"],
                       capture_output=True)
        (cave / "extra.txt").write_text("bonus\n")
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
                        "add", "extra.txt"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
                        "commit", "-m", "extra work"], capture_output=True)

        # Without force: should refuse and show diff
        capsys.readouterr()  # clear
        wrapper.pull_project(slug)
        refuse_output = capsys.readouterr().err
        assert "rewrote history" in refuse_output
        assert "1 -> 2" in refuse_output
        assert "->" in refuse_output  # amended commit
        assert "+" in refuse_output   # new commit

        # With force: should succeed
        capsys.readouterr()
        wrapper.pull_project(slug, force=True)
        force_output = capsys.readouterr().err
        assert "1 -> 2" in force_output
        assert (host / "extra.txt").read_text() == "bonus\n"


# --- ensure_project ---

class TestEnsureProject:
    def test_creates_overlay_dirs(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.ensure_project(repo)
        assert (cave_env.projects_dir / "proj" / "upper").is_dir()
        assert (cave_env.projects_dir / "proj" / "work").is_dir()

    def test_creates_gitdir(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.ensure_project(repo)
        assert (cave_env.projects_dir / "proj" / "gitdir").is_dir()

    def test_returns_slug(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "my-project")
        slug = wrapper.ensure_project(repo)
        assert slug == "my-project"

    def test_idempotent(self, cave_env):
        repo = make_repo(cave_env.tmp_path / "proj")
        wrapper.ensure_project(repo)
        wrapper.ensure_project(repo)
        assert "proj" in wrapper.load_projects()


# --- clean_stale_session_overlays ---

class TestCleanStaleOverlays:
    def test_removes_orphaned_overlay(self, cave_env):
        overlay_root = cave_env.cave_data / "session-overlays"
        stale = overlay_root / "dead-session-123"
        stale.mkdir(parents=True)
        (stale / "marker.txt").write_text("stale")

        wrapper.clean_stale_session_overlays()
        assert not stale.exists()

    def test_keeps_active_overlay(self, cave_env):
        sid = "active-session"
        wrapper.register_session(sid, "proj", os.getpid())
        overlay_root = cave_env.cave_data / "session-overlays"
        active = overlay_root / sid
        active.mkdir(parents=True)
        (active / "marker.txt").write_text("keep")

        wrapper.clean_stale_session_overlays()
        assert active.exists()
