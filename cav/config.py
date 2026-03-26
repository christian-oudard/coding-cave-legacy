"""Constants and naming helpers for Coding Cave."""

import fcntl
import os
import re
import time
from pathlib import Path

SHARED_REMOTE = "cave"
AGENT_ID_RE = re.compile(r"^\d+$")
# agent-<id>/<target>, e.g. agent-1/develop
AGENT_BRANCH_RE = re.compile(r"^agent-(\d+)/(.+)$")
DAEMON_BASE_PORT = 9400

DATA_DIR = Path(os.environ.get(
    "CAV_DATA_DIR",
    Path.home() / ".local" / "share" / "coding-cave",
))
CAVE_USER = "cave-coder"
SESSION_TIMEOUT = 24 * 3600  # seconds before a paused session is reclaimable


def agent_branch(target: str, agent_id: str) -> str:
    return f"agent-{agent_id}/{target}"


def parse_agent_branch(branch: str) -> tuple[str, str] | None:
    """Returns (target, agent_id) or None."""
    m = AGENT_BRANCH_RE.match(branch)
    if m:
        return m.group(2), m.group(1)
    return None


def display_name(base: Path, agent_id: str) -> str:
    """Display name: <slug>@<id>."""
    return f"{project_slug(base)}@{agent_id}"


def parse_agent_spec(spec: str, base: Path) -> str:
    """Parse '@4' or 'project@4' into agent_id. Raises SystemExit on bad input."""
    if "@" not in spec:
        raise SystemExit(f"Invalid format '{spec}'. Use @<n> or <project>@<n>.")
    project_part, agent_id = spec.rsplit("@", 1)
    if project_part and project_part != project_slug(base):
        raise SystemExit(
            f"Project mismatch: expected '{project_slug(base)}', got '{project_part}'."
        )
    if not AGENT_ID_RE.match(agent_id):
        raise SystemExit(f"Invalid agent ID in '{spec}'.")
    return agent_id


# -- Project-level paths (inside the project repo) --

def cav_dir(base: Path) -> Path:
    return base / ".coding-cave"


def shared_repo_path(base: Path) -> Path:
    return cav_dir(base) / "cave-shared.git"


# -- Data dir paths (global, ~/.local/share/coding-cave/) --

def nix_overlay_dir() -> Path:
    return DATA_DIR / "nix-overlay"


def persistent_home() -> Path:
    return DATA_DIR / "home"


def worktree_dir(slug: str, agent_id: str) -> Path:
    return DATA_DIR / "worktrees" / slug / agent_id


def project_slug(base: Path) -> str:
    """Derive a slug from the project path."""
    # Lowercase, replace non-alphanumeric with hyphens, collapse runs
    name = base.name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return slug or "project"


def cave_depth() -> int:
    return int(os.environ.get("CODING_CAVE_DEPTH", "0"))


def agent_lock_dir(slug: str) -> Path:
    depth = cave_depth()
    if depth > 0:
        return DATA_DIR / "locks" / slug / f"d{depth}"
    return DATA_DIR / "locks" / slug


def agent_session_file(slug: str, agent_id: str) -> Path:
    return agent_lock_dir(slug) / f"{agent_id}.session"


def read_session_id(base: Path, agent_id: str) -> str | None:
    """Read the stored Claude session ID for an agent, or None."""
    slug = project_slug(base)
    p = agent_session_file(slug, agent_id)
    try:
        sid = p.read_text().strip()
    except OSError:
        return None
    if not sid:
        return None
    # Verify the session file still exists in the persistent home
    session_file = _claude_sessions_dir(slug) / f"{sid}.jsonl"
    return sid if session_file.exists() else None


def snapshot_sessions(base: Path) -> dict[str, float]:
    """Snapshot the sessions dir before launching an agent.

    Returns {uuid: mtime} for all existing .jsonl files.
    Used by capture_session_id to identify which file this agent created/modified.
    """
    slug = project_slug(base)
    sessions_dir = _claude_sessions_dir(slug)
    try:
        return {p.stem: p.stat().st_mtime for p in sessions_dir.glob("*.jsonl")}
    except OSError:
        return {}


def _capture_lock_path(slug: str) -> Path:
    """Lock file used to serialize session capture across concurrent agents."""
    return agent_lock_dir(slug) / ".capture.lock"


def _claimed_sessions(slug: str) -> set[str]:
    """Return set of session UUIDs already claimed by any agent.

    Must be called while holding the capture lock.
    """
    lock_dir = agent_lock_dir(slug)
    claimed = set()
    if not lock_dir.exists():
        return claimed
    for p in lock_dir.glob("*.session"):
        try:
            sid = p.read_text().strip()
            if sid:
                claimed.add(sid)
        except OSError:
            pass
    return claimed


def capture_session_id(base: Path, agent_id: str,
                       snapshot: dict[str, float] | None = None) -> None:
    """After an agent exits, find the session this agent used and store it.

    Holds an exclusive lock during the entire scan-and-claim operation so
    concurrent agents never pick the same session.
    """
    slug = project_slug(base)
    lock_dir = agent_lock_dir(slug)
    lock_dir.mkdir(parents=True, exist_ok=True)

    lock_path = _capture_lock_path(slug)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        sessions_dir = _claude_sessions_dir(slug)
        try:
            files = list(sessions_dir.glob("*.jsonl"))
        except OSError:
            return
        if not files:
            return

        if snapshot is not None:
            changed = [
                p for p in files
                if p.stem not in snapshot or p.stat().st_mtime > snapshot[p.stem]
            ]
            if changed:
                files = changed

        # Exclude sessions already claimed by other agents
        claimed = _claimed_sessions(slug)
        unclaimed = [p for p in files if p.stem not in claimed]
        if unclaimed:
            files = unclaimed

        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        sid = files[0].stem
        p = agent_session_file(slug, agent_id)
        p.write_text(sid)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _claude_sessions_dir(slug: str) -> Path:
    """Path where Claude stores sessions for /projects/<slug> inside the cave."""
    depth = int(os.environ.get("CODING_CAVE_DEPTH", "0"))
    base = Path.home() if depth > 0 else persistent_home()
    return base / ".claude" / "projects" / f"-projects-{slug}"


def is_agent_running(base: Path, agent_id: str) -> bool:
    """Check if an agent is currently running (holds its lock file)."""
    slug = project_slug(base)
    lock_path = agent_lock_dir(slug) / f"{agent_id}.lock"
    if not lock_path.exists():
        return False
    fd = open(lock_path, "r")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Got the lock, so no one else holds it
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        fd.close()


def agent_exit_time(base: Path, agent_id: str) -> float | None:
    """Read the exit timestamp from an agent's lock file, or None."""
    slug = project_slug(base)
    lock_path = agent_lock_dir(slug) / f"{agent_id}.lock"
    try:
        content = lock_path.read_text().strip()
        return float(content) if content else None
    except (ValueError, OSError):
        return None


def is_session_expired(base: Path, agent_id: str) -> bool:
    """True if the agent exited long enough ago to reclaim its ID."""
    t = agent_exit_time(base, agent_id)
    if t is None:
        # No exit timestamp: crashed, or pre-timestamp era.
        # Fall back to lock file mtime (set when agent last started).
        slug = project_slug(base)
        lock_path = agent_lock_dir(slug) / f"{agent_id}.lock"
        try:
            t = lock_path.stat().st_mtime
        except OSError:
            return True
    return (time.time() - t) > SESSION_TIMEOUT


def find_agent_by_session(slug: str, session_id: str) -> str | None:
    """Return the agent ID that owns the given session UUID, or None."""
    lock_dir = agent_lock_dir(slug)
    if not lock_dir.exists():
        return None
    for p in lock_dir.glob("*.session"):
        try:
            if p.read_text().strip() == session_id:
                return p.stem
        except OSError:
            pass
    return None


def continue_agent_id(base: Path, agent_id: str | None = None):
    """Reacquire a paused agent's lock.

    If agent_id is given, continue that specific agent.
    Otherwise, continue the most recently exited agent.
    Returns (agent_id, lock_fd) or raises SystemExit.
    """
    slug = project_slug(base)
    lock_dir = agent_lock_dir(slug)
    if not lock_dir.exists():
        raise SystemExit("No paused agents to continue.")

    if agent_id is not None:
        lock_path = lock_dir / f"{agent_id}.lock"
        try:
            fd = open(lock_path, "r+")
        except FileNotFoundError:
            raise SystemExit(f"No agent {agent_id} found.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fd.close()
            raise SystemExit(f"Agent {agent_id} is still running.")
        return agent_id, fd

    best = None  # (aid, fd, exit_ts)

    for lock_path in lock_dir.glob("*.lock"):
        aid = lock_path.stem
        if not AGENT_ID_RE.match(aid):
            continue
        try:
            fd = open(lock_path, "r+")
        except FileNotFoundError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fd.close()
            continue  # still running

        fd.seek(0)
        content = fd.read().strip()
        try:
            exit_ts = float(content) if content else 0
        except ValueError:
            exit_ts = 0

        if best is None or exit_ts > best[2]:
            if best is not None:
                best[1].close()
            best = (aid, fd, exit_ts)
        else:
            fd.close()

    if best is None:
        raise SystemExit("No paused agents to continue.")

    return best[0], best[1]


def acquire_agent_id(base: Path, available=None):
    """Acquire the next available agent ID with a lock.

    Returns (agent_id, lock_fd). The caller must keep lock_fd open for the
    duration of the agent's lifetime. Closing it releases the lock.

    If available is provided, it is called with (agent_id) after acquiring
    the lock. If it returns False, the lock is released and the next ID
    is tried.
    """
    slug = project_slug(base)
    lock_dir = agent_lock_dir(slug)
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Try 1, 2, 3, ... until we find an unlocked, available ID
    n = 1
    while True:
        lock_path = lock_dir / f"{n}.lock"
        # Open r+ to preserve exit timestamp; fall back to w for new files
        try:
            fd = open(lock_path, "r+")
        except FileNotFoundError:
            fd = open(lock_path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if available and not available(str(n)):
                fd.close()
                n += 1
                continue
            return str(n), fd
        except BlockingIOError:
            fd.close()
            n += 1
