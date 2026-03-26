"""Git daemon for agent remote isolation.

Runs socat per agent, with a custom handler that parses the git://
protocol pkt-line and execs git-receive-pack or git-upload-pack directly.
This replaces git daemon, which strips environment variables and prevents
CAV_AGENT_ID from reaching the pre-receive hook.
"""

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
    base_dir = Path(tempfile.mkdtemp(prefix=f"cav-daemon-{agent_id}-"))

    for repo_path, slug in shared_repos:
        (base_dir / slug).symlink_to(repo_path)

    python_bin = _find_bin("python3")

    # Write a Python handler that parses pkt-line and execs the git service.
    # This replaces git daemon --inetd, preserving env vars for hooks.
    handler = base_dir / "handler.py"
    # Avoid f-strings for the handler body to prevent \x00 escaping issues.
    handler.write_text(
        "import os, sys\n"
        "\n"
        'os.environ["CAV_AGENT_ID"] = "' + agent_id + '"\n'
        'base_path = "' + str(base_dir) + '"\n'
        "\n"
        "raw_len = sys.stdin.buffer.read(4)\n"
        "if len(raw_len) < 4:\n"
        "    sys.exit(0)\n"
        "pkt_len = int(raw_len, 16)\n"
        "if pkt_len < 4:\n"
        "    sys.exit(1)\n"
        "payload = sys.stdin.buffer.read(pkt_len - 4)\n"
        "\n"
        'parts = payload.split(b"\\x00")\n'
        "request = parts[0].decode()\n"
        'service, path = request.split(" ", 1)\n'
        "\n"
        'ALLOWED = {"git-upload-pack", "git-receive-pack"}\n'
        "if service not in ALLOWED:\n"
        "    sys.exit(1)\n"
        "\n"
        "# Resolve path under base_path, but check BEFORE following symlinks\n"
        "# (repos are symlinked from base_path to the actual bare repos).\n"
        'repo = os.path.normpath(os.path.join(base_path, path.lstrip("/")))\n'
        "if not repo.startswith(base_path):\n"
        "    sys.exit(1)\n"
        "repo = os.path.realpath(repo)\n"
        "\n"
        "os.execvp(service, [service, repo])\n"
    )
    handler.chmod(0o755)

    proc = subprocess.Popen(
        ["socat", f"TCP-LISTEN:{port},fork,reuseaddr",
         f"EXEC:{python_bin} {handler}"],
        start_new_session=True,
        stderr=subprocess.PIPE,
    )

    if not _wait_for_port(port):
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


def remote_url(agent_id: str, slug: str) -> str:
    """Git remote URL for an agent to access a project's bare repo."""
    port = _agent_port(agent_id)
    return f"git://127.0.0.1:{port}/{slug}"


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False
