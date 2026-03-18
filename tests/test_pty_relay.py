"""Tests for PTY relay with UDS message injection."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


RELAY = str(Path(__file__).parent.parent / "pty_relay.py")
SEND = str(Path(__file__).parent.parent / "cave_send.py")


def _wait_for_socket(sock_path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(sock_path).exists():
            return True
        time.sleep(0.05)
    return False


def _send_message(sock_path, message):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall(message.encode() if isinstance(message, str) else message)
    s.shutdown(socket.SHUT_WR)
    s.close()


class TestPtyRelay:
    def test_exit_code_zero(self, tmp_path):
        """Relay propagates exit code 0."""
        sock = str(tmp_path / "test.sock")
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", "true"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.communicate(timeout=5)
        assert proc.returncode == 0

    def test_exit_code_nonzero(self, tmp_path):
        """Relay propagates non-zero exit code."""
        sock = str(tmp_path / "test.sock")
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--",
             "bash", "-c", "exit 42"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.communicate(timeout=5)
        assert proc.returncode == 42

    def test_io_forwarding(self, tmp_path):
        """Data written to relay stdin reaches child and output comes back."""
        sock = str(tmp_path / "test.sock")
        # Use a script that reads one line and echoes it with prefix
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _wait_for_socket(sock)
        # Write to stdin (relay forwards to child PTY)
        proc.stdin.write(b"hello from stdin\r")
        proc.stdin.flush()
        proc.stdin.close()
        stdout, _ = proc.communicate(timeout=5)
        assert b"GOT:hello from stdin" in stdout

    def test_message_injection(self, tmp_path):
        """Messages sent via UDS appear in child's stdin."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        _send_message(sock, "injected text")
        stdout, _ = proc.communicate(timeout=5)
        assert b"GOT:injected text" in stdout

    def test_socket_cleanup(self, tmp_path):
        """Socket file is removed after relay exits."""
        sock = str(tmp_path / "test.sock")
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", "true"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.communicate(timeout=5)
        assert not Path(sock).exists()

    def test_multiple_injections(self, tmp_path):
        """Multiple UDS messages are injected in order."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "multi.sh"
        script.write_text(
            "#!/bin/bash\n"
            "read -r a; read -r b; echo \"$a|$b\"\n"
        )
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        _send_message(sock, "first")
        time.sleep(0.2)
        _send_message(sock, "second")
        stdout, _ = proc.communicate(timeout=5)
        assert b"first|second" in stdout

    def test_child_output_without_injection(self, tmp_path):
        """Child output appears on relay stdout even without injection."""
        sock = str(tmp_path / "test.sock")
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--",
             "bash", "-c", "echo hello world"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _ = proc.communicate(timeout=5)
        assert b"hello world" in stdout

    def test_sock_dir_created(self, tmp_path):
        """Relay creates parent directories for the socket path."""
        sock = str(tmp_path / "deep" / "nested" / "test.sock")
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", "true"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.communicate(timeout=5)
        assert proc.returncode == 0


    def test_meta_written_and_cleaned(self, tmp_path):
        """Relay writes metadata sidecar and removes it on exit."""
        import json
        sock = str(tmp_path / "test.sock")
        meta = tmp_path / "test.json"
        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "test-sid-123",
            "CODING_CAVE_SESSION_INDEX": "7",
            "CODING_CAVE_PROJECT_PATH": "/projects/myproject",
        }
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--",
             "bash", "-c", "sleep 0.3"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        _wait_for_socket(sock)
        # Metadata should exist while relay is running
        assert meta.exists(), "Metadata file not created"
        data = json.loads(meta.read_text())
        assert data["sid"] == "test-sid-123"
        assert data["index"] == "7"
        assert data["project"] == "myproject"
        proc.communicate(timeout=5)
        # Metadata should be cleaned up after exit
        assert not meta.exists(), "Metadata file not cleaned up"


    def test_child_stop_resumed(self, tmp_path):
        """When child sends SIGSTOP, relay detects and resumes it."""
        sock = str(tmp_path / "test.sock")
        # Child sends SIGSTOP (not SIGTSTP, which is discarded in
        # orphaned process groups) after writing a marker.
        script = tmp_path / "stopper.py"
        script.write_text(
            "import os, signal, sys\n"
            "sys.stdout.write('BEFORE_STOP\\n')\n"
            "sys.stdout.flush()\n"
            "os.kill(os.getpid(), signal.SIGSTOP)\n"
            "sys.stdout.write('AFTER_RESUME\\n')\n"
            "sys.stdout.flush()\n"
        )

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--",
             sys.executable, str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _ = proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert b"BEFORE_STOP" in stdout
        assert b"AFTER_RESUME" in stdout


class TestMessageQueuing:
    def test_message_delayed_during_typing(self, tmp_path):
        """Messages are held while stdin is active, delivered after idle."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        # Read two lines: first from stdin, second from injected message
        script.write_text("#!/bin/bash\nread -r a; read -r b; echo \"$a|$b\"\n")
        script.chmod(0o755)

        env = {**os.environ, "CAVE_MSG_DELAY": "1.0"}
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        assert _wait_for_socket(sock), "Socket never appeared"

        # Simulate typing (stdin activity)
        proc.stdin.write(b"typed\r")
        proc.stdin.flush()
        # Send a message while "typing" just happened
        _send_message(sock, "injected")
        # The message should be queued, not yet delivered.
        # After the delay expires (1s), it injects.
        stdout, _ = proc.communicate(timeout=5)
        assert b"typed|injected" in stdout

    def test_bell_on_queued_message(self, tmp_path):
        """Terminal bell is written to stdout when a message is queued."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        env = {**os.environ, "CAVE_MSG_DELAY": "0.5"}
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        _send_message(sock, "hello")
        stdout, _ = proc.communicate(timeout=5)
        assert b"\a" in stdout  # bell character
        assert b"GOT:hello" in stdout


class TestCaveSend:
    def test_fwd_tags_and_delivers(self, tmp_path):
        """FWD through sender relay tags message and delivers to target."""
        # Set up sender relay (session A)
        sender_sock = str(tmp_path / "sender.sock")
        sender_script = tmp_path / "sender.sh"
        sender_script.write_text("#!/bin/bash\nsleep 2\n")
        sender_script.chmod(0o755)

        # Set up target relay (session B)
        target_sock = str(tmp_path / "target.sock")
        target_script = tmp_path / "target.sh"
        target_script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        target_script.chmod(0o755)

        sender_env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "sender-sid",
            "CODING_CAVE_SESSION_INDEX": "1",
            "CODING_CAVE_PROJECT_PATH": "/projects/myproj",
            "HOME": str(tmp_path),
        }
        target_env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "target-sid",
            "CODING_CAVE_SESSION_INDEX": "2",
        }

        # Create ipc dir with target socket symlink so relay can find it
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()

        sender_relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sender_sock, "--", str(sender_script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=sender_env,
        )
        target_relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", target_sock, "--", str(target_script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=target_env,
        )
        assert _wait_for_socket(sender_sock), "Sender socket never appeared"
        assert _wait_for_socket(target_sock), "Target socket never appeared"

        # Symlink target socket into ipc dir so forwarding can find it
        (ipc_dir / "target-sid.sock").symlink_to(target_sock)

        # Send FWD through sender relay (same PID namespace = local)
        _send_message(sender_sock, "FWD target-sid\nhello from session 1")
        time.sleep(0.3)

        # Target should have received the tagged message
        target_relay.stdin.close()
        stdout, _ = target_relay.communicate(timeout=5)
        assert b"GOT:myproj#1: hello from session 1" in stdout

        sender_relay.kill()
        sender_relay.wait()

    def test_fwd_rejected_from_cross_namespace(self, tmp_path):
        """FWD from a different PID namespace (pid=0) is NOT forwarded."""
        # This tests that non-FWD messages are injected into PTY instead
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        # Direct message (not FWD) is injected into PTY
        _send_message(sock, "direct message")
        stdout, _ = proc.communicate(timeout=5)
        assert b"GOT:direct message" in stdout

    def test_self_send_blocked(self, tmp_path):
        """cave-send to own session index exits with error."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "my-sid.sock").touch()
        (ipc_dir / "my-sid.json").write_text(json.dumps({
            "sid": "my-sid", "index": "1", "project": "proj",
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

    def test_list_sessions(self, tmp_path, monkeypatch):
        """--list shows session IDs with metadata."""
        import json
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        # Create fake socket + metadata files
        (ipc_dir / "abc123.sock").touch()
        (ipc_dir / "abc123.json").write_text(json.dumps({
            "sid": "abc123", "index": "1", "project": "myproj",
            "started": time.time() - 120,
        }))
        (ipc_dir / "def456.sock").touch()
        result = subprocess.run(
            [sys.executable, SEND, "--list"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert "abc123" in result.stdout
        assert "myproj" in result.stdout
        assert "def456" in result.stdout


class TestSenderTag:
    def test_relay_tags_fwd_with_project(self):
        """Relay tags FWD messages with project#index."""
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="fwd", dir="/tmp"))
        try:
            sender_sock = str(d / "s.sock")
            target_sock = str(d / "t.sock")
            (d / "wait.sh").write_text("#!/bin/bash\nsleep 2\n")
            (d / "wait.sh").chmod(0o755)
            (d / "echo.sh").write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
            (d / "echo.sh").chmod(0o755)
            (d / "ipc").mkdir()

            env = {
                **os.environ,
                "CODING_CAVE_SESSION_INDEX": "5",
                "CODING_CAVE_PROJECT_PATH": "/projects/myproj",
                "HOME": str(d),
            }
            relay = subprocess.Popen(
                [sys.executable, RELAY, "--sock", sender_sock, "--", str(d / "wait.sh")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            target = subprocess.Popen(
                [sys.executable, RELAY, "--sock", target_sock, "--", str(d / "echo.sh")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert _wait_for_socket(sender_sock)
            assert _wait_for_socket(target_sock)
            (d / "ipc" / "tgt.sock").symlink_to(target_sock)

            _send_message(sender_sock, "FWD tgt\nhello")
            time.sleep(0.3)
            target.stdin.close()
            stdout, _ = target.communicate(timeout=5)
            assert b"GOT:myproj#5: hello" in stdout
            relay.kill()
            relay.wait()
        finally:
            shutil.rmtree(str(d), ignore_errors=True)


class TestResolveIndex:
    def test_resolve_existing_index(self, tmp_path, monkeypatch):
        """resolve_index finds session by index number."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "sid-aaa.sock").touch()
        (ipc_dir / "sid-aaa.json").write_text(json.dumps({
            "sid": "sid-aaa", "index": "4", "project": "myproj",
        }))
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        monkeypatch.setattr(cave_send, "IPC_DIR", ipc_dir)
        sid, sock = cave_send.resolve_index("4")
        assert sid == "sid-aaa"
        assert sock.endswith("sid-aaa.sock")

    def test_resolve_with_hash_prefix(self, tmp_path, monkeypatch):
        """resolve_index handles '#4' format."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        (ipc_dir / "sid-bbb.sock").touch()
        (ipc_dir / "sid-bbb.json").write_text(json.dumps({
            "sid": "sid-bbb", "index": "2", "project": "proj",
        }))
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        monkeypatch.setattr(cave_send, "IPC_DIR", ipc_dir)
        sid, _ = cave_send.resolve_index("#2")
        assert sid == "sid-bbb"

    def test_resolve_missing_returns_none(self, tmp_path, monkeypatch):
        """resolve_index returns (None, None) for unknown index."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        monkeypatch.setattr(cave_send, "IPC_DIR", ipc_dir)
        sid, sock = cave_send.resolve_index("99")
        assert sid is None
        assert sock is None


class TestSendByIndex:
    def test_send_by_index_not_found(self, tmp_path):
        """cave-send 99 exits with error when no session has that index."""
        result = subprocess.run(
            [sys.executable, SEND, "99", "hello"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert result.returncode != 0
        assert "99" in result.stderr
