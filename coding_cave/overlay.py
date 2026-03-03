"""Overlay mount management and cleanup."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import _state, sessions, projects, git as git_mod


def unmount_fuse(mountpoint):
    """Lazy-unmount a fuse mount.  Returns True if fusermount3 succeeded."""
    r = subprocess.run(["fusermount3", "-uz", str(mountpoint)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Most likely cause: non-setuid fusermount3 from nix store
        # shadows the system's setuid one.  Try the system path.
        for p in ["/run/wrappers/bin/fusermount3", "/usr/bin/fusermount3"]:
            if os.path.isfile(p):
                r = subprocess.run([p, "-uz", str(mountpoint)],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    return True
        print(f"fusermount3 -uz {mountpoint}: exit {r.returncode}: "
              f"{r.stderr.strip()}", file=sys.stderr)
    return r.returncode == 0


def unmount_all_under(path):
    """Unmount any fuse mounts inside path (deepest first)."""
    path = Path(path).resolve()
    mounts = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                mp = line.split()[1]
                mp_path = Path(mp)
                if mp_path == path or (mp_path.is_relative_to(path)):
                    mounts.append(mp)
    except OSError:
        return
    # Unmount deepest first to avoid "busy" on parent mounts
    for mp in sorted(mounts, key=len, reverse=True):
        unmount_fuse(mp)


def force_rmtree(path):
    """Remove a directory tree, unmounting and forcing permissions as needed."""
    path = Path(path)
    if not path.exists():
        return
    unmount_all_under(path)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                (Path(dirpath) / name).chmod(0o700)
            except OSError:
                pass
    try:
        shutil.rmtree(path)
    except OSError as e:
        print(f"Warning: could not remove {path}: {e}", file=sys.stderr)


def reset_project_overlay(slug):
    """Reset a project: wipe overlays and recreate gitdir."""
    loaded_projects = projects.load_projects()
    proj_state = _state.projects_dir() / slug

    for subdir in ["upper", "work", "gitdir", "sessions", "saved-upper"]:
        p = proj_state / subdir
        if p.exists():
            force_rmtree(p)
            if subdir not in ("gitdir", "sessions", "saved-upper"):
                p.mkdir()
    (proj_state / "saved-index").unlink(missing_ok=True)

    if slug in loaded_projects:
        git_mod.initialize_gitdir(slug, Path(loaded_projects[slug]))


def clean_stale_session_overlays():
    """Remove session overlay dirs that have no running session."""
    overlay_root = _state.cave_data() / "session-overlays"
    if not overlay_root.exists():
        return
    running = {s["sid"] for s in sessions.list_sessions()}
    for entry in overlay_root.iterdir():
        if entry.is_dir() and entry.name not in running:
            force_rmtree(entry)
