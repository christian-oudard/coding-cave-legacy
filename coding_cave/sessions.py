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


def register_session(sid, project, bwrap_pid):
    """Register a running session."""
    _state.sessions_dir().mkdir(parents=True, exist_ok=True)
    info = {
        "sid": sid,
        "project": project,
        "bwrap_pid": bwrap_pid,
        "started": int(time.time()),
    }
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
    """Update terminal/tmux pane titles for all sessions of a project.

    Assigns sequential indices matching cav shell/status display order.
    Uses tmux select-pane -T if in tmux, otherwise terminal title escape.
    """
    active = list_sessions(project=project)
    has_tmux = shutil.which("tmux") is not None
    for i, s in enumerate(active):
        title = f"cave:{project} #{i}"
        pane = s.get("tmux_pane")
        if pane and has_tmux:
            subprocess.run(
                ["tmux", "select-pane", "-t", pane, "-T", title],
                capture_output=True,
            )
        elif s.get("bwrap_pid"):
            # Write terminal title escape to bwrap's stdin fd via /proc.
            # This works for non-tmux terminals.
            try:
                fd_path = f"/proc/{s['bwrap_pid']}/fd/0"
                with open(fd_path, "w") as fd:
                    fd.write(f"\033]0;{title}\007")
            except OSError:
                pass
