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
    def test_send_injects_tagged_message(self, tmp_path):
        """cave-send delivers a tagged message to a running relay."""
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
               if k != "CODING_CAVE_SESSION_INDEX"}
        subprocess.run(
            [sys.executable, SEND, "--sock", sock, "hello from cave-send"],
            check=True, env=env,
        )
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
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        subprocess.run(
            [sys.executable, SEND, "--raw", "--sock", sock, "no tag"],
            check=True,
        )
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:no tag" in stdout
        assert b"[user]" not in stdout

    def test_send_agent_tag(self, tmp_path):
        """Agent sessions are tagged with their session index."""
        sock = str(tmp_path / "test.sock")
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\nread -r line; echo \"GOT:$line\"\n")
        script.chmod(0o755)

        relay = subprocess.Popen(
            [sys.executable, RELAY, "--sock", sock, "--", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for_socket(sock), "Socket never appeared"
        env = {**os.environ, "CODING_CAVE_SESSION_INDEX": "3"}
        subprocess.run(
            [sys.executable, SEND, "--sock", sock, "hi from agent"],
            check=True, env=env,
        )
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:[#3] hi from agent" in stdout

    def test_send_via_pipe(self, tmp_path):
        """cave-send reads message from stdin when piped."""
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
               if k != "CODING_CAVE_SESSION_INDEX"}
        subprocess.run(
            [sys.executable, SEND, "--sock", sock],
            input=b"piped message",
            check=True, env=env,
        )
        stdout, _ = relay.communicate(timeout=5)
        assert b"GOT:[user] piped message" in stdout

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
