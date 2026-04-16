"""Configurable globals and helpers for Coding Cave."""

import os
import re
from pathlib import Path

from .constants import AGENT_BRANCH_RE, SHARED_REPO_DIR

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
    return cav_dir(base) / SHARED_REPO_DIR


# -- Data dir paths (global, ~/.local/share/coding-cave/) --

def persistent_home() -> Path:
    return DATA_DIR / "home"


def launch_home(agent_name: str) -> Path:
    """Dir mounted as $HOME inside an agent's sandbox.

    Mirrors sandbox.launch: subcaves get an isolated home under
    subcave-homes/<agent-name>/ so their nix overlay dirs don't
    collide with the parent's; host launches share persistent_home.
    """
    if inside_cave():
        return DATA_DIR / "subcave-homes" / agent_name
    return persistent_home()


def worktree_dir(slug: str, agent_id: str) -> Path:
    return DATA_DIR / "worktrees" / slug / agent_id


def _claude_project_dir(slug: str, home: Path | None = None) -> Path:
    """Return the Claude session directory for a project slug.

    Mirrors Claude Code's encoding: the sandbox cwd (/projects/<slug>)
    with every non-alphanumeric character replaced by '-'.
    """
    if home is None:
        home = persistent_home()
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", f"/projects/{slug}")
    return home / ".claude" / "projects" / encoded


def session_file_exists(slug: str, session_id: str,
                        home: Path | None = None) -> bool:
    """Check whether a session .jsonl file exists on disk."""
    return (_claude_project_dir(slug, home) / f"{session_id}.jsonl").is_file()


def latest_session_file(slug: str, home: Path | None = None) -> str | None:
    """Return the session ID of the most recent .jsonl file, or None."""
    d = _claude_project_dir(slug, home)
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


def self_agent_id() -> str | None:
    """Return this cave's agent id in the parent's registry, or None.

    The cave publishes its work upstream as agent-<id>/<base>, where <id>
    is its slot in the PARENT cave's registry (or the host's registry at
    the top level). The id is carried in via CODING_CAVE_AGENT_NAME, set
    by the launching supervisor (e.g. "coding-cave@8" -> "8").

    On the host (outside any cave) this returns None; nothing publishes
    host-level commits as an agent branch.
    """
    name = os.environ.get("CODING_CAVE_AGENT_NAME", "")
    if "@" not in name:
        return None
    aid = name.rsplit("@", 1)[-1]
    return aid if aid.isdigit() else None


