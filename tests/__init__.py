"""Shared test utilities."""

from cav import registry


def find_agent_by_session(session_id: str) -> str | None:
    """Return the agent ID that owns the given session UUID, or None.

    Removed from production code (no longer called by CLI), but kept
    here so tests can verify session-ID storage round-trips.
    """
    db = registry._open()
    try:
        row = db.execute(
            "SELECT agent_id FROM agents WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()
