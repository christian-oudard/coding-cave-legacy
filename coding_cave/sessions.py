"""Session tracking and cleanup."""
import json
import os
import shutil
import subprocess
import time

from . import _state, projects, overlay


def session_id():
    """Generate a unique session ID."""
    return f"{int(time.time())}-{os.getpid()}"


def _next_session_index(project):
    """Find the next unused session index for a project."""
    used = {s.get("index", 0) for s in list_sessions(project=project)}
    i = 1
    while i in used:
        i += 1
    return i


def save_last_index(project, index):
    """Persist the session index for reuse on resume/continue."""
    path = _state.projects_dir() / project / "last-session-index"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(index))


def pop_last_index(project):
    """Load and consume the saved session index. Returns int or None."""
    path = _state.projects_dir() / project / "last-session-index"
    if not path.exists():
        return None
    try:
        index = int(path.read_text().strip())
        path.unlink()
        return index
    except (ValueError, OSError):
        return None


def register_session(sid, project, bwrap_pid, index=None, mounted_projects=None):
    """Register a running session."""
    _state.sessions_dir().mkdir(parents=True, exist_ok=True)
    if index is None:
        index = _next_session_index(project)
    info = {
        "sid": sid,
        "project": project,
        "bwrap_pid": bwrap_pid,
        "started": int(time.time()),
        "index": index,
    }
    if mounted_projects is not None:
        info["mounted_projects"] = sorted(mounted_projects)
    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane:
        info["tmux_pane"] = tmux_pane
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


def update_pane_titles(project):
    """Update tmux pane titles for all sessions of a project.

    Uses the stable index stored in session metadata.
    Uses tmux select-pane -T if in tmux, otherwise terminal title escape.
    """
    active = list_sessions(project=project)
    has_tmux = shutil.which("tmux") is not None
    for s in active:
        idx = s.get("index", "?")
        title = f"cave:{project} #{idx}"
        pane = s.get("tmux_pane")
        if pane and has_tmux:
            subprocess.run(
                ["tmux", "select-pane", "-t", pane, "-T", title],
                capture_output=True,
            )
        elif s.get("bwrap_pid"):
            try:
                fd_path = f"/proc/{s['bwrap_pid']}/fd/0"
                with open(fd_path, "w") as fd:
                    fd.write(f"\033]0;{title}\007")
            except OSError:
                pass
