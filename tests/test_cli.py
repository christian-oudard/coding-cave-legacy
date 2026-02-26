"""Tests for CLI argument parsing and command construction."""
from pathlib import Path
from unittest import mock

import pytest

import wrapper


def parse(argv):
    """Parse argv through the same split logic as main()."""
    claude_args = []
    if "--" in argv:
        idx = argv.index("--")
        claude_args = argv[idx + 1:]
        argv = argv[:idx]
    parser = wrapper.build_parser()
    parsed = parser.parse_args(argv)
    return parsed, claude_args


class TestResumeArg:
    def test_bare_resume(self):
        parsed, _ = parse(["--resume"])
        assert parsed.resume is True

    def test_resume_short(self):
        parsed, _ = parse(["-r"])
        assert parsed.resume is True

    def test_resume_with_session_id(self):
        parsed, _ = parse(["--resume", "abc-123"])
        assert parsed.resume == "abc-123"

    def test_resume_short_with_session_id(self):
        parsed, _ = parse(["-r", "abc-123"])
        assert parsed.resume == "abc-123"

    def test_no_resume(self):
        parsed, _ = parse([])
        assert parsed.resume is None

    def test_resume_on_agent_subcommand(self):
        parsed, _ = parse(["agent", "--resume", "xyz"])
        assert parsed.resume == "xyz"

    def test_bare_resume_on_agent_subcommand(self):
        parsed, _ = parse(["agent", "-r"])
        assert parsed.resume is True


class TestClaudeParts:
    """Test the claude command construction logic extracted from cmd_agent."""

    @staticmethod
    def build_claude_parts(continue_session=False, resume=None, claude_args=None):
        parts = ["claude", "--dangerously-skip-permissions"]
        if continue_session:
            parts.append("--continue")
        elif resume is not None:
            parts.append("--resume")
            if resume is not True:
                parts.append(resume)
        if claude_args:
            parts.extend(claude_args)
        return parts

    def test_no_flags(self):
        assert self.build_claude_parts() == ["claude", "--dangerously-skip-permissions"]

    def test_continue(self):
        parts = self.build_claude_parts(continue_session=True)
        assert parts == ["claude", "--dangerously-skip-permissions", "--continue"]

    def test_resume_picker(self):
        parts = self.build_claude_parts(resume=True)
        assert parts == ["claude", "--dangerously-skip-permissions", "--resume"]

    def test_resume_with_id(self):
        parts = self.build_claude_parts(resume="abc-123")
        assert parts == ["claude", "--dangerously-skip-permissions", "--resume", "abc-123"]

    def test_continue_trumps_resume(self):
        parts = self.build_claude_parts(continue_session=True, resume="abc")
        assert "--resume" not in parts
        assert "--continue" in parts

    def test_resume_with_extra_claude_args(self):
        parts = self.build_claude_parts(resume="abc", claude_args=["--verbose"])
        assert parts == ["claude", "--dangerously-skip-permissions", "--resume", "abc", "--verbose"]


class TestDetectProjectFromCwd:
    """Test CWD-based project detection and the add prompt."""

    def test_home_dir_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert wrapper.detect_project_from_cwd() is None

    def test_registered_project_returns_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        monkeypatch.setattr(wrapper, "list_projects", lambda: [("proj", str(tmp_path))])
        assert wrapper.detect_project_from_cwd() == tmp_path

    def test_accept_prompt_returns_cwd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        monkeypatch.setattr(wrapper, "list_projects", lambda: [])
        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert wrapper.detect_project_from_cwd() == tmp_path

    def test_empty_input_accepts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        monkeypatch.setattr(wrapper, "list_projects", lambda: [])
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert wrapper.detect_project_from_cwd() == tmp_path

    def test_decline_prompt_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        monkeypatch.setattr(wrapper, "list_projects", lambda: [])
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with pytest.raises(SystemExit) as exc_info:
            wrapper.detect_project_from_cwd()
        assert exc_info.value.code == 0

    def test_eof_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        monkeypatch.setattr(wrapper, "list_projects", lambda: [])
        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=EOFError))
        assert wrapper.detect_project_from_cwd() is None


class TestProjectRequired:
    """cmd_agent and cmd_shell exit with error when no project is detected."""

    def _patch_no_project(self, monkeypatch):
        monkeypatch.setattr(wrapper, "detect_project_from_cwd", lambda: None)
        monkeypatch.setattr(wrapper, "load_global_config", lambda: {})
        monkeypatch.setattr(wrapper, "setup_env", lambda _: None)

    def test_cmd_agent_exits_without_project(self, monkeypatch, capsys):
        self._patch_no_project(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            wrapper.cmd_agent(None)
        assert exc_info.value.code == 1
        assert "No project" in capsys.readouterr().err

    def test_cmd_shell_exits_without_project(self, monkeypatch, capsys):
        self._patch_no_project(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            wrapper.cmd_shell(None)
        assert exc_info.value.code == 1
        assert "No project" in capsys.readouterr().err
