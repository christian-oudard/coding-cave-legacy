"""Coding Cave CLI entry point."""

import os
import re
import subprocess
import sys
from pathlib import Path

import click

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


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    pass


@cli.command()
@click.argument("path", default=".")
def init(path):
    """Initialize Coding Cave in a project."""
    from .init import init as _init
    _init(path)


@cli.command()
@click.option("-c", "--continue", "continue_agent", is_flag=True,
              help="Continue the most recently paused agent")
@click.option("-r", "--resume", "resume_session", metavar="UUID",
              help="Resume a specific Claude session by UUID")
@click.option("-p", "--project", "extra_projects", multiple=True, metavar="PATH",
              help="Additional project to mount (repeatable)")
@click.option("--recursive", is_flag=True,
              help="Allow agent to launch subcaves (exposes /dev/fuse)")
def agent(continue_agent, resume_session, extra_projects, recursive):
    """Launch an agent in a sandbox."""
    base = find_repo_root()
    require_init(base)
    _sweep_stale_agents(base)
    from .workspace import create
    from .sandbox import launch

    if resume_session:
        owner = config.find_agent_by_session(resume_session)
        if owner:
            agent_id = config.continue_agent_id(base, owner)
        else:
            # Session not in registry — adopt it with a fresh agent slot.
            agent_id = config.acquire_agent_id(base)
            config.write_session_id(agent_id, resume_session)
        session_id = resume_session
        resuming = True
    elif continue_agent:
        agent_id = config.continue_agent_id(base)
        session_id = config.read_session_id(agent_id)
        resuming = bool(session_id)
    else:
        agent_id = config.acquire_agent_id(base)
        session_id = config.read_session_id(agent_id)
        resuming = False

    projects = [(base, config.project_slug(base))]

    for path in extra_projects:
        extra = resolve_project(path)
        slug = config.project_slug(extra)
        if any(s == slug for _, s in projects):
            raise SystemExit(
                f"Project slug collision: '{slug}' already used.\n"
                f"Two projects resolve to the same slug from: {extra}"
            )
        projects.append((extra, slug))

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
                    recursive=recursive, session_id=session_id,
                    resuming=resuming)
    finally:
        # Claude uses its own UUID regardless of --session-id. Capture what it
        # actually wrote so the next cav -c resumes the right session.
        actual_sid = config.most_recent_session(projects[0][1])
        if actual_sid:
            config.write_session_id(agent_id, actual_sid)
        config.mark_agent_exited(agent_id)
        _auto_cleanup_if_clean(base, agent_id)
    sys.exit(rc)


def _sweep_stale_agents(base: Path):
    """Clean up exited agents with no pending work before launching."""
    slug = config.project_slug(base)
    for agent_id in config.all_agent_ids(project=slug):
        if not config.is_agent_running(agent_id):
            _auto_cleanup_if_clean(base, agent_id)


def _auto_cleanup_if_clean(base, agent_id):
    """Remove agent if all work is approved."""
    from .status import _remove_agent
    try:
        branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    except Exception:
        return
    agent_branches = [
        b for b in branches
        if (parsed := config.parse_agent_branch(b)) and parsed[1] == agent_id
    ]
    if not agent_branches:
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
                return
            if git.cherry_undelivered(target_ref, remote_agent, cwd=base):
                return
        except Exception:
            return
    _remove_agent(base, agent_id, agent_branches)


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def approve(yes):
    """Approve agent work into shared branch."""
    base = find_repo_root()
    require_init(base)
    from .approve import approve as _approve
    _approve(base, yes=yes)


@cli.command()
@click.argument("agent_id", required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def reject(agent_id, yes):
    """Reject tip commit of shared branch."""
    base = find_repo_root()
    require_init(base)
    from .reject import reject as _reject
    _reject(base, agent_id=agent_id, yes=yes)


@cli.command()
@click.argument("branch", required=False)
@click.option("-f", "--force", is_flag=True, help="Hard reset to remote branch")
def pull(branch, force):
    """Fetch from cave and fast-forward merge."""
    base = find_repo_root()
    require_init(base)
    from .pull import pull as _pull
    _pull(base, force=force, branch=branch)


@cli.command()
@click.option("-f", "--force", is_flag=True, help="Force push")
def push(force):
    """Push current branch to cave."""
    base = find_repo_root()
    require_init(base)
    from .push import push as _push
    _push(base, force)


@cli.command()
@click.argument("agent", required=False)
@click.option("-a", "--all", "all_projects", is_flag=True,
              help="Show status for all registered projects")
def status(agent, all_projects):
    """Show agent branch status."""
    from .status import status as _status
    if all_projects:
        import io, contextlib
        from .status import format_all
        projects = config.all_projects()
        if not projects:
            raise SystemExit("No registered projects. Run: cav init")
        entries = []
        for slug, path in projects:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                has_agents = _status(Path(path), scoped=False)
            entries.append((has_agents, slug, buf.getvalue()))
        print(format_all(entries))
        return
    base = find_repo_root()
    require_init(base)
    agent_id = None
    if agent:
        agent_id = agent.lstrip("@").split("@")[-1]
        if not config.AGENT_ID_RE.match(agent_id):
            raise SystemExit(f"Invalid agent ID '{agent}'. Use a number (e.g. 3).")
    _status(base, agent_filter=agent_id)


@cli.command("remove-agent")
@click.argument("agent_id", required=False)
@click.option("-a", "--all", "all_agents", is_flag=True,
              help="Remove all agents and reset the database")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def remove_agent(agent_id, all_agents, yes):
    """Remove a paused agent and its branches."""
    from .status import _remove_agent
    base = find_repo_root()
    require_init(base)

    if all_agents:
        _cmd_remove_all_agents(base, yes=yes)
        return

    if not agent_id:
        raise SystemExit("Specify an agent ID (e.g. 3) or use --all.")

    aid = agent_id.lstrip("@").split("@")[-1]
    if not config.AGENT_ID_RE.match(aid):
        raise SystemExit(f"Invalid agent ID '{agent_id}'. Use a number (e.g. 3).")

    name = config.display_name(base, aid)
    if aid not in config.all_agent_ids():
        raise SystemExit(f"{name} not found.")

    if config.is_agent_running(aid):
        raise SystemExit(f"{name} is running. Stop it first.")

    branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    agent_branches = [
        b for b in branches
        if (parsed := config.parse_agent_branch(b)) and parsed[1] == aid
    ]

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

    _remove_agent(base, aid, agent_branches)
    print(f"Removed {config.display_name(base, aid)}.")


def _cmd_remove_all_agents(base, yes=False):
    from .status import _remove_agent

    if config.any_agent_running():
        raise SystemExit("Cannot remove all: some agents are still running.")

    all_ids = config.all_agent_ids()
    if not all_ids:
        print("No agents in registry.")
        return

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

    for aid in all_ids:
        ab = [b for b in agent_branches
              if (p := config.parse_agent_branch(b)) and p[1] == aid]
        _remove_agent(base, aid, ab)

    config.clear_registry()
    print(f"Removed {n} agent {s}.")


@cli.command("prune-branches")
def prune_branches():
    """Delete orphaned local agent branches."""
    base = find_repo_root()
    require_init(base)
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base, **config.v())

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
            continue
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


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("-R", "-r", "--recursive", is_flag=True,
              help="Copy directories recursively")
@click.option("-f", "--force", is_flag=True,
              help="Overwrite existing files without prompting")
def cp(paths, recursive, force):
    """Copy files to/from a cave agent worktree."""
    from .cp import cp as _cp
    if len(paths) < 2:
        raise SystemExit("Usage: cav cp <src>... <dest>")
    _cp(list(paths[:-1]), paths[-1], recursive=recursive, force=force)


@cli.command()
def version():
    """Show version."""
    ts = "@VERSION_DATE@"
    if ts.isdigit():
        from datetime import datetime
        dt = datetime.fromtimestamp(int(ts))
        print(f"cav @VERSION@ ({dt:%Y-%m-%d %H:%M})")
    else:
        print(f"cav @VERSION@ ({ts})")


def main():
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

    subcmds = {
        "init", "agent", "approve", "reject", "pull", "push",
        "status", "version", "remove-agent", "prune-branches", "cp",
    }
    if not argv:
        argv = ["agent"]
    elif argv[0] == "help":
        argv = argv[1:] + ["--help"] if len(argv) > 1 else ["--help"]
    elif argv[0] not in subcmds | {"-h", "--help"}:
        agent_flags = {"-c", "--continue", "-r", "--resume", "-p", "--project", "--recursive"}
        if argv[0] in agent_flags:
            argv = ["agent"] + argv
        else:
            raise SystemExit(f"Unknown command '{argv[0]}'.")

    cli(args=argv)


if __name__ == "__main__":
    main()
