"""Tests for coding-cave cp: file transfer to/from running sessions."""
import os
import subprocess

import pytest

import wrapper
from coding_cave import _state, commands
from tests.conftest import git_run, write_projects, write_session


@pytest.fixture
def cp_env(tmp_path, monkeypatch):
    """Environment simulating a cave filesystem via a fake /proc/pid/root.

    Creates a fake cave root with /projects/<slug>/ populated with test files.
    Mocks _find_cave_root() to return the fake root.

    Returns namespace with: host, slug, merged (project dir in fake root), cave_root, tmp_path.
    """
    slug = "test-project"

    # Host repo
    host = tmp_path / "host"
    host.mkdir()
    git_run("git", "init", "-b", "main", str(host))
    (host / "file.txt").write_text("initial\n")
    git_run("git", "-C", str(host), "add", ".")
    git_run("git", "-C", str(host), "commit", "-m", "initial")

    # Set up cave data
    cave_data = tmp_path / "cave-data"
    _state.set_cave_data(cave_data)

    # Fake cave root (simulates /proc/<pid>/root)
    cave_root = tmp_path / "cave-root"
    merged = cave_root / "projects" / slug
    merged.mkdir(parents=True)
    (merged / "file.txt").write_text("initial\n")

    # Mock _find_cave_root to return our fake root
    monkeypatch.setattr(commands, "_find_cave_root", lambda project=None: cave_root)

    # Session tracking (needed so _find_cave_root's session lookup succeeds,
    # though we've mocked the function itself)
    write_session(cave_data, "test-session-1", slug)

    # Project registry
    (_state.projects_dir() / slug).mkdir(parents=True)
    write_projects(cave_data, {slug: str(host)})

    class Env:
        pass
    e = Env()
    e.host = host
    e.slug = slug
    e.merged = merged
    e.cave_root = cave_root
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

        wrapper.cmd_cp([str(src_dir)], f"{e.slug}:mydir", recursive=True)

        assert (e.merged / "mydir" / "one.txt").read_text() == "1\n"
        assert (e.merged / "mydir" / "two.txt").read_text() == "2\n"

    def test_copy_directory_without_R_exits(self, cp_env):
        e = cp_env
        src_dir = e.tmp_path / "mydir"
        src_dir.mkdir()
        (src_dir / "one.txt").write_text("1\n")

        with pytest.raises(SystemExit):
            wrapper.cmd_cp([str(src_dir)], f"{e.slug}:mydir")

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

        wrapper.cmd_cp([f"{e.slug}:subdir"], str(dest), recursive=True)

        assert (dest / "a.txt").read_text() == "a\n"
        assert (dest / "b.txt").read_text() == "b\n"

    def test_copy_directory_out_without_R_exits(self, cp_env):
        e = cp_env
        (e.merged / "subdir").mkdir()
        (e.merged / "subdir" / "a.txt").write_text("a\n")
        dest = e.tmp_path / "output-dir"

        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:subdir"], str(dest))

    def test_missing_cave_source_exits(self, cp_env):
        e = cp_env
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:nonexistent.txt"], str(e.tmp_path / "out"))


class TestCpNoSession:
    """cp with no running session falls back to host source for project paths."""

    @pytest.fixture
    def no_session_env(self, tmp_path):
        slug = "test-project"
        cave_data = tmp_path / "cave-data"
        _state.set_cave_data(cave_data)
        (cave_data / "sessions").mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "f.txt").write_text("host content\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "init")
        write_projects(cave_data, {slug: str(host)})
        (_state.projects_dir() / slug).mkdir(parents=True)

        class Env:
            pass
        e = Env()
        e.slug = slug
        e.host = host
        e.tmp_path = tmp_path
        return e

    def test_cave_to_host_falls_back(self, no_session_env):
        """Reading from a project path without a session uses host source."""
        e = no_session_env
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp([f"{e.slug}:f.txt"], str(dest))

        assert dest.read_text() == "host content\n"

    def test_host_to_cave_falls_back(self, no_session_env):
        """Writing to a project path without a session writes to host source."""
        e = no_session_env
        src = e.tmp_path / "data.txt"
        src.write_text("data\n")

        wrapper.cmd_cp([str(src)], f"{e.slug}:data.txt")

        assert (e.host / "data.txt").read_text() == "data\n"

    def test_non_project_path_exits(self, no_session_env):
        """Non-project paths (e.g. /tmp) require a running session."""
        e = no_session_env
        src = e.tmp_path / "data.txt"
        src.write_text("data\n")

        with pytest.raises(SystemExit):
            wrapper.cmd_cp([str(src)], f"{e.slug}:/tmp/data.txt")


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


class TestCpClobberProtection:
    """cave->host prompts before overwriting existing files."""

    def test_existing_file_prompts_and_aborts(self, cp_env, monkeypatch):
        e = cp_env
        (e.merged / "data.txt").write_text("cave data\n")
        dest = e.tmp_path / "data.txt"
        dest.write_text("host original\n")

        monkeypatch.setattr("builtins.input", lambda _: "n")
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:data.txt"], str(dest))

        assert dest.read_text() == "host original\n"

    def test_existing_file_prompts_and_proceeds(self, cp_env, monkeypatch):
        e = cp_env
        (e.merged / "data.txt").write_text("cave data\n")
        dest = e.tmp_path / "data.txt"
        dest.write_text("host original\n")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        wrapper.cmd_cp([f"{e.slug}:data.txt"], str(dest))

        assert dest.read_text() == "cave data\n"

    def test_force_skips_prompt(self, cp_env):
        e = cp_env
        (e.merged / "data.txt").write_text("cave data\n")
        dest = e.tmp_path / "data.txt"
        dest.write_text("host original\n")

        wrapper.cmd_cp([f"{e.slug}:data.txt"], str(dest), force=True)

        assert dest.read_text() == "cave data\n"

    def test_no_prompt_when_dest_new(self, cp_env):
        """No prompt when destination doesn't exist."""
        e = cp_env
        (e.merged / "new.txt").write_text("new\n")
        dest = e.tmp_path / "new.txt"

        # Would fail if input() were called (no monkeypatch)
        wrapper.cmd_cp([f"{e.slug}:new.txt"], str(dest))

        assert dest.read_text() == "new\n"

    def test_existing_dir_prompts(self, cp_env, monkeypatch):
        e = cp_env
        (e.merged / "subdir").mkdir()
        (e.merged / "subdir" / "a.txt").write_text("a\n")
        dest = e.tmp_path / "subdir"
        dest.mkdir()
        (dest / "old.txt").write_text("old\n")

        monkeypatch.setattr("builtins.input", lambda _: "n")
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:subdir"], str(e.tmp_path), recursive=True)

        # Original file untouched
        assert (dest / "old.txt").read_text() == "old\n"
        assert not (dest / "a.txt").exists()

    def test_host_to_cave_no_prompt(self, cp_env):
        """host->cave direction never prompts, even when overwriting."""
        e = cp_env
        src = e.tmp_path / "file.txt"
        src.write_text("overwritten\n")

        # file.txt already exists in merged from cp_env fixture
        wrapper.cmd_cp([str(src)], f"{e.slug}:file.txt")

        assert (e.merged / "file.txt").read_text() == "overwritten\n"


class TestCpTildeExpansion:
    """~ in cave paths resolves to the persistent home directory."""

    def test_tilde_copy_from_home(self, cp_env):
        e = cp_env
        home = _state.cave_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "notes.txt").write_text("from home\n")
        dest = e.tmp_path / "notes.txt"

        wrapper.cmd_cp([f"{e.slug}:~/notes.txt"], str(dest))

        assert dest.read_text() == "from home\n"

    def test_tilde_copy_to_home(self, cp_env):
        e = cp_env
        home = _state.cave_home()
        home.mkdir(parents=True, exist_ok=True)
        src = e.tmp_path / "upload.txt"
        src.write_text("to home\n")

        wrapper.cmd_cp([str(src)], f"{e.slug}:~/upload.txt")

        assert (home / "upload.txt").read_text() == "to home\n"

    def test_tilde_subdir(self, cp_env):
        e = cp_env
        home = _state.cave_home()
        subdir = home / "deep" / "dir"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "file.txt").write_text("deep\n")
        dest = e.tmp_path / "file.txt"

        wrapper.cmd_cp([f"{e.slug}:~/deep/dir/file.txt"], str(dest))

        assert dest.read_text() == "deep\n"

    def test_tilde_with_cave_prefix(self, cp_env, monkeypatch):
        e = cp_env
        monkeypatch.chdir(e.host)
        home = _state.cave_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "data.txt").write_text("via cave:\n")
        dest = e.tmp_path / "data.txt"

        wrapper.cmd_cp(["cave:~/data.txt"], str(dest))

        assert dest.read_text() == "via cave:\n"

    def test_tilde_no_session_needed(self, cp_env):
        """Home paths work even with no running session."""
        e = cp_env
        # Remove all sessions
        for f in _state.sessions_dir().iterdir():
            f.unlink()

        home = _state.cave_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "orphan.txt").write_text("no session\n")
        dest = e.tmp_path / "orphan.txt"

        wrapper.cmd_cp([f"{e.slug}:~/orphan.txt"], str(dest))

        assert dest.read_text() == "no session\n"

    def test_home_cave_coder_prefix(self, cp_env):
        """Absolute /home/cave-coder/ paths resolve to cave home."""
        e = cp_env
        home = _state.cave_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "abs.txt").write_text("absolute\n")
        dest = e.tmp_path / "abs.txt"

        wrapper.cmd_cp([f"{e.slug}:/home/cave-coder/abs.txt"], str(dest))

        assert dest.read_text() == "absolute\n"

    def test_tilde_recursive(self, cp_env):
        e = cp_env
        home = _state.cave_home()
        subdir = home / "mydir"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "one.txt").write_text("1\n")
        (subdir / "two.txt").write_text("2\n")
        dest = e.tmp_path / "mydir"

        wrapper.cmd_cp([f"{e.slug}:~/mydir"], str(dest), recursive=True)

        assert (dest / "one.txt").read_text() == "1\n"
        assert (dest / "two.txt").read_text() == "2\n"


class TestCpCrossProject:
    """cp can access other projects in the cave filesystem."""

    @pytest.fixture
    def multi_env(self, cp_env):
        """Extend cp_env with a second project in the fake cave root."""
        e = cp_env
        slug2 = "other-project"
        host2 = e.tmp_path / "host2"
        host2.mkdir()
        git_run("git", "init", "-b", "main", str(host2))
        (host2 / "other.txt").write_text("from other\n")
        git_run("git", "-C", str(host2), "add", ".")
        git_run("git", "-C", str(host2), "commit", "-m", "initial")

        # Create second project in the fake cave root
        merged2 = e.cave_root / "projects" / slug2
        merged2.mkdir(parents=True)
        (merged2 / "other.txt").write_text("from other\n")

        # Register second project
        import json
        proj = json.loads((_state.projects_file()).read_text())
        proj[slug2] = str(host2)
        _state.projects_file().write_text(json.dumps(proj))

        e.slug2 = slug2
        e.host2 = host2
        e.merged2 = merged2
        return e

    def test_cross_project_by_slug(self, multi_env):
        e = multi_env
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp([f"{e.slug2}:other.txt"], str(dest))

        assert dest.read_text() == "from other\n"

    def test_cross_project_by_path(self, multi_env):
        e = multi_env
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp([f"{e.slug}:/projects/{e.slug2}/other.txt"], str(dest))

        assert dest.read_text() == "from other\n"

    def test_cross_project_cave_prefix(self, multi_env, monkeypatch):
        e = multi_env
        monkeypatch.chdir(e.host)
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp([f"cave:/projects/{e.slug2}/other.txt"], str(dest))

        assert dest.read_text() == "from other\n"

    def test_cross_project_copy_into(self, multi_env):
        e = multi_env
        src = e.tmp_path / "new.txt"
        src.write_text("into other\n")

        wrapper.cmd_cp([str(src)], f"{e.slug2}:new.txt")

        assert (e.merged2 / "new.txt").read_text() == "into other\n"


class TestCpArbitraryPaths:
    """cp can access arbitrary cave filesystem paths via /proc/pid/root."""

    def test_copy_from_tmp(self, cp_env):
        e = cp_env
        tmp = e.cave_root / "tmp"
        tmp.mkdir()
        (tmp / "data.txt").write_text("from /tmp\n")
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp([f"{e.slug}:/tmp/data.txt"], str(dest))

        assert dest.read_text() == "from /tmp\n"

    def test_copy_to_tmp(self, cp_env):
        e = cp_env
        tmp = e.cave_root / "tmp"
        tmp.mkdir()
        src = e.tmp_path / "upload.txt"
        src.write_text("to /tmp\n")

        wrapper.cmd_cp([str(src)], f"{e.slug}:/tmp/upload.txt")

        assert (tmp / "upload.txt").read_text() == "to /tmp\n"

    def test_copy_dir_from_arbitrary_path(self, cp_env):
        e = cp_env
        src_dir = e.cave_root / "tmp" / "mydir"
        src_dir.mkdir(parents=True)
        (src_dir / "a.txt").write_text("a\n")
        (src_dir / "b.txt").write_text("b\n")
        dest = e.tmp_path / "mydir"

        wrapper.cmd_cp([f"{e.slug}:/tmp/mydir"], str(dest), recursive=True)

        assert (dest / "a.txt").read_text() == "a\n"
        assert (dest / "b.txt").read_text() == "b\n"

    def test_nonexistent_arbitrary_path_exits(self, cp_env):
        e = cp_env
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:/tmp/nonexistent"], str(e.tmp_path / "out"))

    def test_nix_store_path(self, cp_env):
        e = cp_env
        store_path = e.cave_root / "nix" / "store" / "abc-pkg" / "bin"
        store_path.mkdir(parents=True)
        (store_path / "tool").write_text("#!/bin/sh\n")
        dest = e.tmp_path / "tool"

        wrapper.cmd_cp([f"{e.slug}:/nix/store/abc-pkg/bin/tool"], str(dest))

        assert dest.read_text() == "#!/bin/sh\n"

    def test_cave_prefix_arbitrary_path(self, cp_env, monkeypatch):
        e = cp_env
        monkeypatch.chdir(e.host)
        tmp = e.cave_root / "tmp"
        tmp.mkdir(exist_ok=True)
        (tmp / "data.txt").write_text("via cave:\n")
        dest = e.tmp_path / "out.txt"

        wrapper.cmd_cp(["cave:/tmp/data.txt"], str(dest))

        assert dest.read_text() == "via cave:\n"


class TestCpNoColonSyntax:
    """cp requires colon syntax for at least one argument."""

    def test_no_colon_exits(self, cp_env):
        e = cp_env
        with pytest.raises(SystemExit):
            wrapper.cmd_cp(["/tmp/a"], "/tmp/b")


class TestCpSessionSelection:
    """Session selection prefers sessions for the target project."""

    def test_single_project_session_auto_selects(self, tmp_path, monkeypatch):
        """One session for the project → auto-selected, no prompt."""
        slug = "myproject"
        cave_data = tmp_path / "cave-data"
        _state.set_cave_data(cave_data)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "f.txt").write_text("x\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "init")
        write_projects(cave_data, {slug: str(host), "other": str(host)})
        (_state.projects_dir() / slug).mkdir(parents=True)

        # Two sessions: one for myproject, one for other
        write_session(cave_data, "sess-mine", slug)
        write_session(cave_data, "sess-other", "other")

        cave_root = tmp_path / "cave-root"
        merged = cave_root / "projects" / slug
        merged.mkdir(parents=True)
        (merged / "f.txt").write_text("from cave\n")

        # _find_cave_root should auto-select sess-mine without prompting.
        # We mock it to verify it receives the project hint.
        called_with = []
        def mock_find(project=None):
            called_with.append(project)
            return cave_root
        monkeypatch.setattr(commands, "_find_cave_root", mock_find)

        dest = tmp_path / "out.txt"
        wrapper.cmd_cp([f"{slug}:f.txt"], str(dest))

        assert called_with == [slug]
        assert dest.read_text() == "from cave\n"

    def test_no_project_sessions_uses_any(self, tmp_path, monkeypatch):
        """No sessions for this project but others exist → uses any session."""
        slug = "myproject"
        cave_data = tmp_path / "cave-data"
        _state.set_cave_data(cave_data)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        write_projects(cave_data, {slug: str(host), "other": str(host)})
        (_state.projects_dir() / slug).mkdir(parents=True)

        # Only a session for "other"
        write_session(cave_data, "sess-other", "other")

        cave_root = tmp_path / "cave-root"
        merged = cave_root / "projects" / slug
        merged.mkdir(parents=True)
        (merged / "f.txt").write_text("cross-session\n")

        monkeypatch.setattr(commands, "_find_cave_root", lambda project=None: cave_root)

        dest = tmp_path / "out.txt"
        wrapper.cmd_cp([f"{slug}:f.txt"], str(dest))

        assert dest.read_text() == "cross-session\n"


class TestCpBwrapIntegration:
    """Integration tests using a real bwrap subprocess."""

    @pytest.fixture
    def bwrap_env(self, tmp_path):
        """Start a bwrap process and provide its /proc/pid/root path."""
        bash = subprocess.run(["which", "bash"], capture_output=True, text=True).stdout.strip()
        proc = subprocess.Popen(
            ["bwrap",
             "--ro-bind", "/nix/store", "/nix/store",
             "--tmpfs", "/tmp",
             "--proc", "/proc",
             "--dev", "/dev",
             "--", bash, "-c",
             "echo integration > /tmp/bwrap-test.txt; "
             "mkdir -p /tmp/testdir; "
             "echo one > /tmp/testdir/one.txt; "
             "echo two > /tmp/testdir/two.txt; "
             "sleep 60"],
        )
        import time
        time.sleep(0.3)

        # Find inner PID
        inner_pid = None
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                from pathlib import Path
                stat = Path(entry.path, "stat").read_text()
                close_paren = stat.rfind(")")
                fields = stat[close_paren + 2:].split()
                ppid = int(fields[1])
                if ppid == proc.pid:
                    inner_pid = int(entry.name)
                    break
            except (FileNotFoundError, ValueError, PermissionError, IndexError):
                continue

        if inner_pid is None:
            proc.kill()
            proc.wait()
            pytest.skip("Could not find bwrap child PID")

        root = Path(f"/proc/{inner_pid}/root")
        if not root.is_dir():
            proc.kill()
            proc.wait()
            pytest.skip("/proc/pid/root not accessible")

        yield root, tmp_path

        proc.kill()
        proc.wait()

    def test_read_file_from_bwrap(self, bwrap_env):
        root, tmp_path = bwrap_env
        dest = tmp_path / "out.txt"
        import shutil
        src = root / "tmp" / "bwrap-test.txt"
        if not src.exists():
            pytest.skip("bwrap file not ready")
        shutil.copy2(src, dest)
        assert dest.read_text() == "integration\n"

    def test_write_file_to_bwrap(self, bwrap_env):
        root, tmp_path = bwrap_env
        target = root / "tmp" / "written.txt"
        target.write_text("from host\n")
        assert target.read_text() == "from host\n"

    def test_copy_dir_from_bwrap(self, bwrap_env):
        root, tmp_path = bwrap_env
        import shutil
        src = root / "tmp" / "testdir"
        if not src.is_dir():
            pytest.skip("bwrap dir not ready")
        dest = tmp_path / "testdir"
        shutil.copytree(src, dest)
        assert (dest / "one.txt").read_text() == "one\n"
        assert (dest / "two.txt").read_text() == "two\n"


class TestCpSymlinkEscape:
    """Symlinks in the cave must not exfiltrate host files."""

    def test_symlink_in_cave_blocked(self, cp_env):
        """Symlink inside cave pointing outside is refused on copy-out."""
        e = cp_env
        secret = e.tmp_path / "secret" / "id_ed25519"
        secret.parent.mkdir()
        secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nSECRET\n")

        link = e.merged / "readme.txt"
        link.symlink_to(secret)

        dest = e.tmp_path / "output.txt"
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:readme.txt"], str(dest), force=True)
        assert not dest.exists()

    def test_symlink_to_host_directory_blocked(self, cp_env):
        """Symlink to a host directory is refused on copy-out."""
        e = cp_env
        ssh_dir = e.tmp_path / "fake-ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_ed25519").write_text("private key\n")

        (e.merged / "docs").symlink_to(ssh_dir)

        dest = e.tmp_path / "stolen"
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:docs"], str(dest), recursive=True, force=True)
        assert not dest.exists()

    def test_symlink_host_to_cave_replaces_link(self, cp_env):
        """Host->cave copy replaces symlink instead of following it."""
        e = cp_env
        host_file = e.tmp_path / "host-config.txt"
        host_file.write_text("original\n")

        (e.merged / "config.txt").symlink_to(host_file)

        payload = e.tmp_path / "payload.txt"
        payload.write_text("new content\n")
        wrapper.cmd_cp([str(payload)], f"{e.slug}:config.txt")

        # Host file untouched, cave file replaced
        assert host_file.read_text() == "original\n"
        assert (e.merged / "config.txt").read_text() == "new content\n"
        assert not (e.merged / "config.txt").is_symlink()

    def test_symlink_inside_directory_skipped(self, cp_env):
        """Symlinks inside a copied directory are skipped."""
        e = cp_env
        secret = e.tmp_path / "host-secret.txt"
        secret.write_text("secret\n")

        subdir = e.merged / "mixed"
        subdir.mkdir()
        (subdir / "real.txt").write_text("safe\n")
        (subdir / "link.txt").symlink_to(secret)

        dest = e.tmp_path / "output"
        wrapper.cmd_cp([f"{e.slug}:mixed"], str(dest), recursive=True, force=True)

        assert (dest / "real.txt").read_text() == "safe\n"
        assert not (dest / "link.txt").exists()


class TestCpPathTraversal:
    """Path traversal via .. must not escape boundary directories."""

    def test_tilde_dotdot_blocked(self, cp_env):
        """~/.. traversal out of cave home is rejected."""
        e = cp_env
        cave_home = _state.cave_home()
        cave_home.mkdir(parents=True, exist_ok=True)

        escaped = cave_home.parent / "outside-home.txt"
        escaped.write_text("host data\n")

        dest = e.tmp_path / "stolen.txt"
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:~/../../outside.txt"], str(dest), force=True)
        assert not dest.exists()

    def test_tilde_dotdot_write_blocked(self, cp_env):
        """~/.. traversal on write is rejected."""
        e = cp_env
        cave_home = _state.cave_home()
        cave_home.mkdir(parents=True, exist_ok=True)

        payload = e.tmp_path / "payload.txt"
        payload.write_text("escaped!\n")

        with pytest.raises(SystemExit):
            wrapper.cmd_cp([str(payload)], f"{e.slug}:~/../../outside.txt")

    def test_fallback_dotdot_blocked(self, cp_env, monkeypatch):
        """.. traversal in sessionless fallback to host source is rejected."""
        e = cp_env
        # Disable session so it falls back to host source dir
        monkeypatch.setattr(commands, "_find_cave_root", lambda project=None: None)

        escaped = e.host.parent / "sensitive.txt"
        escaped.write_text("host secrets\n")

        dest = e.tmp_path / "stolen.txt"
        with pytest.raises(SystemExit):
            wrapper.cmd_cp([f"{e.slug}:../sensitive.txt"], str(dest), force=True)
        assert not dest.exists()
