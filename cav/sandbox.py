"""Sandbox launcher for running agents.

Sets up the nix store overlay, builds the bwrap argv (via cav.bwrap),
and launches the sandbox through supervisor.py. The static bwrap args
and bind-mount paths come from lib/sandbox.nix (consumed as JSON by
cav.bwrap); the per-launch dynamic args (workspaces, GPG socket, DNS,
etc.) are composed here from the agent's environment.
"""

import logging
import os
import shutil
import signal
import stat
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import bwrap, config, daemon, git, registry, user_env
from .constants import (
    CAV_BIN,
    CAVE_SHARED_REMOTE,
    HOST_SHARED_REMOTE,
    SUPERVISOR_PY,
    VERSION,
)

log = logging.getLogger("cav")

GIT_WRAPPER = (Path(__file__).parent / "git_wrapper.py").read_text()


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


CRINJ_PORT = 10255
# slirp4netns built-in DNS forwarder address.
SLIRP_DNS = "10.0.2.3"


def _host_home() -> Path:
    """Real host home, even when running inside a cave."""
    return Path(os.environ.get("CODING_CAVE_HOST_HOME", str(Path.home())))


def _crinj_ca_path() -> Path:
    """Find crinj CA certificate.

    Inside a cave, prefer /etc/crinj-ca.pem (the ancestor cert inherited
    from the parent). Each nesting level's crinj runs in tunnel/passthrough
    mode, so the MITM cert is always from the outermost (host) crinj.
    The inherited /etc/crinj-ca.pem is the right cert at every depth.

    At depth 0 (host), crinj generates its cert in the home directory.
    """
    if config.inside_cave():
        inherited = Path("/etc/crinj-ca.pem")
        if inherited.exists():
            return inherited

    host_home = _host_home()
    cave_home = Path.home()
    xdg = os.environ.get("XDG_DATA_HOME", host_home / ".local" / "share")
    for path in [
        cave_home / ".crinj" / "gateway" / "ca.pem",
        host_home / ".crinj" / "gateway" / "ca.pem",
        Path(xdg) / "crinj" / "gateway" / "ca.pem",
    ]:
        if path.exists():
            return path
    raise SystemExit("crinj CA certificate not found. Expected at ~/.crinj/gateway/ca.pem")


def _crinj_listening() -> bool:
    """Check if crinj is listening on the proxy port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", CRINJ_PORT)) == 0


def _crinj_pid_file() -> Path:
    return config.DATA_DIR / "crinj.pid"


def _crinj_signal(sig: signal.Signals) -> None:
    """Send a signal to the running crinj process via its PID file."""
    pid_file = _crinj_pid_file()
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, sig)
        log.debug(f"sent {sig.name} to crinj pid {pid}")
    except (FileNotFoundError, ValueError, ProcessLookupError) as exc:
        log.debug(f"could not signal crinj: {exc}")


def _crinj_sessions_dir() -> Path:
    return config.DATA_DIR / "crinj-sessions"


def _crinj_register_session() -> Path:
    """Register this process as a crinj user. Returns the session file."""
    d = _crinj_sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Prune stale entries from dead processes.
    for f in d.iterdir():
        try:
            os.kill(int(f.name), 0)
        except (OSError, ValueError):
            f.unlink(missing_ok=True)
    session_file = d / str(os.getpid())
    session_file.touch()
    return session_file


def _crinj_unregister_session(session_file: Path) -> bool:
    """Unregister this session. Returns True if this was the last one."""
    session_file.unlink(missing_ok=True)
    d = _crinj_sessions_dir()
    return not any(d.iterdir()) if d.is_dir() else True


def _start_crinj() -> subprocess.Popen | None:
    """Start crinj if not already running.

    Returns the Popen handle if we started it, or None if an existing
    instance was reused. Callers use _crinj_register/unregister_session
    to track when the last session exits and crinj can be killed.

    Always passes --allow-empty-rules so the same launch path works at
    every nesting level (host == cave == subcave). Inner crinj instances
    chain through HTTPS_PROXY (set by the cave entrypoint from
    CODING_CAVE_PROXY_ADDR) so traffic flows out to the parent without
    re-injection; the outermost crinj is the one with real rules.
    """
    if not shutil.which("crinj"):
        raise SystemExit("crinj not found on PATH")

    if _crinj_listening():
        log.debug("crinj already listening, sending SIGHUP to reload config")
        _crinj_signal(signal.SIGHUP)
        return None

    log.debug("starting crinj...")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Crinj always reads its config file, even with --allow-empty-rules.
    # Touch an empty rules.toml if missing so a fresh cave home works
    # without manual setup; the host case is a no-op when the user
    # already has real rules.
    rules = Path(os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "crinj" / "rules.toml"
    if not rules.exists():
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.touch()
    proc = subprocess.Popen(
        ["crinj", "--port", str(CRINJ_PORT), "--allow-empty-rules"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
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

    _crinj_pid_file().write_text(str(proc.pid))
    return proc


def _prune_nix_caches() -> None:
    """Remove nix cache entries that reference store paths no longer on disk.

    The fetcher cache (flake URL -> store path) and eval cache (flake
    fingerprint -> drv paths) persist across sessions but can reference
    paths the host has since GC'd.
    """
    nix_cache = config.persistent_home() / ".cache" / "nix"

    try:
        store_names = set(os.listdir("/nix/store"))
    except OSError:
        store_names = set()

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
                if sp not in store_names:
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
        if rows and any(
                r[0][len("/nix/store/"):].split("/", 1)[0] not in store_names
                for r in rows):
            db_path.unlink(missing_ok=True)


def _rm_store_path(path: Path):
    """Remove a nix store path from the overlay upper layer.

    Nix store paths are read-only (dr-xr-xr-x). shutil.rmtree fails on
    them unless we chmod the parent directory first.  Non-directory entries
    (symlinks, .drv files, regular files) are unlinked directly.
    """
    if path.is_symlink() or not path.is_dir():
        try:
            path.unlink()
        except PermissionError:
            os.chmod(str(path.parent), stat.S_IRWXU)
            path.unlink()
        return
    def _make_writable(_func, p, _exc):
        parent = os.path.dirname(p)
        os.chmod(parent, stat.S_IRWXU)
        _func(p)
    shutil.rmtree(path, onexc=_make_writable)


def _parse_drv_outputs(drv_path: str) -> dict[str, str]:
    """Parse a .drv file and return {output_name: output_path}."""
    try:
        content = Path(drv_path).read_text()
    except (OSError, UnicodeDecodeError):
        return {}
    end = content.find(']')
    if end < 0:
        return {}
    import re
    return dict(re.findall(r'\("([^"]+)","(/nix/store/[^"]+)"', content[:end]))


def _repair_derivation_outputs(db: "sqlite3.Connection", store_root: Path):
    """Fill missing DerivationOutputs entries from .drv files on disk.

    Nix doesn't populate DerivationOutputs for builds done in an overlay
    store. This causes nix-store --realise to fail with map::at when later
    operations look up derivation outputs.
    """
    missing = db.execute(
        "SELECT vp.id, vp.path, vp.deriver FROM ValidPaths vp "
        "LEFT JOIN DerivationOutputs do2 ON do2.path = vp.path "
        "WHERE vp.deriver != '' AND do2.path IS NULL"
    ).fetchall()
    if not missing:
        return
    # Cache: deriver path -> (ValidPaths.id, parsed outputs)
    drv_cache: dict[str, tuple[int | None, dict[str, str]]] = {}
    repaired = 0
    for _vid, path, deriver in missing:
        if deriver not in drv_cache:
            drv_row = db.execute(
                "SELECT id FROM ValidPaths WHERE path = ?", (deriver,)
            ).fetchone()
            drv_id = drv_row[0] if drv_row else None
            outputs = _parse_drv_outputs(deriver) if drv_id else {}
            drv_cache[deriver] = (drv_id, outputs)
        drv_id, outputs = drv_cache[deriver]
        if not drv_id:
            continue
        # Find which output name maps to this path
        output_name = None
        for name, out_path in outputs.items():
            if out_path == path:
                output_name = name
                break
        if not output_name:
            continue
        db.execute(
            "INSERT OR IGNORE INTO DerivationOutputs (drv, id, path) "
            "VALUES (?, ?, ?)", (drv_id, output_name, path),
        )
        repaired += 1
    if repaired:
        db.commit()


def _seed_nix_db(home: Path, agent_id: str,
                 merged_path: str = "") -> Path | None:
    """Backup parent nix DB, preserving cave-installed package registrations.

    Always starts from a fresh backup of whatever nix DB is visible at
    /nix/var/nix/db/db.sqlite (the real host DB when launched on the host,
    or the parent cave's bind-mounted DB when launched inside a cave), then
    merges back cave-only paths whose files still exist on disk. Finally,
    removes stale entries for paths the parent GC'd after the snapshot.

    Args:
        home: The child's persistent home directory.
        agent_id: Numeric agent id, used to scope the DB to this agent
            (matches the per-agent overlay layout).
        merged_path: The overlay merged directory. When provided, stale
            DB entries are validated against this path (which reflects the
            parent store plus this level's upper layer).

    Returns the DB directory path, or None if the parent DB doesn't exist.
    """
    parent_db = Path("/nix/var/nix/db/db.sqlite")
    if not parent_db.exists():
        return None

    db_dir = (home / ".local" / "share" / "coding-cave"
              / "nix-overlay" / f"agent-{agent_id}" / "db")
    db_dir.mkdir(parents=True, exist_ok=True)
    cave_db = db_dir / "db.sqlite"
    stamp = db_dir / "db.stamp"

    # Skip reseed if the host DB hasn't changed since our last run.
    # mtime+size captures every host-side install or GC.
    parent_st = parent_db.stat()
    state = f"{parent_st.st_mtime_ns}:{parent_st.st_size}"
    if cave_db.exists() and stamp.exists():
        try:
            if stamp.read_text() == state:
                log.debug("nix DB: host unchanged, skipping reseed")
                return db_dir
        except OSError:
            pass

    # Use the merged overlay for existence checks when available,
    # otherwise fall back to the parent's /nix/store view.
    # When the overlay mount is deferred to supervisor.py, the merged dir
    # exists but is empty. Fall back to /nix/store in that case to
    # avoid deleting all DB entries as "stale".
    store_root = Path("/nix/store")
    if merged_path:
        candidate = Path(merged_path)
        if _overlay_alive(candidate):
            store_root = candidate

    try:
        store_names = set(os.listdir(store_root))
    except OSError:
        store_names = set()

    try:
        # Collect cave-only paths and their DerivationOutputs before overwriting
        cave_only_rows = []
        cave_only_deriv_outputs = []  # (drv_path, output_name, output_path)
        if cave_db.exists():
            old = sqlite3.connect(f"file:{cave_db}?mode=ro", uri=True)
            host = sqlite3.connect(f"file:{parent_db}?mode=ro", uri=True)
            host_paths = {r[0] for r in host.execute("SELECT path FROM ValidPaths")}
            cave_only_paths = set()
            for row in old.execute(
                "SELECT path, hash, registrationTime, deriver, narSize, "
                "ultimate, sigs, ca FROM ValidPaths"
            ):
                if row[0] not in host_paths:
                    if Path(row[0]).name in store_names:
                        cave_only_rows.append(row)
                        cave_only_paths.add(row[0])
            # Preserve DerivationOutputs for cave-only derivations
            for row in old.execute(
                "SELECT vp.path, do2.id, do2.path FROM DerivationOutputs do2 "
                "JOIN ValidPaths vp ON vp.id = do2.drv"
            ):
                if row[0] in cave_only_paths:
                    cave_only_deriv_outputs.append(row)
            host.close()
            old.close()

        # Fresh backup from parent DB
        for wal in db_dir.glob("db.sqlite-*"):
            wal.unlink()
        src = sqlite3.connect(f"file:{parent_db}?mode=ro", uri=True)
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

        # Restore cave-only DerivationOutputs (ids changed, remap via path)
        for drv_path, output_name, output_path in cave_only_deriv_outputs:
            drv_row = dst.execute(
                "SELECT id FROM ValidPaths WHERE path = ?", (drv_path,)
            ).fetchone()
            if drv_row:
                dst.execute(
                    "INSERT OR IGNORE INTO DerivationOutputs (drv, id, path) "
                    "VALUES (?, ?, ?)", (drv_row[0], output_name, output_path),
                )
        if cave_only_deriv_outputs:
            dst.commit()

        # Repair DerivationOutputs for overlay-built paths.
        # Nix doesn't populate this table for builds in the overlay store,
        # causing "map::at" errors from nix-store --realise.
        _repair_derivation_outputs(dst, store_root)

        # Remove stale entries: paths the host GC'd after the DB snapshot.
        # The overlay reflects the live host store, so missing paths are
        # genuinely gone.
        stale_ids = []
        for vid, path in dst.execute("SELECT id, path FROM ValidPaths"):
            if Path(path).name not in store_names:
                stale_ids.append(vid)
        if stale_ids:
            dst.executemany(
                "DELETE FROM Refs WHERE referrer = ? OR reference = ?",
                [(vid, vid) for vid in stale_ids],
            )
            dst.executemany(
                "DELETE FROM ValidPaths WHERE id = ?",
                [(vid,) for vid in stale_ids],
            )
            dst.commit()

        # Clean the overlay upper layer: remove orphaned store paths
        # not in the DB (leftover build artifacts whose DB entries were
        # lost during re-seeding). Lock file shadows are cleaned by
        # _setup_nix_overlay, not here.
        if merged_path:
            upper = Path(merged_path).parent / "upper"
            if upper.is_dir():
                db_paths = {Path(r[0]).name
                            for r in dst.execute("SELECT path FROM ValidPaths")}
                for entry in upper.iterdir():
                    if entry.name.startswith("."):
                        continue
                    if entry.name.endswith(".lock"):
                        # Skip lock file shadows. These are managed by
                        # _setup_nix_overlay and supervisor.py.
                        continue
                    if entry.name not in db_paths:
                        _rm_store_path(entry)

        dst.close()
        src.close()
    except sqlite3.Error as e:
        log.warning(f"nix DB seed failed: {e}")
        return db_dir

    _prune_nix_caches()

    # Record the host DB state we just synced from, so the next launch
    # can skip the reseed if nothing on the host changed.
    try:
        stamp.write_text(state)
    except OSError:
        pass

    return db_dir


def _nix_overlay_dirs(home: Path, agent_id: str) -> tuple[Path, Path, Path]:
    """Return (upper, work, merged) for the nix store overlay.

    Each agent gets its own upper/work/merged. Kernel overlayfs
    requires unique upperdir/workdir per mount: sharing them across
    mounts is undefined behavior and produces orphan inodes, ESTALE
    errors, and failed unlinks. Each nesting level also gets its own
    home, so host-level and subcave agents alike land under
    <home>/.local/share/coding-cave/nix-overlay/agent-<id>/.
    """
    agent_dir = (home / ".local" / "share" / "coding-cave"
                 / "nix-overlay" / f"agent-{agent_id}")
    upper = agent_dir / "upper"
    work = agent_dir / "work"
    merged = agent_dir / "merged"
    for d in (upper, work, merged):
        d.mkdir(parents=True, exist_ok=True)
    return upper, work, merged



def _overlay_alive(merged: Path) -> bool:
    """Check if the overlay mount at merged is functional.

    Must verify both that the overlay has content (is mounted, not just an
    empty directory) and that writes work.
    """
    if not any(merged.iterdir()):
        return False
    probe = merged / ".overlay_probe"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _rmtree_readonly(path: Path):
    """Remove a directory tree that may contain read-only directories."""
    os.chmod(path, stat.S_IRWXU)
    for dirpath, _dns, _fns in os.walk(path):
        os.chmod(dirpath, stat.S_IRWXU)
    shutil.rmtree(path)


def _migrate_shared_overlay(home: Path, agent_id: str) -> None:
    """One-shot rename of legacy shared/ to agent-<id>/.

    Pre-#32 the overlay lived at nix-overlay/shared/ and was reused
    across every agent sharing a home. Kernel overlayfs forbids that
    (undefined behavior on shared upperdir), so each agent now gets its
    own agent-<id>/. Rename lets the first agent to launch inherit the
    old upper's packages; subsequent agents start empty.
    """
    root = home / ".local" / "share" / "coding-cave" / "nix-overlay"
    legacy = root / "shared"
    target = root / f"agent-{agent_id}"
    if legacy.is_dir() and not target.exists():
        legacy.rename(target)


def _setup_nix_overlay(home: Path, agent_id: str) -> str:
    """Prepare the nix store overlay directories. Returns merged path.

    Does not mount. The kernel overlayfs mount happens inside the user
    namespace via supervisor.py.
    """
    _migrate_shared_overlay(home, agent_id)
    upper, work, merged = _nix_overlay_dirs(home, agent_id)

    # Clean the upper layer of stale entries.
    try:
        host_names = set(os.listdir("/nix/store"))
    except OSError:
        host_names = set()
    stale = []
    for entry in upper.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.name.endswith(".lock"):
            # Remove stale nix lock files and lock-file shadows (empty
            # regular files) and char-device whiteouts from previous
            # buggy code. Keep source derivations (non-empty, e.g.
            # Cargo.lock) for the GC check below.
            s = entry.lstat()
            if stat.S_ISCHR(s.st_mode) or s.st_size == 0:
                entry.unlink()
                continue
        if entry.name not in host_names:
            stale.append(entry)
    for entry in stale:
        _rm_store_path(entry)
    # Always reset work dir. Previous overlay may have left stale inode
    # metadata. Work dir entries inherit nix store permissions (read-only),
    # so chmod before removing.
    work_inner = work / "work"
    if work_inner.is_dir():
        _rmtree_readonly(work_inner)
        work_inner.mkdir()

    return str(merged)


SLIRP_GATEWAY = "10.0.2.2"


def _apply_firewall(pid: int, allowed_ports: list[int]):
    """Apply iptables firewall rules inside the child's network namespace.

    Uses nsenter to enter the child's user+net namespace and applies rules
    atomically via iptables-restore. Only the listed TCP ports can reach
    the gateway (host loopback). All other gateway traffic is dropped.

    The rules live in the outer user namespace, so bwrap's inner user
    namespace (where the agent runs) cannot modify them.
    """
    rules = "*filter\n"
    for port in allowed_ports:
        rules += (f"-A OUTPUT -d {SLIRP_GATEWAY} -p tcp"
                  f" --dport {port} -j ACCEPT\n")
    rules += f"-A OUTPUT -d {SLIRP_GATEWAY} -j DROP\nCOMMIT\n"

    nsenter = shutil.which("nsenter")
    if not nsenter:
        raise SystemExit("nsenter not found on PATH (install util-linux)")
    iptables_restore = shutil.which("iptables-restore")
    if not iptables_restore:
        raise SystemExit(
            "iptables-restore not found on PATH (install iptables)")

    r = subprocess.run(
        [nsenter, "--preserve-credentials",
         f"--user=/proc/{pid}/ns/user", f"--net=/proc/{pid}/ns/net",
         iptables_restore],
        input=rules, text=True, capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"iptables-restore failed: {r.stderr.strip()}")


def _start_slirp4netns(pid: int) -> subprocess.Popen:
    """Start slirp4netns for a child process.

    Configures tap0 with default 10.0.2.0/24 network. Uses --ready-fd
    to know when networking is fully configured before returning.
    """
    slirp_bin = shutil.which("slirp4netns")
    if not slirp_bin:
        raise SystemExit("slirp4netns not found on PATH")

    ready_r, ready_w = os.pipe()
    slirp_proc = subprocess.Popen(
        [slirp_bin, "--configure", f"--ready-fd={ready_w}",
         str(pid), "tap0"],
        start_new_session=True,
        pass_fds=(ready_w,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(ready_w)

    # Block until slirp4netns signals the network is ready.
    os.read(ready_r, 1)
    os.close(ready_r)

    if slirp_proc.poll() is not None:
        raise SystemExit(
            f"slirp4netns exited with code {slirp_proc.returncode}")

    return slirp_proc



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
    from . import workspace
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
