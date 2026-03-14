"""Tests for coding-cave cp: file transfer to/from running sessions."""
import pytest

import wrapper
from coding_cave import _state
from tests.conftest import git_run, write_projects, write_session


@pytest.fixture
def cp_env(tmp_path):
    """Environment with a running session, simulating the overlay merged dir.

    Returns namespace with: host, slug, merged (overlay merged dir), tmp_path.
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

    # Session overlay merged dir (simulates fuse-overlayfs merged view)
    sid = "test-session-1"
    merged = cave_data / "session-overlays" / sid / f"{slug}-merged"
    merged.mkdir(parents=True)
    # Populate with "host" files (simulates overlay lower + upper)
    (merged / "file.txt").write_text("initial\n")

    # Session tracking
    write_session(cave_data, sid, slug)

    # Project registry
    (_state.projects_dir() / slug).mkdir(parents=True)
    write_projects(cave_data, {slug: str(host)})

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
    """cp fails gracefully when no session is running."""

    def test_no_session_exits(self, cp_env):
        e = cp_env
        # Remove the session file
        for f in _state.sessions_dir().iterdir():
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
    """cp can access other projects mounted in the same session."""

    @pytest.fixture
    def multi_env(self, cp_env):
        """Extend cp_env with a second project in the same session."""
        e = cp_env
        slug2 = "other-project"
        host2 = e.tmp_path / "host2"
        host2.mkdir()
        git_run("git", "init", "-b", "main", str(host2))
        (host2 / "other.txt").write_text("from other\n")
        git_run("git", "-C", str(host2), "add", ".")
        git_run("git", "-C", str(host2), "commit", "-m", "initial")

        cave_data = _state.cave_data()

        # Create merged overlay for second project in same session
        sid = "test-session-1"
        merged2 = cave_data / "session-overlays" / sid / f"{slug2}-merged"
        merged2.mkdir(parents=True)
        (merged2 / "other.txt").write_text("from other\n")

        # Register second project
        (_state.projects_dir() / slug2).mkdir(parents=True, exist_ok=True)
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


class TestCpNoColonSyntax:
    """cp requires colon syntax for at least one argument."""

    def test_no_colon_exits(self, cp_env):
        e = cp_env
        with pytest.raises(SystemExit):
            wrapper.cmd_cp(["/tmp/a"], "/tmp/b")
