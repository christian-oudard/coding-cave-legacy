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
import time
from pathlib import Path

from . import config, daemon, git, user_env

POST_COMMIT_HOOK = (
    "#!/bin/sh\n"
    "# Resolve the branch name. During a rebase HEAD is detached, so read\n"
    "# the original branch from the rebase state directory.\n"
    "branch=$(git symbolic-ref --short HEAD 2>/dev/null)\n"
    "if [ -z \"$branch\" ] && [ -f .git/rebase-merge/head-name ]; then\n"
    "  branch=$(sed 's|^refs/heads/||' .git/rebase-merge/head-name)\n"
    "elif [ -z \"$branch\" ] && [ -f .git/rebase-apply/head-name ]; then\n"
    "  branch=$(sed 's|^refs/heads/||' .git/rebase-apply/head-name)\n"
    "fi\n"
    "# Only push agent branches. Bail on detached HEAD, shared branches,\n"
    "# or anything unexpected (e.g. 'detached HEAD' from rebase state).\n"
    "case \"$branch\" in agent-[0-9]*/*) ;; *) exit 0 ;; esac\n"
    "# During a rebase, skip the auto-rebase (would abort the outer one)\n"
    "# but still push so the remote stays current.\n"
    "if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then\n"
    "  git push --force host-cave \"HEAD:refs/heads/$branch\" >/dev/null 2>&1 || true\n"
    "  exit 0\n"
    "fi\n"
    "git fetch host-cave >/dev/null 2>&1 || true\n"
    "target=$(echo \"$branch\" | sed 's|^agent-[0-9]*/||')\n"
    "if [ -n \"$target\" ] && git rev-parse \"host-cave/$target\" >/dev/null 2>&1; then\n"
    "  if ! git rebase \"host-cave/$target\" >/dev/null 2>&1; then\n"
    "    git rebase --abort >/dev/null 2>&1\n"
    '    echo "WARNING: rebase onto host-cave/$target failed (conflicts). '
    'Run: git fetch host-cave && git rebase host-cave/$target" >&2\n'
    "  fi\n"
    "fi\n"
    "# Trap: detect if we are about to push a HEAD branch.\n"
    "case \"$branch\" in *HEAD*)\n"
    "  echo \"BUG: post-commit hook about to push HEAD branch\" >&2\n"
    "  echo \"  branch=[$branch] HEAD=$(git rev-parse --short HEAD)\" >&2\n"
    "  echo \"  symbolic-ref=$(git symbolic-ref HEAD 2>&1)\" >&2\n"
    "  echo \"  rebase-merge=$(cat .git/rebase-merge/head-name 2>/dev/null)\" >&2\n"
    "  echo \"  rebase-apply=$(cat .git/rebase-apply/head-name 2>/dev/null)\" >&2\n"
    "  exit 1\n"
    "  ;;\n"
    "esac\n"
    "git push --force host-cave HEAD >/dev/null 2>&1 || true\n"
)

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
        ("user.name", "CAV_GIT_NAME"),
        ("user.email", "CAV_GIT_EMAIL"),
        ("user.signingkey", "CAV_GIT_SIGNINGKEY"),
        ("commit.gpgsign", "CAV_GIT_GPGSIGN"),
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
    """Find and ensure the GPG agent socket exists."""
    try:
        sock = subprocess.run(
            ["gpgconf", "--list-dirs", "agent-socket"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    if not sock or not Path(sock).exists():
        # Auto-launch gpg-agent
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
    """Find crinj CA certificate on the host."""
    home = _host_home()
    xdg = os.environ.get("XDG_DATA_HOME", home / ".local" / "share")
    for path in [
        home / ".crinj" / "gateway" / "ca.pem",
        Path(xdg) / "crinj" / "gateway" / "ca.pem",
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

    if not _crinj_listening():
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["crinj", "--port", str(CRINJ_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(20):
            if _crinj_listening():
                break
            time.sleep(0.25)

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


def _setup_nix_overlay(agent_id: str) -> str:
    """Mount a per-agent writable nix store overlay. Returns merged path."""
    agent_dir = config.nix_overlay_dir() / agent_id
    upper = agent_dir / "upper"
    work = agent_dir / "work"
    merged = agent_dir / "merged"

    for d in (upper, work, merged):
        d.mkdir(parents=True, exist_ok=True)

    # Check if already mounted
    if any(merged.iterdir()):
        return str(merged)

    result = subprocess.run(
        [
            "fuse-overlayfs",
            "-o", f"lowerdir=/nix/store,upperdir={upper},workdir={work}",
            str(merged),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"Failed to mount nix store overlay: {result.stderr}")

    return str(merged)


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
        print(f"fusermount3 -uz {merged}: {r.stderr.strip()}",
              file=sys.stderr)


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

    shared_repos_for_daemon = [(sr, slug) for _, sr, slug in workspaces]
    daemon_proc, daemon_dir = daemon.start(agent_id, shared_repos_for_daemon)


    # Ensure post-commit hook is installed in all workspaces (idempotent)
    for ws, _, _ in workspaces:
        hook = ws / ".git" / "hooks" / "post-commit"
        if not hook.exists() or hook.read_text() != POST_COMMIT_HOOK:
            hook.write_text(POST_COMMIT_HOOK)
            hook.chmod(0o755)

    # Network isolation: top-level caves get a pasta network namespace so
    # agents can only reach their own daemon port, not other agents'.
    # Nested caves inherit the parent's namespace (already isolated).
    parent_depth = int(os.environ.get("CODING_CAVE_DEPTH", "0"))
    use_pasta = parent_depth == 0
    daemon_host = PASTA_HOST_LOOPBACK if use_pasta else "127.0.0.1"

    # Migrate old workspaces: rename "cave" -> "host-cave" if needed.
    for ws, _, _ in workspaces:
        r = git.git("remote", "get-url", config.HOST_REMOTE, cwd=ws,
                    echo=False, check=False, capture=True)
        if r.returncode != 0:
            git.git("remote", "rename", config.CAVE_REMOTE, config.HOST_REMOTE,
                    cwd=ws, echo=False, check=False, capture=True)

    # Update workspace remotes to use git:// via daemon (localhost for
    # host-side sync, then switch to pasta address for inside the namespace)
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug)
        git.git("remote", "set-url", config.HOST_REMOTE, url,
                cwd=ws, echo=False)

    # Sync: fetch latest and fast-forward local shared branches

    for ws, _, _ in workspaces:
        git.git("fetch", config.HOST_REMOTE, cwd=ws, echo=False, check=False,
                capture=True)
        current = git.current_branch(cwd=ws)
        for branch in git.remote_branches(config.HOST_REMOTE, cwd=ws):
            if config.parse_agent_branch(branch):
                continue
            if branch == current:
                continue  # Can't force-update the checked-out branch
            remote_ref = f"{config.HOST_REMOTE}/{branch}"
            git.git("branch", "-f", branch, remote_ref, cwd=ws,
                    echo=False, check=False, capture=True)

    # Now set the remote to the pasta-reachable address for inside the namespace
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug, host=daemon_host)
        git.git("remote", "set-url", config.HOST_REMOTE, url,
                cwd=ws, echo=False)

    # Build user environment from cave.nix (cached)

    user_env_path = user_env.build()

    # Nested caves inherit the parent's writable nix store overlay.
    # At depth 0, mount a fresh fuse-overlayfs. At depth > 0, the parent's
    # /nix/store is already writable; skip the mount (fusermount3 isn't
    # setuid inside the cave) and let the jail inherit it.
    if parent_depth == 0:
        nix_db_dir = _seed_nix_db()
        nix_merged = _setup_nix_overlay(agent_id)
    else:
        nix_db_dir = None
        nix_merged = ""

    # Encode multiple workspaces as colon-separated lists
    ws_paths = ":".join(str(ws) for ws, _, _ in workspaces)
    shared_repos = ":".join(str(sr) for _, sr, _ in workspaces)
    slugs = ":".join(s for _, _, s in workspaces)

    # Subcave depth tracking: increment if already inside a cave
    parent_depth = int(os.environ.get("CODING_CAVE_DEPTH", "0"))
    depth = parent_depth + 1

    # Set env vars for the jail binary's add-runtime blocks
    env = {
        **os.environ,
        "CODING_CAVE_NIX_MERGED": nix_merged,
        "CODING_CAVE_HOME": str(home),
        "CODING_CAVE_WORKSPACES": ws_paths,
        "CODING_CAVE_SHARED_REPOS": shared_repos,
        "CAV_AGENT_NAME": agent_name,
        "CODING_CAVE_PROJECT_SLUGS": slugs,
        "CODING_CAVE_PROJECT_SLUG": project_slug,
        "CODING_CAVE_DEPTH": str(depth),
        # Preserve the real host home for nested caves
        "CODING_CAVE_HOST_HOME": os.environ.get(
            "CODING_CAVE_HOST_HOME", str(Path.home())),
    }

    if nix_db_dir:
        env["CODING_CAVE_NIX_DB_DIR"] = str(nix_db_dir)

    if recursive:
        env["CODING_CAVE_RECURSIVE"] = "1"

    if use_pasta:
        env["CODING_CAVE_DNS_SERVER"] = PASTA_HOST_LOOPBACK

    # Lineage: build dot-separated chain of parent agent names
    parent_lineage = os.environ.get("CODING_CAVE_LINEAGE", "")
    parent_agent = os.environ.get("CAV_AGENT_NAME", "")
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
    env["CODING_CAVE_CAV_BIN"] = CAV_BIN

    # User environment from cave.nix
    if user_env_path:
        env["CODING_CAVE_USER_ENV"] = str(user_env_path)

    # User CLAUDE.md
    user_claude_md = _host_home() / ".config" / "coding-cave" / "CLAUDE.md"
    if user_claude_md.exists():
        env["CODING_CAVE_USER_CLAUDE_MD"] = str(user_claude_md)

    # Crinj proxy (required for API auth).
    # Claude Code requires a token env var to consider itself "logged in",
    # but the actual credential is injected by crinj at the HTTP level.
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
        if use_pasta:
            pasta_bin = shutil.which("pasta")
            if not pasta_bin:
                raise SystemExit("pasta not found on PATH (install passt package)")
            daemon_port = daemon._agent_port(agent_id)
            pasta_cmd = [
                pasta_bin, "--quiet", "--config-net",
                "--map-host-loopback", PASTA_HOST_LOOPBACK,
                "--dns-forward", PASTA_HOST_LOOPBACK,
                "-T", f"{daemon_port},{CRINJ_PORT}",
                "-t", "none",
                "-u", "none", "-U", "none",
                "--",
            ]
        else:
            pasta_cmd = []

        proc = subprocess.Popen(pasta_cmd + claude_cmd, env=env)

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
        if parent_depth == 0:
            _teardown_nix_overlay(agent_id)
