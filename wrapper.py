#!/usr/bin/env python3
"""coding-cave wrapper: per-session bwrap sandboxes with shared gitdirs."""
import argparse
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

JAIL_HOME = "/home/cave-coder"
CAVE_DATA = Path.home() / ".local" / "share" / "coding-cave"
PROJECTS_FILE = CAVE_DATA / "projects.json"
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
        if bashrc.exists():
            os.environ["CODING_CAVE_BASHRC"] = str(bashrc)
        bash_profile = Path.home() / ".bash_profile"
        if bash_profile.exists():
            os.environ["CODING_CAVE_BASH_PROFILE"] = str(bash_profile)


# --- Project registry ---

def load_projects():
    """Load the project registry (slug -> source path)."""
    if PROJECTS_FILE.exists():
        with open(PROJECTS_FILE) as f:
            return json.load(f)
    return {}


def save_projects(projects):
    CAVE_DATA.mkdir(parents=True, exist_ok=True)
    with open(PROJECTS_FILE, "w") as f:
        json.dump(projects, f, indent=2, sort_keys=True)


def list_projects():
    return list(load_projects().items())


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def detect_project_from_cwd():
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


def ensure_project(project_dir):
    """Add a project to the registry and create overlay dirs. Returns slug."""
    project_dir = Path(project_dir).resolve()
    slug = project_dir.name
    proj_state = PROJECTS_DIR / slug

    (proj_state / "upper").mkdir(parents=True, exist_ok=True)
    (proj_state / "work").mkdir(parents=True, exist_ok=True)

    # Every project must have a git repo
    if not (project_dir / ".git").is_dir():
        subprocess.run(
            ["git", "init", "-b", "main", str(project_dir)],
            check=True, capture_output=True,
        )

    initialize_gitdir(slug, project_dir)

    projects = load_projects()
    projects[slug] = str(project_dir)
    save_projects(projects)

    return slug


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


# --- Session tracking ---

def session_id():
    """Generate a unique session ID."""
    return f"{int(time.time())}-{os.getpid()}"


def register_session(sid, project, bwrap_pid):
    """Register a running session."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    info = {
        "sid": sid,
        "project": project,
        "bwrap_pid": bwrap_pid,
        "started": int(time.time()),
    }
    with open(SESSIONS_DIR / f"{sid}.json", "w") as f:
        json.dump(info, f)


def list_sessions(project=None):
    """List active sessions, cleaning up stale ones."""
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for f in sorted(SESSIONS_DIR.iterdir()):
        if not f.name.endswith(".json") or f.name.endswith(".manifest.json"):
            continue
        try:
            info = json.loads(f.read_text())
            pid = info["bwrap_pid"]
            os.kill(pid, 0)  # check if alive
            if project is None or info.get("project") == project:
                sessions.append(info)
        except (json.JSONDecodeError, KeyError, ProcessLookupError, ValueError):
            f.unlink(missing_ok=True)
            # Clean up manifest too
            manifest = f.with_suffix(".manifest.json")
            manifest.unlink(missing_ok=True)
    return sessions


def cleanup_session(sid):
    """Unmount overlays and remove session files."""
    unmount_session_overlays(sid)
    for slug in load_projects():
        sess_dir = PROJECTS_DIR / slug / "sessions" / sid
        if sess_dir.exists():
            shutil.rmtree(sess_dir, ignore_errors=True)
    (SESSIONS_DIR / f"{sid}.json").unlink(missing_ok=True)
    (SESSIONS_DIR / f"{sid}.manifest.json").unlink(missing_ok=True)


# --- Overlay management ---

def try_unmount(path):
    for cmd in ["fusermount3 -u", "fusermount -u", "/run/wrappers/bin/fusermount -u"]:
        subprocess.run(cmd.split() + [str(path)], capture_output=True)
        if not path.is_mount():
            return True
    return False


def mount_project_overlay(slug, session_id=None):
    """Mount a project overlay. If session_id is given, use per-session dirs."""
    projects = load_projects()
    if slug not in projects:
        return None
    proj_state = PROJECTS_DIR / slug
    project_dir = projects[slug]

    if not Path(project_dir).is_dir():
        print(f"Warning: source directory for '{slug}' not found: {project_dir}", file=sys.stderr)
        print(f"  Run 'coding-cave remove {slug}' to unregister it.", file=sys.stderr)
        return None

    if session_id:
        sess_dir = proj_state / "sessions" / session_id
        merged = sess_dir / "merged"
        upper = sess_dir / "upper"
        fuse_work = sess_dir / "fuse-work"
    else:
        merged = proj_state / "merged"
        upper = proj_state / "upper"
        fuse_work = proj_state / "fuse-work"

    merged.mkdir(parents=True, exist_ok=True)
    upper.mkdir(parents=True, exist_ok=True)
    fuse_work.mkdir(parents=True, exist_ok=True)

    if merged.is_mount():
        try_unmount(merged)

    uid = os.getuid()
    gid = os.getgid()
    subprocess.run([
        "fuse-overlayfs",
        "-o", f"lowerdir={project_dir},upperdir={upper},workdir={fuse_work}"
               f",squash_to_uid={uid},squash_to_gid={gid}",
        str(merged),
    ], check=True)

    return merged, project_dir


def unmount_session_overlays(sid):
    """Unmount all overlay mounts for a session."""
    for slug in load_projects():
        sess_dir = PROJECTS_DIR / slug / "sessions" / sid
        merged = sess_dir / "merged"
        if merged.exists() and merged.is_mount():
            try_unmount(merged)


# --- Git helpers ---

def initialize_gitdir(slug, project_dir):
    """Create gitdir from host .git if it doesn't exist."""
    proj_state = PROJECTS_DIR / slug
    gitdir = proj_state / "gitdir"
    if gitdir.exists() or not (project_dir / ".git").is_dir():
        return
    tmpclone = proj_state / "_tmpclone"
    subprocess.run(
        ["git", "clone", "--no-checkout", "--local", "--quiet",
         str(project_dir), str(tmpclone)],
        check=True, capture_output=True,
    )
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    subprocess.run(
        ["git", "-C", str(gitdir), "remote", "remove", "origin"],
        capture_output=True,
    )
    # Disable hooks to prevent cross-session code execution
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "config", "core.hooksPath", "/dev/null"],
        capture_output=True,
    )
    hooks_dir = gitdir / "hooks"
    if hooks_dir.exists():
        shutil.rmtree(hooks_dir)
    head = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if head.returncode == 0:
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "update-ref",
             "refs/cave-sync", head.stdout.strip()],
        )
        branch = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        if branch.returncode == 0:
            subprocess.run(
                ["git", "--git-dir", str(gitdir), "update-ref",
                 f"refs/remotes/host/{branch.stdout.strip()}", head.stdout.strip()],
            )
    ensure_cave_remote(project_dir, gitdir)


def ensure_cave_remote(project_dir, gitdir):
    git = ["git", "-C", str(project_dir)]
    existing = subprocess.run(
        git + ["remote", "get-url", "cave"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != str(gitdir):
            subprocess.run(git + ["remote", "set-url", "cave", str(gitdir)], capture_output=True)
    else:
        subprocess.run(git + ["remote", "add", "cave", str(gitdir)], capture_output=True)


def update_cave_sync(gitdir, project_dir, commit, branch=None):
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "update-ref", "refs/cave-sync", commit],
    )
    subprocess.run(["git", "-C", str(project_dir), "fetch", "cave"], capture_output=True)
    if branch:
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "update-ref",
             f"refs/remotes/host/{branch}", commit],
        )


def sync_refs(gitdir, project_dir, host_head, branch):
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "fetch", str(project_dir), "HEAD"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "update-ref",
         f"refs/heads/{branch}", host_head],
        check=True,
    )
    update_cave_sync(gitdir, project_dir, host_head, branch=branch)


def find_tree_merge_base(git, host_tree):
    """Find the cave commit whose tree matches the host HEAD tree."""
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


# --- Session lifecycle ---

def start_session(project, command, gpu=False):
    """Start a new bwrap session with per-session overlays."""
    CAVE_DATA.mkdir(parents=True, exist_ok=True)
    sid = session_id()

    # Nix overlay dirs (shared across sessions)
    (NIX_OVERLAY_DIR / "upper").mkdir(parents=True, exist_ok=True)
    (NIX_OVERLAY_DIR / "work").mkdir(parents=True, exist_ok=True)
    os.environ["CODING_CAVE_NIX_UPPER"] = str(NIX_OVERLAY_DIR / "upper")
    os.environ["CODING_CAVE_NIX_WORK"] = str(NIX_OVERLAY_DIR / "work")

    # Ensure gitdirs exist
    for slug, source in load_projects().items():
        gitdir = PROJECTS_DIR / slug / "gitdir"
        if not gitdir.exists() and (Path(source) / ".git").is_dir():
            print(f"Syncing {slug}...", file=sys.stderr)
        initialize_gitdir(slug, Path(source))

    # Mount per-session overlays
    projects = load_projects()
    for slug in projects:
        mount_project_overlay(slug, session_id=sid)

    # Build extra bwrap args (project mounts, dotfiles, passwd)
    bwrap_args = []

    # CLAUDE.md
    claude_md = os.environ.get("CODING_CAVE_CLAUDE_MD", "")
    if claude_md and Path(claude_md).is_file():
        bwrap_args += ["--ro-bind", claude_md, f"{JAIL_HOME}/.claude/CLAUDE.md"]

    # Zsh dotfiles
    zdotdir = os.environ.get("CODING_CAVE_ZDOTDIR", "")
    if zdotdir and Path(zdotdir).is_dir():
        zsh_cave = f"{JAIL_HOME}/.config/zsh"
        persistent_zdotdir = CAVE_DATA / "zdotdir"
        persistent_zdotdir.mkdir(parents=True, exist_ok=True)
        bwrap_args += ["--bind", str(persistent_zdotdir), zsh_cave]
        for f in sorted(Path(zdotdir).glob(".z*")):
            if not f.is_file():
                continue
            if "private" in f.name or "secret" in f.name:
                continue
            if f.name.startswith(".zhistory") or f.name.startswith(".zcompdump"):
                dest = persistent_zdotdir / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
            else:
                bwrap_args += ["--ro-bind", str(f), f"{zsh_cave}/{f.name}"]
        bwrap_args += ["--setenv", "ZDOTDIR", zsh_cave]

    # Bash dotfiles
    bashrc = os.environ.get("CODING_CAVE_BASHRC", "")
    if bashrc and Path(bashrc).is_file():
        bwrap_args += ["--ro-bind", bashrc, f"{JAIL_HOME}/.bashrc"]
    bash_profile = os.environ.get("CODING_CAVE_BASH_PROFILE", "")
    if bash_profile and Path(bash_profile).is_file():
        bwrap_args += ["--ro-bind", bash_profile, f"{JAIL_HOME}/.bash_profile"]

    # Project mounts
    for slug, source in projects.items():
        sess_merged = PROJECTS_DIR / slug / "sessions" / sid / "merged"
        gitdir = PROJECTS_DIR / slug / "gitdir"
        if sess_merged.exists() and sess_merged.is_mount():
            bwrap_args += ["--bind", str(sess_merged), f"/projects/{slug}"]
        elif Path(source).is_dir():
            upper = PROJECTS_DIR / slug / "upper"
            work = PROJECTS_DIR / slug / "work"
            upper.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
            bwrap_args += ["--overlay-src", source, "--overlay", str(upper), str(work), f"/projects/{slug}"]
        else:
            continue
        if gitdir.is_dir():
            bwrap_args += ["--bind", str(gitdir), f"/projects/{slug}/.git"]

    # Passwd/group files
    uid = os.getuid()
    gid = os.getgid()
    passwd_file = CAVE_DATA / "passwd"
    group_file = CAVE_DATA / "group"
    nologin = "/bin/false"
    passwd_file.write_text(
        f"root:x:0:0:System administrator:/root:{nologin}\n"
        f"cave-coder:x:{uid}:{gid}::{JAIL_HOME}:{nologin}\n"
    )
    group_file.write_text(
        f"root:x:0:\n"
        f"cave-coder:x:{gid}:\n"
    )
    bwrap_args += ["--ro-bind", str(passwd_file), "/etc/passwd"]
    bwrap_args += ["--ro-bind", str(group_file), "/etc/group"]

    # Write bwrap args file (null-separated, for bwrap --args)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    args_file = SESSIONS_DIR / f"{sid}.bwrap-args"
    with open(args_file, "wb") as f:
        for arg in bwrap_args:
            f.write(arg.encode() + b"\0")
    os.environ["CODING_CAVE_BWRAP_ARGS"] = str(args_file)

    binary = SANDBOX_GPU_BIN if gpu else SANDBOX_BIN

    # Sanitize environment: only pass vars the bwrap runtime actually needs.
    # Everything else (host secrets, user identity, etc.) is dropped.
    # This prevents /proc/1/environ inside the cave from leaking host env.
    BWRAP_ENV_ALLOWLIST = {
        # Runtime script needs these to function
        "HOME", "PATH", "TERM",
        # Forwarded into cave via try-fwd-env
        "COLORTERM",
        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
        # coding-cave internal
        "CODING_CAVE_NIX_UPPER", "CODING_CAVE_NIX_WORK",
        "CODING_CAVE_BWRAP_ARGS",
        "CODING_CAVE_SESSION_ID",
    }
    bwrap_env = {k: v for k, v in os.environ.items() if k in BWRAP_ENV_ALLOWLIST}

    proc = subprocess.Popen(
        [binary] + command,
        start_new_session=False,
        env=bwrap_env,
    )

    register_session(sid, project, proc.pid)
    print("Started coding-cave.", file=sys.stderr)

    # Wait with WUNTRACED for Ctrl+Z support
    try:
        while True:
            try:
                _, status = os.waitpid(proc.pid, os.WUNTRACED)
            except ChildProcessError:
                ret = 1
                break
            if os.WIFSTOPPED(status):
                signal.signal(signal.SIGTSTP, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTSTP)
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
        cleanup_session(sid)

    sys.exit(ret)


def find_bwrap_child(bwrap_pid):
    """Find the child process inside bwrap's namespace."""
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            stat = Path(entry.path, "stat").read_text()
            close_paren = stat.rfind(")")
            fields = stat[close_paren + 2:].split()
            ppid = int(fields[1])
            if ppid == bwrap_pid:
                return int(entry.name)
        except (FileNotFoundError, ValueError, PermissionError, IndexError):
            continue
    return None


def enter_session(session_info, shell_type, workdir=None):
    """nsenter into a running session."""
    bwrap_pid = session_info["bwrap_pid"]

    # Verify bwrap is alive
    try:
        os.kill(bwrap_pid, 0)
    except ProcessLookupError:
        print("Session process is gone.", file=sys.stderr)
        sys.exit(1)

    child_pid = find_bwrap_child(bwrap_pid)
    if child_pid is None:
        print("Cannot find namespace process for session.", file=sys.stderr)
        sys.exit(1)

    shell_bin = "/bin/zsh" if shell_type == "zsh" else "/bin/bash"

    # Source cave-env (written at bwrap entry) for the full cave environment,
    # then launch the user's shell. /tmp/cave-env is per-session (tmpfs inside
    # bwrap), so it's safe from cross-session tampering.
    proc = subprocess.Popen([
        "nsenter",
        f"--target={child_pid}",
        "--user", "--mount", "--pid", "--uts", "--ipc",
        "--preserve-credentials",
        "--", "/bin/bash", "-c",
        f'source /tmp/cave-env 2>/dev/null; cd {workdir or "/projects"} 2>/dev/null; exec {shell_bin} -l',
    ])

    try:
        while True:
            try:
                _, status = os.waitpid(proc.pid, os.WUNTRACED)
            except ChildProcessError:
                ret = 1
                break
            if os.WIFSTOPPED(status):
                signal.signal(signal.SIGTSTP, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTSTP)
                os.kill(proc.pid, signal.SIGCONT)
            elif os.WIFEXITED(status):
                ret = os.WEXITSTATUS(status)
                break
            elif os.WIFSIGNALED(status):
                ret = 128 + os.WTERMSIG(status)
                break
    except KeyboardInterrupt:
        ret = 130

    sys.exit(ret)


# --- Pull / Push (commits only) ---

def pull_project(slug, force=False):
    """Pull committed changes from cave gitdir into the host repo."""
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

    git = ["git", "-C", str(project_dir), "-c", "core.hooksPath=/dev/null"]

    # Clean up stale rebase state
    host_git_dir = project_dir / ".git"
    if (host_git_dir / "rebase-merge").is_dir() or (host_git_dir / "rebase-apply").is_dir():
        print(f"Aborting stale rebase in {slug}.", file=sys.stderr)
        subprocess.run(git + ["rebase", "--abort"], capture_output=True)
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

    branch_result = subprocess.run(
        git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    gitdir_head_result = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )

    if gitdir_head_result.returncode != 0:
        print(f"No commits in cave gitdir for {slug} — nothing to pull.", file=sys.stderr)
        return

    gitdir_head = gitdir_head_result.stdout.strip()

    # Host repo has no commits yet — fetch and create branch from cave
    if branch_result.returncode != 0:
        branch = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip() or "main"
        subprocess.run(git + ["fetch", str(gitdir), "HEAD"], check=True, capture_output=True)
        subprocess.run(git + ["update-ref", f"refs/heads/{branch}", gitdir_head], check=True)
        subprocess.run(git + ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
        subprocess.run(git + ["checkout", branch], capture_output=True)
        print(f"Pulled {slug} (initialized from cave):", file=sys.stderr)
        subprocess.run(git + ["log", "--oneline"], check=False)
        update_cave_sync(gitdir, project_dir, gitdir_head, branch=branch)
        return

    branch = branch_result.stdout.strip()
    host_head = subprocess.run(
        git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if host_head == gitdir_head:
        print(f"No new commits to pull from {slug}.", file=sys.stderr)
        update_cave_sync(gitdir, project_dir, host_head, branch=branch)
        return

    # Force with matching trees: adopt cave's history
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
            subprocess.run(git + ["fetch", str(gitdir), "HEAD"], capture_output=True)
            subprocess.run(
                git + ["update-ref", f"refs/heads/{branch}", gitdir_head],
                check=True,
            )
            print(f"Force-updated {slug} (history rewritten, tree unchanged):", file=sys.stderr)
            print(f"  {host_head[:8]} -> {gitdir_head[:8]}", file=sys.stderr)
            update_cave_sync(gitdir, project_dir, gitdir_head, branch=branch)
            return

    fetch = subprocess.run(git + ["fetch", str(gitdir), "HEAD"], capture_output=True)
    if fetch.returncode != 0:
        print(f"Fetch from gitdir failed for {slug}.", file=sys.stderr)
        sys.exit(1)

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

    # Detect history rewrite
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

        old_log = subprocess.run(
            git + ["log", "--oneline", f"{merge_base}..HEAD"],
            capture_output=True, text=True,
        )

        subprocess.run(git + ["reset", "--hard", merge_base], check=True, capture_output=True)
        subprocess.run(git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True, capture_output=True)
        subprocess.run(git + [
            "rebase", "--onto", branch, merge_base, "_cave_pull",
            "--no-reapply-cherry-picks",
            "--exec", "git -c core.hooksPath=/dev/null commit --amend --reset-author --allow-empty --no-edit --quiet",
        ], check=True, capture_output=True)
        subprocess.run(git + ["checkout", branch], check=True, capture_output=True)
        subprocess.run(git + ["merge", "--ff-only", "_cave_pull"], check=True, capture_output=True)
        subprocess.run(git + ["branch", "-D", "_cave_pull"], capture_output=True)

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

        host_head = subprocess.run(
            git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        sync_refs(gitdir, project_dir, host_head, branch)
        return

    pre_head = subprocess.run(
        git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    stash_result = subprocess.run(
        git + ["stash", "--include-untracked"],
        capture_output=True, text=True,
    )
    stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

    subprocess.run(git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True, capture_output=True)
    result = subprocess.run(git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--no-reapply-cherry-picks",
        "--exec", "git -c core.hooksPath=/dev/null commit --amend --reset-author --allow-empty --no-edit --quiet",
    ], capture_output=True)
    if result.returncode != 0:
        rebase_in_progress = ((host_git_dir / "rebase-merge").is_dir() or
                              (host_git_dir / "rebase-apply").is_dir())
        if rebase_in_progress:
            print(f"Rebase conflicts for {slug}. Resolve, then:", file=sys.stderr)
            print(f"  git -C {project_dir} rebase --continue", file=sys.stderr)
            print(f"  git -C {project_dir} checkout {branch}", file=sys.stderr)
            print(f"  git -C {project_dir} merge --ff-only _cave_pull", file=sys.stderr)
            if stashed:
                print(f"  git -C {project_dir} stash pop", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Rebase failed for {slug}.", file=sys.stderr)
            if result.stderr:
                print(result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr, file=sys.stderr, end="")
            if result.stdout:
                print(result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout, file=sys.stderr, end="")
            subprocess.run(git + ["rebase", "--abort"], capture_output=True)
            subprocess.run(git + ["branch", "-D", "_cave_pull"], capture_output=True)
            if stashed:
                subprocess.run(git + ["stash", "pop"], capture_output=True)
            sys.exit(1)

    subprocess.run(git + ["checkout", branch], capture_output=True, check=True)
    subprocess.run(git + ["merge", "--ff-only", "_cave_pull"], capture_output=True, check=True)
    subprocess.run(git + ["branch", "-D", "_cave_pull"], capture_output=True)

    if stashed:
        subprocess.run(git + ["stash", "pop"], capture_output=True)

    actual = subprocess.run(
        git + ["rev-list", "--count", f"{pre_head}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if actual != "0":
        print(f"Pulled {actual} commit(s) from {slug}:", file=sys.stderr)
        log = subprocess.run(
            git + ["log", "--oneline", f"{pre_head}..HEAD"],
            capture_output=True, text=True,
        )
        if log.returncode == 0 and log.stdout.strip():
            print(log.stdout.rstrip(), file=sys.stderr)

    host_head = subprocess.run(
        git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    sync_refs(gitdir, project_dir, host_head, branch)


def push_project(slug, force=False):
    """Push host commits into the cave gitdir."""
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

    branch_result = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    host_head = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    cave_head = subprocess.run(
        ["git", "--git-dir", str(gitdir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if cave_head.returncode == 0 and cave_head.stdout.strip() == host_head:
        print(f"No commits to push to {slug}.", file=sys.stderr)
        update_cave_sync(gitdir, project_dir, host_head, branch=branch)
        return

    # Check for unpulled cave commits
    if not force and cave_head.returncode == 0:
        cave_head_hash = cave_head.stdout.strip()
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

    sync_refs(gitdir, project_dir, host_head, branch)
    print(f"Pushed to {slug} ({host_head[:8]}).", file=sys.stderr)


def reset_project_overlay(slug):
    """Reset a project: wipe overlays and recreate gitdir."""
    projects = load_projects()
    proj_state = PROJECTS_DIR / slug

    # Unmount any session overlays
    if (proj_state / "sessions").exists():
        for sess_dir in (proj_state / "sessions").iterdir():
            merged = sess_dir / "merged"
            if merged.exists() and merged.is_mount():
                try_unmount(merged)

    # Unmount shared overlay
    merged = proj_state / "merged"
    if merged.exists() and merged.is_mount():
        try_unmount(merged)

    for subdir in ["upper", "work", "fuse-work", "gitdir", "sessions"]:
        p = proj_state / subdir
        if p.exists():
            for dirpath, dirnames, _filenames in os.walk(p):
                for d in dirnames:
                    try:
                        (Path(dirpath) / d).chmod(0o700)
                    except OSError:
                        pass
            shutil.rmtree(p)
            if subdir not in ("gitdir", "sessions"):
                p.mkdir()

    if slug in projects:
        initialize_gitdir(slug, Path(projects[slug]))


# --- Commands ---

def cmd_agent(args, gpu=False):
    """Start a new agent session."""
    global_config = load_global_config()
    setup_env(global_config)

    project = None
    project_dir = detect_project_from_cwd()
    if project_dir is not None:
        project = ensure_project(project_dir)

    workdir = cave_workdir(project)
    command = [
        "--", "/bin/bash", "-c",
        f"cd {workdir} && exec claude --dangerously-skip-permissions",
    ]

    start_session(project, command, gpu=gpu)


def cmd_shell(args, gpu=False):
    """Enter cave with a shell — join existing session or start new."""
    global_config = load_global_config()
    setup_env(global_config)

    shell_type = Path(os.environ.get("SHELL", "/bin/bash")).name

    project = None
    project_dir = detect_project_from_cwd()
    if project_dir is not None:
        project = ensure_project(project_dir)

    workdir = cave_workdir(project)

    # Check for running sessions to join
    sessions = list_sessions(project=project)
    if sessions:
        if len(sessions) == 1:
            print(f"Joining session (PID {sessions[0]['bwrap_pid']}).", file=sys.stderr)
            enter_session(sessions[0], shell_type, workdir=workdir)
        else:
            print("Running sessions:", file=sys.stderr)
            for i, s in enumerate(sessions):
                started = time.strftime("%H:%M", time.localtime(s["started"]))
                print(f"  [{i}] [{started}] {s.get('project', '?')} (PID {s['bwrap_pid']})", file=sys.stderr)
            print(f"  [n] Start new session", file=sys.stderr)
            try:
                choice = input("Join session: ").strip()
            except EOFError:
                return
            if choice == "n":
                pass  # fall through to start new
            else:
                try:
                    idx = int(choice)
                    enter_session(sessions[idx], shell_type, workdir=workdir)
                except (ValueError, IndexError):
                    print("Invalid choice.", file=sys.stderr)
                    sys.exit(1)

    # Start new shell session
    workdir = cave_workdir(project)
    shell_bin = "/bin/zsh" if shell_type == "zsh" else "/bin/bash"
    command = [
        "--", "/bin/bash", "-c",
        f"cd {workdir} && exec {shell_bin} -l",
    ]

    start_session(project, command, gpu=gpu)


def cmd_add(args):
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


def cmd_remove(args):
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

    # Check for running sessions
    sessions = list_sessions(project=slug)
    if sessions:
        print(f"Project {slug} has {len(sessions)} running session(s).", file=sys.stderr)
        print("Exit all sessions first.", file=sys.stderr)
        sys.exit(1)

    reset_project_overlay(slug)
    proj_state = PROJECTS_DIR / slug
    if proj_state.exists():
        shutil.rmtree(proj_state)

    del projects[slug]
    save_projects(projects)

    print(f"Removed {slug} from the coding cave.", file=sys.stderr)


def cmd_move(old_slug, new_path):
    projects = load_projects()
    if old_slug not in projects:
        print(f"Project not found: {old_slug}", file=sys.stderr)
        sys.exit(1)

    new_path = Path(new_path).resolve()
    if not new_path.is_dir():
        print(f"Not a directory: {new_path}", file=sys.stderr)
        sys.exit(1)

    sessions = list_sessions(project=old_slug)
    if sessions:
        print(f"Project {old_slug} has {len(sessions)} running session(s).", file=sys.stderr)
        print("Exit all sessions first.", file=sys.stderr)
        sys.exit(1)

    new_slug = new_path.name
    if new_slug in projects and new_slug != old_slug:
        print(f"A project named {new_slug} already exists.", file=sys.stderr)
        sys.exit(1)

    # Update registry
    del projects[old_slug]
    projects[new_slug] = str(new_path)
    save_projects(projects)

    # Rename state dirs if slug changed
    if new_slug != old_slug:
        old_state = PROJECTS_DIR / old_slug
        new_state = PROJECTS_DIR / new_slug
        if old_state.exists():
            old_state.rename(new_state)

    print(f"Moved {old_slug} -> {new_slug} ({new_path})", file=sys.stderr)


def cmd_reset(args):
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

    # Check for running sessions
    sessions = list_sessions(project=slug)
    if sessions:
        print(f"Project {slug} has {len(sessions)} running session(s).", file=sys.stderr)
        print("Exit all sessions first.", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(projects[slug])
    proj_state = PROJECTS_DIR / slug
    gitdir = proj_state / "gitdir"
    changes = []

    # Check for unpulled commits
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
                unpulled = subprocess.run(
                    ["git", "--git-dir", str(gitdir), "rev-list", "--count",
                     f"{merge_base_result.stdout.strip()}..HEAD"],
                    capture_output=True, text=True,
                )
                if (unpulled.returncode == 0 and unpulled.stdout.strip() != "0"):
                    log = subprocess.run(
                        ["git", "--git-dir", str(gitdir), "log", "--oneline",
                         f"{merge_base_result.stdout.strip()}..HEAD"],
                        capture_output=True, text=True,
                    )
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

    reset_project_overlay(slug)
    print(f"Reset overlay for {slug}. Next session will see current host state.", file=sys.stderr)


def cmd_pull(args):
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if args:
        pull_project(args[0], force=force)
        return

    cwd = Path.cwd().resolve()
    for slug, source in load_projects().items():
        if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
            pull_project(slug, force=force)
            return

    print("Not in a project folder. Specify a project or cd into one.", file=sys.stderr)


def cmd_push(args):
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if args:
        push_project(args[0], force=force)
        return

    cwd = Path.cwd().resolve()
    for slug, source in load_projects().items():
        if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
            push_project(slug, force=force)
            return

    projects = list_projects()
    if not projects:
        print("No projects in the cave.", file=sys.stderr)
        return
    for slug, _ in projects:
        try:
            push_project(slug, force=force)
        except SystemExit:
            pass


def project_sync_status(slug, source):
    project_dir = Path(source)
    proj_state = PROJECTS_DIR / slug
    gitdir = proj_state / "gitdir"

    if not project_dir.is_dir():
        return "missing"

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
    if host_head.returncode != 0 and cave_head.returncode != 0:
        return None  # both empty, nothing to compare
    if host_head.returncode != 0 or cave_head.returncode != 0:
        return "?"

    host_h = host_head.stdout.strip()
    cave_h = cave_head.stdout.strip()

    tags = []

    if host_h != cave_h:
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

    return ", ".join(tags) if tags else ""


def cmd_status(args):
    show_all = "--all" in args or "-a" in args

    print(f"coding-cave {VERSION}", file=sys.stderr)

    all_projects = list_projects()
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
    projects = scoped or all_projects

    if not scoped:
        print("Projects:", file=sys.stderr)
    for slug, source in projects:
        status = project_sync_status(slug, source)
        tag = f" [{status}]" if status else ""
        print(f"  {slug}  <- {source}{tag}", file=sys.stderr)

        sessions = list_sessions(project=slug)
        for s in sessions:
            started = time.strftime("%H:%M", time.localtime(s["started"]))
            print(f"    [{started}] PID {s['bwrap_pid']}  running", file=sys.stderr)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="coding-cave",
        description="Sandboxed Claude Code agent sessions with Nix environments.",
    )
    parser.add_argument("--gpu", action="store_true", help="enable GPU access")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("agent", help="start a new agent session")
    sub.add_parser("shell", help="start a shell or join a running session")

    p_add = sub.add_parser("add", help="register a project")
    p_add.add_argument("dir", nargs="?", help="directory to add (default: CWD)")

    p_status = sub.add_parser("status", help="show projects and running sessions")
    p_status.add_argument("-a", "--all", action="store_true", help="show all projects")

    p_pull = sub.add_parser("pull", help="pull commits from cave")
    p_pull.add_argument("project", nargs="?", help="project slug (default: CWD project)")
    p_pull.add_argument("--force", action="store_true", help="adopt cave's history")

    p_push = sub.add_parser("push", help="push host commits into cave")
    p_push.add_argument("project", nargs="?", help="project slug (default: CWD project)")
    p_push.add_argument("--force", action="store_true", help="skip unpulled commit check")

    p_reset = sub.add_parser("reset", help="wipe overlay + gitdir to match host")
    p_reset.add_argument("project", nargs="?", help="project slug (default: CWD project)")

    p_remove = sub.add_parser("remove", help="unregister a project and delete data")
    p_remove.add_argument("project", nargs="?", help="project slug (default: CWD project)")

    p_move = sub.add_parser("move", help="update a project's source path")
    p_move.add_argument("old_slug", help="current project name")
    p_move.add_argument("new_path", help="new directory path")

    return parser


def main():
    parser = build_parser()
    parsed = parser.parse_args()

    if parsed.command is None:
        cmd_agent([], gpu=parsed.gpu)
    elif parsed.command == "agent":
        cmd_agent([], gpu=parsed.gpu)
    elif parsed.command == "shell":
        cmd_shell([], gpu=parsed.gpu)
    elif parsed.command == "add":
        cmd_add([parsed.dir] if parsed.dir else [])
    elif parsed.command == "status":
        cmd_status(["--all"] if parsed.all else [])
    elif parsed.command == "pull":
        args = []
        if parsed.project:
            args.append(parsed.project)
        if parsed.force:
            args.append("--force")
        cmd_pull(args)
    elif parsed.command == "push":
        args = []
        if parsed.project:
            args.append(parsed.project)
        if parsed.force:
            args.append("--force")
        cmd_push(args)
    elif parsed.command == "reset":
        cmd_reset([parsed.project] if parsed.project else [])
    elif parsed.command == "remove":
        cmd_remove([parsed.project] if parsed.project else [])
    elif parsed.command == "move":
        cmd_move(parsed.old_slug, parsed.new_path)


if __name__ == "__main__":
    main()
