"""Agent workspace management.

Each agent gets a lightweight clone (--shared) of the shared remote (bare repo).
Objects are shared via alternates, so disk cost is minimal. The clone has its
own .git, making it self-contained for bwrap mounting.
"""

import subprocess
from pathlib import Path

from . import git
from .. import config
from ..constants import AGENT_ID_RE, CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR


def _repo_is_empty(path: Path) -> bool:
    """True if the repo has no commits."""
    r = git.git("rev-parse", "HEAD", cwd=path, check=False, capture=True, echo=False)
    return r.returncode != 0


def configure(ws: Path, shared: Path) -> None:
    """Apply the workspace layout to ws.

    Ensures .coding-cave/host-cave-shared.git exists with an objects
    symlink to the project's shared bare repo (for host-side
    resolution; bwrap overlays a bind mount here inside a cave) and
    rewrites .git/objects/info/alternates to the relative path that
    resolves there on both host and cave. Idempotent. No-op for repos
    with no alternates (full clones).
    """
    alt = ws / ".git" / "objects" / "info" / "alternates"
    if not alt.exists():
        return
    mount_point = ws / ".coding-cave" / HOST_SHARED_REPO_DIR
    mount_point.mkdir(parents=True, exist_ok=True)
    objects_link = mount_point / "objects"
    if not (objects_link.exists() or objects_link.is_symlink()):
        objects_link.symlink_to(shared / "objects")
    alt.write_text(f"../../.coding-cave/{HOST_SHARED_REPO_DIR}/objects\n")
    exclude = ws / ".git" / "info" / "exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if "/.coding-cave" not in lines:
        with exclude.open("a") as f:
            f.write("/.coding-cave\n")


def verify_layout(ws: Path, agent_id: str) -> None:
    """Ensure ws matches the current scheme: local branch is the base name
    (no agent-<id>/ prefix), and the host-cave push refspec maps it to
    agent-<id>/<base>. Idempotent.

    Migrates workspaces created before the rename (where the local branch
    was agent-<id>/<base>). Runs on every session start and on
    cav init --reinitialize.
    """
    try:
        current = git.current_branch(cwd=ws)
    except Exception:
        return
    parsed = config.parse_dev_branch(current)
    if parsed:
        base_name, _ = parsed
        # Drop any stale local <base> so the rename won't collide.
        git.git("branch", "-D", base_name, cwd=ws,
                echo=False, check=False, capture=True)
        git.git("branch", "-m", current, base_name, cwd=ws,
                echo=False, check=False, capture=True)
    else:
        base_name = current
    set_agent_push_refspec(ws, base_name, agent_id)


def reconfigure(user_repo: Path) -> None:
    """Reapply the workspace layout to each of this project's workspaces.

    Idempotent. Intended for cav init --reinitialize to restore
    workspaces whose alternates, mount point, branch name, or push
    refspec drifted from what workspace.create writes.
    """
    slug = config.project_slug(user_repo)
    worktrees = config.DATA_DIR / "worktrees" / slug
    if not worktrees.is_dir():
        return
    shared = config.shared_repo_path(user_repo)
    for ws in worktrees.iterdir():
        if ws.is_dir():
            configure(ws, shared)
            verify_layout(ws, ws.name)


def agent_push_refspec(base_name: str, agent_id: str) -> str:
    """Push refspec that publishes local <base> as agent-<id>/<base>."""
    return (
        f"refs/heads/{base_name}:"
        f"refs/heads/{config.dev_branch(base_name, agent_id)}"
    )


def set_agent_push_refspec(ws: Path, base_name: str, agent_id: str) -> None:
    """Configure host-cave's push refspec so git push maps local <base>
    to agent-<id>/<base> upstream. Idempotent: replaces any prior value.
    """
    key = f"remote.{HOST_SHARED_REMOTE}.push"
    git.git("config", "--unset-all", key,
            cwd=ws, check=False, capture=True, echo=False)
    git.git("config", "--add", key,
            agent_push_refspec(base_name, agent_id),
            cwd=ws, echo=False, capture=True)


def create(base: Path, agent_id: str) -> tuple[Path, Path]:
    """Create a workspace for an agent. Returns (clone_path, shared_repo).

    The workspace's local branch is the base branch (e.g. 'main'). An
    asymmetric push refspec maps it to agent-<id>/<base> on the shared
    remote so the cave publishes its work under its agent name without
    ever seeing that name inside.
    """
    if not AGENT_ID_RE.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID '{agent_id}'. Must be a number (e.g. 1, 2, 3)."
        )

    slug = config.project_slug(base)
    ws_path = config.worktree_dir(slug, agent_id)
    shared = config.shared_repo_path(base)

    if ws_path.exists():
        return ws_path, shared

    empty = _repo_is_empty(base)
    if empty:
        base_name = "main"
    else:
        user_branch = git.current_branch(cwd=base)
        base_name = config.resolve_base(user_branch)

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

    # Lightweight clone sharing objects with the bare repo. git clone
    # --shared writes a host-absolute path in alternates; configure()
    # rewrites it to a relative path that resolves on both host and cave.
    git.git("clone", "--shared", str(shared), str(ws_path), echo=False, capture=True)
    configure(ws_path, shared)

    # Rename origin -> host-cave (points to the shared remote)
    git.git("remote", "rename", "origin", HOST_SHARED_REMOTE, cwd=ws_path, echo=False, capture=True)

    # Configure asymmetric push refspec for the base branch.
    set_agent_push_refspec(ws_path, base_name, agent_id)

    if empty:
        # No commits yet, create an orphan branch so the agent can make the first commit.
        git.git("checkout", "--orphan", base_name, cwd=ws_path, echo=False, capture=True)
        return ws_path, shared

    dev_name = config.dev_branch(base_name, agent_id)
    dev_ref = f"refs/remotes/{HOST_SHARED_REMOTE}/{dev_name}"
    base_ref = f"refs/remotes/{HOST_SHARED_REMOTE}/{base_name}"

    # Continue if agent-<id>/<base> already exists on the remote; otherwise
    # fork a fresh branch from the base and publish it.
    r = git.git("rev-parse", "--verify", dev_ref,
                cwd=ws_path, check=False, capture=True, echo=False)
    if r.returncode == 0:
        git.git("checkout", "-B", base_name, dev_ref, cwd=ws_path,
                echo=False, capture=True)
        # Track the base so `git rebase` with no args rebases onto the
        # base branch, which is the expected sync workflow.
        git.git("branch", "--set-upstream-to",
                f"{HOST_SHARED_REMOTE}/{base_name}", base_name,
                cwd=ws_path, echo=False, check=False, capture=True)
    else:
        git.git("checkout", "-B", base_name, base_ref, cwd=ws_path,
                echo=False, capture=True)
        r = git.git("push", HOST_SHARED_REMOTE,
                    agent_push_refspec(base_name, agent_id),
                    cwd=ws_path, echo=False, capture=True, check=False)
        if r.returncode and not config.inside_cave():
            msg = (r.stderr or r.stdout or "").strip()
            raise SystemExit(
                f"Failed to push initial branch '{dev_name}' to shared repo.\n"
                f"{msg}"
            )

    return ws_path, shared
