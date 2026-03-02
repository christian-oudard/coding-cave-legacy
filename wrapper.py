#!/usr/bin/env python3
"""coding-cave wrapper: per-session bwrap sandboxes with shared gitdirs."""
import argparse
import json
import os
import re
import shlex
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
CAVE_HOME = CAVE_DATA / "home"


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

    claude_settings = Path.home() / ".config" / "coding-cave" / "settings.json"
    if claude_settings.exists():
        os.environ["CODING_CAVE_CLAUDE_SETTINGS"] = str(claude_settings)

    skills_dir = Path.home() / ".config" / "coding-cave" / "skills"
    if skills_dir.is_dir():
        os.environ["CODING_CAVE_SKILLS_DIR"] = str(skills_dir)

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
    sys.exit(0)


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
    """Remove session files."""
    for slug in load_projects():
        force_rmtree(PROJECTS_DIR / slug / "sessions" / sid)
    (SESSIONS_DIR / f"{sid}.json").unlink(missing_ok=True)
    (SESSIONS_DIR / f"{sid}.manifest.json").unlink(missing_ok=True)


# --- Git helpers ---

SAFE_GIT_OPTS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.fsmonitor=",
    "-c", "diff.external=",
    "-c", "core.sshCommand=echo blocked",
    "-c", "protocol.ext.allow=never",
]


def git(gitdir, *args, **kwargs):
    """Run git against a cave gitdir with config-based code execution neutralized.

    The gitdir is writable from inside the cave, so its .git/config could be
    poisoned with core.pager, core.fsmonitor, diff.external, core.sshCommand,
    or filter drivers. This wrapper overrides all such options.
    """
    cmd = ["git", "--git-dir", str(gitdir)] + SAFE_GIT_OPTS + list(args)
    return subprocess.run(cmd, **kwargs)


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
    git(gitdir, "config", "core.hooksPath", "/dev/null",
        capture_output=True)
    hooks_dir = gitdir / "hooks"
    if hooks_dir.exists():
        shutil.rmtree(hooks_dir)
    head = git(gitdir, "rev-parse", "HEAD",
        capture_output=True, text=True)
    if head.returncode == 0:
        git(gitdir, "update-ref",
             "refs/cave-sync", head.stdout.strip())
        branch = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        if branch.returncode == 0:
            git(gitdir, "update-ref",
                 f"refs/remotes/host/{branch.stdout.strip()}", head.stdout.strip())
    ensure_cave_remote(project_dir, gitdir)
    mirror_origin_refs(gitdir, project_dir)


def ensure_cave_remote(project_dir, gitdir):
    host_git = ["git", "-C", str(project_dir)]
    existing = subprocess.run(
        host_git + ["remote", "get-url", "cave"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != str(gitdir):
            subprocess.run(host_git + ["remote", "set-url", "cave", str(gitdir)], capture_output=True)
    else:
        subprocess.run(host_git + ["remote", "add", "cave", str(gitdir)], capture_output=True)


def mirror_origin_refs(gitdir, project_dir):
    """Copy refs/remotes/origin/* from host project into the cave gitdir."""
    result = subprocess.run(
        ["git", "-C", str(project_dir), "for-each-ref",
         "--format=%(refname) %(objectname)", "refs/remotes/origin/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        ref, sha = line.split()
        git(gitdir, "update-ref", ref, sha,
            capture_output=True)


def update_cave_sync(gitdir, project_dir, commit, branch=None):
    git(gitdir, "update-ref", "refs/cave-sync", commit)
    subprocess.run(["git", "-C", str(project_dir), "fetch", "cave"], capture_output=True)
    if branch:
        git(gitdir, "update-ref",
             f"refs/remotes/host/{branch}", commit)
    mirror_origin_refs(gitdir, project_dir)


def sync_refs(gitdir, project_dir, host_head, branch):
    git(gitdir, "fetch", str(project_dir), "HEAD",
        check=True, capture_output=True)
    git(gitdir, "update-ref",
         f"refs/heads/{branch}", host_head,
        check=True)
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


# --- Per-session gitdir ---

def create_session_gitdir(slug, sid, overlay_base):
    """Create a per-session gitdir with alternates pointing to shared objects.

    The session gitdir has its own HEAD, index, and refs but shares the object
    store with the shared gitdir via alternates.  Alternates starts with the
    host path; the caller rewrites it to the cave-internal path before bwrap,
    and merge_session_gitdirs rewrites it back for cleanup.
    """
    shared_gitdir = PROJECTS_DIR / slug / "gitdir"
    if not shared_gitdir.is_dir():
        return None

    session_gitdir = overlay_base / f"gitdir-{slug}"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(session_gitdir)],
        capture_output=True, check=True,
    )

    # Not actually bare — used with a separate working tree via .git file
    git(session_gitdir, "config", "core.bare", "false", capture_output=True)

    # Alternates: host path (tests + cleanup use this directly)
    objects_info = session_gitdir / "objects" / "info"
    objects_info.mkdir(parents=True, exist_ok=True)
    (objects_info / "alternates").write_text(
        f"{shared_gitdir / 'objects'}\n"
    )

    # Copy HEAD
    (session_gitdir / "HEAD").write_text((shared_gitdir / "HEAD").read_text())

    # Copy all refs from shared gitdir
    result = subprocess.run(
        ["git", "--git-dir", str(shared_gitdir), "for-each-ref",
         "--format=%(refname) %(objectname)"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            ref, sha = line.split()
            git(session_gitdir, "update-ref", ref, sha, capture_output=True)

    # Populate index from HEAD so git status starts clean
    git(session_gitdir, "read-tree", "HEAD", capture_output=True)

    # Disable hooks
    git(session_gitdir, "config", "core.hooksPath", "/dev/null",
        capture_output=True)
    hooks = session_gitdir / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)

    return session_gitdir


def _rebase_onto_reauthored(gitdir, shared_sha, session_sha, merge_base):
    """Rebase session commits onto a re-authored shared chain.

    When the shared gitdir has re-authored commits (same trees, different
    hashes from pull --reset-author), and the session has new commits on top
    of the old chain, this rebases only the new commits onto the shared HEAD.

    Returns the new tip SHA if successful, None if not applicable.
    """
    if not merge_base:
        return None

    # Build tree→commit map from shared chain (merge_base..shared_sha)
    shared_log = git(gitdir, "log", "--format=%H %T",
                     f"{merge_base}..{shared_sha}",
                     capture_output=True, text=True)
    if shared_log.returncode != 0:
        return None
    shared_trees = {}
    for line in shared_log.stdout.strip().splitlines():
        if not line:
            continue
        commit, tree = line.split()
        shared_trees[tree] = commit
    if not shared_trees:
        return None

    # Walk session chain (merge_base..session_sha) to find re-authoring boundary
    session_log = git(gitdir, "log", "--format=%H %T",
                      f"{merge_base}..{session_sha}",
                      capture_output=True, text=True)
    if session_log.returncode != 0:
        return None
    session_commits = []
    for line in session_log.stdout.strip().splitlines():
        if not line:
            continue
        commit, tree = line.split()
        session_commits.append((commit, tree))

    # Find boundary: first session commit whose tree exists in shared chain
    # (session_commits is newest-first from git log)
    boundary_idx = None
    for i, (commit, tree) in enumerate(session_commits):
        if tree in shared_trees:
            boundary_idx = i
            break
    if boundary_idx is None:
        return None

    # Commits above the boundary are new work to rebase (reverse for oldest-first)
    new_commits = list(reversed(session_commits[:boundary_idx]))
    if not new_commits:
        return None

    # Rebase new commits onto shared HEAD using commit-tree
    parent = shared_sha
    for commit, tree in new_commits:
        info = git(gitdir, "log", "-1", "--format=%an%n%ae%n%aI%n%B", commit,
                   capture_output=True, text=True)
        if info.returncode != 0:
            return None
        lines = info.stdout.rstrip("\n").split("\n", 3)
        author_name, author_email, author_date = lines[0], lines[1], lines[2]
        message = lines[3] if len(lines) > 3 else ""
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": author_date,
        }
        result = subprocess.run(
            ["git", "--git-dir", str(gitdir)] + SAFE_GIT_OPTS +
            ["commit-tree", tree, "-p", parent, "-m", message],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            return None
        parent = result.stdout.strip()

    return parent


def _forward_refs(sid, session_gitdir, shared_gitdir, *, force=False):
    """Forward branch refs from a session gitdir into the shared gitdir.

    Handles fast-forwards, re-authoring detection, and rebase-onto-reauthored.

    When force=False (live sync): never force-updates, never moves refs
    backward.  Diverged refs that can't be resolved by fast-forward or
    rebase are skipped — they'll be picked up on final cleanup.

    When force=True (final cleanup): force-updates diverged refs and backs
    up the old shared ref under refs/sessions/<sid>/<branch>.
    """
    result = git(session_gitdir, "for-each-ref",
                 "--format=%(refname) %(objectname)", "refs/heads/",
                 capture_output=True, text=True)
    if result.returncode != 0:
        return

    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        ref, sha = line.split()
        branch = ref.removeprefix("refs/heads/")

        # Fetch the objects into shared gitdir
        fetch = git(shared_gitdir, "fetch", str(session_gitdir), sha,
                    capture_output=True)
        if fetch.returncode != 0:
            continue

        # Check if fast-forward is possible
        existing = git(shared_gitdir, "rev-parse", ref,
                       capture_output=True, text=True)
        if existing.returncode != 0:
            # Ref doesn't exist in shared — create it
            git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)
            continue

        existing_sha = existing.stdout.strip()
        if existing_sha == sha:
            continue  # already up to date

        merge_base = git(shared_gitdir, "merge-base",
                         existing_sha, sha,
                         capture_output=True, text=True)
        if (merge_base.returncode == 0
                and merge_base.stdout.strip() == existing_sha):
            # Fast-forward
            git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)
        elif (merge_base.returncode == 0
                  and merge_base.stdout.strip() == sha):
            continue  # session is behind shared, skip
        else:
            # Diverged — check if it's a real content change or just
            # re-authoring (pull rewrites commits to change author).
            session_tree = git(shared_gitdir, "rev-parse",
                               f"{sha}^{{tree}}",
                               capture_output=True, text=True)
            shared_tree = git(shared_gitdir, "rev-parse",
                              f"{existing_sha}^{{tree}}",
                              capture_output=True, text=True)
            if (session_tree.returncode == 0
                    and shared_tree.returncode == 0
                    and session_tree.stdout.strip()
                        == shared_tree.stdout.strip()):
                continue  # same tree, just metadata (re-authoring)

            # Try rebasing new session commits onto re-authored chain
            rebased = _rebase_onto_reauthored(
                shared_gitdir, existing_sha, sha,
                merge_base.stdout.strip()
                if merge_base.returncode == 0 else None)
            if rebased:
                git(shared_gitdir, "update-ref", ref, rebased,
                    capture_output=True)
                continue

            if not force:
                continue  # live sync: skip diverged refs

            # Final cleanup: force-update shared ref, back up old ref.
            git(shared_gitdir, "update-ref",
                f"refs/sessions/{sid}/{branch}", existing_sha,
                capture_output=True)
            git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)

    # Also fetch tags
    git(shared_gitdir, "fetch", str(session_gitdir),
        "refs/tags/*:refs/tags/*", capture_output=True)


def forward_session_refs(slug, session_gitdir, shared_gitdir):
    """Forward refs from a live session into the shared gitdir (safe).

    Called by sync_live_sessions during pull/push.  Temporarily rewrites
    alternates to host path for git operations, then restores cave path.
    Never force-updates — diverged refs are skipped.
    """
    alt_file = session_gitdir / "objects" / "info" / "alternates"
    cave_path = None
    if alt_file.exists():
        cave_path = alt_file.read_text().strip()
        alt_file.write_text(f"{shared_gitdir / 'objects'}\n")

    _forward_refs(None, session_gitdir, shared_gitdir, force=False)

    # Restore cave-internal alternates path
    if cave_path is not None:
        alt_file.write_text(f"{cave_path}\n")


def merge_session_gitdirs(sid, overlay_base):
    """Merge per-session gitdir refs and objects back into shared gitdirs.

    Called on session exit (final cleanup).  Force-updates diverged refs
    with backup under refs/sessions/<sid>/<branch>.

    Scans overlay_base for gitdir-<slug> directories rather than relying on
    the project registry, so cleanup works even if the registry changes.
    """
    if not overlay_base.is_dir():
        return
    for entry in sorted(overlay_base.iterdir()):
        if not entry.name.startswith("gitdir-") or not entry.is_dir():
            continue
        slug = entry.name.removeprefix("gitdir-")
        session_gitdir = entry
        shared_gitdir = PROJECTS_DIR / slug / "gitdir"
        if not shared_gitdir.is_dir():
            continue

        # Rewrite alternates to host path (cave path won't exist on host)
        alt_file = session_gitdir / "objects" / "info" / "alternates"
        if alt_file.exists():
            alt_file.write_text(f"{shared_gitdir / 'objects'}\n")

        _forward_refs(sid, session_gitdir, shared_gitdir, force=True)


# --- Session lifecycle ---

def start_session(project, command, gpu=False):
    """Start a new bwrap session with per-session overlays."""
    CAVE_DATA.mkdir(parents=True, exist_ok=True)
    sid = session_id()

    # Nix store overlay (shared across sessions, fuse-overlayfs)
    (NIX_OVERLAY_DIR / "upper").mkdir(parents=True, exist_ok=True)
    (NIX_OVERLAY_DIR / "work").mkdir(parents=True, exist_ok=True)
    (NIX_OVERLAY_DIR / "merged").mkdir(parents=True, exist_ok=True)
    nix_merged = str(NIX_OVERLAY_DIR / "merged")
    # Only mount if not already mounted (shared across sessions)
    nix_already_mounted = os.path.ismount(nix_merged)
    if not nix_already_mounted:
        subprocess.run(
            ["fuse-overlayfs",
             "-o", f"lowerdir=/nix/store,upperdir={NIX_OVERLAY_DIR / 'upper'},workdir={NIX_OVERLAY_DIR / 'work'}",
             nix_merged],
            check=True, capture_output=True,
        )
    os.environ["CODING_CAVE_NIX_MERGED"] = nix_merged

    # Seed nix DB from host/parent so nix doesn't try to re-register
    # (chmod) existing store paths, which fails on overlayfs.
    # Use sqlite3 backup API for a consistent copy (handles WAL mode).
    host_nix_db = Path("/nix/var/nix/db/db.sqlite")
    nix_db_dir = NIX_OVERLAY_DIR / "db"
    nix_db_dir.mkdir(parents=True, exist_ok=True)
    cave_nix_db = nix_db_dir / "db.sqlite"
    if host_nix_db.exists():
        import sqlite3
        try:
            cave_nix_db.unlink(missing_ok=True)
            for wal in nix_db_dir.glob("db.sqlite-*"):
                wal.unlink()
            src = sqlite3.connect(f"file:{host_nix_db}?mode=ro", uri=True)
            dst = sqlite3.connect(str(cave_nix_db))
            src.backup(dst)
            dst.close()
            src.close()
        except sqlite3.Error:
            pass
    if cave_nix_db.exists():
        os.environ["CODING_CAVE_NIX_DB_DIR"] = str(nix_db_dir)

    # Ensure gitdirs exist
    for slug, source in load_projects().items():
        gitdir = PROJECTS_DIR / slug / "gitdir"
        if not gitdir.exists() and (Path(source) / ".git").is_dir():
            print(f"Syncing {slug}...", file=sys.stderr)
        initialize_gitdir(slug, Path(source))

    projects = load_projects()

    # When already inside a cave, pasta must be skipped (outer cave's
    # network is already isolated).
    inside_cave = bool(os.environ.get("CODING_CAVE_VERSION"))

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
                # Don't copy host history — keep cave history separate
                dest = persistent_zdotdir / f.name
                if not dest.exists():
                    dest.touch()
            else:
                bwrap_args += ["--ro-bind", str(f), f"{zsh_cave}/{f.name}"]
        for f in sorted(Path(zdotdir).iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if "private" in f.name or "secret" in f.name:
                continue
            bwrap_args += ["--ro-bind", str(f), f"{zsh_cave}/{f.name}"]
        bwrap_args += ["--setenv", "ZDOTDIR", zsh_cave]

    # Bash dotfiles
    bashrc = os.environ.get("CODING_CAVE_BASHRC", "")
    if bashrc and Path(bashrc).is_file():
        bwrap_args += ["--ro-bind", bashrc, f"{JAIL_HOME}/.bashrc"]
    bash_profile = os.environ.get("CODING_CAVE_BASH_PROFILE", "")
    if bash_profile and Path(bash_profile).is_file():
        bwrap_args += ["--ro-bind", bash_profile, f"{JAIL_HOME}/.bash_profile"]

    # Project overlays via fuse-overlayfs.
    # Kernel overlayfs in user namespaces has two problems:
    #   1. uuid=on (forced by kernel with userxattr) remaps inodes, causing
    #      EOVERFLOW on ext4 with high inode numbers.
    #   2. CVE-2023-0386: copy-up requires lower-layer UIDs/GIDs to be mapped
    #      in the mounter's user namespace, but unprivileged users can only
    #      map 1 UID and 1 GID, so files with other ownership fail.
    # fuse-overlayfs handles copy-up in userspace, bypassing both issues.
    #
    # Each session gets its own gitdir (with alternates to the shared object
    # store) so concurrent sessions have independent HEAD/index/refs.
    # Gitdirs are mounted at /projects/.gitdirs/<slug> instead of under
    # the project overlay, because a child bind mount on the overlay
    # creates a locked mount that blocks clone_private_mount() in nested
    # user namespaces ("failed to clone lowerpath").  A .git file in the
    # overlay upper points git to the separate gitdir mount.
    overlay_base = CAVE_DATA / "session-overlays" / sid
    overlay_base.mkdir(parents=True, exist_ok=True)
    overlay_mounts = []  # merged paths for cleanup
    try:
        for slug, source in projects.items():
            if not Path(source).is_dir():
                continue
            upper = overlay_base / f"{slug}-upper"
            work = overlay_base / f"{slug}-work"
            merged = overlay_base / f"{slug}-merged"
            upper.mkdir(exist_ok=True)
            work.mkdir(exist_ok=True)
            merged.mkdir(exist_ok=True)
            shared_gitdir = PROJECTS_DIR / slug / "gitdir"
            if shared_gitdir.is_dir():
                session_gitdir = create_session_gitdir(slug, sid, overlay_base)
                if session_gitdir:
                    # Rewrite alternates from host path to cave path
                    (session_gitdir / "objects" / "info" / "alternates").write_text(
                        f"/projects/.shared-objects/{slug}\n")
                    (upper / ".git").write_text(f"gitdir: /projects/.gitdirs/{slug}\n")
                    bwrap_args += ["--bind", str(session_gitdir), f"/projects/.gitdirs/{slug}"]
                    # Shared objects accessible via alternates inside the cave
                    bwrap_args += ["--ro-bind", str(shared_gitdir / "objects"),
                                   f"/projects/.shared-objects/{slug}"]
            uid, gid = os.getuid(), os.getgid()
            subprocess.run(
                ["fuse-overlayfs",
                 "-o", f"lowerdir={source},upperdir={upper},workdir={work},"
                       f"squash_to_uid={uid},squash_to_gid={gid}",
                 str(merged)],
                check=True, capture_output=True,
            )
            overlay_mounts.append(str(merged))
            bwrap_args += ["--bind", str(merged), f"/projects/{slug}"]
    except BaseException:
        for merged in overlay_mounts:
            unmount_fuse(merged)
        force_rmtree(overlay_base)
        raise

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
    os.environ["CODING_CAVE_SESSION_ID"] = sid

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
        "CODING_CAVE_NIX_MERGED", "CODING_CAVE_NIX_DB_DIR",
        "CODING_CAVE_BWRAP_ARGS",
        "CODING_CAVE_SESSION_ID",
    }
    bwrap_env = {k: v for k, v in os.environ.items() if k in BWRAP_ENV_ALLOWLIST}

    if inside_cave:
        proc = subprocess.Popen(
            [binary] + command,
            start_new_session=False,
            env=bwrap_env,
        )
    else:
        # pasta provides an isolated network namespace with outbound-only access.
        # --quiet: suppress startup messages
        # --config-net: pasta directly configures the network interface (no DHCP wait)
        # --map-host-loopback 192.0.2.1: disable gateway→host-loopback mapping
        #   (pasta by default maps the gateway to host loopback, which would allow
        #    access to host localhost services like 1Password, SSH agent)
        # -T none -U none: disable all TCP/UDP port forwarding
        proc = subprocess.Popen(
            ["pasta", "--quiet", "--config-net",
             "--map-host-loopback", "192.0.2.1",
             "-T", "none", "-U", "none", "--", binary] + command,
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
        merge_session_gitdirs(sid, overlay_base)
        cleanup_session(sid)
        for merged in overlay_mounts:
            unmount_fuse(merged)
        force_rmtree(overlay_base)
        # Clean shared nix overlay when last session exits
        if not list_sessions():
            unmount_fuse(nix_merged)
            for subdir in ["upper", "work", "merged", "db"]:
                force_rmtree(NIX_OVERLAY_DIR / subdir)

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
    # pasta wraps bwrap, so bwrap_pid is pasta and child_pid is bwrap.
    # bwrap's namespaces (mount, user, etc.) belong to bwrap's child, not bwrap itself.
    inner_pid = find_bwrap_child(child_pid)
    if inner_pid is not None:
        child_pid = inner_pid

    shell_bin = "/bin/zsh" if shell_type == "zsh" else "/bin/bash"

    # Source cave-env (written at bwrap entry) for the full cave environment,
    # then launch the user's shell. /tmp/cave-env is per-session (tmpfs inside
    # bwrap), so it's safe from cross-session tampering.
    proc = subprocess.Popen([
        "nsenter",
        f"--target={child_pid}",
        "--user", "--mount", "--net", "--pid", "--uts", "--ipc",
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

def sync_live_sessions(slug):
    """Forward refs from running sessions into the shared gitdir.

    Uses forward_session_refs which handles alternates rewrite/restore
    internally and never force-updates refs.
    """
    shared_gitdir = PROJECTS_DIR / slug / "gitdir"
    if not shared_gitdir.is_dir():
        return
    for session in list_sessions():
        sid = session["sid"]
        overlay_base = CAVE_DATA / "session-overlays" / sid
        session_gitdir = overlay_base / f"gitdir-{slug}"
        if session_gitdir.is_dir():
            forward_session_refs(slug, session_gitdir, shared_gitdir)


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

    sync_live_sessions(slug)
    ensure_cave_remote(project_dir, gitdir)

    host_git = ["git", "-C", str(project_dir)] + SAFE_GIT_OPTS

    # Clean up stale rebase state
    host_git_dir = project_dir / ".git"
    if (host_git_dir / "rebase-merge").is_dir() or (host_git_dir / "rebase-apply").is_dir():
        print(f"Aborting stale rebase in {slug}.", file=sys.stderr)
        subprocess.run(host_git + ["rebase", "--abort"], capture_output=True)
    cur_branch = subprocess.run(
        host_git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if cur_branch.returncode == 0 and cur_branch.stdout.strip() == "_cave_pull":
        branches = subprocess.run(
            host_git + ["branch", "--format=%(refname:short)"],
            capture_output=True, text=True,
        )
        for b in branches.stdout.strip().splitlines():
            if b != "_cave_pull":
                subprocess.run(host_git + ["checkout", b], capture_output=True)
                break
    subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

    branch_result = subprocess.run(
        host_git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    gitdir_head_result = git(gitdir, "rev-parse", "HEAD",
        capture_output=True, text=True)

    if gitdir_head_result.returncode != 0:
        print(f"No commits in cave gitdir for {slug} — nothing to pull.", file=sys.stderr)
        return

    gitdir_head = gitdir_head_result.stdout.strip()

    # Host repo has no commits yet — fetch and create branch from cave
    if branch_result.returncode != 0:
        branch = git(gitdir, "rev-parse", "--abbrev-ref", "HEAD",
            capture_output=True, text=True).stdout.strip()
        if not branch or branch == "HEAD":
            branch = "main"
        subprocess.run(host_git + ["fetch", str(gitdir), "HEAD"], check=True, capture_output=True)
        subprocess.run(host_git + ["update-ref", f"refs/heads/{branch}", gitdir_head], check=True)
        subprocess.run(host_git + ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
        subprocess.run(host_git + ["checkout", branch], capture_output=True)
        print(f"Pulled {slug} (initialized from cave):", file=sys.stderr)
        subprocess.run(host_git + ["log", "--oneline"], check=False)
        update_cave_sync(gitdir, project_dir, gitdir_head, branch=branch)
        return

    branch = branch_result.stdout.strip()
    host_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if host_head == gitdir_head:
        print(f"No new commits to pull from {slug}.", file=sys.stderr)
        update_cave_sync(gitdir, project_dir, host_head, branch=branch)
        return

    # Force with matching trees: adopt cave's history
    if force:
        gitdir_tree = git(gitdir, "rev-parse", "HEAD^{tree}",
            capture_output=True, text=True)
        host_tree = subprocess.run(
            host_git + ["rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        if (gitdir_tree.returncode == 0 and host_tree.returncode == 0 and
                gitdir_tree.stdout.strip() == host_tree.stdout.strip()):
            subprocess.run(host_git + ["fetch", str(gitdir), "HEAD"], capture_output=True)
            subprocess.run(
                host_git + ["update-ref", f"refs/heads/{branch}", gitdir_head],
                check=True,
            )
            print(f"Force-updated {slug} (history rewritten, tree unchanged):", file=sys.stderr)
            print(f"  {host_head[:8]} -> {gitdir_head[:8]}", file=sys.stderr)
            update_cave_sync(gitdir, project_dir, gitdir_head, branch=branch)
            return

    fetch = subprocess.run(host_git + ["fetch", str(gitdir), "HEAD"], capture_output=True)
    if fetch.returncode != 0:
        print(f"Fetch from gitdir failed for {slug}.", file=sys.stderr)
        sys.exit(1)

    cave_sync = git(gitdir, "rev-parse", "refs/cave-sync",
        capture_output=True, text=True)
    cave_sync_hash = cave_sync.stdout.strip() if cave_sync.returncode == 0 else None

    merge_base_result = subprocess.run(
        host_git + ["merge-base", "HEAD", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if merge_base_result.returncode != 0:
        host_tree = subprocess.run(
            host_git + ["rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        merge_base = find_tree_merge_base(host_git, host_tree)
        if merge_base is None:
            print(f"No common ancestor for {slug} — skipping.", file=sys.stderr)
            return
    else:
        merge_base = merge_base_result.stdout.strip()

    count = subprocess.run(
        host_git + ["rev-list", "--count", f"{merge_base}..FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if count == "0":
        print(f"No commits to pull from {slug}.", file=sys.stderr)
        return

    def _parse_log(git_cmd, range_spec):
        """Parse 'git log' output into [(hash, tree, subject), ...]."""
        result = subprocess.run(
            git_cmd + ["log", "--format=%h %T %s", range_spec],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        entries = []
        for line in result.stdout.rstrip().splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 2:
                entries.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
        return entries

    def _print_rewrite_diff(host_git_cmd, merge_base_hash):
        """Show what changed: amended commits (old -> new hash) and new commits."""
        subprocess.run(
            host_git_cmd + ["fetch", str(gitdir), "HEAD"],
            capture_output=True,
        )
        old = _parse_log(host_git_cmd, f"{merge_base_hash}..HEAD")
        new = _parse_log(host_git_cmd, f"{merge_base_hash}..FETCH_HEAD")
        _print_rewrite_summary(old, new)

    def _print_rewrite_summary(old_entries, new_entries):
        # Match by subject first, then by tree hash
        old_by_subject = {}
        old_by_tree = {}
        for h, tree, subj in old_entries:
            old_by_subject[subj] = h
            old_by_tree[tree] = (h, subj)
        for h, tree, subj in new_entries:
            old_h = old_by_subject.pop(subj, None)
            if old_h and old_h != h:
                print(f"  amend {old_h} -> {h} {subj}", file=sys.stderr)
                old_by_tree.pop(tree, None)
            elif old_h:
                print(f"  {h} {subj}", file=sys.stderr)
                old_by_tree.pop(tree, None)
            elif tree in old_by_tree:
                old_h, old_subj = old_by_tree.pop(tree)
                print(f"  amend {old_h} -> {h} {subj}", file=sys.stderr)
            else:
                print(f"  + {h} {subj}", file=sys.stderr)

    # Detect history rewrite: cave amended/squashed commits since last sync.
    if (cave_sync_hash and cave_sync_hash == host_head
            and merge_base != host_head):
        if not force:
            host_count = subprocess.run(
                host_git + ["rev-list", "--count", f"{merge_base}..HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            print(f"Cave rewrote history for {slug} ({host_count} -> {count} commit(s)).", file=sys.stderr)
            _print_rewrite_diff(host_git, merge_base)
            print(f"Run 'coding-cave pull --force' to adopt.", file=sys.stderr)
            return

        host_count = subprocess.run(
            host_git + ["rev-list", "--count", f"{merge_base}..HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        stash_result = subprocess.run(
            host_git + ["stash", "--include-untracked"],
            capture_output=True, text=True,
        )
        stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

        old_entries = _parse_log(host_git, f"{merge_base}..HEAD")

        subprocess.run(host_git + ["reset", "--hard", merge_base], check=True, capture_output=True)
        subprocess.run(host_git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True, capture_output=True)
        subprocess.run(host_git + [
            "rebase", "--onto", branch, merge_base, "_cave_pull",
            "--no-reapply-cherry-picks",
            "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat -c core.fsmonitor= commit --amend --reset-author --allow-empty --no-edit --quiet",
        ], check=True, capture_output=True)
        subprocess.run(host_git + ["checkout", branch], check=True, capture_output=True)
        subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"], check=True, capture_output=True)
        subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

        if stashed:
            subprocess.run(host_git + ["stash", "pop"], capture_output=True)

        print(f"Pulled {slug} ({host_count} -> {count} commit(s)):", file=sys.stderr)
        new_entries = _parse_log(host_git, f"{merge_base}..HEAD")
        _print_rewrite_summary(old_entries, new_entries)

        host_head = subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        sync_refs(gitdir, project_dir, host_head, branch)
        return

    pre_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    stash_result = subprocess.run(
        host_git + ["stash", "--include-untracked"],
        capture_output=True, text=True,
    )
    stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

    subprocess.run(host_git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True, capture_output=True)
    result = subprocess.run(host_git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--no-reapply-cherry-picks",
        "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat -c core.fsmonitor= commit --amend --reset-author --allow-empty --no-edit --quiet",
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
            subprocess.run(host_git + ["rebase", "--abort"], capture_output=True)
            subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)
            if stashed:
                subprocess.run(host_git + ["stash", "pop"], capture_output=True)
            sys.exit(1)

    subprocess.run(host_git + ["checkout", branch], capture_output=True, check=True)
    subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"], capture_output=True, check=True)
    subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

    if stashed:
        subprocess.run(host_git + ["stash", "pop"], capture_output=True)

    actual = subprocess.run(
        host_git + ["rev-list", "--count", f"{pre_head}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if actual != "0":
        print(f"Pulled {actual} commit(s) from {slug}:", file=sys.stderr)
        log = subprocess.run(
            host_git + ["log", "--oneline", f"{pre_head}..HEAD"],
            capture_output=True, text=True,
        )
        if log.returncode == 0 and log.stdout.strip():
            print(log.stdout.rstrip(), file=sys.stderr)

    host_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
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

    sync_live_sessions(slug)
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

    cave_head = git(gitdir, "rev-parse", "HEAD",
        capture_output=True, text=True)
    if cave_head.returncode == 0 and cave_head.stdout.strip() == host_head:
        print(f"No commits to push to {slug}.", file=sys.stderr)
        update_cave_sync(gitdir, project_dir, host_head, branch=branch)
        return

    # Check for unpulled cave commits
    if not force and cave_head.returncode == 0:
        cave_head_hash = cave_head.stdout.strip()
        git(gitdir, "fetch", str(project_dir), "HEAD",
            capture_output=True)
        merge_base = git(gitdir, "merge-base",
             cave_head_hash, host_head,
            capture_output=True, text=True)
        if merge_base.returncode == 0:
            unpulled = git(gitdir, "rev-list", "--count",
                 f"{merge_base.stdout.strip()}..{cave_head_hash}",
                capture_output=True, text=True)
            if unpulled.returncode == 0 and unpulled.stdout.strip() != "0":
                count = unpulled.stdout.strip()
                print(f"Cave has {count} unpulled commit(s) that would be lost:", file=sys.stderr)
                git(gitdir, "log", "--oneline",
                     f"{merge_base.stdout.strip()}..{cave_head_hash}")
                print(f"Run 'coding-cave pull' first, or use 'coding-cave push --force'.", file=sys.stderr)
                sys.exit(1)

    sync_refs(gitdir, project_dir, host_head, branch)
    print(f"Pushed to {slug} ({host_head[:8]}).", file=sys.stderr)


def unmount_fuse(mountpoint):
    """Lazy-unmount a fuse mount.  Returns True if fusermount3 succeeded."""
    r = subprocess.run(["fusermount3", "-uz", str(mountpoint)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Most likely cause: non-setuid fusermount3 from nix store
        # shadows the system's setuid one.  Try the system path.
        for p in ["/run/wrappers/bin/fusermount3", "/usr/bin/fusermount3"]:
            if os.path.isfile(p):
                r = subprocess.run([p, "-uz", str(mountpoint)],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    return True
        print(f"fusermount3 -uz {mountpoint}: exit {r.returncode}: "
              f"{r.stderr.strip()}", file=sys.stderr)
    return r.returncode == 0


def unmount_all_under(path):
    """Unmount any fuse mounts inside path (deepest first)."""
    path = Path(path).resolve()
    mounts = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                mp = line.split()[1]
                mp_path = Path(mp)
                if mp_path == path or (mp_path.is_relative_to(path)):
                    mounts.append(mp)
    except OSError:
        return
    # Unmount deepest first to avoid "busy" on parent mounts
    for mp in sorted(mounts, key=len, reverse=True):
        unmount_fuse(mp)


def force_rmtree(path):
    """Remove a directory tree, unmounting and forcing permissions as needed."""
    path = Path(path)
    if not path.exists():
        return
    unmount_all_under(path)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                (Path(dirpath) / name).chmod(0o700)
            except OSError:
                pass
    try:
        shutil.rmtree(path)
    except OSError as e:
        print(f"Warning: could not remove {path}: {e}", file=sys.stderr)


def reset_project_overlay(slug):
    """Reset a project: wipe overlays and recreate gitdir."""
    projects = load_projects()
    proj_state = PROJECTS_DIR / slug

    for subdir in ["upper", "work", "gitdir", "sessions"]:
        p = proj_state / subdir
        if p.exists():
            force_rmtree(p)
            if subdir not in ("gitdir", "sessions"):
                p.mkdir()

    if slug in projects:
        initialize_gitdir(slug, Path(projects[slug]))


# --- Commands ---

def cmd_agent(args, gpu=False, continue_session=False, resume=None, claude_args=None):
    """Start a new agent session."""
    global_config = load_global_config()
    setup_env(global_config)

    project_dir = detect_project_from_cwd()
    if project_dir is None:
        print("No project. Run 'coding-cave add' or cd into a project directory.", file=sys.stderr)
        sys.exit(1)
    project = ensure_project(project_dir)

    workdir = cave_workdir(project)
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

    start_session(project, command, gpu=gpu)


def cmd_shell(args, gpu=False):
    """Enter cave with a shell — join existing session or start new."""
    global_config = load_global_config()
    setup_env(global_config)

    shell_type = Path(os.environ.get("SHELL", "/bin/bash")).name

    project_dir = detect_project_from_cwd()
    if project_dir is None:
        print("No project. Run 'coding-cave add' or cd into a project directory.", file=sys.stderr)
        sys.exit(1)
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
        gitdir = PROJECTS_DIR / slug / "gitdir"
        if gitdir.exists():
            print(f"{slug} is already in the coding cave.", file=sys.stderr)
            return
        # Re-initialize missing gitdir
        initialize_gitdir(slug, project_dir)

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
    force_rmtree(PROJECTS_DIR / slug)

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


def clean_stale_session_overlays():
    """Remove session overlay dirs that have no running session."""
    overlay_root = CAVE_DATA / "session-overlays"
    if not overlay_root.exists():
        return
    running = {s["sid"] for s in list_sessions()}
    for entry in overlay_root.iterdir():
        if entry.is_dir() and entry.name not in running:
            force_rmtree(entry)


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
        cave_head = git(gitdir, "rev-parse", "HEAD",
            capture_output=True, text=True)
        if cave_head.returncode == 0:
            merge_base_result = subprocess.run(
                ["git", "-C", str(project_dir), "merge-base", "HEAD",
                 cave_head.stdout.strip()],
                capture_output=True, text=True,
            )
            if merge_base_result.returncode == 0:
                unpulled = git(gitdir, "rev-list", "--count",
                     f"{merge_base_result.stdout.strip()}..HEAD",
                    capture_output=True, text=True)
                if (unpulled.returncode == 0 and unpulled.stdout.strip() != "0"):
                    log = git(gitdir, "log", "--oneline",
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

    reset_project_overlay(slug)
    clean_stale_session_overlays()
    print(f"Reset overlay for {slug}. Next session will see current host state.", file=sys.stderr)


def find_unsynced_project(slug):
    """Check if a cave-originated project exists in cave home."""
    cave_repo = CAVE_HOME / "unsynced-projects" / slug
    if cave_repo.is_dir() and (cave_repo / ".git").is_dir():
        return cave_repo
    return None


def pull_cave_project(slug):
    """Pull a cave-originated project to the host for the first time."""
    cave_repo = find_unsynced_project(slug)
    if cave_repo is None:
        print(f"No cave-originated project named {slug}.", file=sys.stderr)
        sys.exit(1)

    host_dir = Path.cwd().resolve() / slug
    if host_dir.exists():
        print(f"Directory already exists: {host_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        answer = input(f"Clone cave project to {host_dir}? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("Aborted.", file=sys.stderr)
        sys.exit(0)

    subprocess.run(
        ["git", "clone", str(cave_repo), str(host_dir)],
        check=True,
    )
    # Remove the clone's origin (points to cave home, not useful)
    subprocess.run(
        ["git", "-C", str(host_dir), "remote", "remove", "origin"],
        capture_output=True,
    )

    # Register as a normal project with gitdir + overlay
    ensure_project(host_dir)
    print(f"Pulled cave project {slug} to {host_dir}", file=sys.stderr)
    print(f"Next cave session will mount it at /projects/{slug}.", file=sys.stderr)


def cmd_pull(args):
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if args:
        slug = args[0]
        projects = load_projects()
        if slug not in projects and find_unsynced_project(slug):
            pull_cave_project(slug)
            return
        pull_project(slug, force=force)
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
    cave_head = git(gitdir, "rev-parse", "HEAD",
        capture_output=True, text=True)
    if host_head.returncode != 0 and cave_head.returncode != 0:
        return None  # both empty, nothing to compare
    if host_head.returncode != 0 or cave_head.returncode != 0:
        return "?"

    host_h = host_head.stdout.strip()
    cave_h = cave_head.stdout.strip()

    tags = []

    if host_h != cave_h:
        git(gitdir, "fetch", str(project_dir), "HEAD",
            capture_output=True)
        mb = git(gitdir, "merge-base", cave_h, host_h,
            capture_output=True, text=True)
        if mb.returncode != 0:
            tags.append("diverged")
        else:
            base = mb.stdout.strip()
            to_pull = git(gitdir, "rev-list", "--count",
                 f"{base}..{cave_h}",
                capture_output=True, text=True)
            to_push = git(gitdir, "rev-list", "--count",
                 f"{base}..{host_h}",
                capture_output=True, text=True)
            n_pull = to_pull.stdout.strip() if to_pull.returncode == 0 else "0"
            n_push = to_push.stdout.strip() if to_push.returncode == 0 else "0"
            if n_pull != "0" and n_push != "0":
                tags.append(f"{n_pull} to pull, {n_push} to push")
            elif n_pull != "0":
                tags.append(f"{n_pull} to pull")
            elif n_push != "0":
                tags.append(f"{n_push} to push")

    return ", ".join(tags) if tags else ""


def cmd_cp(srcs, dest):
    """Copy files between host and a running cave session's overlay."""
    session_overlays = CAVE_DATA / "session-overlays"

    def resolve_cave_path(spec):
        """Parse 'slug:path', ':path', or 'cave:path'. Returns (slug, path) or (None, None)."""
        if ":" not in spec:
            return None, None
        slug, _, path = spec.partition(":")
        if not slug or slug == "cave":
            # Auto-detect from CWD
            slug = None
            cwd = Path.cwd().resolve()
            for s, source in load_projects().items():
                if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
                    slug = s
                    break
            if not slug:
                print("No project specified and CWD is not a registered project.", file=sys.stderr)
                sys.exit(1)
        return slug, path or "."

    def find_session_merged(slug):
        """Find the overlay merged dir for a running session of this project."""
        sessions = list_sessions(project=slug)
        if not sessions:
            print(f"No running sessions for {slug}.", file=sys.stderr)
            sys.exit(1)
        if len(sessions) > 1:
            print("Multiple sessions running:", file=sys.stderr)
            for i, s in enumerate(sessions):
                started = time.strftime("%H:%M", time.localtime(s["started"]))
                print(f"  [{i}] [{started}] PID {s['bwrap_pid']}", file=sys.stderr)
            try:
                choice = int(input("Session: ").strip())
                sid = sessions[choice]["sid"]
            except (ValueError, IndexError, EOFError):
                print("Invalid choice.", file=sys.stderr)
                sys.exit(1)
        else:
            sid = sessions[0]["sid"]
        merged = session_overlays / sid / f"{slug}-merged"
        if not merged.is_dir():
            print(f"Session overlay not found: {merged}", file=sys.stderr)
            sys.exit(1)
        return merged

    # Determine direction: host→cave or cave→host
    dest_slug, dest_path = resolve_cave_path(dest)
    src_slug, src_path = resolve_cave_path(srcs[0]) if len(srcs) == 1 else (None, None)

    if dest_slug:
        # host → cave: cp <host-files>... <slug>:<cave-path>
        merged = find_session_merged(dest_slug)
        cave_dest = merged / dest_path.lstrip("/")
        cave_dest.parent.mkdir(parents=True, exist_ok=True)
        for src in srcs:
            src_path_obj = Path(src).resolve()
            if not src_path_obj.exists():
                print(f"Source not found: {src}", file=sys.stderr)
                sys.exit(1)
            if src_path_obj.is_dir():
                target = cave_dest / src_path_obj.name if cave_dest.is_dir() else cave_dest
                shutil.copytree(src_path_obj, target, dirs_exist_ok=True)
            else:
                target = cave_dest / src_path_obj.name if cave_dest.is_dir() else cave_dest
                shutil.copy2(src_path_obj, target)
            print(f"  {src} -> {dest}", file=sys.stderr)
    elif src_slug:
        # cave → host: cp <slug>:<cave-path> <host-dest>
        merged = find_session_merged(src_slug)
        cave_src = merged / src_path.lstrip("/")
        if not cave_src.exists():
            print(f"Source not found in cave: {src_path}", file=sys.stderr)
            sys.exit(1)
        host_dest = Path(dest).resolve()
        host_dest.parent.mkdir(parents=True, exist_ok=True)
        if cave_src.is_dir():
            target = host_dest / cave_src.name if host_dest.is_dir() else host_dest
            shutil.copytree(cave_src, target, dirs_exist_ok=True)
        else:
            target = host_dest / cave_src.name if host_dest.is_dir() else host_dest
            shutil.copy2(cave_src, target)
        print(f"  {srcs[0]} -> {dest}", file=sys.stderr)
    else:
        print("One of source or destination must use cave: or slug:path syntax.", file=sys.stderr)
        print("  coding-cave cp <file>... cave:              host → cave (project root)", file=sys.stderr)
        print("  coding-cave cp <file>... cave:<path>        host → cave (subpath)", file=sys.stderr)
        print("  coding-cave cp cave:<path> <dest>           cave → host", file=sys.stderr)
        sys.exit(1)


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
        description="Sandboxed Claude Code agent sessions with Nix environments. Use -- to pass extra arguments to claude.",
    )
    parser.add_argument("--version", action="version", version=f"coding-cave {VERSION}")
    parser.add_argument("--gpu", action="store_true", help="enable GPU access")
    parser.add_argument("-c", "--continue", action="store_true", dest="continue_session", help="continue most recent conversation")
    parser.add_argument("-r", "--resume", nargs="?", const=True, default=None, metavar="SESSION_ID", help="resume a conversation by session ID, or interactive picker")
    sub = parser.add_subparsers(dest="command")

    p_agent = sub.add_parser("agent", help="start a new agent session")
    p_agent.add_argument("-c", "--continue", action="store_true", dest="continue_session", help="continue most recent conversation")
    p_agent.add_argument("-r", "--resume", nargs="?", const=True, default=None, metavar="SESSION_ID", help="resume a conversation by session ID, or interactive picker")
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

    p_cp = sub.add_parser("cp", help="copy files to/from a running cave session")
    p_cp.add_argument("paths", nargs="+", help="source(s) and destination (use slug:path for cave)")

    p_move = sub.add_parser("move", help="update a project's source path")
    p_move.add_argument("old_slug", help="current project name")
    p_move.add_argument("new_path", help="new directory path")

    return parser


def main():
    # Split on -- to extract claude passthrough args
    argv = sys.argv[1:]
    claude_args = []
    if "--" in argv:
        idx = argv.index("--")
        claude_args = argv[idx + 1:]
        argv = argv[:idx]

    parser = build_parser()
    parsed = parser.parse_args(argv)

    if parsed.command is None:
        cmd_agent([], gpu=parsed.gpu, continue_session=parsed.continue_session, resume=parsed.resume, claude_args=claude_args)
    elif parsed.command == "agent":
        cmd_agent([], gpu=parsed.gpu, continue_session=parsed.continue_session, resume=parsed.resume, claude_args=claude_args)
    elif parsed.command == "shell":
        cmd_shell(claude_args, gpu=parsed.gpu)
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
    elif parsed.command == "cp":
        if len(parsed.paths) < 2:
            print("Usage: coding-cave cp <src>... <dest>", file=sys.stderr)
            sys.exit(1)
        cmd_cp(parsed.paths[:-1], parsed.paths[-1])
    elif parsed.command == "move":
        cmd_move(parsed.old_slug, parsed.new_path)


if __name__ == "__main__":
    main()
