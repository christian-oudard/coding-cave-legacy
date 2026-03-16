"""Tests for PTY relay with UDS message injection."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


RELAY = str(Path(__file__).parent.parent / "pty_relay.py")
SEND = str(Path(__file__).parent.parent / "cave_send.py")
STATUS = str(Path(__file__).parent.parent / "cave_status.py")


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


class TestCaveSend:
    """Tests for cave-send CLI (uses structured JSON protocol with ack)."""

    def _relay_env(self, **extra):
        """Relay env with session identity for ack support."""
        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "test-relay",
            "CODING_CAVE_SESSION_INDEX": "9",
        }
        env.update(extra)
        return env

    def test_send_injects_tagged_message(self, tmp_path):
        """cave-send delivers a tagged message to a running relay."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._relay_env(),
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        env = {k: v for k, v in os.environ.items()
               if k != "CODING_CAVE_SESSION_INDEX"}
        result = subprocess.run(
            [sys.executable, SEND, "--sock", sock, "hello from cave-send"],
            env=env, capture_output=True, timeout=10,
        )
        assert result.returncode == 0
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:[user] hello from cave-send" in stdout

    def test_send_raw_skips_tag(self, tmp_path):
        """--raw sends message without sender tag."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._relay_env(),
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        result = subprocess.run(
            [sys.executable, SEND, "--raw", "--sock", sock, "no tag"],
            capture_output=True, timeout=10,
        )
        assert result.returncode == 0
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:no tag" in stdout
        assert b"[user]" not in stdout

    def test_send_agent_tag(self, tmp_path):
        """Agent sessions are tagged with project and index."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._relay_env(),
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        env = {
            **os.environ,
            "CODING_CAVE_SESSION_INDEX": "3",
            "CODING_CAVE_PROJECT_PATH": "/projects/myproj",
        }
        result = subprocess.run(
            [sys.executable, SEND, "--sock", sock, "hi from agent"],
            env=env, capture_output=True, timeout=10,
        )
        assert result.returncode == 0
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:[myproj#3] hi from agent" in stdout

    def test_send_via_pipe(self, tmp_path):
        """cave-send reads message from stdin when piped."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._relay_env(),
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        env = {k: v for k, v in os.environ.items()
               if k != "CODING_CAVE_SESSION_INDEX"}
        result = subprocess.run(
            [sys.executable, SEND, "--sock", sock],
            input=b"piped message",
            env=env, capture_output=True, timeout=10,
        )
        assert result.returncode == 0
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:[user] piped message" in stdout

    def test_send_exits_nonzero_on_failure(self, tmp_path):
        """cave-send exits non-zero when relay is not running."""
        sock = str(tmp_path / "nonexistent.sock")
        result = subprocess.run(
            [sys.executable, SEND, "--sock", sock, "should fail"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0

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


class TestStructuredProtocol:
    def test_json_frame_injects_body(self, tmp_path):
        """JSON frame with 'body' injects only the body text."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        frame = json.dumps({"from": "test-sender", "body": "hello structured"})
        _send_message(sock, frame)
        stdout, _ = proc.communicate(timeout=5)
        assert b"GOT:hello structured" in stdout

    def test_json_frame_with_reply_to_sends_ack(self, tmp_path):
        """JSON frame with 'reply_to' triggers a delivery ack."""
        sock = str(tmp_path / "test.sock")
        reply_sock_path = str(tmp_path / "reply.sock")

        # Set up a listener on the reply socket
        reply_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reply_sock.bind(reply_sock_path)
        reply_sock.listen(1)
        reply_sock.settimeout(5)

        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "test-receiver",
            "CODING_CAVE_SESSION_INDEX": "7",
        }
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        assert _wait_for_socket(sock), "Socket never appeared"

        frame = json.dumps({
            "from": "test-sender",
            "body": "ping",
            "reply_to": reply_sock_path,
        })
        _send_message(sock, frame)

        # Read the ack
        conn, _ = reply_sock.accept()
        ack_data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            ack_data += chunk
        conn.close()
        reply_sock.close()

        ack = json.loads(ack_data)
        assert ack["type"] == "ack"
        assert ack["from"] == "test-receiver"
        assert ack["index"] == "7"

        proc.communicate(timeout=5)

    def test_raw_text_still_works(self, tmp_path):
        """Plain text (non-JSON) is still injected as before."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        _send_message(sock, "plain text message")
        stdout, _ = proc.communicate(timeout=5)
        assert b"GOT:plain text message" in stdout

    def test_invalid_json_treated_as_raw(self, tmp_path):
        """Message starting with '{' but not valid JSON is injected raw."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        _send_message(sock, "{not valid json")
        stdout, _ = proc.communicate(timeout=5)
        assert b"GOT:{not valid json" in stdout

    def test_no_reply_to_means_no_ack(self, tmp_path):
        """JSON frame without 'reply_to' does not attempt any reply."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        frame = json.dumps({"body": "no reply expected"})
        _send_message(sock, frame)
        stdout, _ = proc.communicate(timeout=5)
        assert b"GOT:no reply expected" in stdout


class TestMetaUpdate:
    def test_meta_update_modifies_sidecar(self, tmp_path):
        """meta_update frame updates the sidecar JSON without PTY injection."""
        sock = str(tmp_path / "test.sock")
        meta = tmp_path / "test.json"
        script = tmp_path / "wait.sh"
        # Script waits briefly then exits. No read, so any PTY injection would be ignored.
        script.write_text("#!/bin/bash\nsleep 0.5\n")
        script.chmod(0o755)

        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "meta-test",
            "CODING_CAVE_SESSION_INDEX": "9",
            "CODING_CAVE_PROJECT_PATH": "/projects/testproj",
        }
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        assert _wait_for_socket(sock), "Socket never appeared"

        # Send meta_update
        frame = json.dumps({"type": "meta_update", "fields": {"task": "testing comms"}})
        _send_message(sock, frame)
        time.sleep(0.1)

        # Verify sidecar was updated
        data = json.loads(meta.read_text())
        assert data["task"] == "testing comms"
        # Original fields should still be there
        assert data["index"] == "9"
        assert data["project"] == "testproj"

        proc.communicate(timeout=5)

    def test_meta_update_does_not_inject_pty(self, tmp_path):
        """meta_update frame should NOT inject anything into the PTY."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "check.sh"
        # Read with timeout. If nothing arrives, print EMPTY.
        script.write_text(
            "#!/bin/bash\n"
            "read -t 1 -r line && echo \"GOT:$line\" || echo EMPTY\n"
        )
        script.chmod(0o755)

        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "meta-test2",
            "CODING_CAVE_SESSION_INDEX": "5",
            "CODING_CAVE_PROJECT_PATH": "/projects/testproj",
        }
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        assert _wait_for_socket(sock), "Socket never appeared"

        frame = json.dumps({"type": "meta_update", "fields": {"task": "stealth update"}})
        _send_message(sock, frame)

        stdout, _ = proc.communicate(timeout=5)
        assert b"EMPTY" in stdout
        assert b"GOT:" not in stdout

    def test_meta_update_with_ack(self, tmp_path):
        """meta_update with reply_to sends ack back."""
        sock = str(tmp_path / "test.sock")
        reply_sock_path = str(tmp_path / "reply.sock")

        reply_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reply_sock.bind(reply_sock_path)
        reply_sock.listen(1)
        reply_sock.settimeout(5)

        script = tmp_path / "wait.sh"
        script.write_text("#!/bin/bash\nsleep 0.5\n")
        script.chmod(0o755)

        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "ack-meta-test",
            "CODING_CAVE_SESSION_INDEX": "3",
            "CODING_CAVE_PROJECT_PATH": "/projects/testproj",
        }
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        assert _wait_for_socket(sock), "Socket never appeared"

        frame = json.dumps({
            "type": "meta_update",
            "fields": {"task": "ack test"},
            "reply_to": reply_sock_path,
        })
        _send_message(sock, frame)

        conn, _ = reply_sock.accept()
        ack_data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            ack_data += chunk
        conn.close()
        reply_sock.close()

        ack = json.loads(ack_data)
        assert ack["type"] == "ack"
        assert ack["from"] == "ack-meta-test"

        proc.communicate(timeout=5)


class TestSenderTag:
    def test_tag_includes_project_name(self, tmp_path):
        """Agent sender tag includes project name: [project#N]."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        env = {
            **os.environ,
            "CODING_CAVE_SESSION_INDEX": "2",
            "CODING_CAVE_PROJECT_PATH": "/projects/coding-cave",
        }
        subprocess.run(
            [sys.executable, SEND, "--sock", sock, "hello"],
            check=True, env=env,
        )
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:[coding-cave#2] hello" in stdout

    def test_tag_falls_back_without_project(self, tmp_path):
        """Without project path, tag falls back to [#N]."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        env = {k: v for k, v in os.environ.items()
               if k != "CODING_CAVE_PROJECT_PATH"}
        env["CODING_CAVE_SESSION_INDEX"] = "5"
        subprocess.run(
            [sys.executable, SEND, "--sock", sock, "hello"],
            check=True, env=env,
        )
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:[#5] hello" in stdout


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


class TestDeliveryAck:
    def test_send_returns_ack(self, tmp_path):
        """send() returns ack dict from relay."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        env = {
            **os.environ,
            "CODING_CAVE_SESSION_ID": "receiver-2",
            "CODING_CAVE_SESSION_INDEX": "3",
        }
        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        assert _wait_for_socket(sock), "Socket never appeared"

        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        ack = cave_send.send(sock, "test ack", tag=False)
        relay.communicate(timeout=5)

        assert ack is not None
        assert ack["type"] == "ack"
        assert ack["from"] == "receiver-2"
        assert ack["index"] == "3"

    def test_send_returns_none_on_timeout(self, tmp_path):
        """send() returns None when no ack arrives."""
        sock_path = str(tmp_path / "dummy.sock")
        dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dummy.bind(sock_path)
        dummy.listen(1)

        sys.path.insert(0, str(Path(__file__).parent.parent))
        import cave_send
        old_timeout = cave_send.ACK_TIMEOUT
        cave_send.ACK_TIMEOUT = 0.5
        try:
            ack = cave_send.send(sock_path, "hello", tag=False)
        finally:
            cave_send.ACK_TIMEOUT = old_timeout
            dummy.close()

        assert ack is None

    def test_send_by_index_not_found(self, tmp_path):
        """cave-send 99 exits with error when no session has that index."""
        result = subprocess.run(
            [sys.executable, SEND, "99", "hello"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert result.returncode != 0
        assert "99" in result.stderr


class TestCaveStatus:
    def test_status_updates_metadata(self):
        """cave-status sends a meta_update that modifies the sidecar."""
        import tempfile
        # Use /tmp directly for short socket paths (AF_UNIX limit)
        home = tempfile.mkdtemp(prefix="cs", dir="/tmp")
        try:
            home = Path(home)
            ipc_dir = home / "ipc"
            ipc_dir.mkdir()
            sock = str(ipc_dir / "st.sock")
            meta = ipc_dir / "st.json"
            script = home / "w.sh"
            script.write_text("#!/bin/bash\nsleep 0.8\n")
            script.chmod(0o755)

            env = {
                **os.environ,
                "CODING_CAVE_SESSION_ID": "st",
                "CODING_CAVE_SESSION_INDEX": "1",
                "CODING_CAVE_PROJECT_PATH": "/projects/demo",
                "HOME": str(home),
            }

            proc = subprocess.Popen(
                [sys.executable, RELAY, "--sock", sock, "--", str(script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            assert _wait_for_socket(sock), "Socket never appeared"

            result = subprocess.run(
                [sys.executable, STATUS, "improving comms"],
                capture_output=True, text=True, env=env,
            )
            assert result.returncode == 0
            assert "improving comms" in result.stdout

            time.sleep(0.1)
            data = json.loads(meta.read_text())
            assert data["task"] == "improving comms"

            proc.communicate(timeout=5)
        finally:
            import shutil
            shutil.rmtree(str(home), ignore_errors=True)
