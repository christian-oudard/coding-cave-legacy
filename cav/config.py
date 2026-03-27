"""Constants and naming helpers for Coding Cave."""

import os
import re
import sqlite3
import time
import uuid
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
SESSION_TIMEOUT = 24 * 3600  # seconds before a paused agent ID is reclaimable


def agent_branch(target: str, agent_id: str) -> str:
    return f"agent-{agent_id}/{target}"


def parse_agent_branch(branch: str) -> tuple[str, str] | None:
    """Returns (target, agent_id) or None."""
    m = AGENT_BRANCH_RE.match(branch)
    if m:
        return m.group(2), m.group(1)
    return None


def resolve_target(branch: str) -> str:
    """Resolve the ultimate shared-branch target of an agent branch.
    Handles nested agent branches like agent-7/agent-5/main -> main."""
    while True:
        parsed = parse_agent_branch(branch)
        if not parsed:
            return branch
        branch = parsed[0]


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
    name = base.name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return slug or "project"


def cave_depth() -> int:
    return int(os.environ.get("CODING_CAVE_DEPTH", "0"))


# -- Agent registry (SQLite) --

def _registry_path() -> Path:
    return DATA_DIR / "registry.db"


def _open_registry() -> sqlite3.Connection:
    """Open the agent registry, creating table if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_path = _registry_path()
    needs_migration = not db_path.exists()
    db = sqlite3.connect(str(db_path), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            project TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT UNIQUE,
            pid INTEGER,
            started_at REAL,
            exited_at REAL,
            PRIMARY KEY (project, agent_id)
        )
    """)
    if needs_migration:
        _migrate_session_files(db)
    db.commit()
    return db


def _migrate_session_files(db: sqlite3.Connection) -> None:
    """One-time migration of old .session files into the registry."""
    locks_dir = DATA_DIR / "locks"
    if not locks_dir.exists():
        return
    for slug_dir in locks_dir.iterdir():
        if not slug_dir.is_dir():
            continue
        slug = slug_dir.name
        for session_file in slug_dir.rglob("*.session"):
            aid = session_file.stem
            if not AGENT_ID_RE.match(aid):
                continue
            try:
                sid = session_file.read_text().strip()
            except OSError:
                continue
            if sid:
                db.execute(
                    "INSERT OR IGNORE INTO agents (project, agent_id, session_id) VALUES (?, ?, ?)",
                    (slug, aid, sid)
                )
            session_file.unlink()


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is alive by PID."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but can't signal


# -- Session management --

def write_session_id(base: Path, agent_id: str, session_id: str) -> None:
    """Record the session ID for an agent."""
    slug = project_slug(base)
    db = _open_registry()
    try:
        db.execute(
            "UPDATE agents SET session_id = ? WHERE project = ? AND agent_id = ?",
            (session_id, slug, agent_id)
        )
        db.commit()
    finally:
        db.close()


def read_session_id(base: Path, agent_id: str) -> str | None:
    """Read the stored Claude session ID for an agent, or None."""
    slug = project_slug(base)
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT session_id FROM agents WHERE project = ? AND agent_id = ?",
            (slug, agent_id)
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        db.close()


def find_agent_by_session(slug: str, session_id: str) -> str | None:
    """Return the agent ID that owns the given session UUID, or None."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT agent_id FROM agents WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


# -- Agent lifecycle --

def is_agent_running(base: Path, agent_id: str) -> bool:
    """Check if an agent process is alive by PID."""
    slug = project_slug(base)
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT pid, exited_at FROM agents WHERE project = ? AND agent_id = ?",
            (slug, agent_id)
        ).fetchone()
        if not row:
            return False
        pid, exited_at = row
        if exited_at is not None or pid is None:
            return False
        if _is_pid_alive(pid):
            return True
        # Process died without marking exit
        db.execute(
            "UPDATE agents SET exited_at = ?, pid = NULL WHERE project = ? AND agent_id = ?",
            (time.time(), slug, agent_id)
        )
        db.commit()
        return False
    finally:
        db.close()


def agent_exit_time(base: Path, agent_id: str) -> float | None:
    """Read the exit timestamp for an agent, or None."""
    slug = project_slug(base)
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT exited_at FROM agents WHERE project = ? AND agent_id = ?",
            (slug, agent_id)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def is_session_expired(base: Path, agent_id: str) -> bool:
    """True if the agent exited long enough ago to reclaim its ID."""
    t = agent_exit_time(base, agent_id)
    if t is None:
        return True
    return (time.time() - t) > SESSION_TIMEOUT


def running_agent_ids(base: Path) -> list[str]:
    """Return agent IDs that are currently running."""
    slug = project_slug(base)
    db = _open_registry()
    try:
        rows = db.execute(
            "SELECT agent_id, pid FROM agents "
            "WHERE project = ? AND exited_at IS NULL AND pid IS NOT NULL",
            (slug,)
        ).fetchall()
        result = []
        for aid, pid in rows:
            if _is_pid_alive(pid):
                result.append(aid)
            else:
                db.execute(
                    "UPDATE agents SET exited_at = ?, pid = NULL "
                    "WHERE project = ? AND agent_id = ?",
                    (time.time(), slug, aid)
                )
        db.commit()
        return sorted(result, key=int)
    finally:
        db.close()


def acquire_agent_id(base: Path) -> str:
    """Acquire the next available agent ID.

    Returns agent_id. The agent is marked as running in the registry.
    Call mark_agent_exited() when the agent exits.
    """
    slug = project_slug(base)
    now = time.time()
    pid = os.getpid()
    db = _open_registry()
    try:
        db.execute("BEGIN IMMEDIATE")

        # Clean up dead agents
        rows = db.execute(
            "SELECT agent_id, pid FROM agents "
            "WHERE project = ? AND exited_at IS NULL AND pid IS NOT NULL",
            (slug,)
        ).fetchall()
        for aid, p in rows:
            if not _is_pid_alive(p):
                db.execute(
                    "UPDATE agents SET exited_at = ?, pid = NULL "
                    "WHERE project = ? AND agent_id = ?",
                    (now, slug, aid)
                )

        # Find occupied IDs (running or recently exited)
        occupied = set()
        for row in db.execute(
            "SELECT agent_id, exited_at FROM agents WHERE project = ?",
            (slug,)
        ):
            aid, exited_at = row
            if exited_at is None:
                occupied.add(aid)  # running
            elif (now - exited_at) <= SESSION_TIMEOUT:
                occupied.add(aid)  # recently exited, protected

        # Find lowest available ID
        n = 1
        while str(n) in occupied:
            n += 1
        aid = str(n)

        sid = str(uuid.uuid4())
        db.execute(
            "INSERT OR REPLACE INTO agents "
            "(project, agent_id, pid, started_at, exited_at, session_id) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (slug, aid, pid, now, sid)
        )
        db.commit()
        return aid
    finally:
        db.close()


def continue_agent_id(base: Path, agent_id: str | None = None) -> str:
    """Reacquire a paused agent.

    If agent_id is given, continue that specific agent.
    Otherwise, continue the most recently exited agent.
    Returns agent_id or raises SystemExit.
    """
    slug = project_slug(base)
    now = time.time()
    pid = os.getpid()
    db = _open_registry()
    try:
        db.execute("BEGIN IMMEDIATE")

        if agent_id is not None:
            row = db.execute(
                "SELECT pid, exited_at FROM agents "
                "WHERE project = ? AND agent_id = ?",
                (slug, agent_id)
            ).fetchone()
            if not row:
                raise SystemExit(f"No agent {agent_id} found.")
            p, exited_at = row
            if exited_at is None and p is not None and _is_pid_alive(p):
                raise SystemExit(f"Agent {agent_id} is still running.")
            db.execute(
                "UPDATE agents SET pid = ?, started_at = ?, exited_at = NULL "
                "WHERE project = ? AND agent_id = ?",
                (pid, now, slug, agent_id)
            )
            db.commit()
            return agent_id

        # Find most recently exited agent
        row = db.execute(
            "SELECT agent_id FROM agents "
            "WHERE project = ? AND exited_at IS NOT NULL "
            "AND ? - exited_at <= ? "
            "ORDER BY exited_at DESC LIMIT 1",
            (slug, now, SESSION_TIMEOUT)
        ).fetchone()
        if not row:
            raise SystemExit("No paused agents to continue.")

        aid = row[0]
        db.execute(
            "UPDATE agents SET pid = ?, started_at = ?, exited_at = NULL "
            "WHERE project = ? AND agent_id = ?",
            (pid, now, slug, aid)
        )
        db.commit()
        return aid
    finally:
        db.close()


def mark_agent_exited(base: Path, agent_id: str) -> None:
    """Mark an agent as exited."""
    slug = project_slug(base)
    db = _open_registry()
    try:
        db.execute(
            "UPDATE agents SET exited_at = ?, pid = NULL "
            "WHERE project = ? AND agent_id = ?",
            (time.time(), slug, agent_id)
        )
        db.commit()
    finally:
        db.close()


def remove_agent(slug: str, agent_id: str) -> None:
    """Remove an agent from the registry."""
    db = _open_registry()
    try:
        db.execute(
            "DELETE FROM agents WHERE project = ? AND agent_id = ?",
            (slug, agent_id)
        )
        db.commit()
    finally:
        db.close()
