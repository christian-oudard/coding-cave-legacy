"""Session lifecycle: start, wait, nsenter."""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import _state, config, projects, sessions, git as git_mod, gitdir, overlay


# CODING_CAVE_* vars visible at runtime inside the cave.
# Referenced by both start_session (bwrap allowlist) and enter_session (nsenter filter).
_CAVE_RUNTIME_VARS = {
    "CODING_CAVE_VERSION",
    "CODING_CAVE_PROJECT_PATH",
    "CODING_CAVE_SESSION_INDEX",
    "CODING_CAVE_SESSION_ID",
}

# Sanitized environment: only vars the bwrap runtime actually needs.
# Everything else (host secrets, user identity, etc.) is dropped.
# This prevents /proc/1/environ inside the cave from leaking host env.
BWRAP_ENV_ALLOWLIST = {
    # Runtime script needs these to function
    "HOME", "PATH", "TERM",
    # Forwarded into cave via try-fwd-env
    "COLORTERM",
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
    # Git identity (try-fwd-env -> wrap-entry, then unset)
    "CODING_CAVE_GIT_NAME", "CODING_CAVE_GIT_EMAIL",
    "CODING_CAVE_GPG_AGENT_SOCK",
    # Proxy (onecli credential injection)
    "CODING_CAVE_PROXY_ADDR",
    # Host-side only (add-runtime reads from process env)
    "CODING_CAVE_NIX_MERGED", "CODING_CAVE_NIX_DB_DIR",
    "CODING_CAVE_BWRAP_ARGS", "CODING_CAVE_USER_CLAUDE_MD",
    # Setup-only (try-fwd-env -> wrap-entry, then unset)
    "CODING_CAVE_EXTRA_PATH",
} | _CAVE_RUNTIME_VARS


def _merge_nix_db(host_db_path, cave_db_path):
    """Incrementally add new host nix DB entries into the cave DB.

    Copies ValidPaths rows (and their Refs) that exist in the host DB
    but not in the cave DB. Preserves cave-only paths (packages installed
    inside the cave via nix shell).
    """
    src = sqlite3.connect(f"file:{host_db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(cave_db_path))
    try:
        # Find paths in host but not in cave
        cave_paths = {r[0] for r in dst.execute("SELECT path FROM ValidPaths")}
        host_rows = src.execute(
            "SELECT id, path, hash, registrationTime, deriver, narSize, "
            "ultimate, sigs, ca FROM ValidPaths"
        ).fetchall()
        new_rows = [r for r in host_rows if r[1] not in cave_paths]
        if not new_rows:
            return

        # Insert new paths, track host_id -> cave_id mapping
        id_map = {}
        for host_id, path, h, reg_time, deriver, nar_size, ult, sigs, ca in new_rows:
            dst.execute(
                "INSERT OR IGNORE INTO ValidPaths "
                "(path, hash, registrationTime, deriver, narSize, ultimate, sigs, ca) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (path, h, reg_time, deriver, nar_size, ult, sigs, ca),
            )
            cave_id = dst.execute(
                "SELECT id FROM ValidPaths WHERE path = ?", (path,)
            ).fetchone()
            if cave_id:
                id_map[host_id] = cave_id[0]

        # Build full host_id -> cave_id map (including pre-existing paths)
        for r in host_rows:
            if r[0] not in id_map:
                cave_id = dst.execute(
                    "SELECT id FROM ValidPaths WHERE path = ?", (r[1],)
                ).fetchone()
                if cave_id:
                    id_map[r[0]] = cave_id[0]

        # Copy Refs for newly inserted paths
        new_host_ids = [h_id for h_id in id_map if h_id in {r[0] for r in new_rows}]
        if new_host_ids:
            placeholders = ",".join("?" * len(new_host_ids))
            refs = src.execute(
                f"SELECT referrer, reference FROM Refs WHERE referrer IN ({placeholders})",
                new_host_ids,
            ).fetchall()
            for referrer, reference in refs:
                cave_referrer = id_map.get(referrer)
                cave_reference = id_map.get(reference)
                if cave_referrer and cave_reference:
                    dst.execute(
                        "INSERT OR IGNORE INTO Refs (referrer, reference) VALUES (?, ?)",
                        (cave_referrer, cave_reference),
                    )

        dst.commit()
    finally:
        src.close()
        dst.close()


def _build_mirror_args():
    """Build bwrap args for dotfile and binary mirroring.

    IMPORTANT: All bind-mount destinations must be outside /nix/store.
    These args are appended after the sandbox runtime args, but bwrap
    processes them before the nix store overlay is fully set up.
    """
    args = []

    # Zsh dotfiles
    zdotdir = os.environ.get("CODING_CAVE_ZDOTDIR", "")
    if zdotdir and Path(zdotdir).is_dir():
        zsh_cave = f"{_state.JAIL_HOME}/.config/zsh"
        persistent_zdotdir = _state.cave_data() / "zdotdir"
        persistent_zdotdir.mkdir(parents=True, exist_ok=True)
        args += ["--bind", str(persistent_zdotdir), zsh_cave]
        for f in sorted(Path(zdotdir).glob(".z*")):
            if not f.is_file():
                continue
            if "private" in f.name or "secret" in f.name:
                continue
            if f.name.startswith(".zhistory") or f.name.startswith(".zcompdump"):
                dest = persistent_zdotdir / f.name
                if not dest.exists():
                    dest.touch()
            else:
                args += ["--ro-bind", str(f), f"{zsh_cave}/{f.name}"]
        for f in sorted(Path(zdotdir).iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if "private" in f.name or "secret" in f.name:
                continue
            args += ["--ro-bind", str(f), f"{zsh_cave}/{f.name}"]
        args += ["--setenv", "ZDOTDIR", zsh_cave]

    # Bash dotfiles
    bashrc = os.environ.get("CODING_CAVE_BASHRC", "")
    if bashrc and Path(bashrc).is_file():
        args += ["--ro-bind", bashrc, f"{_state.JAIL_HOME}/.bashrc"]
    bash_profile = os.environ.get("CODING_CAVE_BASH_PROFILE", "")
    if bash_profile and Path(bash_profile).is_file():
        args += ["--ro-bind", bash_profile, f"{_state.JAIL_HOME}/.bash_profile"]

    # Neovim config and data (plugins)
    nvim_config = os.environ.get("CODING_CAVE_NVIM_CONFIG", "")
    if nvim_config and Path(nvim_config).is_dir():
        args += ["--ro-bind", nvim_config, f"{_state.JAIL_HOME}/.config/nvim"]
    nvim_data = os.environ.get("CODING_CAVE_NVIM_DATA", "")
    if nvim_data and Path(nvim_data).is_dir():
        args += ["--ro-bind", nvim_data, f"{_state.JAIL_HOME}/.local/share/nvim"]
    nvim_host_bin = os.environ.get("CODING_CAVE_NVIM_HOST_BIN", "")
    if nvim_host_bin:
        local_bin = _state.cave_home() / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        args += ["--ro-bind", nvim_host_bin, f"{_state.JAIL_HOME}/.local/bin/nvim"]

    return args


def _setup_nix_store(sid, inside_cave):
    """Mount the nix store overlay and seed the nix DB.

    Returns the nix_merged path (str).  When inside_cave, returns
    "/nix/store" directly (parent's overlay is reused).
    """
    if inside_cave:
        return "/nix/store"

    nix_upper = _state.nix_overlay_dir() / "upper"
    nix_upper.mkdir(parents=True, exist_ok=True)

    # Remove stale upper entries that shadow host store paths.
    for entry in os.scandir(str(nix_upper)):
        if not entry.is_dir(follow_symlinks=False):
            continue
        host_path = Path("/nix/store") / entry.name
        if host_path.is_dir():
            try:
                contents = os.listdir(entry.path)
                xattrs = os.listxattr(entry.path)
                opaque = any("opaque" in x for x in xattrs)
                print(f"nix overlay: removing stale upper entry: "
                      f"{entry.name} (contents={contents}, "
                      f"opaque={opaque}, xattrs={xattrs})",
                      file=sys.stderr)
            except OSError:
                print(f"nix overlay: removing stale upper entry: "
                      f"{entry.name} (could not inspect)",
                      file=sys.stderr)
            def _force_remove(func, path, exc):
                parent = os.path.dirname(path)
                os.chmod(parent, 0o755)
                func(path)
            shutil.rmtree(entry.path, onexc=_force_remove)

    # Per-session work and merged dirs (clean FUSE cache each session)
    session_nix = _state.cave_data() / "session-overlays" / sid / "nix"
    nix_work = session_nix / "work"
    nix_work.mkdir(parents=True, exist_ok=True)
    nix_merged_dir = session_nix / "merged"
    nix_merged_dir.mkdir(parents=True, exist_ok=True)
    nix_merged = str(nix_merged_dir)
    subprocess.run(
        ["fuse-overlayfs",
         "-o", f"lowerdir=/nix/store,upperdir={nix_upper},workdir={nix_work}",
         nix_merged],
        check=True, capture_output=True,
    )

    # Seed nix DB from host so nix doesn't try to re-register
    # (chmod) existing store paths, which fails on overlayfs.
    host_nix_db = Path("/nix/var/nix/db/db.sqlite")
    nix_db_dir = _state.nix_overlay_dir() / "db"
    nix_db_dir.mkdir(parents=True, exist_ok=True)
    cave_nix_db = nix_db_dir / "db.sqlite"
    if host_nix_db.exists():
        try:
            if not cave_nix_db.exists():
                for wal in nix_db_dir.glob("db.sqlite-*"):
                    wal.unlink()
                src = sqlite3.connect(f"file:{host_nix_db}?mode=ro", uri=True)
                dst = sqlite3.connect(str(cave_nix_db))
                src.backup(dst)
                dst.close()
                src.close()
            else:
                _merge_nix_db(host_nix_db, cave_nix_db)
        except sqlite3.Error:
            pass
    if cave_nix_db.exists():
        os.environ["CODING_CAVE_NIX_DB_DIR"] = str(nix_db_dir)

    return nix_merged


_CAVE_SEND_HOOK = {"type": "command", "command": "cave-send hook"}
_CAVE_SEND_HOOK_EVENTS = ("Stop", "PostToolUse")


def _inject_cave_send_hooks(settings):
    """Ensure cave-send hook is registered on IPC-relevant hook events.

    Appends a cave-send hook entry to each event's hook list if not already
    present, preserving any user-configured hooks.
    """
    hooks = settings.setdefault("hooks", {})
    for event in _CAVE_SEND_HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        # Find or create the catch-all matcher group (matcher "")
        group = None
        for entry in entries:
            if entry.get("matcher", "") == "":
                group = entry
                break
        if group is None:
            group = {"matcher": "", "hooks": []}
            entries.append(group)
        hook_list = group.setdefault("hooks", [])
        if not any(h.get("command") == "cave-send hook" for h in hook_list):
            hook_list.append(dict(_CAVE_SEND_HOOK))


def _apply_user_env(user_env):
    """Apply user environment from cave.nix.

    Merges settings into persistent home, copies skills.
    Returns bwrap args for packages, env vars, and store path binds.
    """
    if not user_env:
        return []

    # Merge user settings into persistent home
    user_settings = user_env / "claude" / "settings.json"
    if user_settings.is_file():
        cave_home = _state.cave_home()
        claude_dir = cave_home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        existing = {}
        settings_file = claude_dir / "settings.json"
        if settings_file.exists():
            try:
                existing = json.loads(settings_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        try:
            user = json.loads(user_settings.read_text())
        except (json.JSONDecodeError, OSError):
            user = {}
        if user:
            existing.update(user)
        _inject_cave_send_hooks(existing)
        settings_file.write_text(json.dumps(existing, indent=2))

    args = []

    # Bind-mount user-env and its package closure directly.
    args += ["--ro-bind", str(user_env), str(user_env)]

    # Extra PATH entries from user packages
    packages_dir = user_env / "packages"
    if packages_dir.is_dir():
        extra_paths = []
        for pkg in sorted(packages_dir.iterdir()):
            if pkg.is_symlink():
                target = pkg.resolve()
                if str(target).startswith("/nix/store/"):
                    args += ["--ro-bind", str(target), str(target)]
            bin_dir = pkg / "bin"
            if bin_dir.is_dir():
                extra_paths.append(str(bin_dir))
        if extra_paths:
            args += ["--setenv", "CODING_CAVE_EXTRA_PATH",
                           ":".join(extra_paths)]

    # Skills from user env
    skills_dir = user_env / "claude" / "skills"
    if skills_dir.is_dir():
        cave_home = _state.cave_home()
        claude_skills = cave_home / ".claude" / "skills"
        claude_skills.mkdir(parents=True, exist_ok=True)
        for skill in skills_dir.iterdir():
            if skill.is_dir():
                dest = claude_skills / skill.name
                if not dest.exists():
                    shutil.copytree(skill, dest)

    # Extra env vars
    env_json = user_env / "claude" / "env.json"
    if env_json.is_file():
        try:
            extra_env = json.loads(env_json.read_text())
            for k, v in extra_env.items():
                args += ["--setenv", k, str(v)]
        except (json.JSONDecodeError, OSError):
            pass

    # User-declared home files (written to persistent home so they appear
    # at the correct path inside the cave; overwrite each session so Nix
    # store paths in the content stay current after rebuilds)
    files_dir = user_env / "files"
    if files_dir.is_dir():
        cave_home = _state.cave_home()
        for file_path in files_dir.rglob("*"):
            if file_path.is_file():
                rel = file_path.relative_to(files_dir)
                dest = cave_home / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.unlink(missing_ok=True)
                shutil.copy2(str(file_path), str(dest))

    return args


def _mount_project_overlays(sid, overlay_base, loaded_projects):
    """Mount fuse-overlayfs for each project and create session gitdirs.

    Returns (overlay_mounts, bwrap_args).
    """
    bwrap_args = []
    overlay_mounts = []

    for slug, source in loaded_projects.items():
        if not Path(source).is_dir():
            continue
        upper = overlay_base / f"{slug}-upper"
        work = overlay_base / f"{slug}-work"
        merged = overlay_base / f"{slug}-merged"
        # Restore saved upper from previous session if available.
        saved_upper = _state.projects_dir() / slug / "saved-upper"
        has_saved_upper = False
        if saved_upper.is_dir():
            saved_upper.rename(upper)
            has_saved_upper = True
        else:
            upper.mkdir(exist_ok=True)
        work.mkdir(exist_ok=True)
        merged.mkdir(exist_ok=True)
        shared_gitdir = _state.projects_dir() / slug / "gitdir"
        session_gitdir = None
        if shared_gitdir.is_dir():
            session_gitdir = gitdir.create_session_gitdir(slug, sid, overlay_base)
            if session_gitdir:
                (upper / ".git").write_text(f"gitdir: /projects/.gitdirs/{slug}\n")
                bwrap_args += ["--bind", str(session_gitdir), f"/projects/.gitdirs/{slug}"]
                bwrap_args += ["--ro-bind", str(shared_gitdir / "objects"),
                               f"/projects/.shared-objects/{slug}"]
                bwrap_args += ["--bind", str(shared_gitdir),
                               f"/projects/.shared-gitdirs/{slug}"]
                git_mod.git(shared_gitdir, "config",
                    "receive.denyCurrentBranch", "ignore",
                    capture_output=True)
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
        # Sync working tree to HEAD when shared gitdir has unpulled
        # commits and no saved upper exists.
        if session_gitdir and not has_saved_upper:
            host_tree = subprocess.run(
                ["git", "-C", source, "rev-parse", "HEAD^{tree}"],
                capture_output=True, text=True,
            )
            session_tree = git_mod.git(session_gitdir, "rev-parse",
                               "HEAD^{tree}",
                               capture_output=True, text=True)
            if (host_tree.returncode == 0
                    and session_tree.returncode == 0
                    and host_tree.stdout.strip()
                        != session_tree.stdout.strip()):
                subprocess.run(
                    ["git", "--git-dir", str(session_gitdir),
                     "--work-tree", str(merged)] + git_mod.SAFE_GIT_OPTS +
                    ["checkout", "-f"],
                    capture_output=True,
                )
        # Rewrite alternates from host path to cave path
        if session_gitdir:
            (session_gitdir / "objects" / "info" / "alternates").write_text(
                f"/projects/.shared-objects/{slug}\n")

    return overlay_mounts, bwrap_args


def _build_credential_args():
    """Build bwrap args for GPG, proxy CA, gitconfig, and passwd/group."""
    args = []

    # GPG agent socket forwarding (for commit signing without private key)
    gpg_sock = os.environ.get("CODING_CAVE_GPG_AGENT_SOCK", "")
    if gpg_sock and os.path.exists(gpg_sock):
        cave_gpg_sock = f"{_state.JAIL_HOME}/.gnupg/S.gpg-agent"
        args += ["--bind", gpg_sock, cave_gpg_sock]
    # GPG public keyring (read-only, for key lookup)
    gnupghome = os.environ.get("CODING_CAVE_GNUPGHOME", "")
    if gnupghome and os.path.isdir(gnupghome):
        cave_gnupghome = f"{_state.JAIL_HOME}/.gnupg"
        for fname in ("pubring.kbx", "pubring.gpg", "trustdb.gpg"):
            src = os.path.join(gnupghome, fname)
            if os.path.isfile(src):
                args += ["--ro-bind", src, f"{cave_gnupghome}/{fname}"]

    # Proxy CA certificate bind-mount (onecli MITM)
    proxy_ca = os.environ.get("CODING_CAVE_PROXY_CA_CERT", "")
    if proxy_ca and os.path.isfile(proxy_ca):
        args += ["--ro-bind", proxy_ca, "/etc/onecli-ca.pem"]

    # Git config mirroring
    gitconfig = os.environ.get("CODING_CAVE_GITCONFIG", "")
    if gitconfig and Path(gitconfig).is_file():
        args += ["--ro-bind", gitconfig, f"{_state.JAIL_HOME}/.gitconfig"]

    # Passwd/group files
    uid = os.getuid()
    gid = os.getgid()
    passwd_file = _state.cave_data() / "passwd"
    group_file = _state.cave_data() / "group"
    nologin = "/bin/false"
    passwd_file.write_text(
        f"root:x:0:0:System administrator:/root:{nologin}\n"
        f"cave-coder:x:{uid}:{gid}::{_state.JAIL_HOME}:{nologin}\n"
    )
    group_file.write_text(
        f"root:x:0:\n"
        f"cave-coder:x:{gid}:\n"
    )
    args += ["--ro-bind", str(passwd_file), "/etc/passwd"]
    args += ["--ro-bind", str(group_file), "/etc/group"]

    return args


def _strip_committed_from_upper(upper, session_gitdir):
    """Remove files from the overlay upper that already match HEAD.

    The overlay upper accumulates all file writes during a session,
    including files that were committed.  Stripping committed files
    prevents phantom diffs on the next session start.
    """
    head = git_mod.git(session_gitdir, "rev-parse", "HEAD",
                       capture_output=True, text=True)
    if head.returncode != 0:
        return
    ls_tree = git_mod.git(session_gitdir, "ls-tree", "-r",
                          head.stdout.strip(),
                          capture_output=True, text=True)
    if ls_tree.returncode != 0:
        return

    # Build path -> blob hash map from HEAD tree.
    head_blobs = {}
    for line in ls_tree.stdout.splitlines():
        if not line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3:
            head_blobs[path] = parts[2]

    # Remove files whose content matches HEAD.
    for root, dirs, files in os.walk(upper):
        for fname in files:
            fpath = Path(root) / fname
            rel = str(fpath.relative_to(upper))
            if rel == ".git":
                continue
            # Keep non-regular files (whiteouts, symlinks, etc.)
            if not fpath.is_file():
                continue
            if rel not in head_blobs:
                continue  # untracked, keep
            h = subprocess.run(
                ["git", "hash-object", str(fpath)],
                capture_output=True, text=True,
            )
            if h.returncode == 0 and h.stdout.strip() == head_blobs[rel]:
                fpath.unlink()

    # Clean up empty directories (bottom-up).
    for root, _dirs, _files in os.walk(upper, topdown=False):
        if root == str(upper):
            continue
        try:
            Path(root).rmdir()
        except OSError:
            pass


def _save_overlay_uppers(overlay_base, loaded_projects):
    """Save overlay uppers for session continuity.

    Preserves uncommitted changes, untracked files, and deletions.
    Must be called after unmount and before rmtree.  Skips uppers
    that only contain .git to avoid clobbering concurrent sessions.
    Strips files that match HEAD so committed changes don't create
    phantom diffs on the next session start.
    """
    for slug in loaded_projects:
        upper = overlay_base / f"{slug}-upper"
        if not upper.is_dir():
            continue
        # Strip committed files before checking emptiness.
        session_gitdir = overlay_base / f"gitdir-{slug}"
        if session_gitdir.is_dir():
            _strip_committed_from_upper(upper, session_gitdir)
        entries = set(os.listdir(upper))
        entries.discard(".git")
        if not entries:
            continue
        saved = _state.projects_dir() / slug / "saved-upper"
        overlay.force_rmtree(saved)
        try:
            upper.rename(saved)
        except OSError:
            pass


def start_session(project, command, gpu=False, reuse_index=False, mounted_projects=None, port=None):
    """Start a new bwrap session with per-session overlays.

    mounted_projects: dict of slug->source to mount. If None, mounts all.
    """
    _state.cave_data().mkdir(parents=True, exist_ok=True)
    sid = sessions.session_id()

    overlay.clean_stale_session_overlays()

    # Build user environment from cave.nix BEFORE mounting the nix overlay.
    # build_user_env() may produce new store paths that must exist in
    # /nix/store before fuse-overlayfs snapshots it.
    user_env = config.build_user_env()

    inside_cave = bool(os.environ.get("CODING_CAVE_VERSION"))
    nix_merged = _setup_nix_store(sid, inside_cave)
    os.environ["CODING_CAVE_NIX_MERGED"] = nix_merged

    loaded_projects = mounted_projects if mounted_projects is not None else projects.load_projects()

    # Ensure gitdirs exist for mounted projects
    for slug, source in loaded_projects.items():
        gitdir_path = _state.projects_dir() / slug / "gitdir"
        if not gitdir_path.exists() and (Path(source) / ".git").is_dir():
            print(f"Syncing {slug}...", file=sys.stderr)
        git_mod.initialize_gitdir(slug, Path(source))

    # Assemble bwrap args from all sources
    bwrap_args = _build_mirror_args()
    bwrap_args += _apply_user_env(user_env)
    bwrap_args += _build_credential_args()

    # Mount project overlays (with error cleanup)
    overlay_base = _state.cave_data() / "session-overlays" / sid
    overlay_base.mkdir(parents=True, exist_ok=True)
    try:
        overlay_mounts, project_args = _mount_project_overlays(
            sid, overlay_base, loaded_projects)
        bwrap_args += project_args
    except BaseException:
        if not inside_cave:
            overlay.unmount_fuse(nix_merged)
        overlay.force_rmtree(overlay_base)
        raise

    # Write bwrap args file (null-separated, for bwrap --args)
    _state.sessions_dir().mkdir(parents=True, exist_ok=True)
    args_file = _state.sessions_dir() / f"{sid}.bwrap-args"
    with open(args_file, "wb") as f:
        for arg in bwrap_args:
            f.write(arg.encode() + b"\0")
    os.environ["CODING_CAVE_BWRAP_ARGS"] = str(args_file)
    os.environ["CODING_CAVE_PROJECT_PATH"] = loaded_projects.get(project, "")
    saved_index = sessions.pop_last_index(project) if reuse_index else None
    session_index = saved_index or sessions._next_session_index(project)
    os.environ["CODING_CAVE_SESSION_INDEX"] = str(session_index)
    os.environ["CODING_CAVE_SESSION_ID"] = sid

    binary = _state.SANDBOX_GPU_BIN if gpu else _state.SANDBOX_BIN
    bwrap_env = {k: v for k, v in os.environ.items() if k in BWRAP_ENV_ALLOWLIST}

    if inside_cave:
        proc = subprocess.Popen(
            [binary] + command,
            start_new_session=False,
            env=bwrap_env,
        )
    else:
        # pasta provides an isolated network namespace with outbound-only access.
        proxy_port = os.environ.get("CODING_CAVE_PROXY_PORT")
        if proxy_port:
            loopback_addr = "10.0.2.2"
            bwrap_env["CODING_CAVE_PROXY_ADDR"] = f"{loopback_addr}:{proxy_port}"
            pasta_args = [
                "pasta", "--quiet", "--config-net",
                "--map-host-loopback", loopback_addr,
                "-T", proxy_port,
            ]
        else:
            # Disable gateway->host-loopback mapping (pasta maps it by default,
            # which would expose host localhost services)
            pasta_args = [
                "pasta", "--quiet", "--config-net",
                "--map-host-loopback", "192.0.2.1",
                "-T", "none",
            ]
        # --port: expose cave ports on host. "auto" = pasta auto-detection,
        # comma-separated ports = explicit list, None = no forwarding.
        if port == "auto":
            pass  # omit -t, let pasta auto-detect
        elif port:
            pasta_args += ["-t", port]
        else:
            pasta_args += ["-t", "none"]
        pasta_args += ["-u", "none", "-U", "none"]
        proc = subprocess.Popen(
            pasta_args + ["--", binary] + command,
            start_new_session=False,
            env=bwrap_env,
        )

    sessions.register_session(sid, project, proc.pid, index=session_index,
                              mounted_projects=list(loaded_projects.keys()))
    sessions.update_pane_titles(project)
    print("Started coding-cave.", file=sys.stderr)

    # Convert SIGTERM/SIGHUP into SystemExit so the finally block runs.
    def _exit_on_signal(signum, _frame):
        try:
            os.kill(proc.pid, signum)
        except OSError:
            pass
        raise SystemExit(128 + signum)

    def _forward_winch(_signum, _frame):
        _signal_descendants(proc.pid, signal.SIGWINCH)

    prev_term = signal.signal(signal.SIGTERM, _exit_on_signal)
    prev_hup = signal.signal(signal.SIGHUP, _exit_on_signal)
    prev_winch = signal.signal(signal.SIGWINCH, _forward_winch)
    try:
        ret = wait_for_child(proc.pid)
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGHUP, prev_hup)
        signal.signal(signal.SIGWINCH, prev_winch)
        gitdir.merge_session_gitdirs(sid, overlay_base)
        sessions.save_last_index(project, session_index)
        sessions.cleanup_session(sid)
        sessions.update_pane_titles(project)
        config.stop_gateway_if_idle()
        for merged in overlay_mounts:
            overlay.unmount_fuse(merged)
        if not inside_cave:
            overlay.unmount_fuse(nix_merged)
        _save_overlay_uppers(overlay_base, loaded_projects)
        overlay.force_rmtree(overlay_base)
        overlay.clean_stale_session_overlays()

    sys.exit(ret)


def wait_for_child(proc_pid):
    """Wait for a child process with WUNTRACED for Ctrl+Z support.

    Returns the exit code.
    """
    try:
        while True:
            try:
                _, status = os.waitpid(proc_pid, os.WUNTRACED)
            except ChildProcessError:
                return 1
            if os.WIFSTOPPED(status):
                signal.signal(signal.SIGTSTP, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTSTP)
                os.kill(proc_pid, signal.SIGCONT)
            elif os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                return 128 + os.WTERMSIG(status)
    except KeyboardInterrupt:
        return 130


def _signal_descendants(pid, signum):
    """Send signum to all descendant processes of pid via /proc walk."""
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            stat = Path(entry.path, "stat").read_text()
            close_paren = stat.rfind(")")
            fields = stat[close_paren + 2:].split()
            ppid = int(fields[1])
            if ppid == pid:
                child_pid = int(entry.name)
                try:
                    os.kill(child_pid, signum)
                except OSError:
                    pass
                _signal_descendants(child_pid, signum)
        except (FileNotFoundError, ValueError, PermissionError, IndexError):
            continue


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


def nsenter_shell_script(shell_cmd, workdir, cave_env="/tmp/cave-env"):
    """Build the bash -c script for nsenter shell entry.

    Clears host direnv state (DIRENV_DIFF contains the host's original PATH;
    direnv's precmd would "unload" it and restore the host PATH), then sources
    cave-env for the full cave environment.
    """
    return (
        f'unset DIRENV_DIFF DIRENV_WATCHES DIRENV_DIR; '
        f'. {cave_env} 2>/dev/null; '
        f'cd {workdir} 2>/dev/null; '
        f'{shell_cmd}'
    )


def enter_session(session_info, shell_type, workdir=None):
    """nsenter into a running session."""
    bwrap_pid = session_info["bwrap_pid"]

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
    inner_pid = find_bwrap_child(child_pid)
    if inner_pid is not None:
        child_pid = inner_pid

    from .commands import _shell_exec_cmd
    shell_cmd = _shell_exec_cmd(shell_type)

    # Strip CODING_CAVE_* vars -- they're host-side config plumbing.
    nsenter_env = {k: v for k, v in os.environ.items()
                   if not k.startswith("CODING_CAVE_") or k in _CAVE_RUNTIME_VARS}

    proc = subprocess.Popen([
        "nsenter",
        f"--target={child_pid}",
        "--user", "--mount", "--net", "--pid", "--uts", "--ipc",
        "--preserve-credentials",
        "--", "/bin/bash", "-c",
        nsenter_shell_script(shell_cmd, workdir or "/projects"),
    ], env=nsenter_env)

    def _forward_winch(_signum, _frame):
        _signal_descendants(proc.pid, signal.SIGWINCH)

    prev_winch = signal.signal(signal.SIGWINCH, _forward_winch)
    try:
        ret = wait_for_child(proc.pid)
    finally:
        signal.signal(signal.SIGWINCH, prev_winch)
    sys.exit(ret)
