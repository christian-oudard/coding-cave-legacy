#!/usr/bin/env python3
"""
coding-cave wrapper: reads config, sets up workspaces, launches sandbox.

Subcommands:
    codingcave                               - overlay cwd, launch claude
    codingcave run <project-dir> [task...]   - overlay project, launch claude with task
    codingcave pull [workspace]              - rebase + re-author commits, clean up
    codingcave review [workspace]            - list workspaces or mount one for inspection
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

SANDBOX_BIN = "@SANDBOX_BIN@"


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


def setup_env(global_config, project_config):
    """Set environment variables from config files."""
    token = global_config.get("credentials", {}).get("claude-oauth-token")
    if token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    sandbox_cfg = global_config.get("sandbox", {})
    home_size_gb = sandbox_cfg.get("home-size-gb")
    if home_size_gb:
        os.environ["CODING_CAVE_HOME_SIZE"] = str(int(home_size_gb * 1073741824))

    for key, val in project_config.get("environment", {}).items():
        os.environ[key] = str(val)


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def workspaces_dir():
    return Path.home() / ".local" / "share" / "coding-cave" / "workspaces"


def is_workspace_locked(workspace):
    """Check if a workspace is in use by another instance."""
    lock_file = workspace / "lock"
    if not lock_file.exists():
        return False
    try:
        fd = os.open(str(lock_file), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


def lock_workspace(workspace):
    """Acquire an exclusive lock that survives execv into the sandbox."""
    lock_file = workspace / "lock"
    fd = os.open(str(lock_file), os.O_WRONLY | os.O_CREAT, 0o644)
    os.set_inheritable(fd, True)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def workspace_has_changes(workspace):
    """Check if a workspace's upper dir contains real file changes."""
    upper = workspace / "upper"
    if not upper.exists():
        return False
    for entry in upper.iterdir():
        return True
    return False


def create_workspace(project_dir, task=None):
    """Create overlay workspace directories. Returns (workspace_path, name).

    Removes any existing workspaces for the same project first,
    but refuses if they contain unpulled changes.
    """
    project_dir = Path(project_dir).resolve()
    project_str = str(project_dir)

    # Clean up old workspaces for this project
    base = workspaces_dir()
    if base.exists():
        for ws in base.iterdir():
            source_file = ws / "source"
            if source_file.exists() and source_file.read_text().strip() == project_str:
                if is_workspace_locked(ws):
                    print(f"Already running: {ws.name}", file=sys.stderr)
                    sys.exit(1)
                if workspace_has_changes(ws):
                    print(f"Unpulled changes in: {ws.name}", file=sys.stderr)
                    print(f"Run 'codingcave pull' first, or remove manually:", file=sys.stderr)
                    print(f"  rm -rf {ws}", file=sys.stderr)
                    sys.exit(1)
                cleanup_workspace(ws)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if task:
        name = f"{project_dir.name}-{slugify(task)}-{timestamp}"
    else:
        name = f"{project_dir.name}-{timestamp}"

    workspace = workspaces_dir() / name
    (workspace / "upper").mkdir(parents=True, exist_ok=True)
    (workspace / "work").mkdir(parents=True, exist_ok=True)
    (workspace / "merged").mkdir(parents=True, exist_ok=True)

    # Store source project path for review/cleanup
    (workspace / "source").write_text(str(project_dir))

    return workspace, name


def launch_sandbox():
    os.execv(SANDBOX_BIN, ["codingcave"])


def cmd_run(args):
    if not args:
        print("Usage: codingcave run <project-dir> [task...]", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(args[0]).resolve()
    task = " ".join(args[1:]) if len(args) > 1 else None

    global_config = load_global_config()
    project_config = load_project_config(project_dir)
    setup_env(global_config, project_config)

    workspace, name = create_workspace(project_dir, task)
    lock_workspace(workspace)
    print(f"Workspace: {name}", file=sys.stderr)

    os.environ["CODING_CAVE_WORKSPACE"] = str(project_dir)
    os.environ["CODING_CAVE_OVERLAY_UPPER"] = str(workspace / "upper")
    os.environ["CODING_CAVE_OVERLAY_WORK"] = str(workspace / "work")
    if task:
        os.environ["CODING_CAVE_TASK"] = task

    launch_sandbox()



def cmd_review(args):
    base = workspaces_dir()

    if not args:
        if not base.exists():
            print("No workspaces.", file=sys.stderr)
            return
        found = False
        for ws in sorted(base.iterdir()):
            source_file = ws / "source"
            if source_file.exists():
                source = source_file.read_text().strip()
                mounted = (ws / "merged").is_mount()
                status = " (mounted)" if mounted else ""
                print(f"  {ws.name}  <- {source}{status}", file=sys.stderr)
                found = True
        if not found:
            print("No workspaces.", file=sys.stderr)
        return

    name = args[0]
    workspace = base / name

    if not workspace.exists():
        print(f"Workspace not found: {name}", file=sys.stderr)
        sys.exit(1)

    source = (workspace / "source").read_text().strip()
    upper = workspace / "upper"
    work = workspace / "work"
    merged = workspace / "merged"

    if merged.is_mount():
        print(f"Already mounted: {merged}", file=sys.stderr)
    else:
        subprocess.run([
            "fuse-overlayfs",
            "-o", f"lowerdir={source},upperdir={upper},workdir={work}",
            str(merged),
        ], check=True)
        print(f"Mounted: {merged}", file=sys.stderr)
    print(f"Source:  {source}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"To pull changes into your project:", file=sys.stderr)
    print(f"  cd {source}", file=sys.stderr)
    print(f"  git fetch {merged} <branch>", file=sys.stderr)
    print(file=sys.stderr)
    print(f"When done:  fusermount3 -u {merged}", file=sys.stderr)


def find_latest_workspace(project_dir):
    """Find the most recent workspace for a project directory."""
    base = workspaces_dir()
    if not base.exists():
        return None
    project_str = str(Path(project_dir).resolve())
    for ws in sorted(base.iterdir(), reverse=True):
        source_file = ws / "source"
        if source_file.exists() and source_file.read_text().strip() == project_str:
            return ws
    return None


def mount_overlay(workspace, project_dir):
    """Mount workspace overlay using fuse-overlayfs."""
    # Separate workdir from bwrap's kernel overlayfs workdir —
    # kernel overlayfs leaves internal state that fuse-overlayfs can't reuse.
    fuse_work = workspace / "fuse-work"
    fuse_work.mkdir(exist_ok=True)
    subprocess.run([
        "fuse-overlayfs",
        "-o", f"lowerdir={project_dir},upperdir={workspace / 'upper'},workdir={fuse_work}",
        str(workspace / "merged"),
    ], check=True)


def try_unmount(path):
    """Try to unmount a FUSE mount. Returns True if successful."""
    for cmd in ["fusermount3 -u", "fusermount -u", "/run/wrappers/bin/fusermount -u"]:
        subprocess.run(cmd.split() + [str(path)], capture_output=True)
        if not path.is_mount():
            return True
    return False


def cleanup_workspace(workspace):
    """Unmount and remove a workspace. Best-effort."""
    merged = workspace / "merged"
    if merged.is_mount() and not try_unmount(merged):
        print(f"Could not unmount {merged}. Run: fusermount -u {merged}", file=sys.stderr)
        return
    # Kernel overlayfs creates work/work with mode 000; fix before removing.
    for dirpath, dirnames, _filenames in os.walk(workspace):
        for d in dirnames:
            p = Path(dirpath) / d
            try:
                p.chmod(0o700)
            except OSError:
                pass
    shutil.rmtree(workspace)


def cmd_pull(args):
    if args:
        workspace = workspaces_dir() / args[0]
        if not workspace.exists():
            print(f"Workspace not found: {args[0]}", file=sys.stderr)
            sys.exit(1)
        project_dir = Path((workspace / "source").read_text().strip())
    else:
        project_dir = Path.cwd()
        workspace = find_latest_workspace(project_dir)
        if not workspace:
            print(f"No workspace found for {project_dir}", file=sys.stderr)
            sys.exit(1)

    merged = workspace / "merged"
    if merged.is_mount():
        try_unmount(merged)
    mount_overlay(workspace, project_dir)

    # Runs outside sandbox — user's git identity and GPG config apply.
    git = ["git", "-C", str(project_dir)]

    fetch = subprocess.run(git + ["fetch", str(merged), "HEAD"])
    if fetch.returncode != 0:
        print(f"Fetch failed. Overlay mounted at: {merged}", file=sys.stderr)
        print(f"codingcave cleanup {workspace.name}", file=sys.stderr)
        sys.exit(1)

    merge_base = subprocess.run(
        git + ["merge-base", "HEAD", "FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    count = subprocess.run(
        git + ["rev-list", "--count", f"{merge_base}..FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if count == "0":
        print("No commits to pull.", file=sys.stderr)
        cleanup_workspace(workspace)
        return

    branch = subprocess.run(
        git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Rebase agent commits onto our branch, re-authoring and re-signing each.
    subprocess.run(git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True)

    result = subprocess.run(git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--exec", "git commit --amend --reset-author --no-edit",
    ])
    if result.returncode != 0:
        print(f"Rebase conflicts. Resolve, then:", file=sys.stderr)
        print(f"  git rebase --continue", file=sys.stderr)
        print(f"  git checkout {branch} && git merge --ff-only _cave_pull && git branch -d _cave_pull", file=sys.stderr)
        print(f"  codingcave cleanup {workspace.name}", file=sys.stderr)
        sys.exit(1)

    subprocess.run(git + ["checkout", branch], check=True)
    subprocess.run(git + ["merge", "--ff-only", "_cave_pull"], check=True)
    subprocess.run(git + ["branch", "-d", "_cave_pull"], check=True)

    cleanup_workspace(workspace)
    print(f"Rebased {count} commit(s) and cleaned up: {workspace.name}", file=sys.stderr)



def main():
    commands = {"run": cmd_run, "pull": cmd_pull, "review": cmd_review}

    if len(sys.argv) < 2:
        cmd_run(["."])
        return

    cmd = sys.argv[1]
    if cmd in commands:
        commands[cmd](sys.argv[2:])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: run, pull, review", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
