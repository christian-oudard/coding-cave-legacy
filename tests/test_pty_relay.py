"""Tests for session wrapper and cave-send."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


RELAY = str(Path(__file__).parent.parent / "session_wrapper.py")
SEND = str(Path(__file__).parent.parent / "cave_send.py")


class TestSessionWrapper:
    @staticmethod
    def _env_no_session(tmp_path):
        """Env with no session ID so wrapper execs directly."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CODING_CAVE_SESSION")}
        env["HOME"] = str(tmp_path)
        return env

    def test_exit_code_zero(self, tmp_path):
        """Wrapper propagates exit code 0."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--", "true"],
            capture_output=True, timeout=5,
            env=self._env_no_session(tmp_path),
        )
        assert proc.returncode == 0

    def test_exit_code_nonzero(self, tmp_path):
        """Wrapper propagates non-zero exit code."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--", "bash", "-c", "exit 42"],
            capture_output=True, timeout=5,
            env=self._env_no_session(tmp_path),
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
        lock_path = ipc_dir / f"{sid}.lock"

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
        assert lock_path.exists()

        proc.wait(timeout=5)
        assert proc.returncode == 0
        # Cleaned up
        assert not meta_path.exists(), "Metadata not cleaned up"
        assert not lock_path.exists(), "Lock file not cleaned up"

    def test_ignores_sock_flag(self, tmp_path):
        """--sock is accepted and ignored for backwards compat."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--sock", "/tmp/ignored.sock",
             "--", "true"],
            capture_output=True, timeout=5,
            env=self._env_no_session(tmp_path),
        )
        assert proc.returncode == 0

    def test_child_runs_directly_on_terminal(self, tmp_path):
        """Child inherits stdin/stdout directly (no PTY intermediary)."""
        proc = subprocess.run(
            [sys.executable, RELAY, "--", "bash", "-c", "echo hello"],
            capture_output=True, text=True, timeout=5,
            env=self._env_no_session(tmp_path),
        )
        assert proc.returncode == 0
        assert "hello" in proc.stdout


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
            assert "abc123" in result.stdout
            assert "myproj" in result.stdout
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
        try:
            sid, _ = cave_send.resolve_index("1")
            assert sid == "new-sid"
        finally:
            cave_send.IPC_DIR = old_ipc
            os.close(lock_fd1)
            os.close(lock_fd2)
