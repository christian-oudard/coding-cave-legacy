"""Assemble the bwrap argv for launching the coding-cave sandbox.

Reads the JSON config baked at nix build time (paths, static bwrap
args, env vars to forward) and combines it with the caller's runtime
environment to build the full argv. Replaces the bash wrapper that
used to live at ${sandbox}/bin/coding-cave.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from .constants import SANDBOX_CONFIG

_cfg_cache: dict | None = None


def _config() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = json.loads(Path(SANDBOX_CONFIG).read_text())
    return _cfg_cache


def _write_cpuinfo() -> str:
    """Create a synthetic /proc/cpuinfo and return its path.

    Node's os.cpus() parses this file and crashes on empty input; tools
    like make -j / nproc use sched_getaffinity(2) instead. Emit N generic
    processor blocks (N = cpu_count) with a fixed fake model to hide the
    host CPU brand.
    """
    ncpu = os.cpu_count() or 1
    blocks = []
    for i in range(ncpu):
        blocks.append(
            f"processor\t: {i}\n"
            f"vendor_id\t: GenericCPU\n"
            f"cpu family\t: 0\n"
            f"model\t\t: 0\n"
            f"model name\t: Generic CPU\n"
            f"stepping\t: 0\n"
            f"cpu MHz\t\t: 0.000\n"
            f"cache size\t: 0 KB\n"
            f"siblings\t: {ncpu}\n"
            f"core id\t\t: {i}\n"
            f"cpu cores\t: {ncpu}\n"
            f"flags\t\t:\n"
            f"bogomips\t: 0.00\n\n"
        )
    fd, path = tempfile.mkstemp(prefix="cave-cpuinfo-")
    os.write(fd, "".join(blocks).encode())
    os.close(fd)
    return path


def _write_resolv_conf(dns_server: str) -> str:
    """Create a resolv.conf pointing at the slirp4netns forwarder."""
    fd, path = tempfile.mkstemp(prefix="cave-resolv-")
    os.write(fd, f"nameserver {dns_server}\n".encode())
    os.close(fd)
    return path


def build_argv(env: dict[str, str],
               command: list[str]) -> tuple[list[str], list[int]]:
    """Return (argv, pass_fds) for launching bwrap with this command.

    pass_fds contains the seccomp filter fd; the caller must pass it
    through to the bwrap process (via subprocess pass_fds / close_fds).
    """
    cfg = _config()
    args: list[str] = [cfg["bwrap"], *cfg["staticArgs"]]

    # Forward caller env vars that are set.
    for var in cfg["forwardEnv"]:
        if var in env:
            args.extend(["--setenv", var, env[var]])

    # Nix store overlay (writable for autonomous package management).
    nix_merged = env.get("CODING_CAVE_NIX_MERGED")
    if nix_merged:
        args.extend(["--bind", nix_merged, "/nix/store"])
        args.extend(["--tmpfs", "/nix/var", "--dir", "/nix/var/nix"])
        nix_db_dir = env.get("CODING_CAVE_NIX_DB_DIR")
        if nix_db_dir and os.path.isdir(nix_db_dir):
            args.extend(["--bind", nix_db_dir, "/nix/var/nix/db"])

    # Subcave support: /dev/net/tun for slirp4netns.
    if os.path.exists("/dev/net/tun"):
        args.extend(["--dev-bind", "/dev/net/tun", "/dev/net/tun"])

    # Persistent home.
    cave_home = env["CODING_CAVE_HOME"]
    args.extend(["--bind", cave_home, "/home/cave-coder"])

    # Seed mutable .claude.json (onboarding skip) only if absent. Everything
    # else under ~/.claude/ comes from the user-env's baked files/ tree,
    # copied in by the inside-bwrap entry script.
    claude_json = Path(cave_home) / ".claude.json"
    if not claude_json.exists():
        shutil.copyfile(cfg["dotClaudeJsonDefault"], claude_json)

    # Per-session user CLAUDE.md addendum.
    user_claude_md = env.get("CODING_CAVE_USER_CLAUDE_MD")
    if user_claude_md and os.path.isfile(user_claude_md):
        args.extend(["--ro-bind", user_claude_md,
                     "/tmp/.cave-user-claude.md"])

    # Agent workspaces at /projects/<slug> (colon-separated lists).
    args.extend(["--dir", "/projects"])
    workspaces = env.get("CODING_CAVE_WORKSPACES", "").split(":")
    slugs = env.get("CODING_CAVE_PROJECT_SLUGS", "").split(":")
    for ws, slug in zip(workspaces, slugs):
        if ws and slug:
            args.extend(["--bind", ws, f"/projects/{slug}"])

    # Shared repos (read-only for alternates object access). Mount under
    # /projects/<slug>/.coding-cave/ instead of at the host-absolute path,
    # to avoid leaking the host home dir. Path must match
    # constants.HOST_SHARED_REPO_DIR.
    shared = env.get("CODING_CAVE_SHARED_REPOS", "").split(":")
    for repo, slug in zip(shared, slugs):
        if repo and slug:
            args.extend(["--ro-bind", repo,
                         f"/projects/{slug}/.coding-cave/host-cave-shared.git"])

    # Git config (copy host .gitconfig into persistent home).
    gitconfig = env.get("CODING_CAVE_GITCONFIG")
    if gitconfig and os.path.isfile(gitconfig):
        shutil.copyfile(gitconfig, Path(cave_home) / ".gitconfig")

    # GPG: socket + public keyring for commit signing. Bind the host
    # agent socket to BOTH the homedir path and the runtime dir path.
    # gpg prefers /run/user/<uid>/gnupg/ and will auto-start a local
    # (keyless) agent there if it finds nothing.
    gpg_socket = env.get("CODING_CAVE_GPG_SOCKET")
    if gpg_socket and os.path.exists(gpg_socket):
        args.extend(["--bind", gpg_socket,
                     "/home/cave-coder/.gnupg/S.gpg-agent"])
        args.extend(["--dir", "/run/user/1000/gnupg"])
        args.extend(["--bind", gpg_socket,
                     "/run/user/1000/gnupg/S.gpg-agent"])
    gpg_pubring = env.get("CODING_CAVE_GPG_PUBRING")
    if gpg_pubring and os.path.isfile(gpg_pubring):
        args.extend(["--ro-bind", gpg_pubring,
                     f"/home/cave-coder/.gnupg/{os.path.basename(gpg_pubring)}"])

    # crinj proxy CA.
    proxy_ca = env.get("CODING_CAVE_PROXY_CA")
    if proxy_ca and os.path.isfile(proxy_ca):
        args.extend(["--ro-bind", proxy_ca, "/etc/crinj-ca.pem"])

    # DNS override inside the netns. Host's resolv.conf may point to a
    # loopback resolver (e.g. systemd-resolved at 127.0.0.53) which is
    # unreachable in the namespace; point to slirp4netns's built-in
    # forwarder (10.0.2.3) instead.
    dns_server = env.get("CODING_CAVE_DNS_SERVER")
    if dns_server:
        args.extend(["--ro-bind", _write_resolv_conf(dns_server),
                     "/etc/resolv.conf"])

    # Mask host-system fingerprinting via /proc.
    for p in cfg["procMaskFiles"]:
        args.extend(["--ro-bind-try", cfg["procMask"], p])
    for d in cfg["procMaskDirs"]:
        args.extend(["--ro-bind-try", cfg["procMaskDir"], d])
    args.extend(["--ro-bind", cfg["procMaskMeminfo"], "/proc/meminfo"])

    # Synthetic /proc/cpuinfo.
    args.extend(["--ro-bind", _write_cpuinfo(), "/proc/cpuinfo"])

    # Seccomp BPF filter, passed via an inheritable FD. bwrap reads it
    # in the child just before execve; os.set_inheritable clears
    # CLOEXEC so the FD survives subprocess boundaries.
    seccomp_fd = os.open(cfg["seccompFilter"], os.O_RDONLY)
    os.set_inheritable(seccomp_fd, True)
    args.extend(["--seccomp", str(seccomp_fd)])

    # Finally: bash /entry.sh <command...>
    args.extend([cfg["bash"], cfg["entry"]])
    args.extend(command)

    return args, [seccomp_fd]
