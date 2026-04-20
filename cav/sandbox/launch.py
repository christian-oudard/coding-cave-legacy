"""Sandbox launcher for running agents.

Sets up the nix store overlay, builds the bwrap argv (via cav.bwrap),
and launches the sandbox through supervisor.py. The static bwrap args
and bind-mount paths come from lib/sandbox.nix (consumed as JSON by
cav.bwrap); the per-launch dynamic args (workspaces, GPG socket, DNS,
etc.) are composed here from the agent's environment.

Helper modules in this package:
- crinj.py        crinj proxy lifecycle
- nix_overlay.py  nix store overlay + DB seeding
- network.py      slirp4netns + iptables firewall
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import bwrap, user_env
from .crinj import (
    CRINJ_PORT,
    _crinj_ca_path,
    _crinj_register_session,
    _crinj_signal,
    _crinj_unregister_session,
    _host_home,
    _start_crinj,
)
from .network import SLIRP_DNS, SLIRP_GATEWAY, _apply_firewall, _start_slirp4netns
from .nix_overlay import _nix_overlay_dirs, _seed_nix_db, _setup_nix_overlay
from .. import config, registry
from ..constants import (
    CAV_BIN,
    CAVE_SHARED_REMOTE,
    HOST_SHARED_REMOTE,
    SUPERVISOR_PY,
    VERSION,
)
from ..git import daemon, git

log = logging.getLogger("cav")

GIT_WRAPPER = (Path(__file__).parent.parent / "scripts" / "git_wrapper.py").read_text()


def _signal_descendants(pid: int, sig: int) -> None:
    """Send a signal to all descendant processes of pid via /proc walk."""
    visited: set[int] = set()

    def walk(p: int) -> None:
        if p in visited:
            return
        visited.add(p)
        try:
            children = Path(f"/proc/{p}/task/{p}/children").read_text().split()
        except (FileNotFoundError, ProcessLookupError):
            return
        for child in children:
            child_pid = int(child)
            walk(child_pid)
            try:
                os.kill(child_pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    walk(pid)


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
    """Find the restricted GPG agent socket to forward into the cave.

    Outside a cave this is gpg-agent's 'extra' socket (restricted mode):
    signing works, secret-key export is blocked. Inside a cave, the
    parent bind-mounts its extra socket at the homedir's plain
    S.gpg-agent (no .extra suffix), so gpgconf's answer doesn't resolve;
    fall back to that bind-mount path.
    """
    try:
        sock = subprocess.run(
            ["gpgconf", "--list-dirs", "agent-extra-socket"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        sock = ""
    if sock and Path(sock).exists():
        return sock
    if not config.inside_cave():
        # Host: auto-launch gpg-agent (creates the extra socket).
        subprocess.run(["gpgconf", "--launch", "gpg-agent"],
                       capture_output=True, check=False)
        return sock if sock and Path(sock).exists() else None
    # Cave: the parent's restricted socket is bind-mounted here.
    homedir = _gpg_homedir()
    if homedir:
        bound = homedir / "S.gpg-agent"
        if bound.exists():
            return str(bound)
    return None


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


def _build_claude_cmd(session_id: str | None, resuming: bool,
                      prompt: str | None) -> list[str]:
    """Assemble the claude argv inside the sandbox.

    --session-id / --resume compose with --print, so iterative probing
    via 'cav agent -c --prompt ...' resumes the same Claude session
    instead of starting a fresh one each call.
    """
    cmd = ["claude", "--dangerously-skip-permissions"]
    if session_id:
        if resuming:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]
    if prompt:
        cmd += ["--print", prompt]
    return cmd


def launch(workspaces: list[tuple[Path, Path, str]], agent_name: str,
           project_slug: str = "project",
           session_id: str | None = None, resuming: bool = False,
           prompt: str | None = None,
           host_ports: list[int] | None = None):
    """Launch claude in a bwrap sandbox via the cave's wrapper binary.

    workspaces: list of (workspace_path, shared_repo, slug) tuples.
    project_slug: slug of the primary project (for cd on entry).
    """
    home = config.launch_home(agent_name)
    home.mkdir(parents=True, exist_ok=True)
    for _, _, slug in workspaces:
        _seed_claude_config(home, slug)

    # Extract numeric agent ID from display name (e.g. "coding-cave@5" -> "5")
    agent_id = agent_name.split("@")[-1]

    # Start git daemon in the background; the workspace layout checks
    # below don't need it, so we let both run concurrently and join
    # before the first remote URL is used.
    log.info("starting git daemon...")
    shared_repos_for_daemon = [(sr, slug) for _, sr, slug in workspaces]
    from concurrent.futures import ThreadPoolExecutor
    daemon_pool = ThreadPoolExecutor(max_workers=1)
    daemon_fut = daemon_pool.submit(
        daemon.start, agent_id, shared_repos_for_daemon)

    # Install git wrapper in shared home (shadows real git on PATH).
    agent_bin = home / "bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    wrapper = agent_bin / "git"
    if not wrapper.exists() or wrapper.read_text() != GIT_WRAPPER:
        wrapper.write_text(GIT_WRAPPER)
        wrapper.chmod(0o755)


    # Network isolation: every nesting level gets its own network
    # namespace via slirp4netns. Only the agent's daemon port and crinj
    # proxy port are forwarded via add_hostfwd. Other host loopback
    # services (including other agents' daemons) are unreachable.
    gateway = SLIRP_GATEWAY

    # Migrate old workspaces: rename "cave" -> "host-cave" if needed.
    for ws, _, _ in workspaces:
        r = git.git("remote", "get-url", HOST_SHARED_REMOTE, cwd=ws,
                    echo=False, check=False, capture=True)
        if r.returncode != 0:
            git.git("remote", "rename", CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE,
                    cwd=ws, echo=False, check=False, capture=True)

    # Verify local branch name and push refspec match the current scheme.
    # Migrates any pre-rename workspace that still has agent-<id>/<base>
    # checked out locally. Idempotent.
    from ..git import workspace
    for ws, _, _ in workspaces:
        workspace.verify_layout(ws, agent_id)

    # Join the daemon thread before we use daemon.remote_url.
    daemon_proc, daemon_dir = daemon_fut.result()
    daemon_pool.shutdown(wait=True)
    log.info("git daemon ready")

    # Update workspace remotes to use git:// via daemon (localhost for
    # host-side sync, then switch to slirp address for inside the namespace)
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug)
        git.git("remote", "set-url", HOST_SHARED_REMOTE, url,
                cwd=ws, echo=False)

    # Sync: fetch latest and fast-forward local base branches
    log.info("syncing workspace branches...")
    for ws, _, _ in workspaces:
        git.git("fetch", HOST_SHARED_REMOTE, cwd=ws, echo=False, check=False,
                capture=True)
        current = git.current_branch(cwd=ws)
        for branch in git.remote_branches(HOST_SHARED_REMOTE, cwd=ws):
            if config.parse_dev_branch(branch):
                continue
            if branch == current:
                continue  # Can't force-update the checked-out branch
            remote_ref = f"{HOST_SHARED_REMOTE}/{branch}"
            git.git("branch", "-f", branch, remote_ref, cwd=ws,
                    echo=False, check=False, capture=True)

    # Now set the remote to the slirp-reachable address for inside the namespace
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug, host=gateway)
        git.git("remote", "set-url", HOST_SHARED_REMOTE, url,
                cwd=ws, echo=False)

    # Build cave environment (skipped if unchanged)
    user_env_path = user_env.build()

    log.info("setting up nix overlay...")
    nix_merged = _setup_nix_overlay(home, agent_id)
    nix_db_dir = _seed_nix_db(home, agent_id, nix_merged)
    log.info("nix overlay ready")

    # Encode multiple workspaces as colon-separated lists
    ws_paths = ":".join(str(ws) for ws, _, _ in workspaces)
    shared_repos = ":".join(str(sr) for _, sr, _ in workspaces)
    slugs = ":".join(s for _, _, s in workspaces)

    # Build a minimal environment for the bwrap wrapper. Only include
    # vars the wrapper's runtime block and forwardEnv list actually need.
    # Using **os.environ here would leak host secrets (API keys, tokens,
    # SSH_AUTH_SOCK, etc.) via /proc/1/environ inside the sandbox.
    # TERM/COLORTERM/TZ are forwarded into the cave via forwardEnv.
    env = {}
    for var in ("TERM", "COLORTERM", "TZ"):
        if var in os.environ:
            env[var] = os.environ[var]
    env.update({
        "CODING_CAVE_VERSION": VERSION,
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

    env["CODING_CAVE_DNS_SERVER"] = SLIRP_DNS

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
    crinj_session = _crinj_register_session()
    crinj_proc = _start_crinj()
    env["CODING_CAVE_GATEWAY"] = gateway
    env["CODING_CAVE_PROXY_ADDR"] = f"{gateway}:{CRINJ_PORT}"
    env["CODING_CAVE_PROXY_CA"] = str(_crinj_ca_path())
    # Placeholder token so Claude Code considers itself "logged in".
    # Crinj replaces the Authorization header, so this never hits the API.
    env["CLAUDE_CODE_OAUTH_TOKEN"] = (
        "sk-ant-oat01-"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-AAAAAAAA"
    )

    try:
        claude_cmd = _build_claude_cmd(session_id, resuming, prompt)
        daemon_port = daemon._agent_port(agent_id)

        # One-directional sync pipe: supervisor.py writes a byte after
        # unshare(1) has created the namespace, then continues exec'ing
        # into bwrap without waiting. The parent reads the byte, applies
        # iptables (firewall before network), then starts slirp4netns.
        # No handshake needed: the child has no network until slirp4netns
        # starts, and nothing in the bwrap exec chain makes network calls.
        sync_r, sync_w = os.pipe()
        os.set_inheritable(sync_w, True)

        # Nix overlay env vars for supervisor.py.
        upper, work, merged = _nix_overlay_dirs(home, agent_id)
        env["CODING_CAVE_NS_SYNC_FD"] = str(sync_w)
        env["CODING_CAVE_NS_OVERLAY_MERGED"] = str(merged)
        env["CODING_CAVE_NS_OVERLAY_UPPER"] = str(upper)
        env["CODING_CAVE_NS_OVERLAY_WORK"] = str(work)

        # supervisor.py runs inside unshare(1) without nix-wrapped PATH.
        # Resolve absolute paths for the binaries it needs.
        # In subcaves, these binaries aren't on PATH but the parent passed
        # their absolute store paths via CODING_CAVE_NS_* env vars. Inherit them.
        mountpoint_bin = (shutil.which("mountpoint")
                          or os.environ.get("CODING_CAVE_NS_MOUNTPOINT"))
        if mountpoint_bin:
            env["CODING_CAVE_NS_MOUNTPOINT"] = mountpoint_bin

        unshare_bin = shutil.which("unshare")
        if not unshare_bin:
            raise SystemExit("unshare not found on PATH (install util-linux)")

        # Build the full bwrap argv from the sandbox config + env. The
        # returned pass_fds are inheritable fds (currently the seccomp
        # BPF filter) that must survive through unshare -> supervisor.py
        # -> bwrap.
        bwrap_argv, bwrap_fds = bwrap.build_argv(env, claude_cmd)

        # Map to uid/gid 0 inside the namespace so iptables-restore
        # (nf_tables backend) works. It checks getuid()==0 rather than
        # capabilities. bwrap creates its own inner user namespace so
        # the outer uid doesn't matter for the agent process.
        ns_cmd = [
            unshare_bin, "--user", "--net", "--mount",
            "--map-user=0", "--map-group=0",
            "--", sys.executable, SUPERVISOR_PY,
        ] + bwrap_argv

        log.info("launching sandbox...")
        proc = subprocess.Popen(
            ns_cmd, env=env, close_fds=False,
            pass_fds=(sync_w, *bwrap_fds),
        )
        os.close(sync_w)
        for fd in bwrap_fds:
            os.close(fd)

        # Record the bwrap child PID so cav cp can reach cave-internal
        # absolute paths via /proc/<cave_pid>/root/<path>.
        registry.set_cave_pid(agent_id, proc.pid)

        # Wait for supervisor.py to signal that the namespace exists.
        data = os.read(sync_r, 1)
        os.close(sync_r)
        if not data:
            raise SystemExit("sandbox child died during namespace setup")

        # Apply firewall before starting network. Zero gap.
        allowed = [daemon_port, CRINJ_PORT] + (host_ports or [])
        _apply_firewall(proc.pid, allowed)

        # Start slirp4netns (network comes up after firewall).
        slirp_proc = _start_slirp4netns(proc.pid)

        _forwarding = False

        def fwd(s, _f):
            nonlocal _forwarding
            if _forwarding:
                return
            _forwarding = True
            try:
                _signal_descendants(proc.pid, s)
            finally:
                _forwarding = False

        fwd_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH)
        orig = {s: signal.getsignal(s) for s in fwd_signals}
        for s in orig:
            signal.signal(s, fwd)

        try:
            return proc.wait()
        finally:
            slirp_proc.terminate()
            slirp_proc.wait(timeout=5)
            if _crinj_unregister_session(crinj_session):
                _crinj_signal(signal.SIGTERM)
                if crinj_proc is not None:
                    crinj_proc.wait(timeout=5)
            for s, handler in orig.items():
                signal.signal(s, handler)

    finally:
        daemon.stop(daemon_proc, daemon_dir)
