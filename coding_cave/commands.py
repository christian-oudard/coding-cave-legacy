"""CLI command implementations."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import _state, config, projects, sessions, git as git_mod, overlay, lifecycle, sync


def _shell_exec_cmd(preferred_shell):
    """Return a shell command string that execs the preferred shell.

    Since we can't check the cave's PATH from the host, the check
    happens at runtime inside the cave via 'command -v'.
    """
    if preferred_shell == "bash":
        return "exec /bin/bash -l"
    return (
        f'if command -v {preferred_shell} >/dev/null 2>&1; then exec {preferred_shell} -l; '
        f'else echo "{preferred_shell} not found (check cave.nix packages)" >&2; exit 1; fi'
    )


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

    preferred_shell = Path(os.environ.get("SHELL", "/bin/bash")).name

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
            lifecycle.enter_session(active_sessions[0], preferred_shell, workdir=workdir)
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
                lifecycle.enter_session(by_index[choice], preferred_shell, workdir=workdir)
            else:
                print("Invalid choice.", file=sys.stderr)
                sys.exit(1)

    # Start new shell session
    workdir = projects.cave_workdir(project)
    if args:
        # Run a specific command (cav shell -- <cmd>)
        user_cmd = shlex.join(args)
        command = [
            "--", "/bin/bash", "-c",
            f"cd {workdir} && exec {user_cmd}",
        ]
    else:
        shell_cmd = _shell_exec_cmd(preferred_shell)
        command = [
            "--", "/bin/bash", "-c",
            f"cd {workdir} && {shell_cmd}",
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


def _find_cave_root(project=None):
    """Find the root filesystem of a running cave session via /proc/<pid>/root.

    If project is given, prefers sessions for that project (auto-selects if
    exactly one). Falls back to any running session since all projects are
    mounted in every session. Returns None if no sessions are running.
    """
    all_sessions = sessions.list_sessions()
    if not all_sessions:
        return None

    if project:
        proj_sessions = [s for s in all_sessions if s.get("project") == project]
        if len(proj_sessions) == 1:
            session_info = proj_sessions[0]
        elif len(proj_sessions) > 1:
            session_info = _prompt_session(proj_sessions)
        else:
            # No sessions for this project — any session will do
            # (all projects are mounted in every session)
            session_info = all_sessions[0]
    elif len(all_sessions) == 1:
        session_info = all_sessions[0]
    else:
        session_info = _prompt_session(all_sessions)

    bwrap_pid = session_info["bwrap_pid"]
    child_pid = lifecycle.find_bwrap_child(bwrap_pid)
    if child_pid is None:
        return None
    inner = lifecycle.find_bwrap_child(child_pid)
    pid = inner if inner is not None else child_pid
    return Path(f"/proc/{pid}/root")


def _prompt_session(session_list):
    """Prompt user to select from multiple sessions."""
    print("Multiple sessions running:", file=sys.stderr)
    by_index = {}
    for s in session_list:
        started = time.strftime("%H:%M", time.localtime(s["started"]))
        idx = s.get("index", "?")
        proj = s.get("project", "?")
        by_index[str(idx)] = s
        print(f"  [{idx}] [{started}] {proj}  PID {s['bwrap_pid']}", file=sys.stderr)
    try:
        choice = input("Session: ").strip()
        return by_index[choice]
    except (KeyError, EOFError):
        print("Invalid choice.", file=sys.stderr)
        sys.exit(1)


def cmd_cp(srcs, dest, recursive=False, force=False):
    """Copy files between host and a running cave session."""

    def resolve_cave_path(spec):
        """Parse cave path spec. Returns (cave_abs_path, is_home) or (None, False).

        cave_abs_path is the absolute path inside the cave filesystem.
        is_home=True means the path can be accessed directly via cave_home()
        without a running session.
        """
        if ":" not in spec:
            return None, False
        slug, _, path = spec.partition(":")

        # Tilde expansion → /home/cave-coder/...
        if path.startswith("~/"):
            return f"{_state.JAIL_HOME}/{path[2:]}", True
        if path == "~":
            return _state.JAIL_HOME, True

        # /home/cave-coder/ already absolute → home
        if path == _state.JAIL_HOME or path.startswith(_state.JAIL_HOME + "/"):
            return path, True

        # Resolve slug for relative paths
        if not slug or slug == "cave":
            slug = None
            cwd = Path.cwd().resolve()
            for s, source in projects.load_projects().items():
                if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
                    slug = s
                    break
            if not slug:
                print("No project specified and CWD is not a registered project.", file=sys.stderr)
                sys.exit(1)

        # Absolute paths use as-is (including /projects/..., /tmp/..., etc.)
        if path.startswith("/"):
            return path, False

        # Relative path → /projects/<slug>/path
        rel = path or "."
        return f"/projects/{slug}/{rel}" if rel != "." else f"/projects/{slug}", False

    def _check_boundary(resolved, boundary, label):
        """Exit if resolved path escapes boundary directory."""
        try:
            resolved.relative_to(boundary.resolve())
        except ValueError:
            print(f"Path escapes {label}: {resolved}", file=sys.stderr)
            sys.exit(1)

    def cave_host_path(cave_path, is_home):
        """Map a cave-absolute path to a host filesystem path.

        Home paths use cave_home() directly (no session needed).
        Project paths fall back to the host source dir if no session is running.
        Other paths require a running session (accessed via /proc/<pid>/root/).

        Home and fallback paths are boundary-checked to prevent ..
        traversal escaping the intended directory.
        """
        if is_home:
            home = _state.cave_home()
            rel = cave_path[len(_state.JAIL_HOME):]
            result = (home / rel.lstrip("/")).resolve() if rel else home.resolve()
            _check_boundary(result, home, "cave home")
            return result

        # Extract project hint from path
        m = re.match(r'^/projects/([^/]+)', cave_path)
        project_hint = m.group(1) if m else None

        root = _find_cave_root(project=project_hint)
        if root is not None:
            # /proc/pid/root paths are sandboxed by the kernel namespace —
            # symlinks and .. resolve within the cave's mount namespace.
            return root / cave_path.lstrip("/")

        # No session — fall back to host source for project paths
        if project_hint:
            all_projects = projects.load_projects()
            if project_hint in all_projects:
                host_source = Path(all_projects[project_hint])
                rel = cave_path[len(f"/projects/{project_hint}"):]
                result = (host_source / rel.lstrip("/")).resolve() if rel else host_source.resolve()
                _check_boundary(result, host_source, "project dir")
                return result

        print("No running sessions.", file=sys.stderr)
        sys.exit(1)

    # Determine direction: host->cave or cave->host
    dest_cave, dest_home = resolve_cave_path(dest)
    src_cave, src_home = resolve_cave_path(srcs[0]) if len(srcs) == 1 else (None, False)

    if dest_cave:
        # host -> cave: cp <host-files>... <slug>:<cave-path>
        cave_dest = cave_host_path(dest_cave, dest_home)
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
                # Replace symlinks instead of following them
                if target.is_symlink():
                    target.unlink()
                shutil.copy2(src_path_obj, target, follow_symlinks=False)
            print(f"  {src} -> {dest}", file=sys.stderr)
    elif src_cave:
        # cave -> host: cp <slug>:<cave-path> <host-dest>
        cave_src = cave_host_path(src_cave, src_home)
        if cave_src.is_symlink():
            print(f"Refusing to copy symlink from cave: {srcs[0]}", file=sys.stderr)
            sys.exit(1)
        if not cave_src.exists():
            print(f"Source not found in cave: {srcs[0]}", file=sys.stderr)
            sys.exit(1)
        host_dest = Path(dest).resolve()
        host_dest.parent.mkdir(parents=True, exist_ok=True)
        if cave_src.is_dir():
            if not recursive:
                print(f"cp: -R not specified; omitting directory '{srcs[0]}'", file=sys.stderr)
                sys.exit(1)
            target = host_dest / cave_src.name if host_dest.is_dir() else host_dest
        else:
            target = host_dest / cave_src.name if host_dest.is_dir() else host_dest
        if not force and target.exists():
            try:
                answer = input(f"Overwrite {target}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                print("Aborted.", file=sys.stderr)
                sys.exit(1)
        if cave_src.is_dir():
            def _copy_no_symlinks(src, dst, **kwargs):
                if Path(src).is_symlink():
                    return  # skip symlinks inside cave directories
                shutil.copy2(src, dst, **kwargs)
            shutil.copytree(cave_src, target, dirs_exist_ok=True,
                            copy_function=_copy_no_symlinks)
        else:
            shutil.copy2(cave_src, target)
        print(f"  {srcs[0]} -> {dest}", file=sys.stderr)
    else:
        print("One of source or destination must use cave: or slug:path syntax.", file=sys.stderr)
        print("  coding-cave cp <file>... cave:<path>        host -> cave", file=sys.stderr)
        print("  coding-cave cp cave:<path> <dest>           cave -> host", file=sys.stderr)
        print("  coding-cave cp cave:~/path <dest>           cave home -> host", file=sys.stderr)
        sys.exit(1)


def cmd_flake_update(inputs):
    """Update flake inputs in user-env. Pass input names to update selectively."""
    user_env = config._user_env_dir()
    if not (user_env / "flake.nix").exists():
        config.build_user_env()
    if not (user_env / "flake.nix").exists():
        print("No user environment to update.", file=sys.stderr)
        sys.exit(1)
    lock_file = user_env / "flake.lock"
    old_lock = lock_file.read_bytes() if lock_file.exists() else b""
    cmd = ["nix", "flake", "update"] + (inputs or [])
    r = subprocess.run(cmd, cwd=str(user_env))
    if r.returncode != 0:
        sys.exit(r.returncode)
    new_lock = lock_file.read_bytes() if lock_file.exists() else b""
    if new_lock != old_lock:
        print("Updated. Changes take effect on next cave session.", file=sys.stderr)
    else:
        print("Already up to date.", file=sys.stderr)


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
