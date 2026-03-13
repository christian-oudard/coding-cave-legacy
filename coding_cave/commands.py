"""CLI command implementations."""
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import _state, config, projects, sessions, git as git_mod, overlay, lifecycle, sync


def cmd_agent(args, gpu=False, continue_session=False, resume=None, claude_args=None):
    """Start a new agent session."""
    global_config = config.load_global_config()
    config.setup_env(global_config)

    project_dir = projects.detect_project_from_cwd()
    if project_dir is None:
        print("No project. Run 'coding-cave add' or cd into a project directory.", file=sys.stderr)
        sys.exit(1)
    project = projects.ensure_project(project_dir)

    workdir = projects.cave_workdir(project)
    claude_parts = ["claude", "--dangerously-skip-permissions"]
    if continue_session:
        claude_parts.append("--continue")
    elif resume is not None:
        claude_parts.append("--resume")
        if resume is not True:
            claude_parts.append(resume)
    if claude_args:
        claude_parts.extend(claude_args)
    claude_cmd = shlex.join(claude_parts)
    command = [
        "--", "/bin/bash", "-c",
        f"cd {workdir} && exec {claude_cmd}",
    ]

    lifecycle.start_session(project, command, gpu=gpu)


def cmd_shell(args, gpu=False):
    """Enter cave with a shell -- join existing session or start new."""
    global_config = config.load_global_config()
    config.setup_env(global_config)

    shell_type = Path(os.environ.get("SHELL", "/bin/bash")).name

    project_dir = projects.detect_project_from_cwd()
    if project_dir is None:
        print("No project. Run 'coding-cave add' or cd into a project directory.", file=sys.stderr)
        sys.exit(1)
    project = projects.ensure_project(project_dir)

    workdir = projects.cave_workdir(project)

    # Check for running sessions to join
    active_sessions = sessions.list_sessions(project=project)
    if active_sessions:
        if len(active_sessions) == 1:
            print(f"Joining session {active_sessions[0]['sid']} (PID {active_sessions[0]['bwrap_pid']}).", file=sys.stderr)
            lifecycle.enter_session(active_sessions[0], shell_type, workdir=workdir)
        else:
            print("Running sessions:", file=sys.stderr)
            by_index = {}
            for s in active_sessions:
                started = time.strftime("%H:%M", time.localtime(s["started"]))
                idx = s.get("index", "?")
                by_index[str(idx)] = s
                print(f"  [{idx}] [{started}] {s['sid']}  {s.get('project', '?')} (PID {s['bwrap_pid']})", file=sys.stderr)
            print(f"  [n] Start new session", file=sys.stderr)
            try:
                choice = input("Join session: ").strip()
            except EOFError:
                return
            if choice == "n":
                pass  # fall through to start new
            elif choice in by_index:
                lifecycle.enter_session(by_index[choice], shell_type, workdir=workdir)
            else:
                print("Invalid choice.", file=sys.stderr)
                sys.exit(1)

    # Start new shell session
    workdir = projects.cave_workdir(project)
    shell_bin = "/bin/zsh" if shell_type == "zsh" else "/bin/bash"
    if args:
        # Run a specific command (cav shell -- <cmd>)
        user_cmd = shlex.join(args)
        command = [
            "--", "/bin/bash", "-c",
            f"cd {workdir} && exec {user_cmd}",
        ]
    else:
        command = [
            "--", "/bin/bash", "-c",
            f"cd {workdir} && exec {shell_bin} -l",
        ]

    lifecycle.start_session(project, command, gpu=gpu)


def cmd_add(args):
    if args:
        project_dir = Path(args[0]).resolve()
    else:
        project_dir = Path.cwd().resolve()

    if not project_dir.is_dir():
        print(f"Not a directory: {project_dir}", file=sys.stderr)
        sys.exit(1)

    slug = project_dir.name
    loaded_projects = projects.load_projects()
    if slug in loaded_projects and Path(loaded_projects[slug]).resolve() == project_dir:
        gitdir_path = _state.projects_dir() / slug / "gitdir"
        if gitdir_path.exists():
            print(f"{slug} is already in the coding cave.", file=sys.stderr)
            return
        # Re-initialize missing gitdir
        git_mod.initialize_gitdir(slug, project_dir)

    slug = projects.ensure_project(project_dir)
    print(f"Added {slug} to the coding cave.", file=sys.stderr)


def cmd_remove(args):
    slug = projects.resolve_slug(args)
    if slug is None:
        print("No project specified and CWD is not a registered project.", file=sys.stderr)
        sys.exit(1)

    loaded_projects = projects.load_projects()
    if slug not in loaded_projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    # Check for running sessions
    active_sessions = sessions.list_sessions(project=slug)
    if active_sessions:
        print(f"Project {slug} has {len(active_sessions)} running session(s).", file=sys.stderr)
        print("Exit all sessions first.", file=sys.stderr)
        sys.exit(1)

    overlay.reset_project_overlay(slug)
    overlay.force_rmtree(_state.projects_dir() / slug)

    del loaded_projects[slug]
    projects.save_projects(loaded_projects)

    print(f"Removed {slug} from the coding cave.", file=sys.stderr)


def cmd_move(old_slug, new_path):
    loaded_projects = projects.load_projects()
    if old_slug not in loaded_projects:
        print(f"Project not found: {old_slug}", file=sys.stderr)
        sys.exit(1)

    new_path = Path(new_path).resolve()
    if not new_path.is_dir():
        print(f"Not a directory: {new_path}", file=sys.stderr)
        sys.exit(1)

    active_sessions = sessions.list_sessions(project=old_slug)
    if active_sessions:
        print(f"Project {old_slug} has {len(active_sessions)} running session(s).", file=sys.stderr)
        print("Exit all sessions first.", file=sys.stderr)
        sys.exit(1)

    new_slug = new_path.name
    if new_slug in loaded_projects and new_slug != old_slug:
        print(f"A project named {new_slug} already exists.", file=sys.stderr)
        sys.exit(1)

    # Update registry
    del loaded_projects[old_slug]
    loaded_projects[new_slug] = str(new_path)
    projects.save_projects(loaded_projects)

    # Rename state dirs if slug changed
    if new_slug != old_slug:
        old_state = _state.projects_dir() / old_slug
        new_state = _state.projects_dir() / new_slug
        if old_state.exists():
            old_state.rename(new_state)

    print(f"Moved {old_slug} -> {new_slug} ({new_path})", file=sys.stderr)


def cmd_reset(args):
    slug = projects.resolve_slug(args)
    if slug is None:
        print("No project specified and CWD is not a registered project.", file=sys.stderr)
        sys.exit(1)

    loaded_projects = projects.load_projects()
    if slug not in loaded_projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    # Check for running sessions
    active_sessions = sessions.list_sessions(project=slug)
    if active_sessions:
        print(f"Project {slug} has {len(active_sessions)} running session(s).", file=sys.stderr)
        print("Exit all sessions first.", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(loaded_projects[slug])
    proj_state = _state.projects_dir() / slug
    gitdir_path = proj_state / "gitdir"
    changes = []

    # Check for unpulled commits
    if gitdir_path.exists():
        cave_head = git_mod.git(gitdir_path, "rev-parse", "HEAD",
            capture_output=True, text=True)
        if cave_head.returncode == 0:
            merge_base_result = subprocess.run(
                ["git", "-C", str(project_dir), "merge-base", "HEAD",
                 cave_head.stdout.strip()],
                capture_output=True, text=True,
            )
            if merge_base_result.returncode == 0:
                unpulled = git_mod.git(gitdir_path, "rev-list", "--count",
                     f"{merge_base_result.stdout.strip()}..HEAD",
                    capture_output=True, text=True)
                if (unpulled.returncode == 0 and unpulled.stdout.strip() != "0"):
                    log = git_mod.git(gitdir_path, "log", "--oneline",
                         f"{merge_base_result.stdout.strip()}..HEAD",
                        capture_output=True, text=True)
                    if log.returncode == 0 and log.stdout.strip():
                        changes.append("Unpulled commits:")
                        changes.append(log.stdout.rstrip())

    if changes:
        print(f"Project {slug} has overlay changes that will be lost:\n", file=sys.stderr)
        for line in changes:
            print(f"  {line}", file=sys.stderr)
        print(file=sys.stderr)
        try:
            answer = input("Overwrite these changes? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            return

    overlay.reset_project_overlay(slug)
    overlay.clean_stale_session_overlays()
    print(f"Reset overlay for {slug}. Next session will see current host state.", file=sys.stderr)


def cmd_pull(args):
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if args:
        slug = args[0]
        loaded_projects = projects.load_projects()
        if slug not in loaded_projects and sync.find_unsynced_project(slug):
            sync.pull_cave_project(slug)
            return
        sync.pull_project(slug, force=force)
        return

    slug = projects.resolve_slug([])
    if slug:
        sync.pull_project(slug, force=force)
    else:
        print("Not in a project folder. Specify a project or cd into one.", file=sys.stderr)


def cmd_push(args):
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if args:
        sync.push_project(args[0], force=force)
        return

    slug = projects.resolve_slug([])
    if slug:
        sync.push_project(slug, force=force)
        return

    all_projects = projects.list_projects()
    if not all_projects:
        print("No projects in the cave.", file=sys.stderr)
        return
    for slug, _ in all_projects:
        try:
            sync.push_project(slug, force=force)
        except SystemExit:
            pass


def cmd_cp(srcs, dest, recursive=False):
    """Copy files between host and a running cave session's overlay."""
    session_overlays = _state.cave_data() / "session-overlays"

    def resolve_cave_path(spec):
        """Parse 'slug:path', ':path', or 'cave:path'. Returns (slug, path) or (None, None)."""
        if ":" not in spec:
            return None, None
        slug, _, path = spec.partition(":")
        if not slug or slug == "cave":
            # Auto-detect from CWD
            slug = None
            cwd = Path.cwd().resolve()
            for s, source in projects.load_projects().items():
                if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
                    slug = s
                    break
            if not slug:
                print("No project specified and CWD is not a registered project.", file=sys.stderr)
                sys.exit(1)
        # Strip /projects/<slug>/ prefix so absolute cave paths work.
        prefix = f"/projects/{slug}/"
        if path.startswith(prefix):
            path = path[len(prefix):]
        return slug, path or "."

    def find_session_merged(slug):
        """Find the overlay merged dir for a running session of this project."""
        active_sessions = sessions.list_sessions(project=slug)
        if not active_sessions:
            print(f"No running sessions for {slug}.", file=sys.stderr)
            sys.exit(1)
        if len(active_sessions) > 1:
            print("Multiple sessions running:", file=sys.stderr)
            by_index = {}
            for s in active_sessions:
                started = time.strftime("%H:%M", time.localtime(s["started"]))
                idx = s.get("index", "?")
                by_index[str(idx)] = s
                print(f"  [{idx}] [{started}] {s['sid']}  PID {s['bwrap_pid']}", file=sys.stderr)
            try:
                choice = input("Session: ").strip()
                sid = by_index[choice]["sid"]
            except (KeyError, EOFError):
                print("Invalid choice.", file=sys.stderr)
                sys.exit(1)
        else:
            sid = active_sessions[0]["sid"]
        merged = session_overlays / sid / f"{slug}-merged"
        if not merged.is_dir():
            print(f"Session overlay not found: {merged}", file=sys.stderr)
            sys.exit(1)
        return merged

    # Determine direction: host->cave or cave->host
    dest_slug, dest_path = resolve_cave_path(dest)
    src_slug, src_path = resolve_cave_path(srcs[0]) if len(srcs) == 1 else (None, None)

    if dest_slug:
        # host -> cave: cp <host-files>... <slug>:<cave-path>
        merged = find_session_merged(dest_slug)
        cave_dest = merged / dest_path.lstrip("/")
        cave_dest.parent.mkdir(parents=True, exist_ok=True)
        for src in srcs:
            src_path_obj = Path(src).resolve()
            if not src_path_obj.exists():
                print(f"Source not found: {src}", file=sys.stderr)
                sys.exit(1)
            if src_path_obj.is_dir():
                if not recursive:
                    print(f"cp: -R not specified; omitting directory '{src}'", file=sys.stderr)
                    sys.exit(1)
                target = cave_dest / src_path_obj.name if cave_dest.is_dir() else cave_dest
                shutil.copytree(src_path_obj, target, dirs_exist_ok=True)
            else:
                target = cave_dest / src_path_obj.name if cave_dest.is_dir() else cave_dest
                shutil.copy2(src_path_obj, target)
            print(f"  {src} -> {dest}", file=sys.stderr)
    elif src_slug:
        # cave -> host: cp <slug>:<cave-path> <host-dest>
        merged = find_session_merged(src_slug)
        cave_src = merged / src_path.lstrip("/")
        if not cave_src.exists():
            print(f"Source not found in cave: {src_path}", file=sys.stderr)
            sys.exit(1)
        host_dest = Path(dest).resolve()
        host_dest.parent.mkdir(parents=True, exist_ok=True)
        if cave_src.is_dir():
            if not recursive:
                print(f"cp: -R not specified; omitting directory '{srcs[0]}'", file=sys.stderr)
                sys.exit(1)
            target = host_dest / cave_src.name if host_dest.is_dir() else host_dest
            shutil.copytree(cave_src, target, dirs_exist_ok=True)
        else:
            target = host_dest / cave_src.name if host_dest.is_dir() else host_dest
            shutil.copy2(cave_src, target)
        print(f"  {srcs[0]} -> {dest}", file=sys.stderr)
    else:
        print("One of source or destination must use cave: or slug:path syntax.", file=sys.stderr)
        print("  coding-cave cp <file>... cave:              host -> cave (project root)", file=sys.stderr)
        print("  coding-cave cp <file>... cave:<path>        host -> cave (subpath)", file=sys.stderr)
        print("  coding-cave cp cave:<path> <dest>           cave -> host", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    show_all = "--all" in args or "-a" in args

    print(f"coding-cave {_state.VERSION}", file=sys.stderr)

    all_projects = projects.list_projects()
    if not all_projects:
        print("No projects in the cave.", file=sys.stderr)
        return

    # Scope to current project unless --all
    scoped = None
    if not show_all:
        cwd = Path.cwd().resolve()
        for slug, source in all_projects:
            source_path = Path(source).resolve()
            if cwd == source_path or cwd.is_relative_to(source_path):
                scoped = [(slug, source)]
                break
    display_projects = scoped or all_projects

    if not scoped:
        print("Projects:", file=sys.stderr)
    for slug, source in display_projects:
        status = sync.project_sync_status(slug, source)
        tag = f" [{status}]" if status else ""
        print(f"  {slug}  <- {source}{tag}", file=sys.stderr)

        active_sessions = sessions.list_sessions(project=slug)
        for s in active_sessions:
            started = time.strftime("%H:%M", time.localtime(s["started"]))
            idx = s.get("index", "?")
            print(f"    #{idx} [{started}] {s['sid']}  PID {s['bwrap_pid']}", file=sys.stderr)
