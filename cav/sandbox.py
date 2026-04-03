"""Sandbox launcher for running agents.

Sets up the nix store overlay, configures environment variables for the
jail.nix-generated sandbox binary, and launches it. The sandbox binary
handles bwrap args, /etc, SSL certs, PATH, and entry script.
"""

import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import config, daemon, git, user_env

log = config.log

GIT_WRAPPER = """\
#!/bin/sh
# Git wrapper: syncs dev branches to shared remote after state changes.
# Installed at $HOME/bin/git inside the cave, shadowing the real git.
# This replaces per-repo post-commit hooks, leaving .git/hooks/ free
# for project use.

# Prevent recursive sync (our own git calls shouldn't trigger again).
if [ -n "$_CAV_GIT_SYNC" ]; then
  exec "$_CAV_REAL_GIT" "$@"
fi

# Find the real git by searching PATH with our wrapper dir removed.
_wrapper_dir="$(dirname "$(readlink -f "$0")")"
_opath="$PATH"
PATH=$(printf '%s' "$_opath" | tr ':' '\\n' | grep -v "^${_wrapper_dir}$" | paste -sd:)
_CAV_REAL_GIT=$(command -v git)
PATH="$_opath"
export _CAV_REAL_GIT

"$_CAV_REAL_GIT" "$@"
_rc=$?
[ $_rc -ne 0 ] && exit $_rc

_cmd="$1"

# Resolve branch name. During a rebase HEAD is detached, so read
# the original branch from the rebase state directory.
_branch=$("$_CAV_REAL_GIT" symbolic-ref --short HEAD 2>/dev/null)
if [ -z "$_branch" ] && [ -f .git/rebase-merge/head-name ]; then
  _branch=$(sed 's|^refs/heads/||' .git/rebase-merge/head-name)
elif [ -z "$_branch" ] && [ -f .git/rebase-apply/head-name ]; then
  _branch=$(sed 's|^refs/heads/||' .git/rebase-apply/head-name)
fi

# Only sync dev branches.
case "$_branch" in agent-[0-9]*/*) ;; *) exit $_rc ;; esac

# Trap: refuse to push a branch with HEAD in the name.
case "$_branch" in *HEAD*)
  echo "BUG: git wrapper about to push HEAD branch" >&2
  echo "  branch=[$_branch] HEAD=$("$_CAV_REAL_GIT" rev-parse --short HEAD)" >&2
  exit $_rc
  ;;
esac

export _CAV_GIT_SYNC=1

case "$_cmd" in
  commit|cherry-pick|revert|am)
    # Full sync: fetch, auto-rebase onto base, push.
    # During a rebase, just push (don't start a nested rebase).
    if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
      "$_CAV_REAL_GIT" push --force host-cave "HEAD:refs/heads/$_branch" >/dev/null 2>&1 || true
      exit $_rc
    fi
    "$_CAV_REAL_GIT" fetch host-cave >/dev/null 2>&1 || true
    _base=$(echo "$_branch" | sed 's|^agent-[0-9]*/||')
    if [ -n "$_base" ] && "$_CAV_REAL_GIT" rev-parse "host-cave/$_base" >/dev/null 2>&1; then
      if ! "$_CAV_REAL_GIT" rebase "host-cave/$_base" >/dev/null 2>&1; then
        "$_CAV_REAL_GIT" rebase --abort >/dev/null 2>&1
        echo "WARNING: rebase onto host-cave/$_base failed (conflicts). Run: git fetch host-cave && git rebase host-cave/$_base" >&2
      fi
    fi
    "$_CAV_REAL_GIT" push --force host-cave HEAD >/dev/null 2>&1 || true
    ;;
  reset|checkout|switch|merge|rebase)
    # Light sync: just push current state.
    "$_CAV_REAL_GIT" push --force host-cave "HEAD:refs/heads/$_branch" >/dev/null 2>&1 || true
    ;;
esac

exit $_rc
"""

# Substituted at nix build time with the jail.nix sandbox binary path.
SANDBOX_BIN = "@SANDBOX_BIN@"
# Substituted at nix build time with the cav bin directory.
CAV_BIN = "@CAV_BIN@"


def _signal_descendants(pid: int, sig: int) -> None:
    """Send a signal to all descendant processes of pid via /proc walk."""
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except (FileNotFoundError, ProcessLookupError):
        return
    for child in children:
        child_pid = int(child)
        _signal_descendants(child_pid, sig)
        try:
            os.kill(child_pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _host_git_identity() -> dict[str, str]:
    """Read git identity from host config."""
    env = {}
    for key, envvar in [
        ("user.name", "CODING_CAVE_GIT_NAME"),
        ("user.email", "CODING_CAVE_GIT_EMAIL"),
        ("user.signingkey", "CODING_CAVE_GIT_SIGNINGKEY"),
        ("commit.gpgsign", "CODING_CAVE_GIT_GPGSIGN"),
    ]:
        try:
            val = git.git_output("config", "--global", key)
            if val:
                env[envvar] = val
        except subprocess.CalledProcessError:
            pass
    return env


def _host_gitconfig() -> Path | None:
    """Find the host user's .gitconfig."""
    home = _host_home()
    for p in [
        home / ".gitconfig",
        Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "git" / "config",
    ]:
        if p.exists():
            return p
    return None


def _gpg_homedir() -> Path | None:
    """Find the GPG home directory."""
    try:
        d = subprocess.run(
            ["gpgconf", "--list-dirs", "homedir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(d) if d else None
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _gpg_socket() -> str | None:
    """Find the restricted GPG agent extra socket.

    The extra socket (S.gpg-agent.extra) runs in restricted mode, which
    blocks secret key export, key generation, key deletion, and other
    dangerous operations while still allowing signing (for git commits).
    """
    try:
        sock = subprocess.run(
            ["gpgconf", "--list-dirs", "agent-extra-socket"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    if not sock or not Path(sock).exists():
        # Auto-launch gpg-agent (creates extra socket by default)
        subprocess.run(["gpgconf", "--launch", "gpg-agent"],
                       capture_output=True, check=False)
    return sock if sock and Path(sock).exists() else None


CRINJ_PORT = 10255
# Inside pasta's network namespace, 127.0.0.1 is the namespace's own
# loopback. Host loopback is mapped to this address via pasta's
# --map-host-loopback so the cave can reach host-side services.
PASTA_HOST_LOOPBACK = "10.0.2.2"


def _host_home() -> Path:
    """Real host home, even when running inside a cave."""
    return Path(os.environ.get("CODING_CAVE_HOST_HOME", str(Path.home())))


def _crinj_ca_path() -> Path:
    """Find crinj CA certificate.

    At depth 0, crinj generates the cert in the host home. At depth > 0,
    crinj runs inside the cave (cert in cave home), or the parent's cert
    is bind-mounted at /etc/crinj-ca.pem by jail.nix.
    """
    host_home = _host_home()
    cave_home = Path.home()
    xdg = os.environ.get("XDG_DATA_HOME", host_home / ".local" / "share")
    for path in [
        cave_home / ".crinj" / "gateway" / "ca.pem",
        host_home / ".crinj" / "gateway" / "ca.pem",
        Path(xdg) / "crinj" / "gateway" / "ca.pem",
        Path("/etc/crinj-ca.pem"),
    ]:
        if path.exists():
            return path
    raise SystemExit("crinj CA certificate not found. Expected at ~/.crinj/gateway/ca.pem")


def _crinj_listening() -> bool:
    """Check if crinj is listening on the proxy port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", CRINJ_PORT)) == 0


def _start_crinj() -> str:
    """Start crinj if not already running. Returns proxy address."""
    if not shutil.which("crinj"):
        raise SystemExit("crinj not found on PATH")

    if _crinj_listening():
        log.debug("crinj already listening")
    else:
        log.debug("starting crinj...")
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            ["crinj", "--port", str(CRINJ_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        for i in range(20):
            if _crinj_listening():
                log.debug(f"crinj ready after {(i + 1) * 0.25:.1f}s")
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace").strip()
                raise SystemExit(f"crinj exited {proc.returncode}: {stderr}")
            time.sleep(0.25)
        else:
            raise SystemExit("crinj failed to start within 5s")

    return f"127.0.0.1:{CRINJ_PORT}"


def _prune_nix_caches() -> None:
    """Remove nix cache entries that reference store paths no longer on disk.

    The fetcher cache (flake URL -> store path) and eval cache (flake
    fingerprint -> drv paths) persist across sessions but can reference
    paths the host has since GC'd.
    """
    nix_cache = config.persistent_home() / ".cache" / "nix"

    # Fetcher cache: delete individual rows with dead store paths
    for db_path in nix_cache.glob("fetcher-cache-*.sqlite"):
        try:
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT domain, key, value FROM Cache"
                " WHERE value LIKE '%storePath%'"
            ).fetchall()
            for domain, key, val in rows:
                idx = val.find('"storePath":"')
                if idx < 0:
                    continue
                sp = val[idx + 13:].split('"', 1)[0]
                if not (Path("/nix/store") / sp).exists():
                    conn.execute(
                        "DELETE FROM Cache WHERE domain=? AND key=?",
                        (domain, key),
                    )
            conn.commit()
            conn.close()
        except (sqlite3.Error, OSError):
            db_path.unlink(missing_ok=True)
            for wal in nix_cache.glob(db_path.name + "-*"):
                wal.unlink(missing_ok=True)

    # Eval cache: each file is one flake evaluation. If any sampled
    # path is gone, the whole file is stale.
    eval_dir = nix_cache / "eval-cache-v6"
    if not eval_dir.is_dir():
        return
    for db_path in eval_dir.glob("*.sqlite"):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT value FROM Attributes"
                " WHERE value LIKE '/nix/store/%' LIMIT 5"
            ).fetchall()
            conn.close()
        except (sqlite3.Error, OSError):
            db_path.unlink(missing_ok=True)
            continue
        if rows and any(not Path(r[0]).exists() for r in rows):
            db_path.unlink(missing_ok=True)


def _seed_nix_db() -> Path | None:
    """Backup host nix DB, preserving cave-installed package registrations.

    Always starts from a fresh host backup (fast, self-cleaning), then
    merges back cave-only paths whose files still exist on disk. This
    avoids unbounded DB growth from stale entries while preserving
    cave-installed packages (which would fail with EPERM on re-substitute
    because nix tries to chmod the existing overlay path).

    Returns the DB directory path, or None if the host DB doesn't exist.
    """
    host_db = Path("/nix/var/nix/db/db.sqlite")
    if not host_db.exists():
        return None

    db_dir = config.nix_state_dir() / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    cave_db = db_dir / "db.sqlite"

    try:
        # Collect cave-only paths before overwriting
        cave_only_rows = []
        if cave_db.exists():
            old = sqlite3.connect(f"file:{cave_db}?mode=ro", uri=True)
            host = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
            host_paths = {r[0] for r in host.execute("SELECT path FROM ValidPaths")}
            for row in old.execute(
                "SELECT path, hash, registrationTime, deriver, narSize, "
                "ultimate, sigs, ca FROM ValidPaths"
            ):
                if row[0] not in host_paths and Path(row[0]).exists():
                    cave_only_rows.append(row)
            host.close()
            old.close()

        # Fresh backup from host
        for wal in db_dir.glob("db.sqlite-*"):
            wal.unlink()
        src = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        dst = sqlite3.connect(str(cave_db))
        src.backup(dst)

        # Merge back verified cave-only paths
        if cave_only_rows:
            for path, h, reg_time, deriver, nar_size, ult, sigs, ca in cave_only_rows:
                dst.execute(
                    "INSERT OR IGNORE INTO ValidPaths "
                    "(path, hash, registrationTime, deriver, narSize, "
                    "ultimate, sigs, ca) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (path, h, reg_time, deriver, nar_size, ult, sigs, ca),
                )
            dst.commit()

        dst.close()
        src.close()
    except sqlite3.Error:
        pass

    _prune_nix_caches()

    return db_dir


def _nix_overlay_dirs(agent_id: str) -> tuple[Path, Path, Path]:
    agent_dir = config.nix_overlay_dir() / agent_id
    upper = agent_dir / "upper"
    work = agent_dir / "work"
    merged = agent_dir / "merged"
    for d in (upper, work, merged):
        d.mkdir(parents=True, exist_ok=True)
    return upper, work, merged


def _mount_fuse_overlay(upper: Path, work: Path, merged: Path):
    subprocess.run(
        [
            "fuse-overlayfs",
            "-o", f"lowerdir=/nix/store,upperdir={upper},workdir={work}",
            str(merged),
        ],
        check=True, capture_output=True, text=True,
    )


def _setup_nix_overlay(agent_id: str) -> str:
    """Mount a per-agent writable nix store overlay. Returns merged path.

    Tries a direct mount (works when fusermount3 is setuid). If that fails,
    defers the mount to _nix_overlay_preexec() which runs in the child
    process after unshare(NEWUSER|NEWNS).
    """
    upper, work, merged = _nix_overlay_dirs(agent_id)

    if any(merged.iterdir()):
        return str(merged)

    try:
        _mount_fuse_overlay(upper, work, merged)
    except subprocess.CalledProcessError:
        pass  # Deferred: mounted in child via _nix_overlay_preexec()

    return str(merged)


def _nix_overlay_preexec(agent_id: str):
    """Mount nix overlay inside a child user+mount namespace.

    Called as preexec_fn when the direct mount in _setup_nix_overlay failed
    (fusermount3 not setuid). Creates a user namespace with mount capability,
    then mounts fuse-overlayfs before exec.
    """
    upper, work, merged = _nix_overlay_dirs(agent_id)
    if any(merged.iterdir()):
        return  # Already mounted (direct mount succeeded)

    uid = os.getuid()
    gid = os.getgid()
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"0 {uid} 1\n")
    Path("/proc/self/gid_map").write_text(f"0 {gid} 1\n")

    _mount_fuse_overlay(upper, work, merged)


def _teardown_nix_overlay(agent_id: str):
    """Unmount the per-agent nix store overlay."""
    merged = config.nix_overlay_dir() / agent_id / "merged"
    if not merged.exists():
        return
    r = subprocess.run(["fusermount3", "-uz", str(merged)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Nix store fusermount3 isn't setuid; try system paths (NixOS).
        for p in ["/run/wrappers/bin/fusermount3", "/usr/bin/fusermount3"]:
            if Path(p).is_file():
                r = subprocess.run([p, "-uz", str(merged)],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    return
        log.error(f"fusermount3 -uz {merged}: {r.stderr.strip()}")


def _host_claude_json() -> Path | None:
    """Find the host user's .claude.json."""
    p = _host_home() / ".claude.json"
    return p if p.exists() else None


def _seed_claude_config(home: Path, project_slug: str):
    """Seed the cave's .claude.json from host on first use, and ensure workspace is trusted."""
    import json
    claude_json = home / ".claude.json"
    if not claude_json.exists():
        host = _host_claude_json()
        if host:
            import shutil
            shutil.copy2(host, claude_json)
    if claude_json.exists():
        try:
            cfg = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError):
            cfg = {}
    else:
        cfg = {}
    # Ensure workspace is trusted
    project_path = f"/projects/{project_slug}"
    projects = cfg.setdefault("projects", {})
    proj = projects.setdefault(project_path, {})
    if not proj.get("hasTrustDialogAccepted"):
        proj["hasTrustDialogAccepted"] = True
        proj["hasCompletedProjectOnboarding"] = True
        claude_json.write_text(json.dumps(cfg))


def launch(workspaces: list[tuple[Path, Path, str]], agent_name: str,
           project_slug: str = "project", recursive: bool = False,
           session_id: str | None = None, resuming: bool = False):
    """Launch claude in a bwrap sandbox via the jail.nix binary.

    workspaces: list of (workspace_path, shared_repo, slug) tuples.
    project_slug: slug of the primary project (for cd on entry).
    """
    home = config.persistent_home()
    home.mkdir(parents=True, exist_ok=True)
    for _, _, slug in workspaces:
        _seed_claude_config(home, slug)

    # Extract numeric agent ID from display name (e.g. "coding-cave@5" -> "5")
    agent_id = agent_name.split("@")[-1]

    # Start git daemon for remote isolation (bare repos served over git://)
    log.info("starting git daemon...")
    shared_repos_for_daemon = [(sr, slug) for _, sr, slug in workspaces]
    daemon_proc, daemon_dir = daemon.start(agent_id, shared_repos_for_daemon)
    log.info("git daemon ready")


    # Install git wrapper in shared home (shadows real git on PATH).
    agent_bin = home / "bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    wrapper = agent_bin / "git"
    if not wrapper.exists() or wrapper.read_text() != GIT_WRAPPER:
        wrapper.write_text(GIT_WRAPPER)
        wrapper.chmod(0o755)

    # Network isolation: every nesting level gets its own pasta network
    # namespace. Each level's services bind to 127.0.0.1 and the parent
    # is reachable at 10.0.2.2 via --map-host-loopback, so port numbers
    # are reused without conflict at any depth.
    daemon_host = PASTA_HOST_LOOPBACK

    # Migrate old workspaces: rename "cave" -> "host-cave" if needed.
    for ws, _, _ in workspaces:
        r = git.git("remote", "get-url", config.HOST_SHARED_REMOTE, cwd=ws,
                    echo=False, check=False, capture=True)
        if r.returncode != 0:
            git.git("remote", "rename", config.CAVE_SHARED_REMOTE, config.HOST_SHARED_REMOTE,
                    cwd=ws, echo=False, check=False, capture=True)

    # Update workspace remotes to use git:// via daemon (localhost for
    # host-side sync, then switch to pasta address for inside the namespace)
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug)
        git.git("remote", "set-url", config.HOST_SHARED_REMOTE, url,
                cwd=ws, echo=False)

    # Sync: fetch latest and fast-forward local base branches
    log.info("syncing workspace branches...")
    for ws, _, _ in workspaces:
        git.git("fetch", config.HOST_SHARED_REMOTE, cwd=ws, echo=False, check=False,
                capture=True)
        current = git.current_branch(cwd=ws)
        for branch in git.remote_branches(config.HOST_SHARED_REMOTE, cwd=ws):
            if config.parse_dev_branch(branch):
                continue
            if branch == current:
                continue  # Can't force-update the checked-out branch
            remote_ref = f"{config.HOST_SHARED_REMOTE}/{branch}"
            git.git("branch", "-f", branch, remote_ref, cwd=ws,
                    echo=False, check=False, capture=True)

    # Now set the remote to the pasta-reachable address for inside the namespace
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug, host=daemon_host)
        git.git("remote", "set-url", config.HOST_SHARED_REMOTE, url,
                cwd=ws, echo=False)

    # Build user environment from cave.nix (cached)
    log.info("building user environment...")
    user_env_path = user_env.build()
    log.info("user environment ready")

    log.info("setting up nix overlay...")
    nix_db_dir = _seed_nix_db()
    nix_merged = _setup_nix_overlay(agent_id)
    log.info("nix overlay ready")

    # Encode multiple workspaces as colon-separated lists
    ws_paths = ":".join(str(ws) for ws, _, _ in workspaces)
    shared_repos = ":".join(str(sr) for _, sr, _ in workspaces)
    slugs = ":".join(s for _, _, s in workspaces)

    # Build a minimal environment for the jail wrapper. Only include
    # vars the add-runtime blocks and try-fwd-env calls actually need.
    # Using **os.environ here would leak host secrets (API keys, tokens,
    # SSH_AUTH_SOCK, etc.) via /proc/1/environ inside the sandbox.
    # HOME is needed by the wrapper for ~/.local/share/jail.nix/passwd.
    # TERM/COLORTERM/TZ are forwarded into the cave via try-fwd-env.
    env = {}
    for var in ("HOME", "TERM", "COLORTERM", "TZ"):
        if var in os.environ:
            env[var] = os.environ[var]
    env.update({
        "CODING_CAVE_VERSION": config.VERSION,
        "CODING_CAVE_NIX_MERGED": nix_merged,
        "CODING_CAVE_HOME": str(home),
        "CODING_CAVE_WORKSPACES": ws_paths,
        "CODING_CAVE_SHARED_REPOS": shared_repos,
        "CODING_CAVE_AGENT_NAME": agent_name,
        "CODING_CAVE_PROJECT_SLUGS": slugs,
        "CODING_CAVE_PROJECT_SLUG": project_slug,
        # Preserve the real host home for nested caves
        "CODING_CAVE_HOST_HOME": os.environ.get(
            "CODING_CAVE_HOST_HOME", str(Path.home())),
    })

    if nix_db_dir:
        env["CODING_CAVE_NIX_DB_DIR"] = str(nix_db_dir)

    if recursive:
        env["CODING_CAVE_RECURSIVE"] = "1"

    env["CODING_CAVE_DNS_SERVER"] = PASTA_HOST_LOOPBACK

    # Lineage: build dot-separated chain of parent agent names
    parent_lineage = os.environ.get("CODING_CAVE_LINEAGE", "")
    parent_agent = os.environ.get("CODING_CAVE_AGENT_NAME", "")
    if parent_agent:
        lineage = f"{parent_lineage}.{parent_agent}" if parent_lineage else parent_agent
        env["CODING_CAVE_LINEAGE"] = lineage

    # Git identity (forwarded into cave via try-fwd-env)
    env.update(_host_git_identity())

    # Git config (full file, read-only)
    gitconfig = _host_gitconfig()
    if gitconfig:
        env["CODING_CAVE_GITCONFIG"] = str(gitconfig)

    # GPG: socket + public keyring for commit signing
    gpg_socket = _gpg_socket()
    if gpg_socket:
        env["CODING_CAVE_GPG_SOCKET"] = gpg_socket
        gpg_home = _gpg_homedir()
        if gpg_home:
            for name in ("pubring.kbx", "pubring.gpg"):
                p = gpg_home / name
                if p.exists():
                    env["CODING_CAVE_GPG_PUBRING"] = str(p)
                    break

    # cav bin dir (so agents can run `cav version` etc. inside the cave)
    env["CODING_CAVE_BIN"] = CAV_BIN


    # User environment from cave.nix
    if user_env_path:
        env["CODING_CAVE_USER_ENV"] = str(user_env_path)

    # User CLAUDE.md
    user_claude_md = _host_home() / ".config" / "coding-cave" / "CLAUDE.md"
    if user_claude_md.exists():
        env["CODING_CAVE_USER_CLAUDE_MD"] = str(user_claude_md)

    # Crinj proxy (required for API auth). Each nesting level runs its own
    # crinj instance. Nested instances inject placeholder keys; the outermost
    # crinj (on the host) replaces them with real credentials. Traffic chains
    # through http_proxy at each level automatically.
    log.info("starting crinj proxy...")
    _start_crinj()
    env["CODING_CAVE_PROXY_ADDR"] = f"{daemon_host}:{CRINJ_PORT}"
    env["CODING_CAVE_PROXY_CA"] = str(_crinj_ca_path())
    # Placeholder token so Claude Code considers itself "logged in".
    # Crinj replaces the Authorization header, so this never hits the API.
    env["CLAUDE_CODE_OAUTH_TOKEN"] = (
        "sk-ant-oat01-"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-AAAAAAAA"
    )

    try:
        claude_cmd = [SANDBOX_BIN, "claude", "--dangerously-skip-permissions"]
        if session_id:
            if resuming:
                claude_cmd += ["--resume", session_id]
            else:
                claude_cmd += ["--session-id", session_id]
        pasta_bin = shutil.which("pasta")
        if not pasta_bin:
            raise SystemExit("pasta not found on PATH (install passt package)")
        pasta_cmd = [
            pasta_bin, "--quiet", "--config-net",
            "--map-host-loopback", PASTA_HOST_LOOPBACK,
            "--dns-forward", PASTA_HOST_LOOPBACK,
            "-t", "none",
            "-u", "none", "-U", "none",
            "--",
        ]

        preexec = lambda: _nix_overlay_preexec(agent_id)

        log.info("launching sandbox...")
        proc = subprocess.Popen(pasta_cmd + claude_cmd, env=env,
                                preexec_fn=preexec)

        def fwd(s, _f):
            _signal_descendants(proc.pid, s)

        fwd_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH)
        orig = {s: signal.getsignal(s) for s in fwd_signals}
        for s in orig:
            signal.signal(s, fwd)

        try:
            return proc.wait()
        finally:
            for s, handler in orig.items():
                signal.signal(s, handler)

    finally:
        daemon.stop(daemon_proc, daemon_dir)
        _teardown_nix_overlay(agent_id)
