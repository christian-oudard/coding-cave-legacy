"""Agent and project registry (SQLite)."""

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from . import config


def _vprint(*args, **kwargs):
    """Print to stderr if verbose mode is on."""
    if config.verbose:
        print(*args, file=sys.stderr, **kwargs)


def _registry_path() -> Path:
    return config.DATA_DIR / "coding-cave.db"


_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def _open() -> sqlite3.Connection:
    """Open the registry, creating tables on first use."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
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


def _sweep_dead_pids(db, now: float) -> None:
    """Mark agents with dead host PIDs as exited. Call inside a transaction."""
    rows = db.execute(
        "SELECT agent_id, host_pid FROM agents "
        "WHERE exited_at IS NULL AND host_pid IS NOT NULL"
    ).fetchall()
    for aid, p in rows:
        if not _is_pid_alive(p):
            db.execute(
                "UPDATE agents SET exited_at = ?, host_pid = NULL, cave_pid = NULL "
                "WHERE agent_id = ?",
                (now, aid)
            )


def display_name(base: Path, agent_id: str) -> str:
    """Display name: <slug>@<id>."""
    slug = agent_project(agent_id) or config.project_slug(base)
    return f"{slug}@{agent_id}"


# -- Session management --

def write_session_id(agent_id: str, session_id: str) -> None:
    """Record the session ID for an agent."""
    db = _open()
    try:
        # Clear from any other agent first (dead agents may still hold it)
        db.execute(
            "UPDATE agents SET session_id = NULL "
            "WHERE session_id = ? AND agent_id != ?",
            (session_id, agent_id)
        )
        db.execute(
            "UPDATE agents SET session_id = ? WHERE agent_id = ?",
            (session_id, agent_id)
        )
        db.commit()
    finally:
        db.close()


def clear_session_id(agent_id: str) -> None:
    """Set an agent's session_id to NULL."""
    db = _open()
    try:
        db.execute(
            "UPDATE agents SET session_id = NULL WHERE agent_id = ?",
            (agent_id,)
        )
        db.commit()
    finally:
        db.close()


def find_agent_by_session(session_id: str) -> str | None:
    """Return the agent ID that owns the given session UUID, or None."""
    db = _open()
    try:
        row = db.execute(
            "SELECT agent_id FROM agents WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def read_session_id(agent_id: str) -> str | None:
    """Read the stored Claude session ID for an agent, or None."""
    db = _open()
    try:
        row = db.execute(
            "SELECT session_id FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        db.close()


# -- Launch options --

def save_agent_launch_opts(agent_id: str, extra_slugs: list[str]) -> None:
    """Persist launch options so cav -c can restore them."""
    db = _open()
    try:
        db.execute(
            "UPDATE agents SET extra_projects = ? WHERE agent_id = ?",
            (json.dumps(extra_slugs) if extra_slugs else None, agent_id)
        )
        db.commit()
    finally:
        db.close()


def read_agent_launch_opts(agent_id: str) -> list[str]:
    """Read saved launch options. Returns extra_project_slugs."""
    db = _open()
    try:
        row = db.execute(
            "SELECT extra_projects FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        if not row:
            return []
        return json.loads(row[0]) if row[0] else []
    finally:
        db.close()


# -- Agent lifecycle --

def is_agent_running(agent_id: str) -> bool:
    """Check if an agent process is alive by its host PID."""
    db = _open()
    try:
        row = db.execute(
            "SELECT host_pid, exited_at FROM agents WHERE agent_id = ?",
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
            "UPDATE agents SET exited_at = ?, host_pid = NULL, cave_pid = NULL "
            "WHERE agent_id = ?",
            (time.time(), agent_id)
        )
        db.commit()
        return False
    finally:
        db.close()


def agent_exit_time(agent_id: str) -> float | None:
    """Read the exit timestamp for an agent, or None."""
    db = _open()
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
    Records host_pid (cav CLI process). cave_pid is set later by the
    sandbox launcher, once the bwrap child exists.
    Returns agent_id. Call mark_agent_exited() when the agent exits.
    """
    slug = config.project_slug(base)
    now = time.time()
    pid = os.getpid()
    db = _open()
    try:
        db.execute("BEGIN IMMEDIATE")
        _sweep_dead_pids(db, now)

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
            "(agent_id, project, host_pid, started_at, exited_at, session_id) "
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
    slug = config.project_slug(base)
    now = time.time()
    pid = os.getpid()
    db = _open()
    try:
        db.execute("BEGIN IMMEDIATE")
        _sweep_dead_pids(db, now)

        if agent_id is not None:
            row = db.execute(
                "SELECT host_pid, exited_at FROM agents WHERE agent_id = ?",
                (agent_id,)
            ).fetchone()
            if not row:
                raise SystemExit(f"No agent {agent_id} found.")
            p, exited_at = row
            if exited_at is None and p is not None and _is_pid_alive(p):
                raise SystemExit(f"Agent {agent_id} is still running.")
            db.execute(
                "UPDATE agents SET host_pid = ?, cave_pid = NULL, "
                "started_at = ?, exited_at = NULL "
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
            all_rows = db.execute(
                "SELECT agent_id, project, exited_at, host_pid FROM agents"
            ).fetchall()
            if all_rows:
                _vprint(f"continue: no exited agent for project "
                        f"'{slug}', registry has:")
                for r in all_rows:
                    state = ("running" if r[2] is None else "exited")
                    _vprint(f"  agent {r[0]}: project={r[1]}, "
                            f"{state}, host_pid={r[3]}")
            else:
                _vprint("continue: registry is empty")
            return None

        aid = row[0]
        db.execute(
            "UPDATE agents SET host_pid = ?, cave_pid = NULL, "
            "started_at = ?, exited_at = NULL "
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
    Raises SystemExit if nothing to continue.
    """
    slug = config.project_slug(base)
    agent_id = continue_agent_id(base, agent_id_hint)

    if agent_id is None:
        # Nothing in the registry. Check for a session file on disk
        # (e.g. a zero-commit agent that was cleaned up on exit).
        found = config.latest_session_file(slug)
        if found:
            agent_id = acquire_agent_id(base)
            _vprint(f"continue: no agent in registry, resuming "
                    f"latest session file: {found}")
            write_session_id(agent_id, found)
            return agent_id, found, True
        d = config._claude_project_dir(slug)
        raise SystemExit(
            f"Nothing to continue. No agents in registry and no "
            f"session files in {d}\n"
            f"Start a new session with: cav agent"
        )

    session_id = read_session_id(agent_id)
    if session_id and config.session_file_exists(slug, session_id):
        _vprint(f"continue: resuming agent {agent_id}, "
                f"session {session_id}")
        return agent_id, session_id, True

    # Session file gone or never created; start fresh in same agent slot.
    d = config._claude_project_dir(slug)
    if session_id:
        f = d / f"{session_id}.jsonl"
        print(f"Warning: session file missing ({f}), "
              f"starting fresh.", file=sys.stderr)
        clear_session_id(agent_id)
    session_id = str(uuid.uuid4())
    write_session_id(agent_id, session_id)
    return agent_id, session_id, False


def mark_agent_exited(agent_id: str) -> None:
    """Mark an agent as exited."""
    db = _open()
    try:
        db.execute(
            "UPDATE agents SET exited_at = ?, host_pid = NULL, cave_pid = NULL "
            "WHERE agent_id = ?",
            (time.time(), agent_id)
        )
        db.commit()
    finally:
        db.close()


def set_cave_pid(agent_id: str, pid: int) -> None:
    """Record the bwrap child PID (lives inside the cave's mount namespace).

    Called by the sandbox launcher once Popen has produced a child PID.
    Used by cav cp to reach cave-internal absolute paths via
    /proc/<cave_pid>/root/<path>, which the kernel resolves through the
    target process's mount namespace.
    """
    db = _open()
    try:
        db.execute(
            "UPDATE agents SET cave_pid = ? WHERE agent_id = ?",
            (pid, agent_id)
        )
        db.commit()
    finally:
        db.close()


def cave_pid(agent_id: str) -> int | None:
    """Return the bwrap child PID for a running agent, or None.

    None if the agent is not in the registry, not yet launched, or
    already exited.
    """
    db = _open()
    try:
        row = db.execute(
            "SELECT cave_pid FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        db.close()


def agent_all_slugs(agent_id: str) -> list[str]:
    """Return primary + extra project slugs for an agent."""
    db = _open()
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
    db = _open()
    try:
        db.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        db.commit()
    finally:
        db.close()


def all_agent_ids(project: str | None = None) -> list[str]:
    """Return all agent IDs, optionally filtered by project slug.

    When project is given, includes agents whose primary project matches
    AND agents that have the project in their extra_projects list.
    """
    db = _open()
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
            ids = [r[0] for r in db.execute(
                "SELECT agent_id FROM agents"
            ).fetchall()]
        return sorted(ids, key=int)
    finally:
        db.close()


# -- Project registry --

def register_project(base: Path) -> str:
    """Register a project. Fails loud on slug collision."""
    slug = config.project_slug(base)
    path = str(base.resolve())
    db = _open()
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
    db = _open()
    try:
        row = db.execute(
            "SELECT path FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def all_projects() -> list[tuple[str, str]]:
    """Return all (slug, path) pairs."""
    db = _open()
    try:
        return db.execute("SELECT slug, path FROM projects").fetchall()
    finally:
        db.close()


def remove_project(slug: str) -> None:
    """Unregister a project and its agents."""
    db = _open()
    try:
        db.execute("DELETE FROM agents WHERE project = ?", (slug,))
        db.execute("DELETE FROM projects WHERE slug = ?", (slug,))
        db.commit()
    finally:
        db.close()


def agent_project(agent_id: str) -> str | None:
    """Return the primary project slug for an agent, or None."""
    db = _open()
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
    """Record an approval."""
    db = _open()
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
    db = _open()
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
    db = _open()
    try:
        db.execute("DELETE FROM approvals WHERE id = ?", (approval_id,))
        db.commit()
    finally:
        db.close()
