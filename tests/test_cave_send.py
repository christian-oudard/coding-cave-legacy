"""Tests for cave-send IPC."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


SEND = str(Path(__file__).parent.parent / "cave_send.py")


class TestAgentCommand:
    """Verify cmd_agent builds the right bash command for session startup."""

    def test_command_sources_ipc_setup(self, tmp_path, monkeypatch):
        """cmd_agent must source ipc_setup.sh before exec-ing claude."""
        from coding_cave import _state, commands

        monkeypatch.setattr(_state, "IPC_SETUP_SH", "/nix/store/fake-ipc_setup.sh")

        # Capture the command passed to lifecycle.start_session
        captured = {}
        def fake_start(project, command, **kwargs):
            captured["command"] = command

        monkeypatch.setattr(commands.lifecycle, "start_session", fake_start)
        monkeypatch.setattr(commands.config, "load_global_config", lambda: {})
        monkeypatch.setattr(commands.config, "setup_env", lambda c: None)
        monkeypatch.setattr(commands.projects, "detect_project_from_cwd",
                            lambda: tmp_path)
        monkeypatch.setattr(commands.projects, "ensure_project",
                            lambda d: "test-proj")
        monkeypatch.setattr(commands.projects, "cave_workdir",
                            lambda p: "/projects/test-proj")
        monkeypatch.setattr(commands.projects, "parse_project_filter",
                            lambda f, p: {"test-proj": str(tmp_path)})

        commands.cmd_agent([])

        bash_script = captured["command"][-1]
        assert "source /nix/store/fake-ipc_setup.sh" in bash_script
        assert "exec claude" in bash_script
        # ipc_setup.sh must come before exec claude
        ipc_pos = bash_script.index("ipc_setup.sh")
        exec_pos = bash_script.index("exec claude")
        assert ipc_pos < exec_pos


def _hold_lock(lock_path):
    """Acquire an exclusive flock, returning the fd (keep open to hold)."""
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class TestCaveSend:
    def test_list_sessions_finds_active(self, tmp_path):
        """list_sessions finds sessions with held locks."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "live-sid.json").write_text(json.dumps({
            "sid": "live-sid", "index": "1", "project": "proj",
            "started": time.time(),
        }))
        lock_fd = _hold_lock(ipc_dir / "live-sid.lock")
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        old_ipc = cave_send.IPC_DIR
        cave_send.IPC_DIR = ipc_dir
        try:
            sessions = cave_send.list_sessions()
            sids = [s[0] for s in sessions]
            assert "live-sid" in sids
        finally:
            cave_send.IPC_DIR = old_ipc
            os.close(lock_fd)

    def test_list_sessions_cleans_stale(self, tmp_path):
        """list_sessions removes entries with no held lock."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "dead-sid.json").write_text(json.dumps({
            "sid": "dead-sid", "index": "1", "project": "proj",
            "started": time.time(),
        }))
        # Create lock file but don't hold it
        (ipc_dir / "dead-sid.lock").touch()
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        old_ipc = cave_send.IPC_DIR
        cave_send.IPC_DIR = ipc_dir
        try:
            sessions = cave_send.list_sessions()
            assert len(sessions) == 0
            assert not (ipc_dir / "dead-sid.json").exists()
        finally:
            cave_send.IPC_DIR = old_ipc

    def test_self_send_blocked(self, tmp_path):
        """cave-send to own session exits with error."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "my-sid.json").write_text(json.dumps({
            "sid": "my-sid", "index": "1", "project": "proj",
        }))
        lock_fd = _hold_lock(ipc_dir / "my-sid.lock")
        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "my-sid",
            "CODING_CAVE_PROJECT_PATH": "/projects/proj",
            "HOME": str(tmp_path),
        }
        try:
            result = subprocess.run(
                [sys.executable, SEND, "1", "hello"],
                capture_output=True, text=True, env=env,
            )
            assert result.returncode != 0
            assert "yourself" in result.stderr.lower()
        finally:
            os.close(lock_fd)

    def test_list_shows_sessions(self, tmp_path):
        """--list shows session metadata."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "abc123.json").write_text(json.dumps({
            "sid": "abc123", "index": "1", "project": "myproj",
            "started": time.time() - 120,
        }))
        lock_fd = _hold_lock(ipc_dir / "abc123.lock")
        try:
            result = subprocess.run(
                [sys.executable, SEND, "--list"],
                capture_output=True, text=True,
                env={**os.environ, "HOME": str(tmp_path)},
            )
            assert "myproj#1" in result.stdout
        finally:
            os.close(lock_fd)

    def test_resolve_prefers_newest(self, tmp_path):
        """resolve_index picks newest session when indices collide."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        # Two sessions with same index, different start times
        (ipc_dir / "old-sid.json").write_text(json.dumps({
            "sid": "old-sid", "index": "1", "project": "proj",
            "started": 1000,
        }))
        (ipc_dir / "new-sid.json").write_text(json.dumps({
            "sid": "new-sid", "index": "1", "project": "proj",
            "started": 2000,
        }))
        lock_fd1 = _hold_lock(ipc_dir / "old-sid.lock")
        lock_fd2 = _hold_lock(ipc_dir / "new-sid.lock")
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        old_ipc = cave_send.IPC_DIR
        cave_send.IPC_DIR = ipc_dir
        old_proj = os.environ.get("CODING_CAVE_PROJECT_PATH")
        os.environ["CODING_CAVE_PROJECT_PATH"] = "/projects/proj"
        try:
            sid, _ = cave_send.resolve_index("1")
            assert sid == "new-sid"
        finally:
            cave_send.IPC_DIR = old_ipc
            if old_proj is not None:
                os.environ["CODING_CAVE_PROJECT_PATH"] = old_proj
            else:
                os.environ.pop("CODING_CAVE_PROJECT_PATH", None)
            os.close(lock_fd1)
            os.close(lock_fd2)
