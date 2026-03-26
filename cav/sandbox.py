"""Sandbox launcher for running agents.

Sets up the nix store overlay, configures environment variables for the
jail.nix-generated sandbox binary, and launches it. The sandbox binary
handles bwrap args, /etc, SSL certs, PATH, and entry script.
"""

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import config, daemon, git, user_env

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
        log = config.DATA_DIR / "crinj.log"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["crinj", "--port", str(CRINJ_PORT)],
            stdout=open(log, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(20):
            if _crinj_listening():
                break
            time.sleep(0.25)

    return f"127.0.0.1:{CRINJ_PORT}"


def _setup_nix_overlay() -> str:
    """Mount a writable nix store overlay. Returns merged path."""
    overlay_dir = config.nix_overlay_dir()
    upper = overlay_dir / "upper"
    work = overlay_dir / "work"
    merged = overlay_dir / "merged"

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


def _teardown_nix_overlay():
    """Unmount the nix store overlay."""
    merged = config.nix_overlay_dir() / "merged"
    if merged.exists():
        subprocess.run(["fusermount", "-u", str(merged)], check=False)


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
           session_id: str | None = None):
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
    _post_commit_hook = (
        "#!/bin/sh\n"
        "git fetch cave >/dev/null 2>&1 || true\n"
        "git push cave HEAD >/dev/null 2>&1 || true\n"
    )
    for ws, _, _ in workspaces:
        hook = ws / ".git" / "hooks" / "post-commit"
        if not hook.exists() or hook.read_text() != _post_commit_hook:
            hook.write_text(_post_commit_hook)
            hook.chmod(0o755)

    # Update workspace remotes to use git:// via daemon
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug)
        git.git("remote", "set-url", config.SHARED_REMOTE, url,
                cwd=ws, echo=False)

    # Sync: fetch latest and fast-forward local shared branches
    for ws, _, _ in workspaces:
        git.git("fetch", config.SHARED_REMOTE, cwd=ws, echo=False, check=False,
                capture=True)
        current = git.current_branch(cwd=ws)
        for branch in git.remote_branches(config.SHARED_REMOTE, cwd=ws):
            if config.parse_agent_branch(branch):
                continue
            if branch == current:
                continue  # Can't force-update the checked-out branch
            remote_ref = f"{config.SHARED_REMOTE}/{branch}"
            git.git("branch", "-f", branch, remote_ref, cwd=ws,
                    echo=False, check=False, capture=True)

    # Build user environment from cave.nix (cached)
    user_env_path = user_env.build()

    nix_merged = _setup_nix_overlay()

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

    if recursive:
        env["CODING_CAVE_RECURSIVE"] = "1"

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
    env["CODING_CAVE_PROXY_ADDR"] = _start_crinj()
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
            claude_cmd += ["--resume", session_id]
        proc = subprocess.Popen(claude_cmd, env=env)

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
        _teardown_nix_overlay()
