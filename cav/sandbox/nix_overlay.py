"""Nix store overlay setup and DB seeding for the sandbox.

Each agent gets its own upper/work/merged under <home>/.local/share/
coding-cave/nix-overlay/agent-<id>/. Kernel overlayfs requires unique
upperdir/workdir per mount, so this layout is non-negotiable. The
actual overlayfs mount happens inside the user namespace in
supervisor.py; this module only prepares the directories and the
per-cave nix DB snapshot.
"""

import logging
import os
import re
import shutil
import sqlite3
import stat
from pathlib import Path

from .. import config

log = logging.getLogger("cav")


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
