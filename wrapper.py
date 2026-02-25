#!/usr/bin/env python3
"""
coding-cave wrapper: persistent cave with multi-agent support.

Commands:
    coding-cave [--gpu]                 - enter cave, run claude
    coding-cave agent [--gpu]           - same as above
    coding-cave shell [--gpu]           - enter cave with shell
    coding-cave add [dir]               - add a project to the cave
    coding-cave status                  - show cave and sessions
    coding-cave pull [project-name]     - pull commits from overlay
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

SANDBOX_BIN = "@SANDBOX_BIN@"
SANDBOX_GPU_BIN = "@SANDBOX_GPU_BIN@"

CAVE_DATA = Path.home() / ".local" / "share" / "coding-cave"
CAVE_PID_FILE = CAVE_DATA / "cave.pid"
PROJECTS_FILE = CAVE_DATA / "projects.json"
LOADED_PROJECTS_FILE = CAVE_DATA / "loaded-projects.json"
PROJECTS_DIR = CAVE_DATA / "projects"
NIX_OVERLAY_DIR = CAVE_DATA / "nix-overlay"
SESSIONS_DIR = CAVE_DATA / "sessions"


# --- Config ---

def load_global_config():
    config_path = Path.home() / ".config" / "coding-cave" / "config.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    return {}


def load_project_config(project_dir):
    config_path = Path(project_dir) / ".claude-sandbox.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    return {}


def setup_env(global_config):
    """Set environment variables from global config."""
    token = global_config.get("credentials", {}).get("claude-oauth-token")
    if token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    sandbox_cfg = global_config.get("sandbox", {})
    home_size_gb = sandbox_cfg.get("home-size-gb")
    if home_size_gb:
        os.environ["CODING_CAVE_HOME_SIZE"] = str(int(home_size_gb * 1073741824))

    claude_md = Path.home() / ".config" / "coding-cave" / "CLAUDE.md"
    if claude_md.exists():
        os.environ["CODING_CAVE_CLAUDE_MD"] = str(claude_md)

    user_shell = Path(os.environ.get("SHELL", "/bin/bash")).name

    if user_shell == "zsh":
        zdotdir = Path.home() / ".config" / "zsh"
        if zdotdir.is_dir():
            os.environ["CODING_CAVE_ZDOTDIR"] = str(zdotdir)
    else:
        bashrc = Path.home() / ".bashrc"
        if bashrc.is_file():
            os.environ["CODING_CAVE_BASHRC"] = str(bashrc)
        bash_profile = Path.home() / ".bash_profile"
        if bash_profile.is_file():
            os.environ["CODING_CAVE_BASH_PROFILE"] = str(bash_profile)


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


# --- Session tracking ---

def register_session():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (SESSIONS_DIR / str(os.getpid())).touch()


def unregister_session():
    (SESSIONS_DIR / str(os.getpid())).unlink(missing_ok=True)


def active_session_count():
    """Count live session processes, cleaning up stale entries."""
    if not SESSIONS_DIR.exists():
        return 0
    count = 0
    for f in SESSIONS_DIR.iterdir():
        try:
            os.kill(int(f.name), 0)
            count += 1
        except (ValueError, ProcessLookupError):
            f.unlink(missing_ok=True)
    return count


# --- Cave lifecycle ---

def cave_pid():
    """Return cave init PID if running, else None."""
    if not CAVE_PID_FILE.exists():
        return None
    try:
        pid = int(CAVE_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        CAVE_PID_FILE.unlink(missing_ok=True)
        return None


def find_child_pid(parent_pid, timeout=10):
    """Find the child of the bwrap process (the init inside the namespace)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                stat = Path(entry.path, "stat").read_text()
                # Format: pid (comm) state ppid ... — comm can contain parens
                close_paren = stat.rfind(")")
                fields = stat[close_paren + 2:].split()
                ppid = int(fields[1])  # fields[0]=state, fields[1]=ppid
                if ppid == parent_pid:
                    return int(entry.name)
            except (FileNotFoundError, ValueError, PermissionError, IndexError):
                continue
        time.sleep(0.2)
    return None


def start_cave(gpu=False):
    """Start the cave. Returns the init process PID (for nsenter)."""
    CAVE_DATA.mkdir(parents=True, exist_ok=True)

    # Nix overlay dirs
    (NIX_OVERLAY_DIR / "upper").mkdir(parents=True, exist_ok=True)
    (NIX_OVERLAY_DIR / "work").mkdir(parents=True, exist_ok=True)
    os.environ["CODING_CAVE_NIX_UPPER"] = str(NIX_OVERLAY_DIR / "upper")
    os.environ["CODING_CAVE_NIX_WORK"] = str(NIX_OVERLAY_DIR / "work")

    # Project manifest for bwrap runtime
    os.environ["CODING_CAVE_PROJECTS_MANIFEST"] = str(PROJECTS_FILE)
    # Snapshot what's loaded so we know if a restart is needed later
    shutil.copy2(PROJECTS_FILE, LOADED_PROJECTS_FILE) if PROJECTS_FILE.exists() else None

    binary = SANDBOX_GPU_BIN if gpu else SANDBOX_BIN

    proc = subprocess.Popen(
        [binary],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # bwrap forks a child into the new namespaces. Find it.
    child_pid = find_child_pid(proc.pid)
    if child_pid is None:
        proc.kill()
        print("Failed to start cave: init process not found", file=sys.stderr)
        sys.exit(1)

    CAVE_PID_FILE.write_text(str(child_pid))
    (CAVE_DATA / "bwrap.pid").write_text(str(proc.pid))

    # Wait for init to write /tmp/cave-env
    time.sleep(0.5)

    print("Started coding-cave.", file=sys.stderr)
    return child_pid


def stop_cave():
    """Stop the cave."""
    pid = cave_pid()
    if pid is None:
        return

    bwrap_pid = None
    bwrap_pid_file = CAVE_DATA / "bwrap.pid"
    if bwrap_pid_file.exists():
        try:
            bwrap_pid = int(bwrap_pid_file.read_text().strip())
        except (ValueError, FileNotFoundError):
            pass

    # Kill init process (tini forwards SIGTERM to sleep, then exits)
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    if bwrap_pid:
        try:
            os.kill(bwrap_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    CAVE_PID_FILE.unlink(missing_ok=True)
    (CAVE_DATA / "bwrap.pid").unlink(missing_ok=True)


def enter_cave(pid, project=None, shell=None):
    """nsenter into the cave and run claude or a shell."""
    parts = ["source /tmp/cave-env 2>/dev/null"]

    if project:
        parts.append(f"cd /projects/{project} || exit 1")
    else:
        parts.append("cd $HOME")

    if shell:
        shell_bin = "/bin/zsh" if shell == "zsh" else "/bin/bash"
        parts.append(f"exec {shell_bin} -l")
    else:
        parts.append("exec claude --dangerously-skip-permissions")

    bash_cmd = " && ".join(parts)

    register_session()
    try:
        ret = subprocess.call([
            "nsenter",
            f"--target={pid}",
            "--user", "--mount", "--pid", "--uts", "--ipc",
            "--preserve-credentials",
            "--", "/bin/bash", "-c", bash_cmd,
        ])
    finally:
        unregister_session()
        if active_session_count() == 0:
            if os.fork() == 0:
                stop_cave()
                os._exit(0)
    sys.exit(ret)


# --- Project management ---

def detect_project_from_cwd():
    """Check CWD against registered projects, or offer to register it.
    Returns the project directory path, or None."""
    cwd = Path.cwd().resolve()

    if cwd == Path.home().resolve():
        return None

    for _slug, source in list_projects():
        source_path = Path(source).resolve()
        if cwd == source_path or cwd.is_relative_to(source_path):
            return source_path

    try:
        answer = input(f"Add {cwd}/ to the coding cave? [Y/n] ").strip().lower()
    except EOFError:
        return None
    if answer in ("", "y", "yes"):
        return cwd
    return None



def load_projects():
    """Load the project registry (slug -> source path)."""
    if PROJECTS_FILE.exists():
        with open(PROJECTS_FILE) as f:
            return json.load(f)
    return {}


def save_projects(projects):
    """Save the project registry."""
    CAVE_DATA.mkdir(parents=True, exist_ok=True)
    with open(PROJECTS_FILE, "w") as f:
        json.dump(projects, f, indent=2, sort_keys=True)


def ensure_project(project_dir):
    """Add a project to the registry and create overlay dirs. Returns slug."""
    project_dir = Path(project_dir).resolve()
    slug = project_dir.name
    proj_state = PROJECTS_DIR / slug

    (proj_state / "upper").mkdir(parents=True, exist_ok=True)
    (proj_state / "work").mkdir(parents=True, exist_ok=True)

    projects = load_projects()
    projects[slug] = str(project_dir)
    save_projects(projects)

    return slug


def list_projects():
    """Return list of (slug, source_path) for all registered projects."""
    return list(load_projects().items())


def project_loaded_in_cave(slug):
    """Check if a project was loaded when the cave started."""
    if not LOADED_PROJECTS_FILE.exists():
        return False
    with open(LOADED_PROJECTS_FILE) as f:
        return slug in json.load(f)


# --- Overlay and pull ---

def try_unmount(path):
    """Try to unmount a FUSE mount."""
    for cmd in ["fusermount3 -u", "fusermount -u", "/run/wrappers/bin/fusermount -u"]:
        subprocess.run(cmd.split() + [str(path)], capture_output=True)
        if not path.is_mount():
            return True
    return False


def mount_project_overlay(slug):
    """Mount a project's overlay using fuse-overlayfs. Returns (merged, source)."""
    projects = load_projects()
    if slug not in projects:
        return None
    proj_state = PROJECTS_DIR / slug
    project_dir = projects[slug]

    merged = proj_state / "merged"
    merged.mkdir(exist_ok=True)
    if merged.is_mount():
        try_unmount(merged)

    fuse_work = proj_state / "fuse-work"
    fuse_work.mkdir(exist_ok=True)

    subprocess.run([
        "fuse-overlayfs",
        "-o", f"lowerdir={project_dir},upperdir={proj_state / 'upper'},workdir={fuse_work}",
        str(merged),
    ], check=True)

    return merged, project_dir


def pull_project(slug):
    """Pull changes from a project overlay into the host repo."""
    projects = load_projects()
    if slug not in projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    proj_state = PROJECTS_DIR / slug
    project_dir = Path(projects[slug])

    # Mount overlay for git access
    merged = proj_state / "merged"
    if merged.exists() and merged.is_mount():
        try_unmount(merged)

    result = mount_project_overlay(slug)
    if result is None:
        print(f"Failed to mount overlay for {slug}", file=sys.stderr)
        sys.exit(1)
    merged, _ = result

    git = ["git", "-C", str(project_dir)]

    fetch = subprocess.run(git + ["fetch", str(merged), "HEAD"])
    if fetch.returncode != 0:
        print(f"Fetch failed for {slug}. Overlay mounted at: {merged}", file=sys.stderr)
        sys.exit(1)

    merge_base_result = subprocess.run(
        git + ["merge-base", "HEAD", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if merge_base_result.returncode != 0:
        try_unmount(merged)
        print(f"No common ancestor for {slug} — skipping.", file=sys.stderr)
        return
    merge_base = merge_base_result.stdout.strip()

    count = subprocess.run(
        git + ["rev-list", "--count", f"{merge_base}..FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    try_unmount(merged)

    if count == "0":
        print(f"No commits to pull from {slug}.", file=sys.stderr)
        return

    branch = subprocess.run(
        git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    subprocess.run(git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True)
    result = subprocess.run(git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--exec", "git commit --amend --reset-author --allow-empty --no-edit",
    ])
    if result.returncode != 0:
        print(f"Rebase conflicts for {slug}. Resolve, then:", file=sys.stderr)
        print(f"  git -C {project_dir} rebase --continue", file=sys.stderr)
        print(f"  git -C {project_dir} checkout {branch}", file=sys.stderr)
        print(f"  git -C {project_dir} merge --ff-only _cave_pull", file=sys.stderr)
        print(f"  git -C {project_dir} branch -d _cave_pull", file=sys.stderr)
        sys.exit(1)

    subprocess.run(git + ["checkout", branch], check=True)
    subprocess.run(git + ["merge", "--ff-only", "_cave_pull"], check=True)
    subprocess.run(git + ["branch", "-d", "_cave_pull"], check=True)

    print(f"Pulled {count} commit(s) from {slug}.", file=sys.stderr)


def cleanup_project_overlay(slug):
    """Clear overlay upper/work dirs for a project (reset overlay debt)."""
    proj_state = PROJECTS_DIR / slug
    merged = proj_state / "merged"
    if merged.exists() and merged.is_mount():
        try_unmount(merged)

    for subdir in ["upper", "work", "fuse-work"]:
        p = proj_state / subdir
        if p.exists():
            for dirpath, dirnames, _filenames in os.walk(p):
                for d in dirnames:
                    try:
                        (Path(dirpath) / d).chmod(0o700)
                    except OSError:
                        pass
            shutil.rmtree(p)
            p.mkdir()


# --- Commands ---

def cmd_agent(args, gpu=False):
    """Enter cave and run claude."""
    global_config = load_global_config()
    setup_env(global_config)

    project = None
    project_dir = detect_project_from_cwd()
    if project_dir is not None:
        project = ensure_project(project_dir)
        project_config = load_project_config(project_dir)
        for key, val in project_config.get("environment", {}).items():
            os.environ[key] = str(val)

    pid = cave_pid()

    if pid is None:
        pid = start_cave(gpu=gpu)
    elif project and not project_loaded_in_cave(project):
        sessions = active_session_count()
        if sessions > 0:
            print(f"{project} is not loaded and {sessions} session(s) are active.", file=sys.stderr)
            print("Exit all sessions first, then re-enter.", file=sys.stderr)
            sys.exit(1)
        stop_cave()
        pid = start_cave(gpu=gpu)
    else:
        print("Entered coding-cave.", file=sys.stderr)

    enter_cave(pid, project=project)


def cmd_add(args):
    """Add a project to the cave."""
    if args:
        project_dir = Path(args[0]).resolve()
    else:
        project_dir = Path.cwd().resolve()

    if not project_dir.is_dir():
        print(f"Not a directory: {project_dir}", file=sys.stderr)
        sys.exit(1)

    slug = ensure_project(project_dir)
    print(f"Added {slug} to the coding cave.", file=sys.stderr)


def cmd_pull(args):
    """Pull changes from project overlays."""
    if args:
        pull_project(args[0])
    else:
        projects = list_projects()
        if not projects:
            print("No projects in the cave.", file=sys.stderr)
            return
        for slug, _ in projects:
            try:
                pull_project(slug)
            except SystemExit:
                pass  # continue with other projects


def cmd_shell(args, gpu=False):
    """Enter the cave with the user's shell."""
    global_config = load_global_config()
    setup_env(global_config)

    shell_type = Path(os.environ.get("SHELL", "/bin/bash")).name

    project = None
    project_dir = detect_project_from_cwd()
    if project_dir is not None:
        project = ensure_project(project_dir)

    pid = cave_pid()

    if pid is None:
        pid = start_cave(gpu=gpu)
    elif project and not project_loaded_in_cave(project):
        sessions = active_session_count()
        if sessions > 0:
            print(f"{project} is not loaded and {sessions} session(s) are active.", file=sys.stderr)
            print("Exit all sessions first, then re-enter.", file=sys.stderr)
            sys.exit(1)
        stop_cave()
        pid = start_cave(gpu=gpu)
    else:
        print("Entered coding-cave.", file=sys.stderr)

    enter_cave(pid, project=project, shell=shell_type)



def cmd_status(args):
    """Show cave status and projects."""
    pid = cave_pid()
    if pid is None:
        print("Cave: stopped", file=sys.stderr)
    else:
        sessions = active_session_count()
        print(f"Cave: running (PID {pid}), {sessions} active session(s)", file=sys.stderr)

    projects = list_projects()
    if projects:
        print("Projects:", file=sys.stderr)
        for slug, source in projects:
            loaded = pid and project_loaded_in_cave(slug)
            tag = " [loaded]" if loaded else ""
            print(f"  {slug}  <- {source}{tag}", file=sys.stderr)
    else:
        print("No projects in the cave.", file=sys.stderr)



def main():
    commands = {
        "agent": cmd_agent,
        "shell": cmd_shell,
        "add": cmd_add,
        "status": cmd_status,
        "pull": cmd_pull,
    }

    args = sys.argv[1:]
    gpu = "--gpu" in args
    if gpu:
        args = [a for a in args if a != "--gpu"]

    if not args:
        cmd_agent([], gpu=gpu)
        return

    cmd = args[0]
    if cmd in commands:
        if cmd in ("agent", "shell"):
            commands[cmd](args[1:], gpu=gpu)
        else:
            commands[cmd](args[1:])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: agent, shell, add, status, pull", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
