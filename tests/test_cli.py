"""Tests for CLI argument parsing and command construction."""
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import wrapper
from coding_cave import _state
from tests.conftest import write_projects


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
        _state.set_cave_data(tmp_path / "cave-data")
        write_projects(tmp_path / "cave-data", {"proj": str(tmp_path)})
        assert wrapper.detect_project_from_cwd() == tmp_path

    def test_accept_prompt_returns_cwd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        _state.set_cave_data(tmp_path / "cave-data")
        write_projects(tmp_path / "cave-data", {})
        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert wrapper.detect_project_from_cwd() == tmp_path

    def test_empty_input_accepts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        _state.set_cave_data(tmp_path / "cave-data")
        write_projects(tmp_path / "cave-data", {})
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert wrapper.detect_project_from_cwd() == tmp_path

    def test_decline_prompt_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        _state.set_cave_data(tmp_path / "cave-data")
        write_projects(tmp_path / "cave-data", {})
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with pytest.raises(SystemExit) as exc_info:
            wrapper.detect_project_from_cwd()
        assert exc_info.value.code == 0

    def test_eof_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path.parent))
        _state.set_cave_data(tmp_path / "cave-data")
        write_projects(tmp_path / "cave-data", {})
        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=EOFError))
        assert wrapper.detect_project_from_cwd() is None


class TestNsenterShellScript:
    """Test nsenter_shell_script and that it clears host direnv state."""

    def test_unsets_direnv_state(self):
        script = wrapper.nsenter_shell_script("/bin/zsh", "/projects/test")
        assert "unset DIRENV_DIFF" in script
        assert "DIRENV_WATCHES" in script
        assert "DIRENV_DIR" in script

    def test_sources_cave_env(self):
        script = wrapper.nsenter_shell_script("/bin/zsh", "/projects/test")
        assert ". /tmp/cave-env" in script

    def test_direnv_unset_before_cave_env(self):
        script = wrapper.nsenter_shell_script("/bin/zsh", "/projects/test")
        assert script.index("unset DIRENV") < script.index(". /tmp/cave-env")

    def test_execs_shell(self):
        script = wrapper.nsenter_shell_script("/bin/zsh", "/projects/test")
        assert "exec /bin/zsh -l" in script
        script = wrapper.nsenter_shell_script("/bin/bash", "/projects/test")
        assert "exec /bin/bash -l" in script

    def test_cds_to_workdir(self):
        script = wrapper.nsenter_shell_script("/bin/zsh", "/projects/my-proj")
        assert "cd /projects/my-proj" in script

    def test_host_direnv_state_does_not_survive(self, tmp_path):
        """Run nsenter_shell_script under a host-like env with active direnv."""
        cave_env = tmp_path / "cave-env"
        cave_env.write_text(
            'declare -x PATH="/nix/store/cave-bin:/bin"\n'
            'declare -x HOME="/home/cave-coder"\n'
        )
        # Build the real script, substituting the cave-env path.
        script = wrapper.nsenter_shell_script(
            "/bin/bash", str(tmp_path), cave_env=str(cave_env))
        # Replace the exec with env printout so we can inspect.
        script = script.replace(
            "exec /bin/bash -l",
            'echo "PATH=$PATH"; '
            'echo "DIRENV_DIFF=${DIRENV_DIFF-UNSET}"; '
            'echo "DIRENV_WATCHES=${DIRENV_WATCHES-UNSET}"; '
            'echo "DIRENV_DIR=${DIRENV_DIR-UNSET}"')
        result = subprocess.run(
            ["/bin/bash", "-c", script],
            capture_output=True, text=True,
            env={
                "PATH": "/home/christian/bin:/usr/bin",
                "HOME": "/home/christian",
                "DIRENV_DIFF": "eJxLzs9Ly0xOBQAJ4QLN",
                "DIRENV_WATCHES": "W3sicGF0aCI6Ii9ob21lLy5lbnZyYyJ9XQ==",
                "DIRENV_DIR": "-/home/christian/project",
            },
        )
        lines = result.stdout.strip().split("\n")
        env = dict(line.split("=", 1) for line in lines)
        assert "/nix/store/cave-bin" in env["PATH"]
        assert "/home/christian" not in env["PATH"]
        assert env["DIRENV_DIFF"] == "UNSET"
        assert env["DIRENV_WATCHES"] == "UNSET"
        assert env["DIRENV_DIR"] == "UNSET"


class TestProjectRequired:
    """cmd_agent and cmd_shell exit with error when no project is detected."""

    def test_cmd_agent_exits_without_project(self, tmp_path, monkeypatch, capsys):
        # CWD == home makes detect_project_from_cwd return None
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        _state.set_cave_data(tmp_path / "cave-data")
        with pytest.raises(SystemExit) as exc_info:
            wrapper.cmd_agent(None)
        assert exc_info.value.code == 1
        assert "No project" in capsys.readouterr().err

    def test_cmd_shell_exits_without_project(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        _state.set_cave_data(tmp_path / "cave-data")
        with pytest.raises(SystemExit) as exc_info:
            wrapper.cmd_shell(None)
        assert exc_info.value.code == 1
        assert "No project" in capsys.readouterr().err
