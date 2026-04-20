"""Crinj proxy lifecycle: start, reuse, and session tracking.

Every nesting level runs its own crinj instance on CRINJ_PORT (host
loopback). Inner instances chain through HTTPS_PROXY set by the cave
entrypoint; only the outermost crinj injects real credentials. Session
files under config.DATA_DIR/crinj-sessions/ track live users so the
last one out can signal the daemon to exit.
"""

import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from .. import config

log = logging.getLogger("cav")

CRINJ_PORT = 10255


def _host_home() -> Path:
    """Real host home, even when running inside a cave."""
    return Path(os.environ.get("CODING_CAVE_HOST_HOME", str(Path.home())))


def _crinj_ca_path() -> Path:
    """Find crinj CA certificate.

    Inside a cave, prefer /etc/crinj-ca.pem (the ancestor cert inherited
    from the parent). Each nesting level's crinj runs in tunnel/passthrough
    mode, so the MITM cert is always from the outermost (host) crinj.
    The inherited /etc/crinj-ca.pem is the right cert at every depth.

    At depth 0 (host), crinj generates its cert in the home directory.
    """
    if config.inside_cave():
        inherited = Path("/etc/crinj-ca.pem")
        if inherited.exists():
            return inherited

    host_home = _host_home()
    cave_home = Path.home()
    xdg = os.environ.get("XDG_DATA_HOME", host_home / ".local" / "share")
    for path in [
        cave_home / ".crinj" / "gateway" / "ca.pem",
        host_home / ".crinj" / "gateway" / "ca.pem",
        Path(xdg) / "crinj" / "gateway" / "ca.pem",
    ]:
        if path.exists():
            return path
    raise SystemExit("crinj CA certificate not found. Expected at ~/.crinj/gateway/ca.pem")


def _crinj_listening() -> bool:
    """Check if crinj is listening on the proxy port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", CRINJ_PORT)) == 0


def _crinj_pid_file() -> Path:
    return config.DATA_DIR / "crinj.pid"


def _crinj_signal(sig: signal.Signals) -> None:
    """Send a signal to the running crinj process via its PID file."""
    pid_file = _crinj_pid_file()
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, sig)
        log.debug(f"sent {sig.name} to crinj pid {pid}")
    except (FileNotFoundError, ValueError, ProcessLookupError) as exc:
        log.debug(f"could not signal crinj: {exc}")


def _crinj_sessions_dir() -> Path:
    return config.DATA_DIR / "crinj-sessions"


def _crinj_register_session() -> Path:
    """Register this process as a crinj user. Returns the session file."""
    d = _crinj_sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Prune stale entries from dead processes.
    for f in d.iterdir():
        try:
            os.kill(int(f.name), 0)
        except (OSError, ValueError):
            f.unlink(missing_ok=True)
    session_file = d / str(os.getpid())
    session_file.touch()
    return session_file


def _crinj_unregister_session(session_file: Path) -> bool:
    """Unregister this session. Returns True if this was the last one."""
    session_file.unlink(missing_ok=True)
    d = _crinj_sessions_dir()
    return not any(d.iterdir()) if d.is_dir() else True


def _start_crinj() -> subprocess.Popen | None:
    """Start crinj if not already running.

    Returns the Popen handle if we started it, or None if an existing
    instance was reused. Callers use _crinj_register/unregister_session
    to track when the last session exits and crinj can be killed.

    Always passes --allow-empty-rules so the same launch path works at
    every nesting level (host == cave == subcave). Inner crinj instances
    chain through HTTPS_PROXY (set by the cave entrypoint from
    CODING_CAVE_PROXY_ADDR) so traffic flows out to the parent without
    re-injection; the outermost crinj is the one with real rules.
    """
    if not shutil.which("crinj"):
        raise SystemExit("crinj not found on PATH")

    if _crinj_listening():
        log.debug("crinj already listening, sending SIGHUP to reload config")
        _crinj_signal(signal.SIGHUP)
        return None

    log.debug("starting crinj...")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Crinj always reads its config file, even with --allow-empty-rules.
    # Touch an empty rules.toml if missing so a fresh cave home works
    # without manual setup; the host case is a no-op when the user
    # already has real rules.
    rules = Path(os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "crinj" / "rules.toml"
    if not rules.exists():
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.touch()
    proc = subprocess.Popen(
        ["crinj", "--port", str(CRINJ_PORT), "--allow-empty-rules"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for i in range(20):
        if _crinj_listening():
            log.debug(f"crinj ready after {(i + 1) * 0.25:.1f}s")
            break
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace").strip()
            raise SystemExit(f"crinj exited {proc.returncode}: {stderr}")
        time.sleep(0.25)
    else:
        raise SystemExit("crinj failed to start within 5s")

    _crinj_pid_file().write_text(str(proc.pid))
    return proc
