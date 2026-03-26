"""Coding Cave CLI entry point."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, git


def _has_undelivered_work(base: Path, agent_id: str) -> bool:
    """Check if any agent branch has commits ahead of its target."""
    shared = config.shared_repo_path(base)
    try:
        refs = git.git_output(
            "for-each-ref", "--format=%(refname:short)",
            f"refs/heads/agent-{agent_id}/*",
            cwd=shared,
        )
    except subprocess.CalledProcessError:
        return False
    for branch in refs.splitlines():
        parsed = config.parse_agent_branch(branch)
        if not parsed:
            continue
        target, _ = parsed
        try:
            ahead = int(git.git_output(
                "rev-list", "--count", f"{target}..{branch}", cwd=shared,
            ))
            if ahead > 0:
                return True
        except subprocess.CalledProcessError:
            pass
    return False


def find_repo_root() -> Path:
    try:
        return Path(git.repo_root())
    except subprocess.CalledProcessError:
        raise SystemExit("Not in a git repository.")


def require_init(base: Path):
    if config.shared_repo_path(base).exists():
        return
    # Inside a cave the local bare repo isn't mounted, but the remote is.
    if int(os.environ.get("CODING_CAVE_DEPTH", "0")) > 0:
        try:
            git.git_output("remote", "get-url", config.SHARED_REMOTE, cwd=base)
            return
        except Exception:
            pass
    raise SystemExit("Coding Cave not initialized. Run: cav init")


def cmd_init(args):
    from .init import init
    init(args.path)


def resolve_project(path: str) -> Path:
    """Resolve a project path to its git root and validate init."""
    p = Path(path).resolve()
    if not (p / ".git").exists():
        raise SystemExit(f"Not a git repository: {p}")
    if not config.shared_repo_path(p).exists():
        raise SystemExit(
            f"Coding Cave not initialized in {p}\n"
            f"Run: cav init {p}"
        )
    return p


def cmd_agent(args):
    base = find_repo_root()
    require_init(base)
    from .worktree import create
    from .sandbox import launch

    def _is_available(aid):
        if not _has_undelivered_work(base, aid):
            return True
        return config.is_session_expired(base, aid)

    if args.resume_session:
        # Route --resume to the agent that owns this session
        slug = config.project_slug(base)
        owner = config.find_agent_by_session(slug, args.resume_session)
        if owner is None:
            raise SystemExit(
                f"No agent owns session {args.resume_session}.\n"
                f"Use --continue to resume the most recent agent instead."
            )
        agent_id, lock_fd = config.continue_agent_id(base, owner)
        session_id = args.resume_session
    elif args.continue_agent:
        agent_id, lock_fd = config.continue_agent_id(base)
        session_id = config.read_session_id(base, agent_id)
    else:
        agent_id, lock_fd = config.acquire_agent_id(base, available=_is_available)
        session_id = None

    # Primary project (cwd)
    projects = [(base, config.project_slug(base))]

    # Additional projects via -p
    for path in (args.extra_projects or []):
        extra = resolve_project(path)
        slug = config.project_slug(extra)
        # Check for slug collisions
        if any(s == slug for _, s in projects):
            raise SystemExit(
                f"Project slug collision: '{slug}' already used.\n"
                f"Two projects resolve to the same slug from: {extra}"
            )
        projects.append((extra, slug))

    # Create workspaces for all projects
    workspaces = []
    for project_base, slug in projects:
        ws = create(project_base, agent_id)
        shared = config.shared_repo_path(project_base)
        workspaces.append((ws, shared, slug))

    name = config.display_name(base, agent_id)
    lineage = os.environ.get("CODING_CAVE_LINEAGE", "")
    if lineage:
        name += f" (depth {int(os.environ.get('CODING_CAVE_DEPTH', '0')) + 1}, parent {lineage})"
    verb = "Continuing" if session_id else "Launching"
    print(f"{verb} {name} ...")
    session_snapshot = config.snapshot_sessions(base)
    try:
        rc = launch(workspaces, name, project_slug=projects[0][1],
                    recursive=args.recursive, session_id=session_id)
    finally:
        config.capture_session_id(base, agent_id, snapshot=session_snapshot)
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(str(int(time.time())))
        lock_fd.flush()
        lock_fd.close()
    sys.exit(rc)


def cmd_pull(args):
    base = find_repo_root()
    require_init(base)
    from .pull import pull
    pull(base, args.force)


def cmd_push(args):
    base = find_repo_root()
    require_init(base)
    from .push import push
    push(base, args.force)


def cmd_remove_agent(args):
    import shutil
    base = find_repo_root()
    require_init(base)

    # Accept "@4" or "coding-cave@4"
    spec = args.agent_id
    if "@" not in spec:
        raise SystemExit(f"Invalid format '{spec}'. Use @<n> or <project>@<n>.")
    project_part, agent_id = spec.rsplit("@", 1)
    if project_part and project_part != config.project_slug(base):
        raise SystemExit(
            f"Project mismatch: expected '{config.project_slug(base)}', got '{project_part}'."
        )
    if not config.AGENT_ID_RE.match(agent_id):
        raise SystemExit(f"Invalid agent ID in '{spec}'.")

    if config.is_agent_running(base, agent_id):
        raise SystemExit(f"Agent {agent_id} is running. Stop it first.")

    # Find remote branches belonging to this agent
    branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    agent_branches = [
        (b, parsed[0])
        for b in branches
        if (parsed := config.parse_agent_branch(b)) and parsed[1] == agent_id
    ]

    # Warn about undelivered work but proceed anyway
    for branch, target in agent_branches:
        remote_agent = f"{config.SHARED_REMOTE}/{branch}"
        target_ref = f"{config.SHARED_REMOTE}/{target}"
        try:
            ahead = git.commits_ahead(remote_agent, target_ref, cwd=base)
            if ahead > 0:
                n = "commit" if ahead == 1 else "commits"
                print(f"Warning: discarding {ahead} undelivered {n} on {branch}.")
        except Exception:
            pass

    # Delete remote branches
    for branch, _ in agent_branches:
        git.git("push", config.SHARED_REMOTE, f":refs/heads/{branch}", cwd=base)

    # Clean up lock, session, and worktree
    slug = config.project_slug(base)
    for path in [
        config.agent_lock_dir(slug) / f"{agent_id}.lock",
        config.agent_session_file(slug, agent_id),
    ]:
        if path.exists():
            path.unlink()
    ws = config.worktree_dir(slug, agent_id)
    if ws.exists():
        shutil.rmtree(ws)

    print(f"Removed {config.display_name(base, agent_id)}.")


def cmd_version(args):
    print(f"cav @VERSION@")


def cmd_status(args):
    base = find_repo_root()
    require_init(base)
    from .status import status
    status(base)


def main():
    parser = argparse.ArgumentParser(
        prog="cav",
        description="Collaborative Agent Versioning",
    )
    sub = parser.add_subparsers(dest="command")

    # cav init <path>
    p_init = sub.add_parser("init", help="Initialize Coding Cave in a project")
    p_init.add_argument("path", nargs="?", default=".", help="Path to the project directory (default: current directory)")

    # cav agent [id]  (also the default action)
    p_agent = sub.add_parser("agent", help="Launch an agent in a sandbox")
    p_agent.add_argument("-c", "--continue", action="store_true", dest="continue_agent",
                         help="Continue the most recently paused agent")
    p_agent.add_argument("-r", "--resume", dest="resume_session", metavar="UUID",
                         help="Resume a specific Claude session by UUID")
    p_agent.add_argument("-p", "--project", action="append", dest="extra_projects",
                         metavar="PATH", help="Additional project to mount (repeatable)")
    p_agent.add_argument("--recursive", action="store_true",
                         help="Allow agent to launch subcaves (exposes /dev/fuse)")

    # cav pull [--force]
    p_pull = sub.add_parser("pull", help="Fetch from cave and fast-forward merge")
    p_pull.add_argument("--force", action="store_true", help="Hard reset to remote branch")

    # cav push [--force]
    p_push = sub.add_parser("push", help="Push current branch to cave")
    p_push.add_argument("--force", action="store_true", help="Force push")

    # cav status
    sub.add_parser("status", help="Show agent branch status")

    # cav remove-agent <@n or project@n>
    p_rm = sub.add_parser("remove-agent", help="Remove a paused agent and its branches")
    p_rm.add_argument("agent_id", help="Agent display name (e.g. @4 or coding-cave@4)")

    # cav version
    sub.add_parser("version", help="Show version")

    # Default to "agent" subcommand when none given.
    # parse_known_args can't handle agent flags (-p, -c) at top level,
    # so check if the first arg looks like a subcommand.
    subcmds = {"init", "agent", "pull", "push", "status", "version", "remove-agent"}
    argv = sys.argv[1:]
    if not argv or argv[0] not in subcmds:
        argv = ["agent"] + argv
    args = parser.parse_args(argv)

    dispatch = {
        "init": cmd_init,
        "agent": cmd_agent,
        "pull": cmd_pull,
        "push": cmd_push,
        "status": cmd_status,
        "remove-agent": cmd_remove_agent,
        "version": cmd_version,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
