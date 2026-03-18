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


def _relative_age(started):
    """Return a human-readable relative age string like '5m ago' or '2d3h ago'."""
    age = int(time.time() - started)
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    if age < 86400:
        h, m = divmod(age, 3600)
        return f"{h}h{m // 60:02d}m ago"
    d, rem = divmod(age, 86400)
    return f"{d}d{rem // 3600}h ago"


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


def cmd_agent(args, gpu=False, continue_session=False, resume=None, claude_args=None, projects_flag=None, port=None):
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
        f". {_state.IPC_SETUP_SH}; cd {workdir} && exec {claude_cmd}",
    ]

    mounted = projects.parse_project_filter(projects_flag, project)
    lifecycle.start_session(project, command, gpu=gpu,
                            reuse_index=continue_session or resume is not None,
                            mounted_projects=mounted, port=port)


def cmd_shell(args, gpu=False, projects_flag=None, port=None):
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
            for s in sorted(active_sessions, key=lambda s: s.get("index", 0)):
                ago = _relative_age(s["started"])
                idx = s.get("index", "?")
                by_index[str(idx)] = s
                print(f"  [{idx}] [{ago}] {s['sid']}  {s.get('project', '?')} (PID {s['bwrap_pid']})", file=sys.stderr)
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

    mounted = projects.parse_project_filter(projects_flag, project)
    lifecycle.start_session(project, command, gpu=gpu, mounted_projects=mounted, port=port)


def cmd_add(directory=None):
    if directory:
        project_dir = Path(directory).resolve()
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


def cmd_remove(slug=None):
    slug = projects.resolve_slug(slug)
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


def cmd_reset(slug=None):
    slug = projects.resolve_slug(slug)
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


def cmd_pull(slug=None, force=False):
    if slug:
        loaded_projects = projects.load_projects()
        if slug not in loaded_projects and sync.find_unsynced_project(slug):
            sync.pull_cave_project(slug)
            return
        sync.pull_project(slug, force=force)
        return

    slug = projects.resolve_slug()
    if slug:
        sync.pull_project(slug, force=force)
    else:
        print("Not in a project folder. Specify a project or cd into one.", file=sys.stderr)


def cmd_push(slug=None, force=False):
    if slug:
        sync.push_project(slug, force=force)
        return

    slug = projects.resolve_slug()
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

    If project is given, selects a session that has it mounted (checks
    mounted_projects in session metadata). Returns None if no sessions
    are running or none have the project mounted.
    """
    all_sessions = sessions.list_sessions()
    if not all_sessions:
        return None

    if project:
        # Filter to sessions that have this project mounted
        has_project = [s for s in all_sessions
                       if project in s.get("mounted_projects", [s.get("project")])]
        if not has_project:
            return None
        if len(has_project) == 1:
            session_info = has_project[0]
        else:
            # Prefer sessions where this is the primary project
            primary = [s for s in has_project if s.get("project") == project]
            if len(primary) == 1:
                session_info = primary[0]
            elif primary:
                session_info = _prompt_session(primary)
            else:
                session_info = _prompt_session(has_project)
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
    """Prompt user to select from multiple sessions.

    Groups sessions by project (most recent first), with per-project numbering.
    Each line gets a sequential number for easy selection.
    """
    from collections import defaultdict

    print("Multiple sessions running:", file=sys.stderr)

    # Group by project
    by_project = defaultdict(list)
    for s in session_list:
        by_project[s.get("project", "?")].append(s)

    # Sort each project's sessions by index
    for proj in by_project:
        by_project[proj].sort(key=lambda s: s.get("index", 0))

    # Sort projects by most recent session (newest first)
    project_order = sorted(
        by_project.keys(),
        key=lambda p: max(s.get("started", 0) for s in by_project[p]),
        reverse=True,
    )

    by_key = {}
    n = 0
    for proj in project_order:
        proj_sessions = by_project[proj]
        multi = len(proj_sessions) > 1
        for s in proj_sessions:
            n += 1
            ago = _relative_age(s["started"])
            idx = s.get("index", "?")
            label = f"{proj} #{idx}" if multi else proj
            by_key[str(n)] = s
            print(f"  {n}) {label}  [{ago}]", file=sys.stderr)

    try:
        choice = input("Session: ").strip()
        return by_key[choice]
    except (KeyError, EOFError):
        print("Invalid choice.", file=sys.stderr)
        sys.exit(1)


def cmd_cp(srcs, dest, recursive=False, force=False):
    """Copy files between host and a running cave session."""

    def resolve_cave_path(spec):
        """Parse cave path spec. Returns (cave_abs_path, is_home, project) or (None, False, None).

        Two prefixes:
          cave:path  — CWD project (auto-detected)
          slug:path  — specific project by name

        Path types:
          relative  — project-relative, resolves to /projects/<slug>/path
          ~/path    — cave home, no session needed (is_home=True)
          /abs/path — absolute cave path, uses project hint for session selection

        cave_abs_path is the absolute path inside the cave filesystem.
        is_home=True means the path can be accessed directly via cave_home()
        without a running session.
        project is the slug hint for session selection.
        """
        if ":" not in spec:
            return None, False, None
        prefix, _, path = spec.partition(":")

        if prefix == "cave":
            # cave: — CWD project, resolve slug from CWD
            slug = None
            cwd = Path.cwd().resolve()
            for s, source in projects.load_projects().items():
                if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
                    slug = s
                    break
            if not slug:
                print("CWD is not inside a registered project.", file=sys.stderr)
                sys.exit(1)
            prefix = slug

        # slug: — must be a registered project
        if prefix not in projects.load_projects():
            print(f"Unknown project: {prefix}", file=sys.stderr)
            sys.exit(1)
        # ~/path — cave home, no session needed
        if path.startswith("~/") or path == "~":
            expanded = f"{_state.JAIL_HOME}/{path[2:]}" if path != "~" else _state.JAIL_HOME
            return expanded, True, prefix
        if path == _state.JAIL_HOME or path.startswith(_state.JAIL_HOME + "/"):
            return path, True, prefix
        # Absolute path — use as-is with project hint for session selection
        if path.startswith("/"):
            return path, False, prefix
        rel = path or "."
        return (f"/projects/{prefix}/{rel}" if rel != "." else f"/projects/{prefix}"), False, prefix

    def _check_boundary(resolved, boundary, label):
        """Exit if resolved path escapes boundary directory."""
        try:
            resolved.relative_to(boundary.resolve())
        except ValueError:
            print(f"Path escapes {label}: {resolved}", file=sys.stderr)
            sys.exit(1)

    def cave_host_path(cave_path, is_home, project=None, writing=False):
        """Map a cave-absolute path to a host filesystem path.

        Home paths use cave_home() directly (no session needed).
        Project paths: with a session, use /proc/pid/root. Without a session,
        writes go to saved-upper (persists into next session), reads check
        saved-upper first then fall back to host source dir.
        Non-project absolute paths require a running session.

        Home and no-session paths are boundary-checked to prevent ..
        traversal escaping the intended directory.
        """
        if is_home:
            home = _state.cave_home()
            rel = cave_path[len(_state.JAIL_HOME):]
            result = (home / rel.lstrip("/")).resolve() if rel else home.resolve()
            _check_boundary(result, home, "cave home")
            return result

        # Use explicit project hint, or extract from path
        if not project:
            m = re.match(r'^/projects/([^/]+)', cave_path)
            project = m.group(1) if m else None

        root = _find_cave_root(project=project)
        if root is not None:
            # /proc/pid/root paths are sandboxed by the kernel namespace —
            # symlinks and .. resolve within the cave's mount namespace.
            return root / cave_path.lstrip("/")

        # No session — use saved-upper for writes, saved-upper then
        # host source for reads.  Only for project-relative paths.
        project_prefix = f"/projects/{project}" if project else None
        if project_prefix and (cave_path == project_prefix or cave_path.startswith(project_prefix + "/")):
            rel = cave_path[len(project_prefix):]
            saved_upper = _state.projects_dir() / project / "saved-upper"

            if writing:
                saved_upper.mkdir(parents=True, exist_ok=True)
                result = (saved_upper / rel.lstrip("/")).resolve() if rel else saved_upper.resolve()
                _check_boundary(result, saved_upper, "saved-upper")
                return result

            # Read: check saved-upper first (overlay semantics)
            if saved_upper.is_dir():
                candidate = (saved_upper / rel.lstrip("/")).resolve() if rel else saved_upper.resolve()
                if candidate.exists():
                    _check_boundary(candidate, saved_upper, "saved-upper")
                    return candidate

            # Fall back to host source (the overlay lower layer)
            all_projects = projects.load_projects()
            if project in all_projects:
                host_source = Path(all_projects[project])
                result = (host_source / rel.lstrip("/")).resolve() if rel else host_source.resolve()
                _check_boundary(result, host_source, "project dir")
                return result

        print("No running sessions.", file=sys.stderr)
        sys.exit(1)

    # Determine direction: host->cave or cave->host
    dest_cave, dest_home, dest_proj = resolve_cave_path(dest)
    src_cave, src_home, src_proj = resolve_cave_path(srcs[0]) if len(srcs) == 1 else (None, False, None)

    if dest_cave:
        # host -> cave: cp <host-files>... <slug>:<cave-path>
        cave_dest = cave_host_path(dest_cave, dest_home, project=dest_proj, writing=True)
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
        cave_src = cave_host_path(src_cave, src_home, project=src_proj)
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
                # Remove read-only destination files before overwriting
                dst_path = Path(dst)
                if dst_path.exists() and not os.access(dst, os.W_OK):
                    dst_path.unlink()
                shutil.copy2(src, dst, **kwargs)
            shutil.copytree(cave_src, target, dirs_exist_ok=True,
                            copy_function=_copy_no_symlinks)
        else:
            shutil.copy2(cave_src, target)
        print(f"  {srcs[0]} -> {dest}", file=sys.stderr)
    else:
        print("One of source or destination must use a cave path prefix.", file=sys.stderr)
        print("  coding-cave cp <file>... <slug>:<path>        host -> project", file=sys.stderr)
        print("  coding-cave cp <file>... cave:<path>          host -> CWD project", file=sys.stderr)
        print("  coding-cave cp <slug>:<path> <dest>           project -> host", file=sys.stderr)
        print("  coding-cave cp <slug>:~/path <dest>           cave home -> host", file=sys.stderr)
        print("  coding-cave cp <file>... <slug>:/tmp/path     host -> cave filesystem", file=sys.stderr)
        sys.exit(1)


def cmd_flake_update(inputs):
    """Update flake inputs in user-env. Pass input names to update selectively."""
    # Always regenerate flake.nix to pick up code fixes and cave.nix changes
    config.build_user_env()
    user_env = config._user_env_dir()
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


def cmd_status(show_all=False):
    overlay.clean_stale_session_overlays()

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
        for s in sorted(active_sessions, key=lambda s: s.get("index", 0)):
            ago = _relative_age(s["started"])
            idx = s.get("index", "?")
            print(f"    #{idx} [{ago}] {s['sid']}  PID {s['bwrap_pid']}", file=sys.stderr)
