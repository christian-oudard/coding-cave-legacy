"""Git daemon for agent remote isolation.

Runs socat per agent, with a custom handler that parses the git://
protocol pkt-line and execs git-receive-pack or git-upload-pack directly.
This replaces git daemon, which strips environment variables and prevents
CAV_AGENT_ID from reaching the pre-receive hook.
"""

import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config


def _find_bin(name: str) -> str:
    """Find a binary on PATH, return its absolute path."""
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"{name} not found on PATH")
    return path


def start(agent_id: str, shared_repos: list[tuple[Path, str]]) -> tuple[subprocess.Popen, Path]:
    """Start git daemon for an agent.

    shared_repos: list of (bare_repo_path, slug) tuples.
    Returns (socat_process, base_dir) for cleanup.
    """
    port = _agent_port(agent_id)
    _kill_stale_listener(port)
    base_dir = Path(tempfile.mkdtemp(prefix=f"cav-daemon-{agent_id}-"))

    for repo_path, slug in shared_repos:
        (base_dir / slug).symlink_to(repo_path)

    python_bin = _find_bin("python3")
    handler_module = Path(__file__).parent / "handler.py"

    proc = subprocess.Popen(
        ["socat", f"TCP-LISTEN:{port},fork,reuseaddr,bind=127.0.0.1",
         f"EXEC:{python_bin} {handler_module}"],
        start_new_session=True,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "CAV_HANDLER_AGENT_ID": agent_id,
            "CAV_HANDLER_BASE_PATH": str(base_dir),
        },
    )

    if not _wait_for_port(port, proc=proc):
        proc.kill()
        shutil.rmtree(base_dir, ignore_errors=True)
        raise SystemExit(f"Git daemon failed to start on port {port}")

    return proc, base_dir


def stop(proc: subprocess.Popen, base_dir: Path):
    """Stop the daemon and clean up."""
    proc.terminate()
    try:
        _, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
    if proc.returncode not in (0, -signal.SIGTERM, 128 + signal.SIGTERM):
        sys.stderr.write(stderr.decode(errors="replace"))
    shutil.rmtree(base_dir, ignore_errors=True)


def _agent_port(agent_id: str) -> int:
    """Port for an agent's git daemon, scoped by depth to avoid collisions."""
    depth = config.cave_depth()
    return config.DAEMON_BASE_PORT + depth * 100 + int(agent_id)


def remote_url(agent_id: str, slug: str, host: str = "127.0.0.1") -> str:
    """Git remote URL for an agent to access a project's bare repo."""
    port = _agent_port(agent_id)
    return f"git://{host}:{port}/{slug}"


def _kill_stale_listener(port: int) -> None:
    """Kill any process listening on this port (stale daemon from a previous session)."""
    try:
        with open("/proc/net/tcp") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                local = parts[1]
                if ":" not in local:
                    continue
                state = parts[3]
                if state != "0A":  # 0A = LISTEN
                    continue
                lport = int(local.split(":")[1], 16)
                if lport != port:
                    continue
                inode = int(parts[9])
                _kill_by_inode(inode)
                return
    except (FileNotFoundError, ValueError):
        pass


def _kill_by_inode(inode: int) -> None:
    """Find and kill the process owning a socket inode."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target == f"socket:[{inode}]":
                    pid = int(entry.name)
                    os.kill(pid, signal.SIGTERM)
                    # Wait briefly for it to exit
                    for _ in range(10):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            return
                        time.sleep(0.1)
                    os.kill(pid, signal.SIGKILL)
                    return
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue


def _wait_for_port(port: int, timeout: float = 5.0,
                   proc: subprocess.Popen | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc and proc.poll() is not None:
            return False  # process died
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False
