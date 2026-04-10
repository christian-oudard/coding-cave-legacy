"""Sandbox launcher for running agents.

Sets up the nix store overlay, configures environment variables for the
jail.nix-generated sandbox binary, and launches it. The sandbox binary
handles bwrap args, /etc, SSL certs, PATH, and entry script.
"""

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

from . import config, daemon, git, registry, user_env

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
    "$_CAV_REAL_GIT" push --force host-cave "HEAD:refs/heads/$_branch" >/dev/null 2>&1 || true
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
# slirp4netns built-in DNS forwarder address.
SLIRP_DNS = "10.0.2.3"


def _host_home() -> Path:
    """Real host home, even when running inside a cave."""
    return Path(os.environ.get("CODING_CAVE_HOST_HOME", str(Path.home())))


def _crinj_ca_path() -> Path:
    """Find crinj CA certificate.

    Inside a cave, prefer /etc/crinj-ca.pem (the ancestor cert inherited
    from jail.nix). Each nesting level's crinj runs in tunnel/passthrough
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


def _start_crinj() -> str:
    """Start crinj if not already running. Returns proxy address.

    Always passes --allow-empty-rules so the same launch path works at
    every nesting level (host == cave == subcave). Inner crinj instances
    chain through HTTPS_PROXY (set by the cave entrypoint from
    CODING_CAVE_PROXY_ADDR) so traffic flows out to the parent without
    re-injection; the outermost crinj is the one with real rules.
    """
    if not shutil.which("crinj"):
        raise SystemExit("crinj not found on PATH")

    if _crinj_listening():
        log.debug("crinj already listening")
    else:
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


def _seed_nix_db(home: Path, merged_path: str = "") -> Path | None:
    """Backup parent nix DB, preserving cave-installed package registrations.

    Always starts from a fresh backup of whatever nix DB is visible at
    /nix/var/nix/db/db.sqlite (the real host DB when launched on the host,
    or the parent cave's bind-mounted DB when launched inside a cave), then
    merges back cave-only paths whose files still exist on disk. Finally,
    removes stale entries for paths the parent GC'd after the snapshot.

    Args:
        home: The child's persistent home directory. DB is stored under
            its data dir so each nesting level has its own DB.
        merged_path: The mounted fuse-overlayfs merged directory. When
            provided, stale DB entries are validated against this path
            (which reflects the parent store plus this level's upper layer).

    Returns the DB directory path, or None if the parent DB doesn't exist.
    """
    parent_db = Path("/nix/var/nix/db/db.sqlite")
    if not parent_db.exists():
        return None

    db_dir = home / ".local" / "share" / "coding-cave" / "nix-overlay" / "shared" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    cave_db = db_dir / "db.sqlite"

    # Use the merged overlay for existence checks when available,
    # otherwise fall back to the parent's /nix/store view.
    # When the overlay mount is deferred to cave-ns.sh, the merged dir
    # exists but is empty. Fall back to /nix/store in that case to
    # avoid deleting all DB entries as "stale".
    store_root = Path("/nix/store")
    if merged_path:
        candidate = Path(merged_path)
        if _overlay_alive(candidate):
            store_root = candidate

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
                    basename = Path(row[0]).name
                    if (store_root / basename).exists():
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
            basename = Path(path).name
            if not (store_root / basename).exists():
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

        # Clean the overlay upper layer:
        # 1. Remove orphaned store paths not in the DB (leftover build
        #    artifacts whose DB entries were lost during re-seeding).
        # 2. Remove stale .lock files (nix build locks are transient).
        if merged_path:
            upper = Path(merged_path).parent / "upper"
            if upper.is_dir():
                db_paths = {Path(r[0]).name
                            for r in dst.execute("SELECT path FROM ValidPaths")}
                for entry in upper.iterdir():
                    if entry.name.startswith("."):
                        continue
                    if entry.name.endswith(".lock"):
                        entry.unlink(missing_ok=True)
                    elif entry.name not in db_paths:
                        _rm_store_path(entry)

        dst.close()
        src.close()
    except sqlite3.Error:
        pass

    _prune_nix_caches()

    return db_dir


def _nix_overlay_dirs(home: Path) -> tuple[Path, Path, Path]:
    """Return (upper, work, merged) for the nix store overlay.

    Each nesting level gets its own home directory, so the "shared/"
    overlay is naturally distinct per level without needing lineage.
    """
    shared = home / ".local" / "share" / "coding-cave" / "nix-overlay" / "shared"
    upper = shared / "upper"
    work = shared / "work"
    merged = shared / "merged"
    for d in (upper, work, merged):
        d.mkdir(parents=True, exist_ok=True)
    return upper, work, merged


def _mount_fuse_overlay(upper: Path, work: Path, merged: Path):
    # squash_to_uid/gid: Host store paths are owned by the nix daemon
    # (uid 65534). Without squashing, fuse-overlayfs can't copy-up these
    # files on chmod/chown, causing EPERM when nix tries to register
    # or validate existing store paths.
    uid = os.getuid()
    gid = os.getgid()
    subprocess.run(
        [
            "fuse-overlayfs",
            "-o", (f"lowerdir=/nix/store,upperdir={upper},workdir={work}"
                   f",squash_to_uid={uid},squash_to_gid={gid}"),
            str(merged),
        ],
        check=True, capture_output=True, text=True,
    )


def _unmount_stale_overlay(merged: Path) -> None:
    """Try to unmount a dead fuse-overlayfs on merged/."""
    for name in ("fusermount3", "fusermount"):
        bin = shutil.which(name)
        if bin:
            subprocess.run([bin, "-u", str(merged)],
                           capture_output=True, check=False)
            return



def _overlay_alive(merged: Path) -> bool:
    """Check if the fuse-overlayfs mount at merged is functional.

    A stale mount (fuse daemon died) leaves the directory present but
    inoperable. Test with a probe file to confirm writes actually work.
    """
    probe = merged / ".overlay_probe"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _setup_shared_nix_overlay(home: Path) -> str:
    """Mount the shared writable nix store overlay. Returns merged path.

    All agents at a given nesting level share a single overlay. The
    overlay persists across agent launches and is not torn down when an
    agent exits. Each nesting level gets its own overlay via its own home.

    Tries a direct mount (works when fusermount3 is setuid). If that fails,
    defers the mount to cave-ns.sh which runs inside the user namespace
    created by unshare(1).
    """
    upper, work, merged = _nix_overlay_dirs(home)

    if _overlay_alive(merged):
        return str(merged)

    # Unmount stale fuse-overlayfs if one is lingering on merged/.
    # Without this, the new mount attempt fails ("already mounted") and
    # cave-ns.sh's mountpoint check also skips remounting.
    _unmount_stale_overlay(merged)

    # Before mounting, clean the upper layer of paths the host GC'd.
    # If the host ran nix store gc after the overlay was last used,
    # the upper dir may reference paths no longer in the lower layer
    # (host's /nix/store). This stale metadata confuses fuse-overlayfs.
    # Also clean the work dir since it caches inode state that becomes
    # invalid when lower-layer paths disappear.
    host_store = Path("/nix/store")
    stale = []
    for entry in upper.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.name.endswith(".lock"):
            entry.unlink(missing_ok=True)
            continue
        if not (host_store / entry.name).exists():
            stale.append(entry)
    for entry in stale:
        _rm_store_path(entry)
    if stale:
        # Reset work dir to clear stale inode metadata
        work_inner = work / "work"
        if work_inner.is_dir():
            shutil.rmtree(work_inner, ignore_errors=True)
            work_inner.mkdir()

    try:
        _mount_fuse_overlay(upper, work, merged)
    except subprocess.CalledProcessError:
        pass  # Deferred: mounted in child via cave-ns.sh

    return str(merged)


SLIRP_GATEWAY = "10.0.2.2"

# Substituted at nix build time with the cave-ns.sh helper path.
CAVE_NS_SH = "@CAVE_NS_SH@"


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


def launch(workspaces: list[tuple[Path, Path, str]], agent_name: str,
           project_slug: str = "project",
           session_id: str | None = None, resuming: bool = False,
           prompt: str | None = None,
           host_ports: list[int] | None = None):
    """Launch claude in a bwrap sandbox via the jail.nix binary.

    workspaces: list of (workspace_path, shared_repo, slug) tuples.
    project_slug: slug of the primary project (for cd on entry).
    """
    if config.inside_cave():
        # Subcave: isolated home so nix overlay dirs don't collide
        # with the parent's. Keyed by agent name (unique per project).
        home = config.DATA_DIR / "subcave-homes" / agent_name
    else:
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

    # Network isolation: every nesting level gets its own network
    # namespace via slirp4netns. Only the agent's daemon port and crinj
    # proxy port are forwarded via add_hostfwd. Other host loopback
    # services (including other agents' daemons) are unreachable.
    gateway = SLIRP_GATEWAY

    # Migrate old workspaces: rename "cave" -> "host-cave" if needed.
    for ws, _, _ in workspaces:
        r = git.git("remote", "get-url", config.HOST_SHARED_REMOTE, cwd=ws,
                    echo=False, check=False, capture=True)
        if r.returncode != 0:
            git.git("remote", "rename", config.CAVE_SHARED_REMOTE, config.HOST_SHARED_REMOTE,
                    cwd=ws, echo=False, check=False, capture=True)

    # Update workspace remotes to use git:// via daemon (localhost for
    # host-side sync, then switch to slirp address for inside the namespace)
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

    # Now set the remote to the slirp-reachable address for inside the namespace
    for ws, _, slug in workspaces:
        url = daemon.remote_url(agent_id, slug, host=gateway)
        git.git("remote", "set-url", config.HOST_SHARED_REMOTE, url,
                cwd=ws, echo=False)

    # Build cave environment (skipped if unchanged)
    user_env_path = user_env.build()

    log.info("setting up nix overlay...")
    nix_merged = _setup_shared_nix_overlay(home)
    nix_db_dir = _seed_nix_db(home, nix_merged)
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
    _start_crinj()
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
        claude_cmd = [SANDBOX_BIN, "claude", "--dangerously-skip-permissions"]
        if prompt:
            claude_cmd += ["--print", prompt]
        elif session_id:
            if resuming:
                claude_cmd += ["--resume", session_id]
            else:
                claude_cmd += ["--session-id", session_id]
        daemon_port = daemon._agent_port(agent_id)

        # One-directional sync pipe: cave-ns.sh writes a byte after
        # unshare(1) has created the namespace, then continues exec'ing
        # into bwrap without waiting. The parent reads the byte, applies
        # iptables (firewall before network), then starts slirp4netns.
        # No handshake needed: the child has no network until slirp4netns
        # starts, and nothing in the bwrap exec chain makes network calls.
        sync_r, sync_w = os.pipe()
        os.set_inheritable(sync_w, True)

        # Nix overlay env vars for cave-ns.sh.
        upper, work, merged = _nix_overlay_dirs(home)
        env["CAVE_NS_SYNC_FD"] = str(sync_w)
        env["CAVE_NS_OVERLAY_MERGED"] = str(merged)
        env["CAVE_NS_OVERLAY_UPPER"] = str(upper)
        env["CAVE_NS_OVERLAY_WORK"] = str(work)

        # cave-ns.sh runs inside unshare(1) without nix-wrapped PATH.
        # Resolve absolute paths for the binaries it needs.
        mountpoint_bin = shutil.which("mountpoint")
        if mountpoint_bin:
            env["CAVE_NS_MOUNTPOINT"] = mountpoint_bin
        fuse_overlayfs_bin = shutil.which("fuse-overlayfs")
        if fuse_overlayfs_bin:
            env["CAVE_NS_FUSE_OVERLAYFS"] = fuse_overlayfs_bin
        fusermount_bin = shutil.which("fusermount3") or shutil.which("fusermount")
        if fusermount_bin:
            env["CAVE_NS_FUSERMOUNT"] = fusermount_bin

        unshare_bin = shutil.which("unshare")
        if not unshare_bin:
            raise SystemExit("unshare not found on PATH (install util-linux)")

        # Map to uid/gid 0 inside the namespace so iptables-restore
        # (nf_tables backend) works. It checks getuid()==0 rather than
        # capabilities. bwrap creates its own inner user namespace so
        # the outer uid doesn't matter for the agent process.
        ns_cmd = [
            unshare_bin, "--user", "--net", "--mount",
            "--map-user=0", "--map-group=0",
            "--", CAVE_NS_SH,
        ] + claude_cmd

        log.info("launching sandbox...")
        proc = subprocess.Popen(ns_cmd, env=env, close_fds=False)
        os.close(sync_w)

        # Record the bwrap child PID so cav cp can reach cave-internal
        # absolute paths via /proc/<cave_pid>/root/<path>.
        registry.set_cave_pid(agent_id, proc.pid)

        # Wait for cave-ns.sh to signal that the namespace exists.
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
            for s, handler in orig.items():
                signal.signal(s, handler)

    finally:
        daemon.stop(daemon_proc, daemon_dir)
