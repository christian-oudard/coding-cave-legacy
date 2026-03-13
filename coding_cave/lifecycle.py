"""Session lifecycle: start, wait, nsenter."""
import os
import signal
import subprocess
import sys
from pathlib import Path

from . import _state, projects, sessions, git as git_mod, gitdir, overlay


def start_session(project, command, gpu=False):
    """Start a new bwrap session with per-session overlays."""
    _state.cave_data().mkdir(parents=True, exist_ok=True)
    sid = sessions.session_id()

    # Nix store overlay (shared across sessions, fuse-overlayfs).
    # Upper and db persist across sessions so nix packages survive.
    # When already inside a cave, /nix/store is the parent's overlay
    # and we can't fuse-mount again — just pass it through.
    inside_cave = bool(os.environ.get("CODING_CAVE_VERSION"))
    nix_already_mounted = False
    if inside_cave:
        nix_merged = "/nix/store"
    else:
        (_state.nix_overlay_dir() / "upper").mkdir(parents=True, exist_ok=True)
        (_state.nix_overlay_dir() / "merged").mkdir(parents=True, exist_ok=True)
        nix_merged = str(_state.nix_overlay_dir() / "merged")
        # Only mount if not already mounted (shared across sessions)
        nix_already_mounted = os.path.ismount(nix_merged)
        if not nix_already_mounted:
            # Work dir must be empty for fuse-overlayfs
            nix_work = _state.nix_overlay_dir() / "work"
            overlay.force_rmtree(nix_work)
            nix_work.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["fuse-overlayfs",
                 "-o", f"lowerdir=/nix/store,upperdir={_state.nix_overlay_dir() / 'upper'},workdir={nix_work}",
                 nix_merged],
                check=True, capture_output=True,
            )
    os.environ["CODING_CAVE_NIX_MERGED"] = nix_merged

    # Seed nix DB from host/parent so nix doesn't try to re-register
    # (chmod) existing store paths, which fails on overlayfs.
    # Only seed if no existing cave DB (to preserve cave-installed pkgs).
    # Use sqlite3 backup API for a consistent copy (handles WAL mode).
    if not inside_cave:
        host_nix_db = Path("/nix/var/nix/db/db.sqlite")
        nix_db_dir = _state.nix_overlay_dir() / "db"
        nix_db_dir.mkdir(parents=True, exist_ok=True)
        cave_nix_db = nix_db_dir / "db.sqlite"
        if host_nix_db.exists() and not cave_nix_db.exists():
            import sqlite3
            try:
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
    for slug, source in projects.load_projects().items():
        gitdir_path = _state.projects_dir() / slug / "gitdir"
        if not gitdir_path.exists() and (Path(source) / ".git").is_dir():
            print(f"Syncing {slug}...", file=sys.stderr)
        git_mod.initialize_gitdir(slug, Path(source))

    loaded_projects = projects.load_projects()

    # Build extra bwrap args (project mounts, dotfiles, passwd)
    bwrap_args = []

    # CLAUDE.md
    claude_md = os.environ.get("CODING_CAVE_CLAUDE_MD", "")
    if claude_md and Path(claude_md).is_file():
        bwrap_args += ["--ro-bind", claude_md, f"{_state.JAIL_HOME}/.claude/CLAUDE.md"]

    # Zsh dotfiles
    zdotdir = os.environ.get("CODING_CAVE_ZDOTDIR", "")
    if zdotdir and Path(zdotdir).is_dir():
        zsh_cave = f"{_state.JAIL_HOME}/.config/zsh"
        persistent_zdotdir = _state.cave_data() / "zdotdir"
        persistent_zdotdir.mkdir(parents=True, exist_ok=True)
        bwrap_args += ["--bind", str(persistent_zdotdir), zsh_cave]
        for f in sorted(Path(zdotdir).glob(".z*")):
            if not f.is_file():
                continue
            if "private" in f.name or "secret" in f.name:
                continue
            if f.name.startswith(".zhistory") or f.name.startswith(".zcompdump"):
                # Don't copy host history -- keep cave history separate
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
        bwrap_args += ["--ro-bind", bashrc, f"{_state.JAIL_HOME}/.bashrc"]
    bash_profile = os.environ.get("CODING_CAVE_BASH_PROFILE", "")
    if bash_profile and Path(bash_profile).is_file():
        bwrap_args += ["--ro-bind", bash_profile, f"{_state.JAIL_HOME}/.bash_profile"]

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
    overlay_base = _state.cave_data() / "session-overlays" / sid
    overlay_base.mkdir(parents=True, exist_ok=True)
    overlay_mounts = []  # merged paths for cleanup
    try:
        for slug, source in loaded_projects.items():
            if not Path(source).is_dir():
                continue
            upper = overlay_base / f"{slug}-upper"
            work = overlay_base / f"{slug}-work"
            merged = overlay_base / f"{slug}-merged"
            # Restore saved upper from previous session if available.
            # This preserves uncommitted changes, untracked files, and
            # deletions across session restarts.
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
            # Sync working tree to HEAD when shared gitdir has unpulled
            # commits and no saved upper exists.  When a saved upper is
            # restored, it already contains the correct working tree
            # state (both committed and uncommitted changes).
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
    except BaseException:
        for merged in overlay_mounts:
            overlay.unmount_fuse(merged)
        overlay.force_rmtree(overlay_base)
        raise

    # GPG agent socket forwarding (for commit signing without private key)
    gpg_sock = os.environ.get("CODING_CAVE_GPG_AGENT_SOCK", "")
    if gpg_sock and os.path.exists(gpg_sock):
        cave_gpg_sock = f"{_state.JAIL_HOME}/.gnupg/S.gpg-agent"
        bwrap_args += ["--bind", gpg_sock, cave_gpg_sock]
    # GPG public keyring (read-only, for key lookup)
    gnupghome = os.environ.get("CODING_CAVE_GNUPGHOME", "")
    if gnupghome and os.path.isdir(gnupghome):
        cave_gnupghome = f"{_state.JAIL_HOME}/.gnupg"
        # Bind individual files read-only to avoid exposing private keys
        for fname in ("pubring.kbx", "pubring.gpg", "trustdb.gpg"):
            src = os.path.join(gnupghome, fname)
            if os.path.isfile(src):
                bwrap_args += ["--ro-bind", src, f"{cave_gnupghome}/{fname}"]

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
    bwrap_args += ["--ro-bind", str(passwd_file), "/etc/passwd"]
    bwrap_args += ["--ro-bind", str(group_file), "/etc/group"]

    # Write bwrap args file (null-separated, for bwrap --args)
    _state.sessions_dir().mkdir(parents=True, exist_ok=True)
    args_file = _state.sessions_dir() / f"{sid}.bwrap-args"
    with open(args_file, "wb") as f:
        for arg in bwrap_args:
            f.write(arg.encode() + b"\0")
    os.environ["CODING_CAVE_BWRAP_ARGS"] = str(args_file)
    os.environ["CODING_CAVE_SESSION_ID"] = sid
    os.environ["CODING_CAVE_PROJECT"] = project
    # Stable session index: find next unused slot for this project.
    session_index = sessions._next_session_index(project)
    os.environ["CODING_CAVE_SESSION_INDEX"] = str(session_index)

    binary = _state.SANDBOX_GPU_BIN if gpu else _state.SANDBOX_BIN

    # Sanitize environment: only pass vars the bwrap runtime actually needs.
    # Everything else (host secrets, user identity, etc.) is dropped.
    # This prevents /proc/1/environ inside the cave from leaking host env.
    BWRAP_ENV_ALLOWLIST = {
        # Runtime script needs these to function
        "HOME", "PATH", "TERM",
        # Forwarded into cave via try-fwd-env
        "COLORTERM",
        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
        # Host git identity and GPG signing
        "CODING_CAVE_GIT_NAME", "CODING_CAVE_GIT_EMAIL",
        "CODING_CAVE_GIT_SIGNING_KEY", "CODING_CAVE_GIT_GPG_SIGN",
        "CODING_CAVE_GPG_AGENT_SOCK", "CODING_CAVE_GNUPGHOME",
        # coding-cave internal
        "CODING_CAVE_NIX_MERGED", "CODING_CAVE_NIX_DB_DIR",
        "CODING_CAVE_BWRAP_ARGS",
        "CODING_CAVE_SESSION_ID", "CODING_CAVE_PROJECT",
        "CODING_CAVE_SESSION_INDEX",
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
        # --map-host-loopback 192.0.2.1: disable gateway->host-loopback mapping
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

    sessions.register_session(sid, project, proc.pid, session_index)
    sessions.update_pane_titles(project)
    print("Started coding-cave.", file=sys.stderr)

    try:
        ret = wait_for_child(proc.pid)
    finally:
        gitdir.merge_session_gitdirs(sid, overlay_base)
        sessions.cleanup_session(sid)
        sessions.update_pane_titles(project)
        for merged in overlay_mounts:
            overlay.unmount_fuse(merged)
        # Save overlay uppers for session continuity (uncommitted
        # changes, untracked files, deletions).  Must happen after
        # unmount (upper is busy while mounted) and before rmtree.
        # Skip uppers that only contain .git (no real changes) to
        # avoid clobbering another concurrent session's saved state.
        for slug in loaded_projects:
            upper = overlay_base / f"{slug}-upper"
            if not upper.is_dir():
                continue
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
        overlay.force_rmtree(overlay_base)
        # Unmount shared nix overlay when last session exits but keep
        # upper and db so installed packages persist across sessions.
        # Work dir must be cleaned (fuse-overlayfs needs it empty).
        # Skip when inside a cave (nix_merged is /nix/store, not ours).
        if not inside_cave and not sessions.list_sessions():
            overlay.unmount_fuse(nix_merged)
            for subdir in ["work", "merged"]:
                overlay.force_rmtree(_state.nix_overlay_dir() / subdir)

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


def nsenter_shell_script(shell_bin, workdir, cave_env="/tmp/cave-env"):
    """Build the bash -c script for nsenter shell entry.

    Clears host direnv state (DIRENV_DIFF contains the host's original PATH;
    direnv's precmd would "unload" it and restore the host PATH), then sources
    cave-env for the full cave environment.
    """
    return (
        f'unset DIRENV_DIFF DIRENV_WATCHES DIRENV_DIR; '
        f'. {cave_env} 2>/dev/null; '
        f'cd {workdir} 2>/dev/null; '
        f'exec {shell_bin} -l'
    )


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

    proc = subprocess.Popen([
        "nsenter",
        f"--target={child_pid}",
        "--user", "--mount", "--net", "--pid", "--uts", "--ipc",
        "--preserve-credentials",
        "--", "/bin/bash", "-c",
        nsenter_shell_script(shell_bin, workdir or "/projects"),
    ])

    sys.exit(wait_for_child(proc.pid))
