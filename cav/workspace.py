"""Agent workspace management.

Each agent gets a lightweight clone (--shared) of the shared remote (bare repo).
Objects are shared via alternates, so disk cost is minimal. The clone has its
own .git, making it self-contained for bwrap mounting.
"""

import subprocess
from pathlib import Path

from . import config, git
from .constants import AGENT_ID_RE, CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR


def _repo_is_empty(path: Path) -> bool:
    """True if the repo has no commits."""
    r = git.git("rev-parse", "HEAD", cwd=path, check=False, capture=True, echo=False)
    return r.returncode != 0


def create(base: Path, agent_id: str) -> tuple[Path, Path]:
    """Create a workspace for an agent. Returns (clone_path, shared_repo)."""
    if not AGENT_ID_RE.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID '{agent_id}'. Must be a number (e.g. 1, 2, 3)."
        )

    empty = _repo_is_empty(base)
    if empty:
        base_name = "main"
    else:
        user_branch = git.current_branch(cwd=base)
        base_name = config.resolve_base(user_branch)

    slug = config.project_slug(base)
    ws_path = config.worktree_dir(slug, agent_id)
    shared = config.shared_repo_path(base)

    if ws_path.exists():
        return ws_path, shared

    # "HEAD" means detached HEAD, which is not a valid base.
    if base_name == "HEAD":
        raise SystemExit(
            f"Cannot launch a new agent from detached HEAD.\n"
            f"Switch to a branch first (e.g. 'git checkout main')."
        )

    ws_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure the shared repo accepts pushes from shallow clones.
    git.git("config", "receive.shallowUpdate", "true",
            cwd=shared, echo=False, capture=True)

    # Sync user repo to shared repo so the clone has all branches.
    # Only push base branches; dev branches are managed by agents
    # and the host's local copies can be stale (behind the shared remote).
    if not empty:
        branches = git.git_output(
            "for-each-ref", "--format=%(refname:short)", "refs/heads/",
            cwd=base)
        for branch in branches.splitlines():
            if config.parse_dev_branch(branch):
                continue
            git.git("push", "--no-set-upstream", CAVE_SHARED_REMOTE, branch,
                    cwd=base, echo=False, check=False, capture=True)

    # Lightweight clone sharing objects with the bare repo
    git.git("clone", "--shared", str(shared), str(ws_path), echo=False, capture=True)

    # Make alternates work on both host and inside the cave namespace.
    # git clone --shared writes a host-absolute path in alternates, which
    # won't resolve inside the cave (different mount namespace).  We create
    # a mount-point directory with a symlink for host access, then use a
    # relative alternates path.  Inside the cave, bwrap mounts the real
    # shared repo at HOST_SHARED_REPO_DIR (read-only), hiding the symlink.
    mount_point = ws_path / ".coding-cave" / HOST_SHARED_REPO_DIR
    mount_point.mkdir(parents=True)
    (mount_point / "objects").symlink_to(shared / "objects")
    alt = ws_path / ".git" / "objects" / "info" / "alternates"
    alt.write_text(f"../../.coding-cave/{HOST_SHARED_REPO_DIR}/objects\n")
    exclude = ws_path / ".git" / "info" / "exclude"
    with exclude.open("a") as f:
        f.write("/.coding-cave\n")

    # Rename origin -> host-cave (points to the shared remote)
    git.git("remote", "rename", "origin", HOST_SHARED_REMOTE, cwd=ws_path, echo=False, capture=True)

    dev = config.dev_branch(base_name, agent_id)

    if empty:
        # No commits yet, create an orphan branch so the agent can make the first commit.
        git.git("checkout", "--orphan", dev, cwd=ws_path, echo=False, capture=True)
        return ws_path, shared

    remote_ref = f"refs/remotes/{HOST_SHARED_REMOTE}/{dev}"
    base_ref = f"refs/remotes/{HOST_SHARED_REMOTE}/{base_name}"

    # If the dev branch already exists on the remote (re-creating a
    # workspace for a continuing agent), check it out with tracking.
    # Otherwise create a fresh branch from the base and push it.
    r = git.git("rev-parse", "--verify", remote_ref,
                cwd=ws_path, check=False, capture=True, echo=False)
    if r.returncode == 0:
        git.git("checkout", dev, cwd=ws_path, echo=False, capture=True)
    else:
        git.git("checkout", "-b", dev, base_ref, cwd=ws_path,
                echo=False, capture=True)
        r = git.git("push", HOST_SHARED_REMOTE, dev,
                    cwd=ws_path, echo=False, capture=True, check=False)
        if r.returncode and not config.inside_cave():
            msg = (r.stderr or r.stdout or "").strip()
            raise SystemExit(
                f"Failed to push initial branch '{dev}' to shared repo.\n"
                f"{msg}"
            )

    return ws_path, shared
