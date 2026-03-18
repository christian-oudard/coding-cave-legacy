"""Mutable constants — the ONLY place with @SANDBOX_BIN@ etc."""
from pathlib import Path

SANDBOX_BIN = "@SANDBOX_BIN@"
SANDBOX_GPU_BIN = "@SANDBOX_GPU_BIN@"
GATEWAY_LOCAL_BIN = "@GATEWAY_LOCAL_BIN@"
IPC_SETUP_SH = "@IPC_SETUP_SH@"
VERSION = "@VERSION@"

JAIL_HOME = "/home/cave-coder"

_cave_data = None


def cave_data():
    """Return the cave data root, defaulting to ~/.local/share/coding-cave."""
    if _cave_data is not None:
        return _cave_data
    return Path.home() / ".local" / "share" / "coding-cave"


def set_cave_data(path):
    """Override the cave data root (used by tests)."""
    global _cave_data
    _cave_data = Path(path) if path is not None else None


def projects_file():
    return cave_data() / "projects.json"


def projects_dir():
    return cave_data() / "projects"


def nix_overlay_dir():
    return cave_data() / "nix-overlay"


def sessions_dir():
    return cave_data() / "sessions"


def cave_home():
    return cave_data() / "home"
