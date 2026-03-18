"""Tests for session wrapper (pty-relay replacement)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


RELAY = str(Path(__file__).parent.parent / "pty_relay.py")
SEND = str(Path(__file__).parent.parent / "cave_send.py")


class TestSessionWrapper:
    def test_exit_code_zero(self, tmp_path):
        """Wrapper propagates exit code 0."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--", "true"],
            capture_output=True, timeout=5,
        )
        assert proc.returncode == 0

    def test_exit_code_nonzero(self, tmp_path):
        """Wrapper propagates non-zero exit code."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--", "bash", "-c", "exit 42"],
            capture_output=True, timeout=5,
        )
        assert proc.returncode == 42

    def test_meta_written_and_cleaned(self, tmp_path):
        """Wrapper writes metadata and cleans up on exit."""
        sid = "test-sid-123"
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "CODING_CAVE_SESSION_ID": sid,
            "CODING_CAVE_SESSION_INDEX": "7",
            "CODING_CAVE_PROJECT_PATH": "/projects/myproject",
        }
        ipc_dir = tmp_path / "ipc"
        meta_path = ipc_dir / f"{sid}.json"
        pid_path = ipc_dir / f"{sid}.pid"

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--", "bash", "-c", "sleep 0.3"],
            env=env,
        )
        # Wait for metadata to appear
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if meta_path.exists():
                break
            time.sleep(0.05)
        assert meta_path.exists(), "Metadata not written"
        data = json.loads(meta_path.read_text())
        assert data["sid"] == sid
        assert data["index"] == "7"
        assert data["project"] == "myproject"
        assert "pid" in data
        assert pid_path.exists()

        proc.wait(timeout=5)
        assert proc.returncode == 0
        # Cleaned up
        assert not meta_path.exists(), "Metadata not cleaned up"
        assert not pid_path.exists(), "PID file not cleaned up"

    def test_ignores_sock_flag(self, tmp_path):
        """--sock is accepted and ignored for backwards compat."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--sock", "/tmp/ignored.sock",
             "--", "true"],
            capture_output=True, timeout=5,
        )
        assert proc.returncode == 0

    def test_child_runs_directly_on_terminal(self, tmp_path):
        """Child inherits stdin/stdout directly (no PTY intermediary)."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--", "bash", "-c", "echo hello"],
            capture_output=True, text=True, timeout=5,
        )
        assert proc.returncode == 0
        assert "hello" in proc.stdout


class TestCaveSend:
    def test_list_sessions_finds_active(self, tmp_path):
        """list_sessions finds sessions with live PIDs."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        # Write metadata with our own PID (guaranteed alive)
        (ipc_dir / "live-sid.json").write_text(json.dumps({
            "sid": "live-sid", "index": "1", "project": "proj",
            "started": time.time(), "pid": os.getpid(),
        }))
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

    def test_list_sessions_cleans_stale(self, tmp_path):
        """list_sessions removes entries with dead PIDs."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        # Write metadata with a definitely-dead PID
        (ipc_dir / "dead-sid.json").write_text(json.dumps({
            "sid": "dead-sid", "index": "1", "project": "proj",
            "started": time.time(), "pid": 999999999,
        }))
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        old_ipc = cave_send.IPC_DIR
        cave_send.IPC_DIR = ipc_dir
        try:
            sessions = cave_send.list_sessions()
            assert len(sessions) == 0
            # File should be cleaned up
            assert not (ipc_dir / "dead-sid.json").exists()
        finally:
            cave_send.IPC_DIR = old_ipc

    def test_self_send_blocked(self, tmp_path):
        """cave-send to own session exits with error."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "my-sid.json").write_text(json.dumps({
            "sid": "my-sid", "index": "1", "project": "proj",
            "pid": os.getpid(),
        }))
        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "my-sid",
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [sys.executable, SEND, "1", "hello"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode != 0
        assert "yourself" in result.stderr.lower()

    def test_list_shows_sessions(self, tmp_path):
        """--list shows session metadata."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "abc123.json").write_text(json.dumps({
            "sid": "abc123", "index": "1", "project": "myproj",
            "started": time.time() - 120, "pid": os.getpid(),
        }))
        result = subprocess.run(
            [sys.executable, SEND, "--list"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert "abc123" in result.stdout
        assert "myproj" in result.stdout

    def test_resolve_prefers_newest(self, tmp_path):
        """resolve_index picks newest session when indices collide."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        # Two sessions with same index, different start times
        (ipc_dir / "old-sid.json").write_text(json.dumps({
            "sid": "old-sid", "index": "1", "project": "proj",
            "started": 1000, "pid": os.getpid(),
        }))
        (ipc_dir / "new-sid.json").write_text(json.dumps({
            "sid": "new-sid", "index": "1", "project": "proj",
            "started": 2000, "pid": os.getpid(),
        }))
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        old_ipc = cave_send.IPC_DIR
        cave_send.IPC_DIR = ipc_dir
        try:
            sid, _ = cave_send.resolve_index("1")
            assert sid == "new-sid"
        finally:
            cave_send.IPC_DIR = old_ipc
