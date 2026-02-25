#!/usr/bin/env python3
"""
coding-cave wrapper: persistent cave with multi-agent support.

Commands:
    coding-cave [--gpu] [project-dir] [task...]  - enter cave, run claude
    coding-cave run [--gpu] <project-dir> [task...] - load project, enter cave
    coding-cave pull [project-name]               - rebase commits from overlay
    coding-cave down                              - stop the cave
    coding-cave resync                            - pull all, recreate cave
    coding-cave review [project-name]             - list projects or inspect one
"""
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
PROJECTS_MANIFEST = CAVE_DATA / "projects.tsv"
PROJECTS_DIR = CAVE_DATA / "projects"
NIX_OVERLAY_DIR = CAVE_DATA / "nix-overlay"


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


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


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

    # Project mount script
    write_projects_manifest()
    os.environ["CODING_CAVE_PROJECTS_MANIFEST"] = str(PROJECTS_MANIFEST)

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

    print(f"Cave started (PID {child_pid})", file=sys.stderr)
    return child_pid


def stop_cave():
    """Stop the cave."""
    pid = cave_pid()
    if pid is None:
        print("No cave running.", file=sys.stderr)
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
    print("Cave stopped.", file=sys.stderr)


def enter_cave(pid, project=None, task=None):
    """nsenter into the cave and run claude. Does not return."""
    parts = ["source /tmp/cave-env 2>/dev/null"]

    if project:
        parts.append(f"cd /projects/{project} || exit 1")
    else:
        parts.append("cd $HOME")

    if task:
        escaped = task.replace("'", "'\\''")
        parts.append(f"exec claude --dangerously-skip-permissions -p '{escaped}'")
    else:
        parts.append("exec claude --dangerously-skip-permissions")

    bash_cmd = " && ".join(parts)

    os.execvp("nsenter", [
        "nsenter",
        f"--target={pid}",
        "--user", "--mount", "--pid", "--uts", "--ipc",
        "--", "bash", "-c", bash_cmd,
    ])


# --- Project management ---

def ensure_project(project_dir):
    """Register a project with overlay dirs. Returns slug."""
    project_dir = Path(project_dir).resolve()
    slug = project_dir.name
    proj_state = PROJECTS_DIR / slug

    if not proj_state.exists():
        (proj_state / "upper").mkdir(parents=True, exist_ok=True)
        (proj_state / "work").mkdir(parents=True, exist_ok=True)
        (proj_state / "source").write_text(str(project_dir))

    return slug


def list_projects():
    """Return list of (slug, source_path) for all registered projects."""
    if not PROJECTS_DIR.exists():
        return []
    projects = []
    for proj in sorted(PROJECTS_DIR.iterdir()):
        source_file = proj / "source"
        if source_file.exists():
            projects.append((proj.name, source_file.read_text().strip()))
    return projects


def project_loaded_in_cave(slug):
    """Check if a project was included in the running cave's mount script."""
    if not PROJECTS_MANIFEST.exists():
        return False
    return f"\t{slug}\n" in PROJECTS_MANIFEST.read_text()


def write_projects_manifest():
    """Write tab-delimited manifest of project overlays for bwrap."""
    CAVE_DATA.mkdir(parents=True, exist_ok=True)
    lines = []
    for slug, source in list_projects():
        proj_state = PROJECTS_DIR / slug
        upper = proj_state / "upper"
        work = proj_state / "work"
        lines.append(f"{source}\t{upper}\t{work}\t{slug}")
    PROJECTS_MANIFEST.write_text("\n".join(lines) + "\n" if lines else "")


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
    proj_state = PROJECTS_DIR / slug
    source_file = proj_state / "source"
    if not source_file.exists():
        return None
    project_dir = source_file.read_text().strip()

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
    proj_state = PROJECTS_DIR / slug
    source_file = proj_state / "source"
    if not source_file.exists():
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(source_file.read_text().strip())

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

def cmd_run(args, gpu=False):
    """Enter cave and run claude, optionally in a project directory."""
    global_config = load_global_config()
    setup_env(global_config)

    project = None
    task = None

    if args:
        candidate = Path(args[0])
        if candidate.exists() and candidate.is_dir():
            project = ensure_project(candidate)
            task = " ".join(args[1:]) if len(args) > 1 else None
            project_config = load_project_config(candidate)
            for key, val in project_config.get("environment", {}).items():
                os.environ[key] = str(val)
        else:
            task = " ".join(args)

    pid = cave_pid()

    if pid is None:
        pid = start_cave(gpu=gpu)
    elif project and not project_loaded_in_cave(project):
        print(f"Loading {project} into cave (restarting)...", file=sys.stderr)
        stop_cave()
        pid = start_cave(gpu=gpu)

    enter_cave(pid, project=project, task=task)


def cmd_pull(args):
    """Pull changes from project overlays."""
    if args:
        pull_project(args[0])
    else:
        projects = list_projects()
        if not projects:
            print("No projects registered.", file=sys.stderr)
            return
        for slug, _ in projects:
            try:
                pull_project(slug)
            except SystemExit:
                pass  # continue with other projects


def cmd_down(args):
    """Stop the cave."""
    stop_cave()


def cmd_resync(args):
    """Pull all projects, recreate cave with fresh overlays."""
    projects = list_projects()

    # Pull all projects
    for slug, _ in projects:
        try:
            pull_project(slug)
        except (subprocess.CalledProcessError, SystemExit):
            print(f"Warning: pull failed for {slug}, continuing", file=sys.stderr)

    # Stop cave
    stop_cave()

    # Reset project overlays
    for slug, _ in projects:
        cleanup_project_overlay(slug)

    # Reset nix overlay
    for subdir in ["upper", "work"]:
        p = NIX_OVERLAY_DIR / subdir
        if p.exists():
            shutil.rmtree(p)
            p.mkdir()

    print("Resync complete. Run 'coding-cave' to start fresh.", file=sys.stderr)


def cmd_review(args):
    """List projects or inspect one."""
    projects = list_projects()

    if not args:
        pid = cave_pid()
        status = f"running (PID {pid})" if pid else "stopped"
        print(f"Cave: {status}", file=sys.stderr)
        if not projects:
            print("No projects registered.", file=sys.stderr)
            return
        for slug, source in projects:
            loaded = project_loaded_in_cave(slug)
            tag = " [loaded]" if loaded else ""
            print(f"  {slug}  <- {source}{tag}", file=sys.stderr)
        return

    slug = args[0]
    proj_state = PROJECTS_DIR / slug
    if not proj_state.exists():
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    result = mount_project_overlay(slug)
    if result is None:
        print(f"Failed to mount overlay for {slug}", file=sys.stderr)
        sys.exit(1)
    merged, source = result
    print(f"Mounted: {merged}", file=sys.stderr)
    print(f"Source:  {source}", file=sys.stderr)
    print(f"When done: fusermount3 -u {merged}", file=sys.stderr)


def main():
    commands = {
        "run": cmd_run,
        "pull": cmd_pull,
        "down": cmd_down,
        "resync": cmd_resync,
        "review": cmd_review,
    }

    args = sys.argv[1:]
    gpu = "--gpu" in args
    if gpu:
        args = [a for a in args if a != "--gpu"]

    if not args:
        cmd_run([], gpu=gpu)
        return

    cmd = args[0]
    if cmd in commands:
        if cmd == "run":
            commands[cmd](args[1:], gpu=gpu)
        else:
            commands[cmd](args[1:])
    elif Path(cmd).is_dir():
        # coding-cave ./project [task...] → shorthand for run
        cmd_run(args, gpu=gpu)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: run, pull, down, resync, review", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
