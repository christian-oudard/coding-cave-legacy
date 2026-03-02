"""Tests for coding-cave cp: file transfer to/from running sessions."""
import json
import os
import subprocess
from pathlib import Path

import pytest

import wrapper


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(*args, **kwargs):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(list(args), capture_output=True, text=True, env=env, **kwargs)


@pytest.fixture
def cp_env(tmp_path, monkeypatch):
    """Environment with a running session, simulating the overlay merged dir.

    Returns namespace with: host, slug, merged (overlay merged dir), sessions_dir.
    """
    slug = "test-project"

    # Host repo
    host = tmp_path / "host"
    host.mkdir()
    git("git", "init", "-b", "main", str(host))
    (host / "file.txt").write_text("initial\n")
    git("git", "-C", str(host), "add", ".")
    git("git", "-C", str(host), "commit", "-m", "initial")

    # Set up CAVE_DATA
    cave_data = tmp_path / "cave-data"
    monkeypatch.setattr(wrapper, "CAVE_DATA", cave_data)

    # Session overlay merged dir (simulates fuse-overlayfs merged view)
    sid = "test-session-1"
    merged = cave_data / "session-overlays" / sid / f"{slug}-merged"
    merged.mkdir(parents=True)
    # Populate with "host" files (simulates overlay lower + upper)
    (merged / "file.txt").write_text("initial\n")

    # Session tracking
    sessions_dir = cave_data / "sessions"
    sessions_dir.mkdir(parents=True)
    monkeypatch.setattr(wrapper, "SESSIONS_DIR", sessions_dir)
    (sessions_dir / f"{sid}.json").write_text(json.dumps({
        "sid": sid, "project": slug,
        "bwrap_pid": os.getpid(), "started": 0,
    }))

    # Project registry
    projects_dir = cave_data / "projects"
    (projects_dir / slug).mkdir(parents=True)
    monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(wrapper, "load_projects", lambda: {slug: str(host)})

    class Env:
        pass
    e = Env()
    e.host = host
    e.slug = slug
    e.merged = merged
    e.tmp_path = tmp_path
    return e


class TestCpHostToCave:
    """Copy files from host into a running cave session."""

    def test_copy_single_file(self, cp_env):
        e = cp_env
        src = e.tmp_path / "data.txt"
        src.write_text("hello from host\n")

        wrapper.cmd_cp([str(src)], f"{e.slug}:data.txt")

        assert (e.merged / "data.txt").read_text() == "hello from host\n"

    def test_copy_to_subdirectory(self, cp_env):
        e = cp_env
        src = e.tmp_path / "config.json"
        src.write_text('{"key": "value"}\n')

        wrapper.cmd_cp([str(src)], f"{e.slug}:subdir/config.json")

        assert (e.merged / "subdir" / "config.json").read_text() == '{"key": "value"}\n'

    def test_copy_multiple_files(self, cp_env):
        e = cp_env
        a = e.tmp_path / "a.txt"
        b = e.tmp_path / "b.txt"
        a.write_text("aaa\n")
        b.write_text("bbb\n")

        # Destination must be a directory for multiple sources
        (e.merged / "dest").mkdir()
        wrapper.cmd_cp([str(a), str(b)], f"{e.slug}:dest")

        assert (e.merged / "dest" / "a.txt").read_text() == "aaa\n"
        assert (e.merged / "dest" / "b.txt").read_text() == "bbb\n"

    def test_copy_directory(self, cp_env):
        e = cp_env
        src_dir = e.tmp_path / "mydir"
        src_dir.mkdir()
        (src_dir / "one.txt").write_text("1\n")
        (src_dir / "two.txt").write_text("2\n")

        wrapper.cmd_cp([str(src_dir)], f"{e.slug}:mydir")

        assert (e.merged / "mydir" / "one.txt").read_text() == "1\n"
        assert (e.merged / "mydir" / "two.txt").read_text() == "2\n"

    def test_overwrite_existing_file(self, cp_env):
        e = cp_env
        src = e.tmp_path / "file.txt"
        src.write_text("overwritten\n")

        wrapper.cmd_cp([str(src)], f"{e.slug}:file.txt")

        assert (e.merged / "file.txt").read_text() == "overwritten\n"

    def test_missing_source_exits(self, cp_env):
        e = cp_env
        with pytest.raises(SystemExit):
            wrapper.cmd_cp(["/nonexistent/file.txt"], f"{e.slug}:dest.txt")


class TestCpCaveToHost:
    """Copy files from a running cave session to the host."""

    def test_copy_file_out(self, cp_env):
        e = cp_env
        (e.merged / "cave-work.txt").write_text("from cave\n")
        dest = e.tmp_path / "output.txt"

        wrapper.cmd_cp([f"{e.slug}:cave-work.txt"], str(dest))

        assert dest.read_text() == "from cave\n"

    def test_copy_file_to_existing_dir(self, cp_env):
        e = cp_env
        (e.merged / "result.txt").write_text("result\n")
        dest_dir = e.tmp_path / "output"
        dest_dir.mkdir()

        wrapper.cmd_cp([f"{e.slug}:result.txt"], str(dest_dir))

        assert (dest_dir / "result.txt").read_text() == "result\n"

    def test_copy_directory_out(self, cp_env):
        e = cp_env
        (e.merged / "subdir").mkdir()
        (e.merged / "subdir" / "a.txt").write_text("a\n")
        (e.merged / "subdir" / "b.txt").write_text("b\n")
        dest = e.tmp_path / "output-dir"

        wrapper.cmd_cp([f"{e.slug}:subdir"], str(dest))

        assert (dest / "a.txt").read_text() == "a\n"
        assert (dest / "b.txt").read_text() == "b\n"

    def test_missing_cave_source_exits(self, cp_env):
        e = cp_env
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:nonexistent.txt"], str(e.tmp_path / "out"))


class TestCpNoSession:
    """cp fails gracefully when no session is running."""

    def test_no_session_exits(self, cp_env):
        e = cp_env
        # Remove the session file
        for f in (wrapper.SESSIONS_DIR).iterdir():
            f.unlink()

        src = e.tmp_path / "data.txt"
        src.write_text("data\n")

        with pytest.raises(SystemExit):
            wrapper.cmd_cp([str(src)], f"{e.slug}:data.txt")


class TestCpCavePrefix:
    """'cave:' prefix auto-detects project from CWD."""

    def test_cave_prefix_copies_to_root(self, cp_env, monkeypatch):
        e = cp_env
        monkeypatch.chdir(e.host)
        src = e.tmp_path / "data.txt"
        src.write_text("via cave:\n")

        wrapper.cmd_cp([str(src)], "cave:")

        assert (e.merged / "data.txt").read_text() == "via cave:\n"

    def test_cave_prefix_with_subpath(self, cp_env, monkeypatch):
        e = cp_env
        monkeypatch.chdir(e.host)
        src = e.tmp_path / "config.json"
        src.write_text('{"ok": true}\n')

        wrapper.cmd_cp([str(src)], "cave:subdir/config.json")

        assert (e.merged / "subdir" / "config.json").read_text() == '{"ok": true}\n'

    def test_cave_prefix_copies_from(self, cp_env, monkeypatch):
        e = cp_env
        monkeypatch.chdir(e.host)
        (e.merged / "result.txt").write_text("from cave\n")
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp(["cave:result.txt"], str(dest))

        assert dest.read_text() == "from cave\n"

    def test_cave_prefix_outside_project_exits(self, cp_env, monkeypatch):
        e = cp_env
        monkeypatch.chdir(e.tmp_path)  # not inside any project
        src = e.tmp_path / "data.txt"
        src.write_text("data\n")

        with pytest.raises(SystemExit):
            wrapper.cmd_cp([str(src)], "cave:")


class TestCpAbsoluteCavePaths:
    """Absolute cave paths (/projects/<slug>/...) should resolve correctly."""

    def test_absolute_path_strips_prefix(self, cp_env):
        e = cp_env
        (e.merged / "deep" / "file.txt").parent.mkdir(parents=True, exist_ok=True)
        (e.merged / "deep" / "file.txt").write_text("found\n")
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp([f"{e.slug}:/projects/{e.slug}/deep/file.txt"], str(dest))

        assert dest.read_text() == "found\n"

    def test_absolute_path_copy_to_cave(self, cp_env):
        e = cp_env
        src = e.tmp_path / "input.txt"
        src.write_text("into cave\n")

        wrapper.cmd_cp([str(src)], f"{e.slug}:/projects/{e.slug}/input.txt")

        assert (e.merged / "input.txt").read_text() == "into cave\n"

    def test_relative_path_still_works(self, cp_env):
        e = cp_env
        (e.merged / "rel.txt").write_text("relative\n")
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp([f"{e.slug}:rel.txt"], str(dest))

        assert dest.read_text() == "relative\n"


class TestCpNoColonSyntax:
    """cp requires colon syntax for at least one argument."""

    def test_no_colon_exits(self, cp_env):
        e = cp_env
        with pytest.raises(SystemExit):
            wrapper.cmd_cp(["/tmp/a"], "/tmp/b")
