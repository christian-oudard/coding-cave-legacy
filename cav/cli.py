"""Coding Cave CLI entry point."""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from . import config, git


def find_repo_root() -> Path:
    try:
        return Path(git.repo_root())
    except subprocess.CalledProcessError:
        raise SystemExit("Not in a git repository.")


def require_init(base: Path):
    if config.shared_repo_path(base).exists():
        if not config.project_path(config.project_slug(base)):
            raise SystemExit("Project not registered. Run: cav init")
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
    """Resolve a project slug or path to its git root and validate init."""
    registered = config.project_path(path)
    if registered:
        return Path(registered)
    p = Path(path).resolve()
    if not (p / ".git").exists():
        raise SystemExit(f"Not a git repository: {p}")
    if not config.shared_repo_path(p).exists():
        raise SystemExit(
            f"Coding Cave not initialized in {p}\n"
            f"Run: cav init {p}"
        )
    if not config.project_path(config.project_slug(p)):
        raise SystemExit(f"Project not registered: {p}\nRun: cav init {p}")
    return p


def cmd_agent(args):
    base = find_repo_root()
    require_init(base)
    from .workspace import create
    from .sandbox import launch

    if args.resume_session:
        owner = config.find_agent_by_session(args.resume_session)
        if not owner:
            raise SystemExit(
                f"No agent owns session {args.resume_session}."
            )
        agent_id = config.continue_agent_id(base, owner)
        session_id = args.resume_session
        resuming = True
    elif args.continue_agent:
        agent_id = config.continue_agent_id(base)
        session_id = config.read_session_id(agent_id)
        resuming = bool(session_id)
    else:
        agent_id = config.acquire_agent_id(base)
        session_id = config.read_session_id(agent_id)
        resuming = False

    # Primary project (cwd)
    projects = [(base, config.project_slug(base))]

    # Additional projects via -p
    for path in (args.extra_projects or []):
        extra = resolve_project(path)
        slug = config.project_slug(extra)
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
    verb = "Continuing" if resuming else "Launching"
    print(f"{verb} {name} ...")
    try:
        rc = launch(workspaces, name, project_slug=projects[0][1],
                    recursive=args.recursive, session_id=session_id,
                    resuming=resuming)
    finally:
        config.mark_agent_exited(agent_id)
        _auto_cleanup_if_clean(base, agent_id)
    sys.exit(rc)


def _auto_cleanup_if_clean(base, agent_id):
    """Remove agent if all work is approved. Keep if unapproved work remains."""
    from .status import _remove_agent
    try:
        branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    except Exception:
        return  # can't check, keep the agent
    agent_branches = [
        b for b in branches
        if (parsed := config.parse_agent_branch(b)) and parsed[1] == agent_id
    ]
    if not agent_branches:
        # No branches at all, clean up
        _remove_agent(base, agent_id, [])
        return
    for b in agent_branches:
        parsed = config.parse_agent_branch(b)
        if not parsed:
            continue
        target = config.resolve_target(b)
        remote_agent = f"{config.SHARED_REMOTE}/{b}"
        target_ref = f"{config.SHARED_REMOTE}/{target}"
        try:
            if not git.has_merge_base(remote_agent, target_ref, cwd=base):
                return  # can't determine, keep
            if git.cherry_undelivered(target_ref, remote_agent, cwd=base):
                return  # has unapproved work, keep
        except Exception:
            return  # can't check, keep
    # All branches fully approved
    _remove_agent(base, agent_id, agent_branches)


def cmd_approve(args):
    base = find_repo_root()
    require_init(base)
    from .approve import approve
    approve(base, yes=args.yes)


def cmd_reject(args):
    base = find_repo_root()
    require_init(base)
    from .reject import reject
    reject(base, agent_id=args.agent_id, yes=args.yes)


def cmd_pull(args):
    base = find_repo_root()
    require_init(base)
    from .pull import pull
    pull(base, force=args.force, branch=args.branch)


def cmd_push(args):
    base = find_repo_root()
    require_init(base)
    from .push import push
    push(base, args.force)


def cmd_prune_branches(args):
    base = find_repo_root()
    require_init(base)
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base, **config.v())

    # Find local agent branches that tracked cave/* but whose upstream is gone.
    # After fetch --prune, git marks these with [gone] in %(upstream:track).
    local_out = git.git_output(
        "for-each-ref",
        "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
        "refs/heads/", cwd=base)
    if not local_out:
        print("No orphaned agent branches.")
        return

    cave_prefix = f"{config.SHARED_REMOTE}/"
    orphans = []
    for line in local_out.splitlines():
        parts = line.split("\t")
        branch = parts[0]
        upstream = parts[1] if len(parts) > 1 else ""
        track = parts[2] if len(parts) > 2 else ""
        if not config.parse_agent_branch(branch):
            continue
        if upstream and not upstream.startswith(cave_prefix):
            raise SystemExit(
                f"Branch '{branch}' tracks '{upstream}', not cave. "
                "Refusing to prune. Remove it manually if intended."
            )
        if upstream and "[gone]" not in track:
            continue  # upstream still live, skip
        orphans.append(branch)

    if not orphans:
        print("No orphaned agent branches.")
        return

    current = git.current_branch(cwd=base)
    if current in orphans:
        raise SystemExit(
            f"Currently on orphaned branch '{current}'. Switch to main first."
        )

    print("Orphaned local agent branches:")
    for b in orphans:
        print(f"  {b}")
    try:
        answer = input(f"\nDelete {len(orphans)} branches? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nAborted.")
    if answer.strip().lower() in ("n", "no"):
        raise SystemExit("Aborted.")

    for b in orphans:
        git.git("branch", "-D", b, cwd=base, **config.v())


def cmd_remove_agent(args):
    from .status import _remove_agent
    base = find_repo_root()
    require_init(base)

    if args.all:
        _cmd_remove_all_agents(base, yes=args.yes)
        return

    if not args.agent_id:
        raise SystemExit("Specify an agent ID (e.g. 3) or use --all.")

    agent_id = args.agent_id.lstrip("@").split("@")[-1]  # accept "3", "@3", "slug@3"
    if not config.AGENT_ID_RE.match(agent_id):
        raise SystemExit(f"Invalid agent ID '{args.agent_id}'. Use a number (e.g. 3).")

    name = config.display_name(base, agent_id)
    if agent_id not in config.all_agent_ids():
        raise SystemExit(f"{name} not found.")

    if config.is_agent_running(agent_id):
        raise SystemExit(f"{name} is running. Stop it first.")

    # Find remote branches belonging to this agent
    branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    agent_branches = [
        b for b in branches
        if (parsed := config.parse_agent_branch(b)) and parsed[1] == agent_id
    ]

    # Warn about unapproved work (using cherry for accuracy)
    for b in agent_branches:
        parsed = config.parse_agent_branch(b)
        if not parsed:
            continue
        target = parsed[0]
        remote_agent = f"{config.SHARED_REMOTE}/{b}"
        target_ref = f"{config.SHARED_REMOTE}/{target}"
        try:
            if git.has_merge_base(remote_agent, target_ref, cwd=base):
                lines = git.cherry_undelivered(target_ref, remote_agent, cwd=base)
                if lines:
                    n = "commit" if len(lines) == 1 else "commits"
                    print(f"Warning: discarding {len(lines)} unapproved {n} on {b}.")
        except Exception:
            pass

    _remove_agent(base, agent_id, agent_branches)
    print(f"Removed {config.display_name(base, agent_id)}.")


def _cmd_remove_all_agents(base, yes=False):
    from .status import _remove_agent

    if config.any_agent_running():
        raise SystemExit("Cannot remove all: some agents are still running.")

    all_ids = config.all_agent_ids()
    if not all_ids:
        print("No agents in registry.")
        return

    # Find all agent branches in this project
    branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    agent_branches = [b for b in branches if config.parse_agent_branch(b)]

    n = len(all_ids)
    s = "session" if n == 1 else "sessions"
    print(f"This will remove {n} agent {s} and {len(agent_branches)} agent branches.")

    if not yes:
        try:
            answer = input("Continue? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nAborted.")
        if answer.strip().lower() not in ("y", "yes"):
            raise SystemExit("Aborted.")

    # Remove branches and workspaces per agent
    for agent_id in all_ids:
        ab = [b for b in agent_branches
              if (p := config.parse_agent_branch(b)) and p[1] == agent_id]
        _remove_agent(base, agent_id, ab)

    # Clear registry completely
    config.clear_registry()
    print(f"Removed {n} agent {s}.")


def cmd_cp(args):
    from .cp import cp
    if len(args.paths) < 2:
        raise SystemExit("Usage: cav cp <src>... <dest>")
    cp(args.paths[:-1], args.paths[-1], recursive=args.recursive, force=args.force)


def cmd_version(args):
    ts = "@VERSION_DATE@"
    if ts.isdigit():
        from datetime import datetime
        dt = datetime.fromtimestamp(int(ts))
        print(f"cav @VERSION@ ({dt:%Y-%m-%d %H:%M})")
    else:
        print(f"cav @VERSION@ ({ts})")


def cmd_status(args):
    from .status import status
    if args.all:
        projects = config.all_projects()
        if not projects:
            raise SystemExit("No registered projects. Run: cav init")
        for i, (slug, path) in enumerate(projects):
            if i > 0:
                print()
            print(f"=== {slug} ===")
            status(Path(path))
        return
    base = find_repo_root()
    require_init(base)
    agent_id = None
    if args.agent:
        agent_id = args.agent.lstrip("@").split("@")[-1]  # accept "3", "@3", "slug@3"
        if not config.AGENT_ID_RE.match(agent_id):
            raise SystemExit(f"Invalid agent ID '{args.agent}'. Use a number (e.g. 3).")
    status(base, agent_filter=agent_id)


def main():
    parser = argparse.ArgumentParser(
        prog="cav",
        description="Collaborative Agent Versioning",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
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

    # cav approve [--yes]
    p_approve = sub.add_parser("approve", help="Approve agent work into shared branch")
    p_approve.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    # cav reject [agent-id] [--yes]
    p_reject = sub.add_parser("reject", help="Reject tip commit of shared branch")
    p_reject.add_argument("agent_id", nargs="?", help="Agent ID to assign rework")
    p_reject.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    # cav pull [branch]
    p_pull = sub.add_parser("pull", help="Fetch from cave and fast-forward merge")
    p_pull.add_argument("branch", nargs="?", default=None, help="Remote branch to pull (default: current branch)")
    p_pull.add_argument("-f", "--force", action="store_true", help="Hard reset to remote branch")

    # cav push [--force]
    p_push = sub.add_parser("push", help="Push current branch to cave")
    p_push.add_argument("-f", "--force", action="store_true", help="Force push")

    # cav status [n]
    p_status = sub.add_parser("status", help="Show agent branch status")
    p_status.add_argument("agent", nargs="?", help="Agent ID to filter (e.g. 1)")
    p_status.add_argument("-a", "--all", action="store_true", help="Show status for all registered projects")

    # cav remove-agent <n> | --all
    p_rm = sub.add_parser("remove-agent", help="Remove a paused agent and its branches")
    p_rm.add_argument("agent_id", nargs="?", help="Agent ID (e.g. 3)")
    p_rm.add_argument("-a", "--all", action="store_true", help="Remove all agents and reset the database")
    p_rm.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # cav prune-branches
    sub.add_parser("prune-branches", help="Delete orphaned local agent branches")

    # cav cp
    p_cp = sub.add_parser("cp", help="Copy files to/from a cave agent worktree")
    p_cp.add_argument("-R", "-r", "--recursive", action="store_true",
                      help="Copy directories recursively")
    p_cp.add_argument("-f", "--force", action="store_true",
                      help="Overwrite existing files without prompting")
    p_cp.add_argument("paths", nargs="+",
                      help="Source(s) and destination. "
                           "Cave paths: slug:path, slug@N:path, cave:path, slug:~/path")

    # cav version
    sub.add_parser("version", help="Show version")

    # Default to "agent" subcommand when none given.
    # parse_known_args can't handle agent flags (-p, -c) at top level,
    # so check if the first arg looks like a subcommand.
    subcmds = {"init", "agent", "approve", "reject", "pull", "push", "status", "version", "remove-agent", "prune-branches", "cp"}
    argv = sys.argv[1:]

    # Strip -v/--verbose anywhere in argv (including combined like -va).
    clean = []
    for a in argv:
        if a == "--verbose":
            config.verbose = True
        elif re.match(r'^-[a-zA-Z]+$', a) and 'v' in a:
            config.verbose = True
            rest = a.replace('v', '', 1)
            if rest != '-':
                clean.append(rest)
        else:
            clean.append(a)
    argv = clean

    if not argv:
        argv = ["agent"]
    elif argv[0] == "help":
        argv = argv[1:] + ["--help"] if len(argv) > 1 else ["--help"]
    elif argv[0] not in subcmds | {"-h", "--help"}:
        # Only default to "agent" for known agent flags.
        agent_flags = {"-c", "--continue", "-r", "--resume", "-p", "--project", "--recursive"}
        if argv[0] in agent_flags:
            argv = ["agent"] + argv
        else:
            parser.error(f"Unknown command '{argv[0]}'.")
    args = parser.parse_args(argv)

    dispatch = {
        "init": cmd_init,
        "agent": cmd_agent,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "pull": cmd_pull,
        "push": cmd_push,
        "status": cmd_status,
        "remove-agent": cmd_remove_agent,
        "prune-branches": cmd_prune_branches,
        "cp": cmd_cp,
        "version": cmd_version,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
