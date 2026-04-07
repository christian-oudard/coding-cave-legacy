"""Tests for cav cp: file transfer between host and cave agent worktrees."""

import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cav import config, registry
from cav.cp import (
    cp, _resolve_cave_path, _worktree_for_slug, _complete_cp_path,
    _expand_cave_glob,
)


def _make_worktree(data_dir: Path, slug: str, agent_id: str) -> Path:
    """Create a fake worktree directory structure."""
    ws = data_dir / "worktrees" / slug / agent_id
    ws.mkdir(parents=True)
    return ws


def _make_home(data_dir: Path) -> Path:
    """Create a fake persistent home directory."""
    home = data_dir / "home"
    home.mkdir(parents=True)
    return home


class CpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self.tmpdir) / "data"
        self.data_dir.mkdir()
        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        # Reload config so DATA_DIR picks up the env var
        config.DATA_DIR = Path(os.environ["CODING_CAVE_DATA_DIR"])

    def tearDown(self):
        os.environ.pop("CODING_CAVE_DATA_DIR", None)
        config.DATA_DIR = Path(
            os.environ.get("CODING_CAVE_DATA_DIR",
                           Path.home() / ".local" / "share" / "coding-cave")
        )
        shutil.rmtree(self.tmpdir)


class TestCpHostToProject(CpTestCase):
    """Copy files from host into a cave agent worktree."""

    def setUp(self):
        super().setUp()
        self.ws = _make_worktree(self.data_dir, "myproject", "1")

    def test_copy_single_file(self):
        src = Path(self.tmpdir) / "hello.txt"
        src.write_text("hello\n")

        cp([str(src)], "myproject:hello.txt")

        assert (self.ws / "hello.txt").read_text() == "hello\n"

    def test_copy_to_subdirectory(self):
        src = Path(self.tmpdir) / "conf.json"
        src.write_text('{"x": 1}\n')

        cp([str(src)], "myproject:sub/conf.json")

        assert (self.ws / "sub" / "conf.json").read_text() == '{"x": 1}\n'

    def test_copy_multiple_files_to_directory(self):
        a = Path(self.tmpdir) / "a.txt"
        b = Path(self.tmpdir) / "b.txt"
        a.write_text("aaa\n")
        b.write_text("bbb\n")
        (self.ws / "dest").mkdir()

        cp([str(a), str(b)], "myproject:dest")

        assert (self.ws / "dest" / "a.txt").read_text() == "aaa\n"
        assert (self.ws / "dest" / "b.txt").read_text() == "bbb\n"

    def test_copy_directory_requires_recursive(self):
        src_dir = Path(self.tmpdir) / "mydir"
        src_dir.mkdir()
        (src_dir / "f.txt").write_text("f\n")

        with self.assertRaises(SystemExit) as ctx:
            cp([str(src_dir)], "myproject:mydir")
        assert "-R" in str(ctx.exception)

    def test_copy_directory_recursive(self):
        src_dir = Path(self.tmpdir) / "mydir"
        src_dir.mkdir()
        (src_dir / "f.txt").write_text("f\n")

        cp([str(src_dir)], "myproject:mydir", recursive=True)

        assert (self.ws / "mydir" / "f.txt").read_text() == "f\n"

    def test_overwrite_prompt_yes(self):
        (self.ws / "existing.txt").write_text("old\n")
        src = Path(self.tmpdir) / "existing.txt"
        src.write_text("new\n")

        with patch("builtins.input", return_value="y"):
            cp([str(src)], "myproject:existing.txt")

        assert (self.ws / "existing.txt").read_text() == "new\n"

    def test_overwrite_prompt_no_aborts(self):
        (self.ws / "existing.txt").write_text("old\n")
        src = Path(self.tmpdir) / "existing.txt"
        src.write_text("new\n")

        with patch("builtins.input", return_value="n"):
            with self.assertRaises(SystemExit) as ctx:
                cp([str(src)], "myproject:existing.txt")
        assert "Aborted" in str(ctx.exception)
        assert (self.ws / "existing.txt").read_text() == "old\n"

    def test_force_skips_prompt(self):
        (self.ws / "existing.txt").write_text("old\n")
        src = Path(self.tmpdir) / "existing.txt"
        src.write_text("new\n")

        cp([str(src)], "myproject:existing.txt", force=True)

        assert (self.ws / "existing.txt").read_text() == "new\n"

    def test_overwrite_prompt_eof_aborts(self):
        (self.ws / "existing.txt").write_text("old\n")
        src = Path(self.tmpdir) / "existing.txt"
        src.write_text("new\n")

        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(SystemExit) as ctx:
                cp([str(src)], "myproject:existing.txt")
        assert "Aborted" in str(ctx.exception)
        assert (self.ws / "existing.txt").read_text() == "old\n"

    def test_copy_replaces_symlink(self):
        base = Path(self.tmpdir) / "real.txt"
        base.write_text("real\n")
        (self.ws / "link.txt").symlink_to(base)
        src = Path(self.tmpdir) / "link.txt"
        src.write_text("content\n")

        cp([str(src)], "myproject:link.txt", force=True)

        dest = self.ws / "link.txt"
        assert not dest.is_symlink()
        assert dest.read_text() == "content\n"

    def test_source_not_found(self):
        with self.assertRaises(SystemExit) as ctx:
            cp([str(Path(self.tmpdir) / "missing.txt")], "myproject:missing.txt")
        assert "not found" in str(ctx.exception).lower()

    def test_abs_cave_path(self):
        src = Path(self.tmpdir) / "data.txt"
        src.write_text("abs\n")

        cp([str(src)], "myproject:/projects/myproject/data.txt")

        assert (self.ws / "data.txt").read_text() == "abs\n"


class TestCpProjectToHost(CpTestCase):
    """Copy files from a cave agent worktree to the host."""

    def setUp(self):
        super().setUp()
        self.ws = _make_worktree(self.data_dir, "myproject", "1")
        (self.ws / "file.txt").write_text("from cave\n")

    def test_copy_file_to_host(self):
        dest = Path(self.tmpdir) / "out.txt"

        cp(["myproject:file.txt"], str(dest))

        assert dest.read_text() == "from cave\n"

    def test_copy_to_existing_directory(self):
        dest_dir = Path(self.tmpdir) / "outdir"
        dest_dir.mkdir()

        cp(["myproject:file.txt"], str(dest_dir))

        assert (dest_dir / "file.txt").read_text() == "from cave\n"

    def test_overwrite_prompt_yes(self):
        dest = Path(self.tmpdir) / "out.txt"
        dest.write_text("old\n")

        with patch("builtins.input", return_value="y"):
            cp(["myproject:file.txt"], str(dest))

        assert dest.read_text() == "from cave\n"

    def test_overwrite_prompt_no_aborts(self):
        dest = Path(self.tmpdir) / "out.txt"
        dest.write_text("old\n")

        with patch("builtins.input", return_value="n"):
            with self.assertRaises(SystemExit) as ctx:
                cp(["myproject:file.txt"], str(dest))
        assert "Aborted" in str(ctx.exception)
        assert dest.read_text() == "old\n"

    def test_force_skips_prompt(self):
        dest = Path(self.tmpdir) / "out.txt"
        dest.write_text("old\n")

        cp(["myproject:file.txt"], str(dest), force=True)

        assert dest.read_text() == "from cave\n"

    def test_refuses_symlink(self):
        real = self.ws / "real.txt"
        real.write_text("real\n")
        (self.ws / "link.txt").symlink_to(real)
        dest = Path(self.tmpdir) / "out.txt"

        with self.assertRaises(SystemExit) as ctx:
            cp(["myproject:link.txt"], str(dest))
        assert "symlink" in str(ctx.exception).lower()

    def test_copy_directory_requires_recursive(self):
        subdir = self.ws / "subdir"
        subdir.mkdir()
        (subdir / "x.txt").write_text("x\n")
        dest = Path(self.tmpdir) / "out"

        with self.assertRaises(SystemExit) as ctx:
            cp(["myproject:subdir"], str(dest))
        assert "-R" in str(ctx.exception)

    def test_copy_directory_recursive(self):
        subdir = self.ws / "subdir"
        subdir.mkdir()
        (subdir / "x.txt").write_text("x\n")
        dest = Path(self.tmpdir) / "out"

        cp(["myproject:subdir"], str(dest), recursive=True)

        assert (dest / "x.txt").read_text() == "x\n"

    def test_source_not_found(self):
        dest = Path(self.tmpdir) / "out.txt"
        with self.assertRaises(SystemExit) as ctx:
            cp(["myproject:missing.txt"], str(dest))
        assert "not found" in str(ctx.exception).lower()

    def test_skips_symlinks_in_recursive(self):
        subdir = self.ws / "subdir"
        subdir.mkdir()
        real = subdir / "real.txt"
        real.write_text("real\n")
        (subdir / "link.txt").symlink_to(real)
        dest = Path(self.tmpdir) / "out"

        cp(["myproject:subdir"], str(dest), recursive=True, force=True)

        assert (dest / "real.txt").exists()
        assert not (dest / "link.txt").exists()


class TestCpHome(CpTestCase):
    """Copy files to/from persistent cave home."""

    def setUp(self):
        super().setUp()
        self.home = _make_home(self.data_dir)

    def test_copy_to_home(self):
        src = Path(self.tmpdir) / "rc"
        src.write_text("rc content\n")

        cp([str(src)], "myproject:~/rc")

        assert (self.home / "rc").read_text() == "rc content\n"

    def test_copy_from_home(self):
        (self.home / "notes.txt").write_text("notes\n")
        dest = Path(self.tmpdir) / "out.txt"

        cp(["myproject:~/notes.txt"], str(dest))

        assert dest.read_text() == "notes\n"

    def test_home_tilde_only(self):
        """slug:~ resolves to the home directory root."""
        src = Path(self.tmpdir) / "item.txt"
        src.write_text("item\n")

        cp([str(src)], "myproject:~/item.txt")

        assert (self.home / "item.txt").read_text() == "item\n"

    def test_home_no_worktree_needed(self):
        """Home copies work even without a worktree for the slug."""
        src = Path(self.tmpdir) / "rc"
        src.write_text("rc\n")

        # No worktree exists for 'nosession', but ~/path should still work
        cp([str(src)], "nosession:~/rc")

        assert (self.home / "rc").read_text() == "rc\n"


class TestCpAgentSelection(CpTestCase):
    """Worktree selection when multiple agents exist."""

    def test_specific_agent_id(self):
        ws1 = _make_worktree(self.data_dir, "proj", "1")
        ws2 = _make_worktree(self.data_dir, "proj", "2")
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("for agent 2\n")

        cp([str(src)], "proj@2:f.txt")

        assert not (ws1 / "f.txt").exists()
        assert (ws2 / "f.txt").read_text() == "for agent 2\n"

    def test_multiple_agents_prompts_picker(self):
        ws1 = _make_worktree(self.data_dir, "proj", "1")
        ws2 = _make_worktree(self.data_dir, "proj", "2")
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("data\n")

        # Pick agent 2 (second in sorted list)
        with patch("builtins.input", return_value="2"):
            cp([str(src)], "proj:f.txt")

        assert not (ws1 / "f.txt").exists()
        assert (ws2 / "f.txt").read_text() == "data\n"

    def test_multiple_agents_picker_invalid_choice(self):
        _make_worktree(self.data_dir, "proj", "1")
        _make_worktree(self.data_dir, "proj", "2")
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("data\n")

        with patch("builtins.input", return_value="9"):
            with self.assertRaises(SystemExit) as ctx:
                cp([str(src)], "proj:f.txt")
        assert "Invalid choice" in str(ctx.exception)

    def test_multiple_agents_picker_eof(self):
        _make_worktree(self.data_dir, "proj", "1")
        _make_worktree(self.data_dir, "proj", "2")
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("data\n")

        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(SystemExit) as ctx:
                cp([str(src)], "proj:f.txt")
        assert "Invalid choice" in str(ctx.exception)

    def test_single_agent_selected_automatically(self):
        ws = _make_worktree(self.data_dir, "proj", "3")
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("auto\n")

        cp([str(src)], "proj:f.txt")

        assert (ws / "f.txt").read_text() == "auto\n"

    def test_nonexistent_slug_errors(self):
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("data\n")

        with self.assertRaises(SystemExit) as ctx:
            cp([str(src)], "ghost:f.txt")
        assert "No worktrees" in str(ctx.exception)

    def test_nonexistent_agent_id_errors(self):
        _make_worktree(self.data_dir, "proj", "1")
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("data\n")

        with self.assertRaises(SystemExit) as ctx:
            cp([str(src)], "proj@99:f.txt")
        assert "No worktree" in str(ctx.exception)


class TestCpPathResolution(CpTestCase):
    """Path resolution edge cases."""

    def setUp(self):
        super().setUp()
        self.ws = _make_worktree(self.data_dir, "proj", "1")

    def test_no_cave_prefix_returns_none(self):
        path, slug = _resolve_cave_path("just/a/host/path")
        assert path is None
        assert slug is None

    def test_traversal_blocked_relative(self):
        with self.assertRaises(SystemExit) as ctx:
            _resolve_cave_path("proj:../../etc/passwd")
        assert "escapes" in str(ctx.exception).lower()

    def test_traversal_blocked_abs(self):
        with self.assertRaises(SystemExit) as ctx:
            _resolve_cave_path("proj:/etc/passwd")
        assert "/projects/proj" in str(ctx.exception)

    def test_invalid_agent_id(self):
        with self.assertRaises(SystemExit) as ctx:
            _resolve_cave_path("proj@bad:file.txt")
        assert "Invalid agent ID" in str(ctx.exception)

    def test_both_host_paths_errors(self):
        """Two host paths (no cave prefix) should give usage error."""
        src = Path(self.tmpdir) / "a.txt"
        src.write_text("a\n")
        dest = Path(self.tmpdir) / "b.txt"

        with self.assertRaises(SystemExit) as ctx:
            cp([str(src)], str(dest))
        assert "cave path prefix" in str(ctx.exception).lower()

    def test_cave_prefix_cwd(self):
        """cave:path resolves slug from registered projects."""
        src = Path(self.tmpdir) / "f.txt"
        src.write_text("cave cwd\n")

        with patch("cav.cp._cwd_info", return_value=("proj", None)):
            cp([str(src)], "cave:f.txt")

        assert (self.ws / "f.txt").read_text() == "cave cwd\n"

    def test_cave_prefix_from_agent_branch(self):
        """cave:path resolves slug from dev branch when not in a registered project."""
        ws = _make_worktree(self.data_dir, "branchproj", "5")
        src = Path(self.tmpdir) / "g.txt"
        src.write_text("from branch\n")

        db = registry._open()
        db.execute(
            "INSERT OR REPLACE INTO agents (agent_id, project, started_at) VALUES (?, ?, ?)",
            ("5", "branchproj", 0.0)
        )
        db.commit()
        db.close()

        with patch("cav.cp.git.current_branch", return_value="agent-5/main"):
            cp([str(src)], "cave:g.txt")

        assert (ws / "g.txt").read_text() == "from branch\n"

    def test_cave_prefix_selects_branch_agent_when_multiple(self):
        """cave:path on dev branch selects that agent's worktree even when multiple exist."""
        ws1 = _make_worktree(self.data_dir, "multi", "1")
        ws2 = _make_worktree(self.data_dir, "multi", "2")
        src = Path(self.tmpdir) / "h.txt"
        src.write_text("for agent 2\n")

        db = registry._open()
        for aid in ("1", "2"):
            db.execute(
                "INSERT OR REPLACE INTO agents (agent_id, project, started_at) VALUES (?, ?, ?)",
                (aid, "multi", 0.0)
            )
        db.commit()
        db.close()

        with patch("cav.cp.git.current_branch", return_value="agent-2/main"):
            cp([str(src)], "cave:h.txt")

        assert not (ws1 / "h.txt").exists()
        assert (ws2 / "h.txt").read_text() == "for agent 2\n"

    def test_cave_at_n_uses_registry_slug_no_cwd_needed(self):
        """cave@N:path resolves the slug from the registry, no CWD lookup required."""
        ws = _make_worktree(self.data_dir, "regproj", "7")
        src = Path(self.tmpdir) / "i.txt"
        src.write_text("via registry\n")

        db = registry._open()
        db.execute(
            "INSERT OR REPLACE INTO agents (agent_id, project, started_at) VALUES (?, ?, ?)",
            ("7", "regproj", 0.0)
        )
        db.commit()
        db.close()

        # _cwd_info would fail (CWD not in any project), but cave@7 shouldn't need it
        with patch("cav.cp._cwd_info", side_effect=SystemExit("should not be called")):
            cp([str(src)], "cave@7:i.txt")

        assert (ws / "i.txt").read_text() == "via registry\n"

    def test_multiple_sources_to_nondir_dest_fails(self):
        """Multiple host sources require the cave destination to already be a directory."""
        ws = _make_worktree(self.data_dir, "destproj", "1")
        a = Path(self.tmpdir) / "a2.txt"
        b = Path(self.tmpdir) / "b2.txt"
        a.write_text("a\n")
        b.write_text("b\n")

        with self.assertRaises(SystemExit) as ctx:
            cp([str(a), str(b)], "destproj:newfile")
        assert "not a directory" in str(ctx.exception).lower()


def _values(items):
    return [i.value for i in items]


class TestCpCompleter(CpTestCase):
    """Shell completion for `cav cp` paths."""

    def setUp(self):
        super().setUp()
        self.ws = _make_worktree(self.data_dir, "demo", "1")
        (self.ws / "README.md").write_text("")
        (self.ws / "src").mkdir()
        (self.ws / "src" / "cli.py").write_text("")
        (self.ws / "src" / "cp.py").write_text("")
        (self.ws / ".hidden").write_text("")
        db = registry._open()
        db.execute("INSERT INTO projects (slug, path) VALUES (?, ?)",
                   ("demo", self.tmpdir))
        db.execute(
            "INSERT INTO agents (agent_id, project, started_at) VALUES (?, ?, ?)",
            ("1", "demo", 0.0))
        db.commit()
        db.close()

    def test_no_colon_returns_file_marker(self):
        items = _complete_cp_path(None, None, "loc")
        assert len(items) == 1
        assert items[0].type == "file"
        assert items[0].value == "loc"

    def test_empty_returns_file_marker(self):
        items = _complete_cp_path(None, None, "")
        assert items[0].type == "file"

    def test_lists_worktree_root(self):
        vals = _values(_complete_cp_path(None, None, "demo:"))
        assert "demo:README.md" in vals
        assert "demo:src/" in vals
        assert "demo:.hidden" not in vals  # hidden hidden unless typed

    def test_lists_worktree_subdir(self):
        vals = _values(_complete_cp_path(None, None, "demo:src/"))
        assert vals == ["demo:src/cli.py", "demo:src/cp.py"]

    def test_filters_by_partial(self):
        vals = _values(_complete_cp_path(None, None, "demo:src/cl"))
        assert vals == ["demo:src/cli.py"]

    def test_directory_gets_trailing_slash(self):
        vals = _values(_complete_cp_path(None, None, "demo:s"))
        assert vals == ["demo:src/"]

    def test_hidden_shown_when_explicit(self):
        vals = _values(_complete_cp_path(None, None, "demo:."))
        assert "demo:.hidden" in vals

    def test_unknown_slug_returns_empty(self):
        assert _complete_cp_path(None, None, "ghost:") == []

    def test_invalid_agent_id_returns_empty(self):
        assert _complete_cp_path(None, None, "demo@bad:") == []

    def test_specific_agent_id(self):
        ws2 = _make_worktree(self.data_dir, "demo", "2")
        (ws2 / "only2.txt").write_text("")
        db = registry._open()
        db.execute(
            "INSERT INTO agents (agent_id, project, started_at) VALUES (?, ?, ?)",
            ("2", "demo", 0.0))
        db.commit()
        db.close()
        vals = _values(_complete_cp_path(None, None, "demo@2:"))
        assert "demo@2:only2.txt" in vals

    def test_never_prompts_with_multiple_agents(self):
        _make_worktree(self.data_dir, "demo", "2")
        db = registry._open()
        db.execute(
            "INSERT INTO agents (agent_id, project, started_at) VALUES (?, ?, ?)",
            ("2", "demo", 0.0))
        db.commit()
        db.close()
        # Picker would call input(); patch to detect any call.
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            vals = _values(_complete_cp_path(None, None, "demo:"))
        assert "demo:README.md" in vals

    def test_home_path_completion(self):
        home = _make_home(self.data_dir)
        (home / "rcfile").write_text("")
        (home / "notes.txt").write_text("")
        vals = _values(_complete_cp_path(None, None, "demo:~/"))
        assert "demo:~/rcfile" in vals
        assert "demo:~/notes.txt" in vals

    def test_absolute_project_path(self):
        vals = _values(_complete_cp_path(None, None, "demo:/projects/demo/src/"))
        assert "demo:/projects/demo/src/cli.py" in vals

    def test_nonexistent_directory_returns_empty(self):
        assert _complete_cp_path(None, None, "demo:nope/") == []


class TestCpCaveGlob(CpTestCase):
    """Glob expansion for cave-side source paths."""

    def setUp(self):
        super().setUp()
        self.ws = _make_worktree(self.data_dir, "proj", "1")
        (self.ws / "tmp").mkdir()
        (self.ws / "tmp" / "a.png").write_bytes(b"A")
        (self.ws / "tmp" / "b.png").write_bytes(b"B")
        (self.ws / "tmp" / "c.txt").write_bytes(b"C")

    def test_expand_relative_glob(self):
        expanded = _expand_cave_glob("proj@1:tmp/*.png")
        assert expanded == ["proj@1:tmp/a.png", "proj@1:tmp/b.png"]

    def test_expand_absolute_glob(self):
        expanded = _expand_cave_glob("proj@1:/projects/proj/tmp/*.png")
        assert expanded == [
            "proj@1:/projects/proj/tmp/a.png",
            "proj@1:/projects/proj/tmp/b.png",
        ]

    def test_expand_top_level_glob(self):
        (self.ws / "x.md").write_text("x")
        (self.ws / "y.md").write_text("y")
        expanded = _expand_cave_glob("proj@1:*.md")
        assert expanded == ["proj@1:x.md", "proj@1:y.md"]

    def test_expand_no_glob_returns_unchanged(self):
        assert _expand_cave_glob("proj@1:tmp/a.png") == ["proj@1:tmp/a.png"]

    def test_expand_no_cave_prefix_returns_unchanged(self):
        assert _expand_cave_glob("/host/path/*.png") == ["/host/path/*.png"]

    def test_expand_no_matches_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            _expand_cave_glob("proj@1:tmp/*.gif")
        assert "No matches" in str(ctx.exception)

    def test_expand_question_mark(self):
        expanded = _expand_cave_glob("proj@1:tmp/?.png")
        assert expanded == ["proj@1:tmp/a.png", "proj@1:tmp/b.png"]

    def test_expand_charclass(self):
        expanded = _expand_cave_glob("proj@1:tmp/[ab].png")
        assert expanded == ["proj@1:tmp/a.png", "proj@1:tmp/b.png"]

    def test_glob_rejects_symlink_escape(self):
        outside = Path(self.tmpdir) / "outside"
        outside.mkdir()
        (outside / "leaked.png").write_bytes(b"LEAK")
        (self.ws / "tmp" / "leak.png").symlink_to(outside / "leaked.png")
        with self.assertRaises(SystemExit) as ctx:
            _expand_cave_glob("proj@1:tmp/*.png")
        assert "escapes" in str(ctx.exception).lower()

    def test_cp_glob_to_directory(self):
        out = Path(self.tmpdir) / "out"
        out.mkdir()
        cp(["proj@1:tmp/*.png"], str(out))
        names = sorted(p.name for p in out.iterdir())
        assert names == ["a.png", "b.png"]
        assert (out / "a.png").read_bytes() == b"A"
        assert (out / "b.png").read_bytes() == b"B"

    def test_cp_glob_single_match_to_file(self):
        """A glob that matches one file can be copied to a non-directory dest."""
        out = Path(self.tmpdir) / "single.png"
        cp(["proj@1:tmp/a*.png"], str(out))
        assert out.read_bytes() == b"A"

    def test_cp_glob_multi_match_requires_directory(self):
        out = Path(self.tmpdir) / "notadir"
        with self.assertRaises(SystemExit) as ctx:
            cp(["proj@1:tmp/*.png"], str(out))
        assert "not a directory" in str(ctx.exception).lower()

    def test_cp_home_glob(self):
        home = _make_home(self.data_dir)
        (home / "a.log").write_text("LA")
        (home / "b.log").write_text("LB")
        out = Path(self.tmpdir) / "out"
        out.mkdir()
        cp(["proj@1:~/*.log"], str(out))
        names = sorted(p.name for p in out.iterdir())
        assert names == ["a.log", "b.log"]

    def test_escaped_star_is_literal(self):
        """\\* should be treated as a literal star, not a glob."""
        (self.ws / "tmp" / "lit*.png").write_bytes(b"LIT")
        expanded = _expand_cave_glob("proj@1:tmp/lit\\*.png")
        # No unescaped magic -> single literal spec with escapes stripped.
        assert expanded == ["proj@1:tmp/lit*.png"]

        out = Path(self.tmpdir) / "out.png"
        cp(["proj@1:tmp/lit\\*.png"], str(out))
        assert out.read_bytes() == b"LIT"

    def test_escaped_question_is_literal(self):
        (self.ws / "tmp" / "q?.txt").write_bytes(b"Q")
        out = Path(self.tmpdir) / "out.txt"
        cp(["proj@1:tmp/q\\?.txt"], str(out))
        assert out.read_bytes() == b"Q"

    def test_escaped_brackets_are_literal(self):
        (self.ws / "tmp" / "[abc].txt").write_bytes(b"BR")
        out = Path(self.tmpdir) / "out.txt"
        cp(["proj@1:tmp/\\[abc\\].txt"], str(out))
        assert out.read_bytes() == b"BR"

    def test_escaped_literal_does_not_glob_other_files(self):
        """A fully-escaped pattern must not match files matching the unescaped form."""
        # tmp/ already has a.png and b.png from setUp.
        # tmp/*.png as a literal name doesn't exist; should error, not match a.png/b.png.
        with self.assertRaises(SystemExit) as ctx:
            cp(["proj@1:tmp/\\*.png"], str(Path(self.tmpdir) / "out"))
        # Source not found, not "no matches" -- went through literal path.
        assert "not found" in str(ctx.exception).lower()

    def test_mixed_escape_and_glob(self):
        """An escaped star next to an unescaped one matches files starting with '*'."""
        (self.ws / "tmp" / "*foo.png").write_bytes(b"S1")
        (self.ws / "tmp" / "*bar.png").write_bytes(b"S2")
        out = Path(self.tmpdir) / "out"
        out.mkdir()
        cp(["proj@1:tmp/\\**.png"], str(out))
        names = sorted(p.name for p in out.iterdir())
        assert names == ["*bar.png", "*foo.png"]

    def test_backslash_before_nonmagic_is_stripped(self):
        """\\X for non-magic X collapses to X (POSIX shell convention)."""
        (self.ws / "tmp" / "abc.txt").write_text("A")
        out = Path(self.tmpdir) / "out.txt"
        cp(["proj@1:tmp/a\\bc.txt"], str(out))
        assert out.read_text() == "A"

    def test_double_backslash_is_literal_backslash(self):
        r"""\\\\ collapses to a single literal backslash."""
        (self.ws / "tmp" / "back\\slash.txt").write_text("B")
        out = Path(self.tmpdir) / "out.txt"
        cp(["proj@1:tmp/back\\\\slash.txt"], str(out))
        assert out.read_text() == "B"

    def test_glob_multi_agent_picker_only_prompts_once(self):
        """When the slug has multiple agents, the picker should fire once per cp,
        not once per glob match.
        """
        ws2 = _make_worktree(self.data_dir, "proj", "2")
        (ws2 / "tmp").mkdir()
        (ws2 / "tmp" / "x.png").write_bytes(b"X")
        (ws2 / "tmp" / "y.png").write_bytes(b"Y")

        db = registry._open()
        for aid in ("1", "2"):
            db.execute(
                "INSERT OR REPLACE INTO agents (agent_id, project, started_at) "
                "VALUES (?, ?, ?)",
                (aid, "proj", 0.0),
            )
        db.commit()
        db.close()

        out = Path(self.tmpdir) / "out"
        out.mkdir()

        # Without the agent_id encoding fix, the picker would prompt once per
        # match. Provide exactly one input so any extra prompt raises.
        with patch("builtins.input", side_effect=["2"]):
            cp(["proj:tmp/*.png"], str(out))

        names = sorted(p.name for p in out.iterdir())
        assert names == ["x.png", "y.png"]
