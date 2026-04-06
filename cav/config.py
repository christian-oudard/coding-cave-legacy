"""Constants and naming helpers for Coding Cave."""

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("cav")

VERSION = "@VERSION@"

CAVE_SHARED_REMOTE = "cave"
HOST_SHARED_REMOTE = "host-cave"
AGENT_ID_RE = re.compile(r"^\d+$")
# agent-<id>/<base>, e.g. agent-1/develop
AGENT_BRANCH_RE = re.compile(r"^agent-(\d+)/(.+)$")
DAEMON_BASE_PORT = 61000

DATA_DIR = Path(os.environ.get(
    "CODING_CAVE_DATA_DIR",
    Path.home() / ".local" / "share" / "coding-cave",
))

# Runtime flags set once in main() before dispatch
verbose: bool = False


def v() -> dict:
    """Return git() kwargs for the current verbosity setting."""
    return {"echo": verbose, "capture": not verbose}


def dev_branch(base: str, agent_id: str) -> str:
    return f"agent-{agent_id}/{base}"


def parse_dev_branch(branch: str) -> tuple[str, str] | None:
    """Returns (base, agent_id) or None."""
    m = AGENT_BRANCH_RE.match(branch)
    if m:
        return m.group(2), m.group(1)
    return None


def resolve_base(branch: str) -> str:
    """Resolve the ultimate base branch of a dev branch.
    Handles nested dev branches like agent-7/agent-5/main -> main."""
    while True:
        parsed = parse_dev_branch(branch)
        if not parsed:
            return branch
        branch = parsed[0]


# -- Project-level paths (inside the project repo) --

def cav_dir(base: Path) -> Path:
    return base / ".coding-cave"


def shared_repo_path(base: Path) -> Path:
    return cav_dir(base) / "cave-shared.git"


# -- Data dir paths (global, ~/.local/share/coding-cave/) --

def nix_overlay_dir() -> Path:
    return DATA_DIR / "nix-overlay"


def nix_db_dir() -> Path:
    return DATA_DIR / "nix-overlay" / "shared" / "db"


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
    slug = re.sub(r"[^a-z0-9_-]+", "-", name).strip("-")
    return slug or "project"


def inside_cave() -> bool:
    return "CODING_CAVE_VERSION" in os.environ
