"""Tests for cave-send hook message delivery via Claude Code hooks."""
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SEND = str(Path(__file__).parent.parent / "cave_send.py")


def _run_hook(tmp_path, sid, hook_input_str):
    """Run `cave-send hook` with the given hook input JSON."""
    return subprocess.run(
        [sys.executable, SEND, "hook"],
        input=hook_input_str, capture_output=True, text=True,
        env={**os.environ, "HOME": str(tmp_path),
             "CODING_CAVE_SESSION_ID": sid},
    )


class TestCaveHook:
    def _write_messages(self, msg_dir, sid, messages):
        """Write messages to the queue file."""
        msg_dir.mkdir(parents=True, exist_ok=True)
        path = msg_dir / f"{sid}.jsonl"
        for msg in messages:
            with open(path, "a") as f:
                f.write(json.dumps(msg) + "\n")

    def test_stop_hook_blocks_with_messages(self, tmp_path):
        """Stop hook returns decision=block when messages are pending."""
        sid = "test-sid"
        msg_dir = tmp_path / "ipc" / "messages"
        self._write_messages(msg_dir, sid, [
            {"sender": "proj#1:", "message": "hello from session 1", "ts": 1.0},
        ])
        result = _run_hook(tmp_path, sid, json.dumps({
            "hook_event_name": "Stop",
        }))
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["decision"] == "block"
        assert "proj#1:" in output["reason"]
        assert "hello from session 1" in output["reason"]
        assert (msg_dir / f"{sid}.jsonl").read_text().strip() == ""

    def test_post_tool_use_hook_exits_2_with_stderr(self, tmp_path):
        """PostToolUse hook uses exit 2 + stderr (visible to user)."""
        sid = "test-sid"
        msg_dir = tmp_path / "ipc" / "messages"
        self._write_messages(msg_dir, sid, [
            {"sender": "proj#2:", "message": "build done", "ts": 1.0},
        ])
        result = _run_hook(tmp_path, sid, json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
        }))
        assert result.returncode == 2
        assert "proj#2: build done" in result.stderr

    def test_no_messages_exits_cleanly(self, tmp_path):
        """Hook exits 0 with no output when queue is empty."""
        result = _run_hook(tmp_path, "no-messages-sid", json.dumps({
            "hook_event_name": "Stop",
        }))
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_multiple_messages_concatenated(self, tmp_path):
        """Multiple queued messages are joined with newlines."""
        sid = "test-sid"
        msg_dir = tmp_path / "ipc" / "messages"
        self._write_messages(msg_dir, sid, [
            {"sender": "proj#1:", "message": "first", "ts": 1.0},
            {"sender": "proj#2:", "message": "second", "ts": 2.0},
        ])
        result = _run_hook(tmp_path, sid, json.dumps({
            "hook_event_name": "Stop",
        }))
        output = json.loads(result.stdout)
        assert "first" in output["reason"]
        assert "second" in output["reason"]

    def test_queue_cleared_after_read(self, tmp_path):
        """Queue file is truncated after reading."""
        sid = "test-sid"
        msg_dir = tmp_path / "ipc" / "messages"
        self._write_messages(msg_dir, sid, [
            {"sender": "proj#1:", "message": "hello", "ts": 1.0},
        ])
        hook_input = json.dumps({"hook_event_name": "Stop"})
        r1 = _run_hook(tmp_path, sid, hook_input)
        assert r1.returncode == 0
        assert "hello" in r1.stdout
        r2 = _run_hook(tmp_path, sid, hook_input)
        assert r2.returncode == 0
        assert r2.stdout.strip() == ""


class TestQueueBasedSend:
    def test_send_creates_queue_file(self, tmp_path):
        """cave-send writes a message to the target's queue file."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        target_sid = "target-sid"
        (ipc_dir / f"{target_sid}.json").write_text(json.dumps({
            "sid": target_sid, "index": "2", "project": "proj",
        }))
        # Hold lock so list_sessions finds it
        lock_fd = os.open(str(ipc_dir / f"{target_sid}.lock"),
                          os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "CODING_CAVE_SESSION_ID": "sender-sid",
            "CODING_CAVE_SESSION_INDEX": "1",
            "CODING_CAVE_PROJECT_PATH": "/projects/myproj",
        }
        try:
            result = subprocess.run(
                [sys.executable, SEND, "2", "hello from sender"],
                capture_output=True, text=True, env=env,
            )
            assert result.returncode == 0
            queue_file = ipc_dir / "messages" / f"{target_sid}.jsonl"
            assert queue_file.exists()
            msg = json.loads(queue_file.read_text().strip())
            assert msg["sender"] == "myproj#1:"
            assert msg["message"] == "hello from sender"
        finally:
            os.close(lock_fd)

    def test_send_from_host(self, tmp_path):
        """cave-send from host (no session ID) uses 'user:' tag."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        target_sid = "target-sid"
        (ipc_dir / f"{target_sid}.json").write_text(json.dumps({
            "sid": target_sid, "index": "1", "project": "proj",
        }))
        lock_fd = os.open(str(ipc_dir / f"{target_sid}.lock"),
                          os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CODING_CAVE_SESSION")}
        env["HOME"] = str(tmp_path)
        try:
            result = subprocess.run(
                [sys.executable, SEND, "1", "hello from host"],
                capture_output=True, text=True, env=env,
            )
            assert result.returncode == 0
            queue_file = ipc_dir / "messages" / f"{target_sid}.jsonl"
            msg = json.loads(queue_file.read_text().strip())
            assert msg["sender"] == "user:"
            assert msg["message"] == "hello from host"
        finally:
            os.close(lock_fd)


class TestInjectCaveSendHooks:
    def test_injects_into_empty_settings(self):
        """Hooks are added to settings with no existing hooks."""
        from coding_cave.lifecycle import _inject_cave_send_hooks
        settings = {}
        _inject_cave_send_hooks(settings)
        for event in ("Stop", "PostToolUse"):
            entries = settings["hooks"][event]
            assert len(entries) == 1
            assert entries[0]["matcher"] == ""
            cmds = [h["command"] for h in entries[0]["hooks"]]
            assert "cave-send hook" in cmds

    def test_preserves_existing_hooks(self):
        """Existing hooks are not removed when cave-send hook is added."""
        from coding_cave.lifecycle import _inject_cave_send_hooks
        settings = {
            "hooks": {
                "Stop": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "persist hook"}],
                }],
            }
        }
        _inject_cave_send_hooks(settings)
        stop_hooks = settings["hooks"]["Stop"][0]["hooks"]
        cmds = [h["command"] for h in stop_hooks]
        assert "persist hook" in cmds
        assert "cave-send hook" in cmds

    def test_idempotent(self):
        """Running inject twice doesn't duplicate cave-send hook."""
        from coding_cave.lifecycle import _inject_cave_send_hooks
        settings = {}
        _inject_cave_send_hooks(settings)
        _inject_cave_send_hooks(settings)
        for event in ("Stop", "PostToolUse"):
            hooks = settings["hooks"][event][0]["hooks"]
            cave_hooks = [h for h in hooks if h["command"] == "cave-send hook"]
            assert len(cave_hooks) == 1
