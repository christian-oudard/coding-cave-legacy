"""Coding Cave CLI entry point."""

import os
import re
import subprocess
import sys
from pathlib import Path

import click

from . import config, git, registry


def find_repo_root() -> Path:
    try:
        return Path(git.repo_root())
    except subprocess.CalledProcessError:
        raise SystemExit("Not a git repository. Run: cav init")


def require_init(base: Path):
    if config.shared_repo_path(base).exists():
        if not registry.project_path(config.project_slug(base)):
            raise SystemExit("Project not registered. Run: cav init")
        return
    # Inside a cave the bare repo is on the host; check for the cave remote instead.
    if int(os.environ.get("CODING_CAVE_DEPTH", "0")) > 0:
        try:
            git.git_output("remote", "get-url", config.CAVE_REMOTE, cwd=base)
            return
        except Exception:
            pass
    raise SystemExit("Coding Cave not initialized. Run: cav init")


def resolve_project(path: str) -> Path:
    """Resolve a project slug or path to its git root and validate init."""
    registered = registry.project_path(path)
    if registered:
        return Path(registered)
    p = Path(path).resolve()
    if not p.is_dir():
        raise SystemExit(f"No project named '{path}'")
    if not (p / ".git").exists():
        raise SystemExit(f"Not a git repository: {p}")
    if not config.shared_repo_path(p).exists():
        raise SystemExit(
            f"Coding Cave not initialized in {p}\n"
            f"Run: cav init {p}"
        )
    if not registry.project_path(config.project_slug(p)):
        raise SystemExit(f"Project not registered: {p}\nRun: cav init {p}")
    return p



@click.group(name="cav", context_settings={"help_option_names": ["-h", "--help"]})
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
@click.option("-r", "--resume", "resume_agent", metavar="AGENT",
              help="Resume a specific agent (e.g. @3, coding-cave@3)")
@click.option("-p", "--project", "extra_projects", multiple=True, metavar="PATH",
              help="Additional project to mount (repeatable)")
@click.option("--recursive", is_flag=True,
              help="Allow agent to launch subcaves (exposes /dev/fuse)")
def agent(continue_agent, resume_agent, extra_projects, recursive):
    """Launch an agent in a sandbox."""
    base = find_repo_root()
    require_init(base)
    from .workspace import create
    from .sandbox import launch

    # Resolve branch context upfront. Agent branches carry routing info:
    # agent-1/main tells us which agent to continue.
    on_agent_branch = config.parse_agent_branch(git.current_branch(cwd=base))

    if resume_agent:
        aid = resume_agent.lstrip("@").split("@")[-1]
        if not config.AGENT_ID_RE.match(aid):
            raise SystemExit(
                f"Invalid agent '{resume_agent}'. Use @<id> or <slug>@<id>.")
        agent_id, session_id, resuming = registry.resolve_continue(base, aid)
    elif continue_agent:
        hint = on_agent_branch[1] if on_agent_branch else None
        agent_id, session_id, resuming = registry.resolve_continue(base, hint)
    else:
        agent_id = registry.acquire_agent_id(base)
        session_id = registry.read_session_id(agent_id)
        resuming = False

    # Restore saved launch options when continuing
    if continue_agent or resume_agent:
        saved_slugs, saved_recursive = registry.read_agent_launch_opts(agent_id)
        if not extra_projects and saved_slugs:
            extra_projects = tuple(
                registry.project_path(s) for s in saved_slugs
                if registry.project_path(s)
            )
        if not recursive and saved_recursive:
            recursive = True

    _sweep_stale_agents(base)

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

    registry.save_agent_launch_opts(
        agent_id, [slug for _, slug in projects[1:]], recursive)

    workspaces = []
    for project_base, slug in projects:
        ws = create(project_base, agent_id)
        shared = config.shared_repo_path(project_base)
        workspaces.append((ws, shared, slug))

    name = registry.display_name(base, agent_id)
    lineage = os.environ.get("CODING_CAVE_LINEAGE", "")
    if lineage:
        name += f" (depth {int(os.environ.get('CODING_CAVE_DEPTH', '0')) + 1}, parent {lineage})"
    try:
        rc = launch(workspaces, name, project_slug=projects[0][1],
                    recursive=recursive, session_id=session_id,
                    resuming=resuming)
    finally:
        if session_id and not config.session_file_exists(projects[0][1], session_id):
            registry.clear_session_id(agent_id)
        registry.mark_agent_exited(agent_id)
        _auto_cleanup_if_clean(base, agent_id)
    sys.exit(rc)


def _sweep_stale_agents(base: Path):
    """Clean up exited agents with no pending work before launching."""
    before = {aid: registry.display_name(base, aid)
              for aid in registry.all_agent_ids()}
    for agent_id in before:
        if not registry.is_agent_running(agent_id):
            _auto_cleanup_if_clean(base, agent_id)
    removed = set(before) - set(registry.all_agent_ids())
    for aid in sorted(removed, key=int):
        print(f"Warning: cleaned up stale agent {before[aid]}.")


def _auto_cleanup_if_clean(base, agent_id):
    """Remove agent if all work is approved or irrecoverable across all projects."""
    from .status import _remove_agent

    name = registry.display_name(base, agent_id)
    all_agent_branches = []  # (base_path, branch) pairs for removal
    for slug in registry.agent_all_slugs(agent_id):
        # Check for uncommitted work in the worktree.
        ws = config.worktree_dir(slug, agent_id)
        if ws.is_dir():
            try:
                st = git.git_output("status", "--porcelain", cwd=ws)
                if st.strip():
                    print(f"Warning: keeping {name}: "
                          f"worktree {ws} has uncommitted changes.")
                    return
            except Exception as e:
                print(f"Warning: keeping {name}: "
                      f"cannot verify worktree {ws}: {e}")
                return

        path = registry.project_path(slug)
        if not path:
            continue
        proj_base = Path(path)
        if not config.shared_repo_path(proj_base).is_dir():
            continue  # shared repo gone, nothing to preserve
        try:
            git.git("fetch", config.CAVE_REMOTE, cwd=proj_base, echo=False,
                    check=False, capture=True)
            branches = git.remote_branches(config.CAVE_REMOTE, cwd=proj_base)
        except Exception as e:
            print(f"Warning: keeping {name}: "
                  f"git fetch failed for {slug}: {e}")
            return
        for b in branches:
            parsed = config.parse_agent_branch(b)
            if not parsed or parsed[1] != agent_id:
                continue
            all_agent_branches.append((proj_base, b))
            target = config.resolve_target(b)
            remote_agent = f"{config.CAVE_REMOTE}/{b}"
            target_ref = f"{config.CAVE_REMOTE}/{target}"
            try:
                if not git.has_merge_base(remote_agent, target_ref, cwd=proj_base):
                    print(f"Warning: keeping {name}: "
                          f"{b} has diverged from {target}.")
                    return
                if git.cherry_undelivered(target_ref, remote_agent, cwd=proj_base):
                    return  # has unapproved work, normal
            except Exception as e:
                print(f"Warning: keeping {name}: "
                      f"cannot check {b} against {target}: {e}")
                return

    # All branches in all projects are approved (or irrecoverable),
    # and no worktrees have uncommitted changes.
    primary_branches = [b for p, b in all_agent_branches if p == base]
    _remove_agent(base, agent_id, primary_branches)


@cli.command()
@click.argument("agent_id", required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def approve(agent_id, yes):
    """Approve agent work into shared branch."""
    base = find_repo_root()
    require_init(base)
    from .approve import approve as _approve
    _approve(base, agent_id=agent_id, yes=yes)


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
        projects = registry.all_projects()
        if not projects:
            raise SystemExit("No registered projects. Run: cav init")
        entries = []
        missing = []
        for slug, path in projects:
            if not Path(path).is_dir():
                missing.append((slug, path))
                continue
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                last_activity = _status(Path(path), scoped=False)
            entries.append((last_activity, slug, buf.getvalue()))
        print(format_all(entries))
        for slug, path in missing:
            click.echo(
                f"\"{slug}\" is registered but {path} is missing."
                f" Restore directory or remove with: cav remove-project {slug}",
                err=True)
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

    name = registry.display_name(base, aid)
    if aid not in registry.all_agent_ids():
        raise SystemExit(f"{name} not found.")

    if registry.is_agent_running(aid):
        raise SystemExit(f"{name} is running. Stop it first.")

    branches = git.remote_branches(config.CAVE_REMOTE, cwd=base)
    agent_branches = [
        b for b in branches
        if (parsed := config.parse_agent_branch(b)) and parsed[1] == aid
    ]

    for b in agent_branches:
        parsed = config.parse_agent_branch(b)
        if not parsed:
            continue
        target = parsed[0]
        remote_agent = f"{config.CAVE_REMOTE}/{b}"
        target_ref = f"{config.CAVE_REMOTE}/{target}"
        try:
            if git.has_merge_base(remote_agent, target_ref, cwd=base):
                commits = git.cherry_undelivered(target_ref, remote_agent, cwd=base)
                if commits:
                    n = "commit" if len(commits) == 1 else "commits"
                    print(f"Warning: discarding {len(commits)} unapproved {n} on {b}:")
                    for c in git.commit_oneline(f"{target_ref}..{remote_agent}", cwd=base):
                        print(f"  {c}")
        except Exception:
            pass

    _remove_agent(base, aid, agent_branches)
    print(f"Removed {registry.display_name(base, aid)}.")


def _cmd_remove_all_agents(base, yes=False):
    from .status import _remove_agent

    if registry.any_agent_running():
        raise SystemExit("Cannot remove all: some agents are still running.")

    all_ids = registry.all_agent_ids()
    if not all_ids:
        print("No agents in registry.")
        return

    branches = git.remote_branches(config.CAVE_REMOTE, cwd=base)
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

    registry.clear_registry()
    print(f"Removed {n} agent {s}.")


@cli.command("remove-project")
@click.argument("slug")
def remove_project(slug):
    """Unregister a project from the cave."""
    projects = dict(registry.all_projects())
    if slug not in projects:
        raise SystemExit(f"No project \"{slug}\" in registry.")
    registry.remove_project(slug)
    print(f"Removed project \"{slug}\".")


@cli.command()
@click.argument("branch")
@click.argument("agent_id", required=False)
def checkout(branch, agent_id):
    """Retarget an agent to deliver to a different branch."""
    base = find_repo_root()
    # Self-retarget (on an agent branch) uses host-cave, not cave,
    # so it works inside a cave without cav init.
    current = git.current_branch(cwd=base)
    if not config.parse_agent_branch(current):
        require_init(base)
    from .checkout import checkout as _checkout
    _checkout(base, branch, agent_id=agent_id)


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
        "init", "agent", "approve", "reject", "pull", "push", "checkout",
        "status", "version", "remove-agent", "remove-project", "cp",
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
