"""Session tracking and cleanup."""
import json
import os
import time

from . import _state, projects, overlay


def session_id():
    """Generate a unique session ID."""
    return f"{int(time.time())}-{os.getpid()}"


def register_session(sid, project, bwrap_pid):
    """Register a running session."""
    _state.sessions_dir().mkdir(parents=True, exist_ok=True)
    info = {
        "sid": sid,
        "project": project,
        "bwrap_pid": bwrap_pid,
        "started": int(time.time()),
    }
    with open(_state.sessions_dir() / f"{sid}.json", "w") as f:
        json.dump(info, f)


def list_sessions(project=None):
    """List active sessions, cleaning up stale ones."""
    if not _state.sessions_dir().exists():
        return []
    sessions = []
    for f in sorted(_state.sessions_dir().iterdir()):
        if not f.name.endswith(".json") or f.name.endswith(".manifest.json"):
            continue
        try:
            info = json.loads(f.read_text())
            pid = info["bwrap_pid"]
            os.kill(pid, 0)  # check if alive
            if project is None or info.get("project") == project:
                sessions.append(info)
        except (json.JSONDecodeError, KeyError, ProcessLookupError, ValueError):
            f.unlink(missing_ok=True)
            # Clean up manifest too
            manifest = f.with_suffix(".manifest.json")
            manifest.unlink(missing_ok=True)
    return sessions


def cleanup_session(sid):
    """Remove session files."""
    for slug in projects.load_projects():
        overlay.force_rmtree(_state.projects_dir() / slug / "sessions" / sid)
    (_state.sessions_dir() / f"{sid}.json").unlink(missing_ok=True)
    (_state.sessions_dir() / f"{sid}.manifest.json").unlink(missing_ok=True)
