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

SCHEMA_VERSION = 3  # v1: (project,agent_id) PK; v2: agent_id PK; v3: +projects table


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


def _claude_project_dir(slug: str) -> Path:
    """Return the Claude session directory for a project slug.

    Mirrors Claude Code's encoding: the sandbox cwd (/projects/<slug>)
    with every non-alphanumeric character replaced by '-'.
    """
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", f"/projects/{slug}")
    return persistent_home() / ".claude" / "projects" / encoded


def most_recent_session(slug: str) -> str | None:
    """Find the most recent Claude session ID for a project by file mtime.

    Scans the Claude session directory for .jsonl files and returns the
    session ID (filename stem) with the highest modification time.
    """
    session_dir = _claude_project_dir(slug)
    if not session_dir.is_dir():
        return None
    best_mtime = 0.0
    best_session = None
    for f in session_dir.iterdir():
        if not f.is_file() or f.suffix != ".jsonl":
            continue
        # Skip agent subagent transcripts
        if f.stem.startswith("agent-"):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best_session = f.stem
    return best_session


def project_slug(base: Path) -> str:
    """Derive a slug from the project path."""
    name = base.name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return slug or "project"


def cave_depth() -> int:
    return int(os.environ.get("CODING_CAVE_DEPTH", "0"))


# -- Agent registry (SQLite) --

def _registry_path() -> Path:
    return DATA_DIR / "coding-cave.db"


def _open_registry() -> sqlite3.Connection:
    """Open the agent registry, creating/migrating table as needed.

    Schema v2: agent_id is the sole primary key (globally unique).
    The project column records the primary project where the session started.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_path = _registry_path()
    is_new = not db_path.exists()
    db = sqlite3.connect(str(db_path), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")

    version = db.execute("PRAGMA user_version").fetchone()[0]

    if version < SCHEMA_VERSION:
        if version == 0 and not is_new:
            _migrate_v1_to_v2(db)
        elif is_new:
            db.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    session_id TEXT UNIQUE,
                    pid INTEGER,
                    started_at REAL,
                    exited_at REAL
                )
            """)
            _migrate_session_files(db)

        # v3: add projects table
        db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                slug TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE
            )
        """)

        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()

    return db


def _migrate_v1_to_v2(db: sqlite3.Connection) -> None:
    """Migrate from v1 schema (project, agent_id) PK to v2 (agent_id PK)."""
    # Check if old table exists
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agents'"
    ).fetchone()
    if not has_table:
        db.execute("""
            CREATE TABLE agents (
                agent_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                session_id TEXT UNIQUE,
                pid INTEGER,
                started_at REAL,
                exited_at REAL
            )
        """)
        return

    # Save old data, keeping most recently started entry per agent_id
    db.execute("""
        CREATE TABLE agents_v2 (
            agent_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            session_id TEXT UNIQUE,
            pid INTEGER,
            started_at REAL,
            exited_at REAL
        )
    """)
    db.execute("""
        INSERT OR IGNORE INTO agents_v2
            (agent_id, project, session_id, pid, started_at, exited_at)
        SELECT agent_id, project, session_id, pid, started_at, exited_at
        FROM agents
        ORDER BY COALESCE(started_at, 0) DESC
    """)
    db.execute("DROP TABLE agents")
    db.execute("ALTER TABLE agents_v2 RENAME TO agents")


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
                    "INSERT OR IGNORE INTO agents (agent_id, project, session_id) VALUES (?, ?, ?)",
                    (aid, slug, sid)
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

def write_session_id(agent_id: str, session_id: str) -> None:
    """Record the session ID for an agent."""
    db = _open_registry()
    try:
        db.execute(
            "UPDATE agents SET session_id = ? WHERE agent_id = ?",
            (session_id, agent_id)
        )
        db.commit()
    finally:
        db.close()


def read_session_id(agent_id: str) -> str | None:
    """Read the stored Claude session ID for an agent, or None."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT session_id FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        db.close()


def find_agent_by_session(session_id: str) -> str | None:
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

def is_agent_running(agent_id: str) -> bool:
    """Check if an agent process is alive by PID."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT pid, exited_at FROM agents WHERE agent_id = ?",
            (agent_id,)
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
            "UPDATE agents SET exited_at = ?, pid = NULL WHERE agent_id = ?",
            (time.time(), agent_id)
        )
        db.commit()
        return False
    finally:
        db.close()


def agent_exit_time(agent_id: str) -> float | None:
    """Read the exit timestamp for an agent, or None."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT exited_at FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def running_agent_ids(base: Path) -> list[str]:
    """Return agent IDs that are currently running for a given project."""
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
                    "WHERE agent_id = ?",
                    (time.time(), aid)
                )
        db.commit()
        return sorted(result, key=int)
    finally:
        db.close()


def acquire_agent_id(base: Path) -> str:
    """Acquire the next available agent ID.

    Agent IDs are globally unique across all projects.
    The base path records which project this session started from.
    Returns agent_id. Call mark_agent_exited() when the agent exits.
    """
    slug = project_slug(base)
    now = time.time()
    pid = os.getpid()
    db = _open_registry()
    try:
        db.execute("BEGIN IMMEDIATE")

        # Clean up dead agents (globally)
        rows = db.execute(
            "SELECT agent_id, pid FROM agents "
            "WHERE exited_at IS NULL AND pid IS NOT NULL"
        ).fetchall()
        for aid, p in rows:
            if not _is_pid_alive(p):
                db.execute(
                    "UPDATE agents SET exited_at = ?, pid = NULL "
                    "WHERE agent_id = ?",
                    (now, aid)
                )

        # All IDs in the registry are occupied (running or paused with work)
        occupied = {r[0] for r in db.execute("SELECT agent_id FROM agents")}

        # Find lowest available ID
        n = 1
        while str(n) in occupied:
            n += 1
        aid = str(n)

        sid = str(uuid.uuid4())
        db.execute(
            "INSERT OR REPLACE INTO agents "
            "(agent_id, project, pid, started_at, exited_at, session_id) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (aid, slug, pid, now, sid)
        )
        db.commit()
        return aid
    finally:
        db.close()


def continue_agent_id(base: Path, agent_id: str | None = None) -> str:
    """Reacquire a paused agent.

    If agent_id is given, continue that specific agent (regardless of project).
    Otherwise, continue the most recently exited agent whose primary project
    matches base.
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
                "SELECT pid, exited_at FROM agents WHERE agent_id = ?",
                (agent_id,)
            ).fetchone()
            if not row:
                raise SystemExit(f"No agent {agent_id} found.")
            p, exited_at = row
            if exited_at is None and p is not None and _is_pid_alive(p):
                raise SystemExit(f"Agent {agent_id} is still running.")
            db.execute(
                "UPDATE agents SET pid = ?, started_at = ?, exited_at = NULL "
                "WHERE agent_id = ?",
                (pid, now, agent_id)
            )
            db.commit()
            return agent_id

        # Find most recently exited agent for this project
        row = db.execute(
            "SELECT agent_id FROM agents "
            "WHERE project = ? AND exited_at IS NOT NULL "
            "ORDER BY exited_at DESC LIMIT 1",
            (slug,)
        ).fetchone()
        if not row:
            # No agent entry; acquire a fresh one and find the most
            # recent session by file mtime so the caller can --resume it.
            db.rollback()
            db.close()
            aid = acquire_agent_id(base)
            sid = most_recent_session(slug)
            if sid:
                write_session_id(aid, sid)
            return aid

        aid = row[0]
        db.execute(
            "UPDATE agents SET pid = ?, started_at = ?, exited_at = NULL "
            "WHERE agent_id = ?",
            (pid, now, aid)
        )
        db.commit()
        return aid
    finally:
        db.close()


def mark_agent_exited(agent_id: str) -> None:
    """Mark an agent as exited."""
    db = _open_registry()
    try:
        db.execute(
            "UPDATE agents SET exited_at = ?, pid = NULL WHERE agent_id = ?",
            (time.time(), agent_id)
        )
        db.commit()
    finally:
        db.close()


def remove_agent(agent_id: str) -> None:
    """Remove an agent from the registry."""
    db = _open_registry()
    try:
        db.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        db.commit()
    finally:
        db.close()


def all_agent_ids(project: str | None = None) -> list[str]:
    """Return all agent IDs in the registry, optionally filtered by project slug."""
    db = _open_registry()
    try:
        if project:
            rows = db.execute(
                "SELECT agent_id FROM agents WHERE project = ?", (project,)
            ).fetchall()
        else:
            rows = db.execute("SELECT agent_id FROM agents").fetchall()
        return sorted([r[0] for r in rows], key=int)
    finally:
        db.close()


def any_agent_running() -> bool:
    """Return True if any agent in the registry is currently running."""
    db = _open_registry()
    try:
        rows = db.execute(
            "SELECT agent_id, pid FROM agents "
            "WHERE exited_at IS NULL AND pid IS NOT NULL"
        ).fetchall()
        for aid, pid in rows:
            if _is_pid_alive(pid):
                return True
            db.execute(
                "UPDATE agents SET exited_at = ?, pid = NULL WHERE agent_id = ?",
                (time.time(), aid)
            )
        db.commit()
        return False
    finally:
        db.close()


def clear_registry() -> None:
    """Remove all agents from the registry."""
    db = _open_registry()
    try:
        db.execute("DELETE FROM agents")
        db.commit()
    finally:
        db.close()


# -- Project registry --

def register_project(base: Path) -> str:
    """Register a project in the registry. Fails loud on slug collision."""
    slug = project_slug(base)
    path = str(base.resolve())
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT path FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        if row and row[0] != path:
            raise SystemExit(
                f"Slug collision: '{slug}' is already registered to {row[0]}\n"
                f"Rename one of the project directories to avoid this."
            )
        db.execute(
            "INSERT OR REPLACE INTO projects (slug, path) VALUES (?, ?)",
            (slug, path)
        )
        db.commit()
        return slug
    finally:
        db.close()


def project_path(slug: str) -> str | None:
    """Look up the path for a project slug, or None."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT path FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def all_projects() -> list[tuple[str, str]]:
    """Return all (slug, path) pairs."""
    db = _open_registry()
    try:
        return db.execute("SELECT slug, path FROM projects").fetchall()
    finally:
        db.close()
