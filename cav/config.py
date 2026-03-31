"""Constants and naming helpers for Coding Cave."""

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

CAVE_REMOTE = "cave"
HOST_REMOTE = "host-cave"
AGENT_ID_RE = re.compile(r"^\d+$")
# agent-<id>/<target>, e.g. agent-1/develop
AGENT_BRANCH_RE = re.compile(r"^agent-(\d+)/(.+)$")
DAEMON_BASE_PORT = 9400

DATA_DIR = Path(os.environ.get(
    "CAV_DATA_DIR",
    Path.home() / ".local" / "share" / "coding-cave",
))

# Runtime flags set once in main() before dispatch
verbose: bool = False


def v() -> dict:
    """Return git() kwargs for the current verbosity setting."""
    return {"echo": verbose, "capture": not verbose}


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
    slug = agent_project(agent_id) or project_slug(base)
    return f"{slug}@{agent_id}"


# -- Project-level paths (inside the project repo) --

def cav_dir(base: Path) -> Path:
    return base / ".coding-cave"


def shared_repo_path(base: Path) -> Path:
    return cav_dir(base) / "cave-shared.git"


# -- Data dir paths (global, ~/.local/share/coding-cave/) --

def nix_overlay_dir() -> Path:
    return DATA_DIR / "nix-overlay"


def nix_state_dir() -> Path:
    return DATA_DIR / "nix-state"


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


def session_file_exists(slug: str, session_id: str) -> bool:
    """Check whether a session .jsonl file exists on disk."""
    return (_claude_project_dir(slug) / f"{session_id}.jsonl").is_file()


def latest_session_file(slug: str) -> str | None:
    """Return the session ID of the most recent .jsonl file, or None."""
    d = _claude_project_dir(slug)
    if not d.is_dir():
        return None
    best, best_mtime = None, 0.0
    for f in d.iterdir():
        if f.suffix == ".jsonl" and f.stat().st_mtime > best_mtime:
            best, best_mtime = f.stem, f.stat().st_mtime
    return best


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


_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def _open_registry() -> sqlite3.Connection:
    """Open the agent registry, creating tables on first use."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_registry_path()), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    has_tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agents'"
    ).fetchone()
    if not has_tables:
        db.executescript(_SCHEMA)
    return db


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


def clear_session_id(agent_id: str) -> None:
    """Set an agent's session_id to NULL."""
    db = _open_registry()
    try:
        db.execute(
            "UPDATE agents SET session_id = NULL WHERE agent_id = ?",
            (agent_id,)
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


# -- Launch options --

def save_agent_launch_opts(agent_id: str, extra_slugs: list[str],
                           recursive: bool) -> None:
    """Persist launch options so cav -c can restore them."""
    db = _open_registry()
    try:
        db.execute(
            "UPDATE agents SET extra_projects = ?, recursive = ? "
            "WHERE agent_id = ?",
            (json.dumps(extra_slugs) if extra_slugs else None,
             int(recursive), agent_id)
        )
        db.commit()
    finally:
        db.close()


def read_agent_launch_opts(agent_id: str) -> tuple[list[str], bool]:
    """Read saved launch options. Returns (extra_project_slugs, recursive)."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT extra_projects, recursive FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        if not row:
            return [], False
        extras = json.loads(row[0]) if row[0] else []
        return extras, bool(row[1])
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


def continue_agent_id(base: Path, agent_id: str | None = None) -> str | None:
    """Reacquire a paused agent.

    If agent_id is given, continue that specific agent (regardless of project).
    Otherwise, continue the most recently exited agent whose primary project
    matches base.
    Returns agent_id, None if nothing to continue, or raises SystemExit.
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
            return None

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


def resolve_continue(base: Path, agent_id_hint: str | None = None):
    """Resolve agent_id, session_id, resuming for cav -c.

    Returns (agent_id, session_id, resuming).
    """
    agent_id = continue_agent_id(base, agent_id_hint)
    if agent_id is None:
        # Nothing in the registry. Check for a session file on disk
        # (e.g. a zero-commit agent that was cleaned up on exit).
        agent_id = acquire_agent_id(base)
        slug = project_slug(base)
        found = latest_session_file(slug)
        if found:
            write_session_id(agent_id, found)
            return agent_id, found, True
        return agent_id, read_session_id(agent_id), False

    session_id = read_session_id(agent_id)
    slug = project_slug(base)
    if session_id and session_file_exists(slug, session_id):
        return agent_id, session_id, True

    # Session file gone or never created; start fresh.
    if session_id:
        clear_session_id(agent_id)
    session_id = str(uuid.uuid4())
    write_session_id(agent_id, session_id)
    return agent_id, session_id, False


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


def agent_all_slugs(agent_id: str) -> list[str]:
    """Return primary + extra project slugs for an agent."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT project, extra_projects FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        if not row:
            return []
        slugs = [row[0]]
        if row[1]:
            slugs.extend(json.loads(row[1]))
        return slugs
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
    """Return all agent IDs in the registry, optionally filtered by project slug.

    When project is given, includes agents whose primary project matches
    AND agents that have the project in their extra_projects list.
    """
    db = _open_registry()
    try:
        if project:
            rows = db.execute(
                "SELECT agent_id, project, extra_projects FROM agents"
            ).fetchall()
            ids = []
            for aid, primary, extras_json in rows:
                if primary == project:
                    ids.append(aid)
                elif extras_json and project in json.loads(extras_json):
                    ids.append(aid)
        else:
            ids = [r[0] for r in db.execute("SELECT agent_id FROM agents").fetchall()]
        return sorted(ids, key=int)
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


def agent_project(agent_id: str) -> str | None:
    """Return the primary project slug for an agent, or None."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT project FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


# -- Approvals --

def record_approval(project: str, shared_branch: str, agent_id: str,
                    before_sha: str, after_sha: str) -> None:
    """Record an approval in the database."""
    db = _open_registry()
    try:
        db.execute(
            "INSERT INTO approvals "
            "(project, shared_branch, agent_id, before_sha, after_sha, approved_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, shared_branch, agent_id, before_sha, after_sha, time.time())
        )
        db.commit()
    finally:
        db.close()


def lookup_approval_by_after(project: str, shared_branch: str,
                             after_sha: str) -> dict | None:
    """Find the approval that produced the given after_sha on a branch."""
    db = _open_registry()
    try:
        row = db.execute(
            "SELECT id, agent_id, before_sha, after_sha FROM approvals "
            "WHERE project = ? AND shared_branch = ? AND after_sha = ?",
            (project, shared_branch, after_sha)
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "agent_id": row[1], "before_sha": row[2], "after_sha": row[3]}
    finally:
        db.close()


def delete_approval(approval_id: int) -> None:
    """Delete an approval record by ID."""
    db = _open_registry()
    try:
        db.execute("DELETE FROM approvals WHERE id = ?", (approval_id,))
        db.commit()
    finally:
        db.close()
