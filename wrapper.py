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
from contextlib import contextmanager
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

    # Defer SIGINT from Popen through PID file writes — if interrupted
    # in this window the bwrap process would be orphaned without tracking
    old_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)

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
        signal.signal(signal.SIGINT, old_handler)
        print("Failed to start cave: init process not found", file=sys.stderr)
        sys.exit(1)

    CAVE_PID_FILE.write_text(str(child_pid))
    (CAVE_DATA / "bwrap.pid").write_text(str(proc.pid))

    signal.signal(signal.SIGINT, old_handler)

    # Wait for init to write /tmp/cave-env
    time.sleep(0.5)

    print("Started coding-cave.", file=sys.stderr)
    return child_pid


def stop_cave():
    """Stop the cave."""
    # Ignore SIGINT during teardown — partial cleanup is worse than
    # a slightly delayed exit
    old_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    pid = cave_pid()
    if pid is None:
        signal.signal(signal.SIGINT, old_handler)
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
    signal.signal(signal.SIGINT, old_handler)


def cave_workdir(project):
    """Compute the working directory inside the cave based on host CWD."""
    if not project:
        return "/projects"
    cwd = Path.cwd().resolve()
    projects = load_projects()
    source = Path(projects.get(project, "")).resolve()
    try:
        rel = cwd.relative_to(source)
    except ValueError:
        return f"/projects/{project}"
    return f"/projects/{project}/{rel}" if str(rel) != "." else f"/projects/{project}"


def enter_cave(pid, project=None, shell=None, workdir=None):
    """nsenter into the cave and run claude or a shell."""
    parts = ["source /tmp/cave-env 2>/dev/null"]

    parts.append(f"cd {workdir or '/projects'}")

    if shell:
        shell_bin = "/bin/zsh" if shell == "zsh" else "/bin/bash"
        parts.append(f"exec {shell_bin} -l")
    else:
        # exec replaces bash so claude becomes the direct child — required
        # for WUNTRACED job control propagation below.
        parts.append("exec claude --dangerously-skip-permissions")

    bash_cmd = " && ".join(parts)

    register_session()
    try:
        proc = subprocess.Popen([
            "nsenter",
            f"--target={pid}",
            "--user", "--mount", "--pid", "--uts", "--ipc",
            "--preserve-credentials",
            "--", "/bin/bash", "-c", bash_cmd,
        ])
        # Wait with WUNTRACED so we detect when the child stops (Ctrl+Z).
        # Without this, a stopped child hangs forever because bash -c has
        # no job control and subprocess.call ignores stopped children.
        while True:
            try:
                _, status = os.waitpid(proc.pid, os.WUNTRACED)
            except ChildProcessError:
                ret = 1
                break
            if os.WIFSTOPPED(status):
                # Child stopped (e.g. Claude Code's Ctrl+Z handler).
                # Stop ourselves so the outer shell sees the job as
                # suspended and the user can fg back.
                signal.signal(signal.SIGTSTP, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTSTP)
                # Resumed by outer shell — continue the child.
                os.kill(proc.pid, signal.SIGCONT)
            elif os.WIFEXITED(status):
                ret = os.WEXITSTATUS(status)
                break
            elif os.WIFSIGNALED(status):
                ret = 128 + os.WTERMSIG(status)
                break
    except KeyboardInterrupt:
        ret = 130
    finally:
        # Ignore SIGINT during cleanup so rapid Ctrl-C can't interrupt
        # session unregistration or cave teardown
        signal.signal(signal.SIGINT, signal.SIG_IGN)
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
    # Record sync point so pull can detect history rewrites
    head = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if head.returncode == 0:
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "update-ref",
             "refs/cave-sync", head.stdout.strip()],
        )
        # Set initial host remote ref in gitdir so cave git log shows host/main
        branch = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        if branch.returncode == 0:
            subprocess.run(
                ["git", "--git-dir", str(gitdir), "update-ref",
                 f"refs/remotes/host/{branch.stdout.strip()}", head.stdout.strip()],
            )
    # Add gitdir as 'cave' remote in host repo so cave/main shows in git log
    ensure_cave_remote(project_dir, gitdir)


def ensure_cave_remote(project_dir, gitdir):
    """Ensure the host repo has a 'cave' remote pointing to the gitdir."""
    git = ["git", "-C", str(project_dir)]
    existing = subprocess.run(
        git + ["remote", "get-url", "cave"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != str(gitdir):
            subprocess.run(
                git + ["remote", "set-url", "cave", str(gitdir)],
                capture_output=True,
            )
    else:
        subprocess.run(
            git + ["remote", "add", "cave", str(gitdir)],
            capture_output=True,
        )
    subprocess.run(git + ["fetch", "cave"], capture_output=True)


def update_cave_sync(gitdir, project_dir, commit, branch=None):
    """Write cave-sync ref and update remote tracking refs in both repos."""
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "update-ref",
         "refs/cave-sync", commit],
    )
    # Update cave remote in host repo (so host git log shows cave/main)
    subprocess.run(
        ["git", "-C", str(project_dir), "fetch", "cave"],
        capture_output=True,
    )
    # Update host remote in gitdir (so cave git log shows host/main)
    if branch:
        host_head = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        if host_head.returncode == 0:
            subprocess.run(
                ["git", "--git-dir", str(gitdir), "update-ref",
                 f"refs/remotes/host/{branch}", host_head.stdout.strip()],
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


def find_tree_merge_base(git, host_tree):
    """Find the cave commit whose tree matches the host HEAD tree.

    Used when histories have diverged (e.g. filter-branch, amend) to
    find the effective merge-base by content rather than ancestry.
    Expects FETCH_HEAD to be set from a prior fetch.
    """
    cave_log = subprocess.run(
        git + ["log", "--format=%H %T", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if cave_log.returncode != 0:
        return None
    for line in cave_log.stdout.strip().splitlines():
        commit_hash, tree_hash = line.split()
        if tree_hash == host_tree:
            return commit_hash
    return None


def pull_working_tree(slug, project_dir, gitdir):
    """Pull uncommitted working tree changes from the cave overlay.

    Stages the changes in the host index. Refuses if the host working
    tree already has uncommitted changes.
    """
    proj_state = PROJECTS_DIR / slug
    merged = proj_state / "merged"

    # Use existing mount or mount temporarily
    mounted_here = False
    if not (merged.exists() and merged.is_mount()):
        result = mount_project_overlay(slug)
        if result is None:
            return
        mounted_here = True

    try:
        git_cave = ["git", "--git-dir", str(gitdir), "--work-tree", str(merged)]

        # Check for tracked/staged changes vs HEAD
        diff_check = subprocess.run(
            git_cave + ["diff", "HEAD", "--quiet"], capture_output=True,
        )
        if diff_check.returncode == 0:
            return

        # Refuse if host working tree is dirty
        host_status = subprocess.run(
            ["git", "-C", str(project_dir), "status", "--porcelain"],
            capture_output=True, text=True,
        )
        if host_status.returncode == 0 and host_status.stdout.strip():
            print(f"Host has uncommitted changes — skipping working tree pull for {slug}.", file=sys.stderr)
            print("Commit or stash host changes first.", file=sys.stderr)
            return

        patch = subprocess.run(
            git_cave + ["diff", "HEAD"], capture_output=True,
        )
        if patch.stdout:
            apply_result = subprocess.run(
                ["git", "-C", str(project_dir), "apply", "--index"],
                input=patch.stdout, capture_output=True,
            )
            if apply_result.returncode != 0:
                print(f"Failed to apply working tree changes for {slug}.", file=sys.stderr)
                return

        print(f"Staged working tree changes from {slug}.", file=sys.stderr)

    finally:
        if mounted_here:
            try_unmount(merged)


def pull_project(slug, force=False):
    """Pull changes from a project's cave gitdir into the host repo.

    Pulls both committed and uncommitted changes. Commits are re-authored
    as the host user. Uncommitted working tree changes are staged in the
    host index (refused if the host working tree is already dirty).

    Handles rewritten history (amend, rebase, filter-branch) automatically
    via tree matching when normal ancestry-based merge-base fails.

    With force=True, also allows replacing host history when trees match
    at HEAD (e.g. message-only rewrites). This is destructive: host-only
    commits not in the cave will be lost.
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

    ensure_cave_remote(project_dir, gitdir)

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

    branch = subprocess.run(
        git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Quick check: identical HEADs means nothing to pull
    gitdir_head = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    host_head = subprocess.run(
        git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if host_head == gitdir_head:
        print(f"No new commits to pull from {slug}.", file=sys.stderr)
        update_cave_sync(gitdir, project_dir, host_head, branch)
        pull_working_tree(slug, project_dir, gitdir)
        return

    # Force with matching trees: adopt cave's history on the host
    # (e.g. after filter-branch / amend in cave, same content)
    if force:
        gitdir_tree = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        host_tree = subprocess.run(
            git + ["rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        if (gitdir_tree.returncode == 0 and host_tree.returncode == 0 and
                gitdir_tree.stdout.strip() == host_tree.stdout.strip()):
            subprocess.run(
                git + ["fetch", str(gitdir), "HEAD"],
                capture_output=True,
            )
            subprocess.run(
                git + ["update-ref", f"refs/heads/{branch}", gitdir_head],
                check=True,
            )
            print(f"Force-updated {slug} (history rewritten, tree unchanged):", file=sys.stderr)
            print(f"  {host_head[:8]} -> {gitdir_head[:8]}", file=sys.stderr)
            update_cave_sync(gitdir, project_dir, gitdir_head, branch)
            pull_working_tree(slug, project_dir, gitdir)
            return

    # Fetch cave commits from gitdir (a regular dir on the host filesystem)
    fetch = subprocess.run(git + ["fetch", str(gitdir), "HEAD"], capture_output=True)
    if fetch.returncode != 0:
        print(f"Fetch from gitdir failed for {slug}.", file=sys.stderr)
        sys.exit(1)

    # Read cave-sync marker (last agreed-upon commit)
    cave_sync = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "refs/cave-sync"],
        capture_output=True, text=True,
    )
    cave_sync_hash = cave_sync.stdout.strip() if cave_sync.returncode == 0 else None

    merge_base_result = subprocess.run(
        git + ["merge-base", "HEAD", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if merge_base_result.returncode != 0:
        # No common ancestor — find effective merge-base by tree
        # matching (handles rewritten history from amend/rebase/filter-branch)
        host_tree = subprocess.run(
            git + ["rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        merge_base = find_tree_merge_base(git, host_tree)
        if merge_base is None:
            print(f"No common ancestor for {slug} — skipping.", file=sys.stderr)
            return
    else:
        merge_base = merge_base_result.stdout.strip()

    count = subprocess.run(
        git + ["rev-list", "--count", f"{merge_base}..FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if count == "0":
        print(f"No commits to pull from {slug}.", file=sys.stderr)
        return

    # Detect history rewrite: if cave-sync exists, host hasn't moved since
    # last sync, but merge-base is older, the cave rewrote history.
    # Require --force to adopt rewritten history.
    if (cave_sync_hash and cave_sync_hash == host_head
            and merge_base != host_head):
        host_count = subprocess.run(
            git + ["rev-list", "--count", f"{merge_base}..HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        if not force:
            print(f"Cave rewrote history for {slug} "
                  f"({host_count} host commit(s) -> {count} cave commit(s)).",
                  file=sys.stderr)
            print(f"Run 'coding-cave pull --force' to adopt the cave's history.",
                  file=sys.stderr)
            return

        stash_result = subprocess.run(
            git + ["stash", "--include-untracked"],
            capture_output=True, text=True,
        )
        stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

        # Capture old log before reset
        old_log = subprocess.run(
            git + ["log", "--oneline", f"{merge_base}..HEAD"],
            capture_output=True, text=True,
        )

        subprocess.run(git + ["reset", "--hard", "FETCH_HEAD"], check=True, capture_output=True)

        if stashed:
            subprocess.run(git + ["stash", "pop"], capture_output=True)

        print(f"Force-pulled {slug} ({host_count} commit(s) replaced with {count}):", file=sys.stderr)
        if old_log.returncode == 0 and old_log.stdout.strip():
            print(f"  Replaced:", file=sys.stderr)
            for line in old_log.stdout.rstrip().splitlines():
                print(f"    {line}", file=sys.stderr)
        new_log = subprocess.run(
            git + ["log", "--oneline", f"{merge_base}..HEAD"],
            capture_output=True, text=True,
        )
        if new_log.returncode == 0 and new_log.stdout.strip():
            print(f"  With:", file=sys.stderr)
            for line in new_log.stdout.rstrip().splitlines():
                print(f"    {line}", file=sys.stderr)

        # Sync gitdir to match host
        host_head = subprocess.run(
            git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "fetch", str(project_dir), "HEAD"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "update-ref",
             f"refs/heads/{branch}", host_head],
            check=True,
        )
        update_cave_sync(gitdir, project_dir, host_head, branch)
        pull_working_tree(slug, project_dir, gitdir)
        return

    pre_head = subprocess.run(
        git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Stash any working tree / index changes so rebase can proceed
    stash_result = subprocess.run(
        git + ["stash", "--include-untracked"],
        capture_output=True, text=True,
    )
    stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

    subprocess.run(git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True, capture_output=True)
    result = subprocess.run(git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--no-reapply-cherry-picks",
        "--exec", "git commit --amend --reset-author --allow-empty --no-edit --quiet",
    ], capture_output=True)
    if result.returncode != 0:
        rebase_in_progress = ((git_dir / "rebase-merge").is_dir() or
                              (git_dir / "rebase-apply").is_dir())
        if rebase_in_progress:
            print(f"Rebase conflicts for {slug}. Resolve, then:", file=sys.stderr)
            print(f"  git -C {project_dir} rebase --continue", file=sys.stderr)
            print(f"  git -C {project_dir} checkout {branch}", file=sys.stderr)
            print(f"  git -C {project_dir} merge --ff-only _cave_pull", file=sys.stderr)
            print(f"  git -C {project_dir} branch -d _cave_pull", file=sys.stderr)
            if stashed:
                print(f"  git -C {project_dir} stash pop", file=sys.stderr)
            print(f"  coding-cave push {slug}", file=sys.stderr)
        else:
            print(f"Rebase failed for {slug}.", file=sys.stderr)
            subprocess.run(git + ["branch", "-D", "_cave_pull"], capture_output=True)
            if stashed:
                subprocess.run(git + ["stash", "pop"], capture_output=True)
        sys.exit(1)

    subprocess.run(git + ["checkout", branch], check=True, capture_output=True)
    subprocess.run(git + ["merge", "--ff-only", "_cave_pull"], check=True, capture_output=True)
    subprocess.run(git + ["branch", "-d", "_cave_pull"], check=True, capture_output=True)

    if stashed:
        subprocess.run(git + ["stash", "pop"], capture_output=True)

    actual = subprocess.run(
        git + ["rev-list", "--count", f"{pre_head}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if actual == "0":
        print(f"No new commits to pull from {slug}.", file=sys.stderr)
    else:
        print(f"Pulled {actual} commit(s) from {slug}:", file=sys.stderr)
        log = subprocess.run(
            git + ["log", "--oneline", f"{pre_head}..HEAD"],
            capture_output=True, text=True,
        )
        if log.returncode == 0 and log.stdout.strip():
            print(log.stdout.rstrip(), file=sys.stderr)

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
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "update-ref",
         f"refs/heads/{branch}", host_head],
        check=True,
    )
    update_cave_sync(gitdir, project_dir, host_head, branch)

    pull_working_tree(slug, project_dir, gitdir)


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

    enter_cave(pid, project=project, workdir=cave_workdir(project))


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


def push_working_tree(slug, project_dir, gitdir):
    """Push uncommitted host working tree changes into the cave overlay.

    Stages the changes in the cave's index. Refuses if the cave working
    tree already has uncommitted changes.
    """
    proj_state = PROJECTS_DIR / slug
    merged = proj_state / "merged"

    # Use existing mount or mount temporarily
    mounted_here = False
    if not (merged.exists() and merged.is_mount()):
        result = mount_project_overlay(slug)
        if result is None:
            return
        mounted_here = True

    try:
        git_host = ["git", "-C", str(project_dir)]
        git_cave = ["git", "--git-dir", str(gitdir), "--work-tree", str(merged)]

        # Check for host tracked/staged changes vs HEAD
        diff_check = subprocess.run(
            git_host + ["diff", "HEAD", "--quiet"], capture_output=True,
        )
        if diff_check.returncode == 0:
            return

        # Refuse if cave working tree is dirty
        cave_diff = subprocess.run(
            git_cave + ["diff", "HEAD", "--quiet"], capture_output=True,
        )
        if cave_diff.returncode != 0:
            print(f"Cave has uncommitted changes — skipping working tree push for {slug}.", file=sys.stderr)
            print("Commit or stash cave changes first.", file=sys.stderr)
            return

        patch = subprocess.run(
            git_host + ["diff", "HEAD"], capture_output=True,
        )
        if patch.stdout:
            apply_result = subprocess.run(
                git_cave + ["apply", "--index"],
                input=patch.stdout, capture_output=True,
            )
            if apply_result.returncode != 0:
                print(f"Failed to apply working tree changes to {slug}.", file=sys.stderr)
                return

        print(f"Pushed working tree changes to {slug}.", file=sys.stderr)

    finally:
        if mounted_here:
            try_unmount(merged)


def push_project(slug, force=False):
    """Push host commits and working tree changes into the cave.

    Updates the gitdir refs and objects to match the host, then applies
    any uncommitted host working tree changes to the cave overlay (refused
    if the cave working tree is already dirty).

    Warns if the cave has unpulled commits that would be lost. Use
    force=True to skip the check.
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

    ensure_cave_remote(project_dir, gitdir)

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
        update_cave_sync(gitdir, project_dir, host_head, branch)
        push_working_tree(slug, project_dir, gitdir)
        return

    # Check for unpulled cave commits that would be lost
    if not force and cave_head.returncode == 0:
        cave_head_hash = cave_head.stdout.strip()
        # Fetch host objects into gitdir so merge-base can work
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "fetch", str(project_dir), "HEAD"],
            capture_output=True,
        )
        merge_base = subprocess.run(
            ["git", "--git-dir", str(gitdir), "merge-base",
             cave_head_hash, host_head],
            capture_output=True, text=True,
        )
        if merge_base.returncode == 0:
            unpulled = subprocess.run(
                ["git", "--git-dir", str(gitdir), "rev-list", "--count",
                 f"{merge_base.stdout.strip()}..{cave_head_hash}"],
                capture_output=True, text=True,
            )
            if unpulled.returncode == 0 and unpulled.stdout.strip() != "0":
                count = unpulled.stdout.strip()
                print(f"Cave has {count} unpulled commit(s) that would be lost:", file=sys.stderr)
                subprocess.run(
                    ["git", "--git-dir", str(gitdir), "log", "--oneline",
                     f"{merge_base.stdout.strip()}..{cave_head_hash}"],
                )
                print(f"Run 'coding-cave pull' first, or use 'coding-cave push --force'.", file=sys.stderr)
                sys.exit(1)

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
    update_cave_sync(gitdir, project_dir, host_head, branch)

    print(f"Pushed to {slug} ({host_head[:8]}).", file=sys.stderr)

    push_working_tree(slug, project_dir, gitdir)


def cmd_push(args):
    """Push host commits into project overlays."""
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if args:
        push_project(args[0], force=force)
        return

    # If CWD is inside a registered project, push only that project
    cwd = Path.cwd().resolve()
    for slug, source in load_projects().items():
        if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
            push_project(slug, force=force)
            return

    # Otherwise push all projects
    projects = list_projects()
    if not projects:
        print("No projects in the cave.", file=sys.stderr)
        return
    for slug, _ in projects:
        try:
            push_project(slug, force=force)
        except SystemExit:
            pass  # continue with other projects


def cmd_pull(args):
    """Pull changes from project overlays."""
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if args:
        pull_project(args[0], force=force)
        return

    # If CWD is inside a registered project, pull only that project
    cwd = Path.cwd().resolve()
    for slug, source in load_projects().items():
        if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
            pull_project(slug, force=force)
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

    enter_cave(pid, project=project, shell=shell_type, workdir=cave_workdir(project))



def project_sync_status(slug, source):
    """Return a short sync status string for a project."""
    project_dir = Path(source)
    proj_state = PROJECTS_DIR / slug
    gitdir = proj_state / "gitdir"

    if not gitdir.exists():
        return "no gitdir"

    host_head = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    cave_head = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if host_head.returncode != 0 or cave_head.returncode != 0:
        return "?"

    host_h = host_head.stdout.strip()
    cave_h = cave_head.stdout.strip()

    tags = []

    # Commit sync
    if host_h == cave_h:
        pass  # commits in sync
    else:
        # Fetch host objects into gitdir for merge-base
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "fetch", str(project_dir), "HEAD"],
            capture_output=True,
        )
        mb = subprocess.run(
            ["git", "--git-dir", str(gitdir), "merge-base", cave_h, host_h],
            capture_output=True, text=True,
        )
        if mb.returncode != 0:
            tags.append("diverged")
        else:
            base = mb.stdout.strip()
            to_pull = subprocess.run(
                ["git", "--git-dir", str(gitdir), "rev-list", "--count",
                 f"{base}..{cave_h}"],
                capture_output=True, text=True,
            )
            to_push = subprocess.run(
                ["git", "--git-dir", str(gitdir), "rev-list", "--count",
                 f"{base}..{host_h}"],
                capture_output=True, text=True,
            )
            n_pull = to_pull.stdout.strip() if to_pull.returncode == 0 else "0"
            n_push = to_push.stdout.strip() if to_push.returncode == 0 else "0"
            if n_pull != "0" and n_push != "0":
                tags.append(f"{n_pull} to pull, {n_push} to push")
            elif n_pull != "0":
                tags.append(f"{n_pull} to pull")
            elif n_push != "0":
                tags.append(f"{n_push} to push")

    # Working tree changes (only if overlay is already mounted)
    merged = proj_state / "merged"
    if merged.exists() and merged.is_mount():
        git_cave = ["git", "--git-dir", str(gitdir), "--work-tree", str(merged)]
        wt = subprocess.run(
            git_cave + ["status", "--porcelain", "-uno"], capture_output=True, text=True,
        )
        if wt.returncode == 0 and wt.stdout.strip():
            n = len(wt.stdout.strip().splitlines())
            tags.append(f"{n} changed")

    return ", ".join(tags) if tags else ""


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
            status = project_sync_status(slug, source)
            tag = f" [{status}]" if status else ""
            print(f"  {slug}  <- {source}{tag}", file=sys.stderr)
    else:
        print("No projects in the cave.", file=sys.stderr)



HELP = """\
Usage: coding-cave [command] [options]

Commands:
  (default)            enter cave and run claude agent
  shell                enter cave with your shell
  add [dir]            register a project (default: current dir)
  pull [project]       pull commits + working tree changes from cave
                       --force: adopt cave's commit history on host (destructive)
  push [project]       push host commits + working tree changes to cave
                       --force: skip unpulled commit check
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
