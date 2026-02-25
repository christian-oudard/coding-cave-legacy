#!/usr/bin/env python3
"""coding-cave wrapper: persistent cave with multi-agent support."""
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
VERSION = "@VERSION@"

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

    # Ensure gitdirs exist for all registered projects (migration)
    for slug, source in load_projects().items():
        gitdir = PROJECTS_DIR / slug / "gitdir"
        if not gitdir.exists() and (Path(source) / ".git").is_dir():
            print(f"Syncing {slug}...", file=sys.stderr)
        initialize_gitdir(slug, Path(source))

    # Mount fuse-overlayfs for all projects. Kernel overlayfs in user
    # namespaces can't copy-up files with unmapped GIDs (EOVERFLOW).
    # fuse-overlayfs runs in userspace and handles this correctly.
    mount_project_overlays()

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

    unmount_project_overlays()


def enter_cave(pid, project=None, shell=None):
    """nsenter into the cave and run claude or a shell."""
    parts = ["source /tmp/cave-env 2>/dev/null"]

    parts.append("cd /projects")

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

    initialize_gitdir(slug, project_dir)

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


def initialize_gitdir(slug, project_dir):
    """Create gitdir from host .git if it doesn't exist.

    Uses git clone --no-checkout --local for hardlinked objects (fast,
    space-efficient). The gitdir is bind-mounted at /projects/<slug>/.git
    inside the cave, keeping .git off the overlayfs.
    """
    proj_state = PROJECTS_DIR / slug
    gitdir = proj_state / "gitdir"
    if gitdir.exists() or not (project_dir / ".git").is_dir():
        return
    tmpclone = proj_state / "_tmpclone"
    subprocess.run(
        ["git", "clone", "--no-checkout", "--local", "--quiet",
         str(project_dir), str(tmpclone)],
        check=True,
    )
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    # Remove origin remote — host path doesn't exist inside the cave
    subprocess.run(
        ["git", "-C", str(gitdir), "remote", "remove", "origin"],
        capture_output=True,
    )
    # Populate the index from HEAD — clone --no-checkout leaves it empty,
    # which makes git think all tracked files are deleted.
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "read-tree", "HEAD"],
        check=True,
    )


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

    upper = proj_state / "upper"
    fuse_work = proj_state / "fuse-work"
    upper.mkdir(parents=True, exist_ok=True)
    fuse_work.mkdir(exist_ok=True)

    # squash_to_uid/gid prevents fuse-overlayfs from trying to chown
    # during copy-up. Without this, files whose GID doesn't match the
    # current user cause EPERM on modification.
    uid = os.getuid()
    gid = os.getgid()
    subprocess.run([
        "fuse-overlayfs",
        "-o", f"lowerdir={project_dir},upperdir={upper},workdir={fuse_work}"
               f",squash_to_uid={uid},squash_to_gid={gid}",
        str(merged),
    ], check=True)

    return merged, project_dir


def mount_project_overlays():
    """Mount fuse-overlayfs for all registered projects.

    Called before starting bwrap so the runtime script can bind-mount
    the merged dirs. fuse-overlayfs handles unmapped GIDs that kernel
    overlayfs cannot copy-up in user namespaces (EOVERFLOW).
    """
    for slug in load_projects():
        merged = PROJECTS_DIR / slug / "merged"
        if merged.exists() and merged.is_mount():
            continue
        mount_project_overlay(slug)


def unmount_project_overlays():
    """Unmount all project fuse-overlayfs mounts."""
    for slug in load_projects():
        merged = PROJECTS_DIR / slug / "merged"
        if merged.exists() and merged.is_mount():
            try_unmount(merged)


def pull_project(slug):
    """Pull changes from a project's cave gitdir into the host repo.

    Post-condition: the host git repo becomes identical to the cave repo,
    except that unsigned cave-coder commits are re-authored as the user.
    The cave's gitdir is then synced to match the host's rebased history.
    """
    projects = load_projects()
    if slug not in projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    proj_state = PROJECTS_DIR / slug
    project_dir = Path(projects[slug])
    gitdir = proj_state / "gitdir"

    if not gitdir.exists():
        print(f"No gitdir for {slug} — run 'coding-cave reset' first.", file=sys.stderr)
        sys.exit(1)

    # Clean up stale rebase/branch state from a previous failed pull
    git = ["git", "-C", str(project_dir)]
    git_dir = project_dir / ".git"
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        print(f"Aborting stale rebase in {slug}.", file=sys.stderr)
        subprocess.run(git + ["rebase", "--abort"], capture_output=True)
    # If left on _cave_pull branch, get back to a real branch
    cur_branch = subprocess.run(
        git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if cur_branch.returncode == 0 and cur_branch.stdout.strip() == "_cave_pull":
        branches = subprocess.run(
            git + ["branch", "--format=%(refname:short)"],
            capture_output=True, text=True,
        )
        for b in branches.stdout.strip().splitlines():
            if b != "_cave_pull":
                subprocess.run(git + ["checkout", b], capture_output=True)
                break
    subprocess.run(git + ["branch", "-D", "_cave_pull"], capture_output=True)

    # Fetch cave commits from gitdir (a regular dir on the host filesystem)
    fetch = subprocess.run(git + ["fetch", str(gitdir), "HEAD"])
    if fetch.returncode != 0:
        print(f"Fetch from gitdir failed for {slug}.", file=sys.stderr)
        sys.exit(1)

    merge_base_result = subprocess.run(
        git + ["merge-base", "HEAD", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if merge_base_result.returncode != 0:
        print(f"No common ancestor for {slug} — skipping.", file=sys.stderr)
        return
    merge_base = merge_base_result.stdout.strip()

    count = subprocess.run(
        git + ["rev-list", "--count", f"{merge_base}..FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if count == "0":
        print(f"No commits to pull from {slug}.", file=sys.stderr)
        return

    branch = subprocess.run(
        git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    pre_head = subprocess.run(
        git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    subprocess.run(git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True)
    result = subprocess.run(git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--no-reapply-cherry-picks",
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

    actual = subprocess.run(
        git + ["rev-list", "--count", f"{pre_head}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if actual == "0":
        print(f"No new commits to pull from {slug}.", file=sys.stderr)
    else:
        print(f"Pulled {actual} commit(s) from {slug}.", file=sys.stderr)

    # Sync gitdir to the rebased history so both repos share the same
    # commit hashes. The gitdir is bind-mounted into the cave, so this
    # update is immediately visible inside.
    # Fetch objects without updating refs (git refuses to fetch into a
    # checked-out branch), then update the ref manually.
    host_head = subprocess.run(
        git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "fetch", str(project_dir), "HEAD"],
        check=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "update-ref",
         f"refs/heads/{branch}", host_head],
        check=True,
    )


def reset_project_overlay(slug):
    """Reset a project to match the host: wipe overlay and recreate gitdir.

    Post-condition: the entire project folder in the cave becomes identical
    to the host project folder on next start.
    """
    projects = load_projects()
    proj_state = PROJECTS_DIR / slug
    merged = proj_state / "merged"
    if merged.exists() and merged.is_mount():
        try_unmount(merged)

    for subdir in ["upper", "work", "fuse-work", "gitdir"]:
        p = proj_state / subdir
        if p.exists():
            for dirpath, dirnames, _filenames in os.walk(p):
                for d in dirnames:
                    try:
                        (Path(dirpath) / d).chmod(0o700)
                    except OSError:
                        pass
            shutil.rmtree(p)
            if subdir != "gitdir":
                p.mkdir()

    # Recreate gitdir from host
    if slug in projects:
        initialize_gitdir(slug, Path(projects[slug]))


# --- Commands ---

def cmd_agent(args, gpu=False):
    """Enter cave and run claude."""
    global_config = load_global_config()
    setup_env(global_config)

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

    slug = project_dir.name
    projects = load_projects()
    if slug in projects and Path(projects[slug]).resolve() == project_dir:
        print(f"{slug} is already in the coding cave.", file=sys.stderr)
        return

    slug = ensure_project(project_dir)
    print(f"Added {slug} to the coding cave.", file=sys.stderr)
    if cave_pid():
        print("Note: running agents must be restarted to access this project.", file=sys.stderr)


def cmd_remove(args):
    """Remove a project from the cave."""
    # Resolve project — explicit arg or CWD match
    slug = None
    if args:
        slug = args[0]
    else:
        cwd = Path.cwd().resolve()
        for s, source in load_projects().items():
            if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
                slug = s
                break

    if slug is None:
        print("No project specified and CWD is not a registered project.", file=sys.stderr)
        sys.exit(1)

    projects = load_projects()
    if slug not in projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    if cave_pid() and project_loaded_in_cave(slug):
        print(f"Project {slug} is loaded in the running cave.", file=sys.stderr)
        print("Stop the cave first (exit all sessions).", file=sys.stderr)
        sys.exit(1)

    reset_project_overlay(slug)
    proj_state = PROJECTS_DIR / slug
    if proj_state.exists():
        shutil.rmtree(proj_state)

    del projects[slug]
    save_projects(projects)

    print(f"Removed {slug} from the coding cave.", file=sys.stderr)


def cmd_reset(args):
    """Reset a project overlay to match host state."""
    if cave_pid() is not None:
        print("Cave is running. Exit all sessions first.", file=sys.stderr)
        sys.exit(1)

    # Resolve project — explicit arg, CWD match, or error
    slug = None
    if args:
        slug = args[0]
    else:
        cwd = Path.cwd().resolve()
        for s, source in load_projects().items():
            if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
                slug = s
                break

    if slug is None:
        print("No project specified and CWD is not a registered project.", file=sys.stderr)
        sys.exit(1)

    projects = load_projects()
    if slug not in projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(projects[slug])
    proj_state = PROJECTS_DIR / slug
    gitdir = proj_state / "gitdir"
    changes = []

    # Check for unpulled commits using gitdir (no fuse-overlayfs needed)
    if gitdir.exists():
        cave_head = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        if cave_head.returncode == 0:
            merge_base_result = subprocess.run(
                ["git", "-C", str(project_dir), "merge-base", "HEAD",
                 cave_head.stdout.strip()],
                capture_output=True, text=True,
            )
            if merge_base_result.returncode == 0:
                log = subprocess.run(
                    ["git", "--git-dir", str(gitdir), "log", "--oneline",
                     f"{merge_base_result.stdout.strip()}..HEAD"],
                    capture_output=True, text=True,
                )
                if log.returncode == 0 and log.stdout.strip():
                    changes.append("Unpulled commits:")
                    changes.append(log.stdout.rstrip())

    # Check uncommitted changes using fuse-overlayfs for working tree access
    result = mount_project_overlay(slug)
    if result is not None:
        merged, _ = result
        git_args = ["git", "--git-dir", str(gitdir), "--work-tree", str(merged),
                     "status", "--short"] if gitdir.exists() else \
                   ["git", "-C", str(merged), "status", "--short"]
        status = subprocess.run(git_args, capture_output=True, text=True)
        if status.returncode == 0 and status.stdout.strip():
            changes.append("Uncommitted changes:")
            changes.append(status.stdout.rstrip())
        try_unmount(merged)

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

    reset_project_overlay(slug)
    print(f"Reset overlay for {slug}. Cave will see current host state on next start.", file=sys.stderr)


def push_project(slug):
    """Push host commits into the cave, making the cave identical to the host.

    Pre-condition: the user has approved clobbering changes in the cave.
    Post-condition: the cave git repo becomes identical to the host repo.

    If the cave is running, nsenter to reset the working tree on the overlay.
    If stopped, wipe the overlay so the next start assembles fresh from host.
    """
    projects = load_projects()
    if slug not in projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    proj_state = PROJECTS_DIR / slug
    project_dir = Path(projects[slug])
    gitdir = proj_state / "gitdir"

    if not gitdir.exists():
        print(f"No gitdir for {slug} — run 'coding-cave reset' first.", file=sys.stderr)
        sys.exit(1)

    # Determine host branch and HEAD
    branch_result = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    host_head = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Check if gitdir is already up to date
    cave_head = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if cave_head.returncode == 0 and cave_head.stdout.strip() == host_head:
        print(f"No commits to push to {slug}.", file=sys.stderr)
        return

    # Update gitdir refs and objects from host. The gitdir is bind-mounted
    # into the cave, so the update is immediately visible inside. The
    # working tree (on the overlay) is left untouched — uncommitted changes
    # in the cave are preserved.
    # Fetch objects without updating refs (git refuses to fetch into a
    # checked-out branch), then update the ref manually.
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "fetch", str(project_dir), "HEAD"],
        check=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "update-ref",
         f"refs/heads/{branch}", host_head],
        check=True,
    )

    print(f"Pushed to {slug} ({host_head[:8]}).", file=sys.stderr)


def cmd_push(args):
    """Push host commits into project overlays."""
    if args:
        push_project(args[0])
        return

    # If CWD is inside a registered project, push only that project
    cwd = Path.cwd().resolve()
    for slug, source in load_projects().items():
        if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
            push_project(slug)
            return

    # Otherwise push all projects
    projects = list_projects()
    if not projects:
        print("No projects in the cave.", file=sys.stderr)
        return
    for slug, _ in projects:
        try:
            push_project(slug)
        except SystemExit:
            pass  # continue with other projects


def cmd_pull(args):
    """Pull changes from project overlays."""
    if args:
        pull_project(args[0])
        return

    # If CWD is inside a registered project, pull only that project
    cwd = Path.cwd().resolve()
    for slug, source in load_projects().items():
        if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
            pull_project(slug)
            return

    # Not in a project folder — do nothing
    print("Not in a project folder. Specify a project or cd into one.", file=sys.stderr)


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
        print(f"Cave: stopped (version {VERSION})", file=sys.stderr)
    else:
        sessions = active_session_count()
        print(f"Cave: running (PID {pid}, version {VERSION}), {sessions} active session(s)", file=sys.stderr)

    projects = list_projects()
    if projects:
        print("Projects:", file=sys.stderr)
        for slug, source in projects:
            loaded = pid and project_loaded_in_cave(slug)
            tag = " [loaded]" if loaded else ""
            print(f"  {slug}  <- {source}{tag}", file=sys.stderr)
    else:
        print("No projects in the cave.", file=sys.stderr)



HELP = """\
Usage: coding-cave [command] [options]

Commands:
  (default)            enter cave and run claude agent
  shell                enter cave with your shell
  add [dir]            register a project (default: current dir)
  pull [project]       pull commits from cave gitdir, re-author
  push [project]       make cave identical to host (clobbers cave)
  reset [project]      wipe overlay + gitdir to match host (cave must be stopped)
  remove [project]     unregister a project and delete overlay data
  status               show cave and project info
  help                 show this help

Options:
  --gpu                enable GPU access (for agent/shell)

Examples:
  coding-cave                          run claude in current project
  coding-cave ./my-project             run claude in my-project
  coding-cave shell                    get a shell in the cave
  coding-cave add ~/code/my-project    register a project
  coding-cave pull                     pull changes from current project
  coding-cave push                     push host commits into cave
  coding-cave reset                    wipe overlay to match host
  coding-cave remove                   unregister current project
  coding-cave status                   show what's running\
"""


def print_help():
    print(HELP, file=sys.stderr)


def main():
    commands = {
        "agent": cmd_agent,
        "shell": cmd_shell,
        "add": cmd_add,
        "status": cmd_status,
        "pull": cmd_pull,
        "push": cmd_push,
        "reset": cmd_reset,
        "remove": cmd_remove,
    }

    args = sys.argv[1:]
    gpu = "--gpu" in args
    if gpu:
        args = [a for a in args if a != "--gpu"]

    if not args:
        cmd_agent([], gpu=gpu)
        return

    cmd = args[0]
    if cmd in ("help", "--help", "-h"):
        print_help()
        return

    if cmd in commands:
        if cmd in ("agent", "shell"):
            commands[cmd](args[1:], gpu=gpu)
        else:
            commands[cmd](args[1:])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Run 'coding-cave help' for usage.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
