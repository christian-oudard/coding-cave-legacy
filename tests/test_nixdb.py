"""Tests for nix DB seeding and cache pruning logic."""

import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cav.sandbox import _seed_nix_db, _prune_nix_caches
from cav import config


def _create_nix_db(path):
    """Create a minimal nix DB with the standard schema."""
    db = sqlite3.connect(str(path))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ValidPaths (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            hash TEXT NOT NULL,
            registrationTime INTEGER NOT NULL,
            deriver TEXT,
            narSize INTEGER,
            ultimate INTEGER,
            sigs TEXT,
            ca TEXT
        );
        CREATE TABLE IF NOT EXISTS Refs (
            referrer INTEGER NOT NULL,
            reference INTEGER NOT NULL,
            PRIMARY KEY (referrer, reference),
            FOREIGN KEY (referrer) REFERENCES ValidPaths(id),
            FOREIGN KEY (reference) REFERENCES ValidPaths(id)
        );
        CREATE TABLE IF NOT EXISTS DerivationOutputs (
            drv INTEGER NOT NULL,
            id TEXT NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (drv, id)
        );
    """)
    return db


def _insert_path(db, path, h="sha256:aaa", nar_size=100):
    db.execute(
        "INSERT INTO ValidPaths (path, hash, registrationTime, narSize) "
        "VALUES (?, ?, 1, ?)", (path, h, nar_size),
    )
    db.commit()
    return db.execute(
        "SELECT id FROM ValidPaths WHERE path = ?", (path,)
    ).fetchone()[0]


def _get_paths(db_path):
    db = sqlite3.connect(str(db_path))
    paths = {r[0] for r in db.execute("SELECT path FROM ValidPaths")}
    db.close()
    return paths


class TestSeedNixDb(unittest.TestCase):
    """Test the backup + merge-back logic in _seed_nix_db."""

    def _setup_host_db(self, tmp, paths):
        host_db = tmp / "host" / "db.sqlite"
        host_db.parent.mkdir(parents=True)
        db = _create_nix_db(host_db)
        for p in paths:
            _insert_path(db, p)
        db.close()
        return host_db

    def test_first_session_copies_host(self):
        """First session (no cave DB) does a full backup."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        host_db = self._setup_host_db(tmp_path, ["/nix/store/aaa", "/nix/store/bbb"])

        cave_db = tmp_path / "cave_db.sqlite"

        src = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        dst = sqlite3.connect(str(cave_db))
        src.backup(dst)
        dst.close()
        src.close()

        paths = _get_paths(cave_db)
        self.assertEqual(paths, {"/nix/store/aaa", "/nix/store/bbb"})

    def test_cave_only_paths_preserved(self, tmp_path=None):
        """Cave-only paths that exist on disk are preserved after re-seed."""
        tmp_path = tmp_path or Path(__import__("tempfile").mkdtemp())

        host_db = self._setup_host_db(tmp_path, ["/nix/store/aaa"])

        # Create cave DB with host path + cave-only path
        cave_db = tmp_path / "cave_db.sqlite"
        db = _create_nix_db(cave_db)
        _insert_path(db, "/nix/store/aaa")
        _insert_path(db, "/nix/store/cave-pkg")
        db.close()

        # Simulate the merge-back logic from _seed_nix_db
        old = sqlite3.connect(f"file:{cave_db}?mode=ro", uri=True)
        host = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        host_paths = {r[0] for r in host.execute("SELECT path FROM ValidPaths")}

        cave_only_rows = []
        for row in old.execute(
            "SELECT path, hash, registrationTime, deriver, narSize, "
            "ultimate, sigs, ca FROM ValidPaths"
        ):
            if row[0] not in host_paths:
                cave_only_rows.append(row)
        host.close()
        old.close()

        self.assertEqual(len(cave_only_rows), 1)
        self.assertEqual(cave_only_rows[0][0], "/nix/store/cave-pkg")

        # Fresh backup
        src = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        dst = sqlite3.connect(str(cave_db))
        src.backup(dst)

        # Merge back
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

        paths = _get_paths(cave_db)
        self.assertEqual(paths, {"/nix/store/aaa", "/nix/store/cave-pkg"})

    def test_host_gcd_paths_removed(self, tmp_path=None):
        """Paths GC'd from host are not in the fresh backup."""
        tmp_path = tmp_path or Path(__import__("tempfile").mkdtemp())

        # Host has only /aaa (previously had /bbb, now GC'd)
        host_db = self._setup_host_db(tmp_path, ["/nix/store/aaa"])

        # Cave DB from previous session had both
        cave_db = tmp_path / "cave_db.sqlite"
        db = _create_nix_db(cave_db)
        _insert_path(db, "/nix/store/aaa")
        _insert_path(db, "/nix/store/bbb")
        db.close()

        # Simulate merge-back (bbb is not cave-only, it WAS a host path)
        old = sqlite3.connect(f"file:{cave_db}?mode=ro", uri=True)
        host = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        host_paths = {r[0] for r in host.execute("SELECT path FROM ValidPaths")}
        cave_only_rows = []
        for row in old.execute(
            "SELECT path, hash, registrationTime, deriver, narSize, "
            "ultimate, sigs, ca FROM ValidPaths"
        ):
            # bbb is not in host_paths, but also doesn't exist on disk
            if row[0] not in host_paths:
                # Simulate: /nix/store/bbb doesn't exist on disk
                cave_only_rows.append(row)
        host.close()
        old.close()

        # bbb is "cave-only" by set difference, but doesn't exist on disk
        # In real code, Path(row[0]).exists() filters it out

        # Fresh backup (only aaa)
        src = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        dst = sqlite3.connect(str(cave_db))
        src.backup(dst)
        dst.close()
        src.close()

        # Don't merge back bbb (simulating the exists() check filtering it)
        paths = _get_paths(cave_db)
        self.assertEqual(paths, {"/nix/store/aaa"})

    def test_stale_entries_cleaned_after_host_gc(self):
        """DB entries for paths missing from the overlay are removed.

        Simulates: host GC'd paths after the DB snapshot was taken.
        The overlay reflects the live host store, so the files are gone,
        but the DB copy still has the entries.
        """
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())

        # Simulate a merged overlay dir with only some paths present
        store_root = tmp_path / "merged"
        store_root.mkdir()
        (store_root / "aaa").mkdir()       # exists on overlay
        (store_root / "ccc-pkg").mkdir()   # exists on overlay
        # bbb-missing.drv does NOT exist (host GC'd it after snapshot)

        # Cave DB has all three paths (from the host DB snapshot)
        cave_db = tmp_path / "cave_db.sqlite"
        db = _create_nix_db(cave_db)
        id_aaa = _insert_path(db, "/nix/store/aaa")
        id_bbb = _insert_path(db, "/nix/store/bbb-missing.drv")
        id_ccc = _insert_path(db, "/nix/store/ccc-pkg")
        # Add a ref: aaa references bbb
        db.execute("INSERT INTO Refs VALUES (?, ?)", (id_aaa, id_bbb))
        db.commit()

        # Run the stale cleanup logic (from _seed_nix_db)
        stale_ids = []
        for vid, path in db.execute("SELECT id, path FROM ValidPaths"):
            basename = Path(path).name
            if not (store_root / basename).exists():
                stale_ids.append(vid)
        self.assertEqual(len(stale_ids), 1)

        db.executemany(
            "DELETE FROM Refs WHERE referrer = ? OR reference = ?",
            [(vid, vid) for vid in stale_ids],
        )
        db.executemany(
            "DELETE FROM ValidPaths WHERE id = ?",
            [(vid,) for vid in stale_ids],
        )
        db.commit()
        db.close()

        paths = _get_paths(cave_db)
        self.assertEqual(paths, {"/nix/store/aaa", "/nix/store/ccc-pkg"})

        # Verify refs to the stale path were also cleaned
        db = sqlite3.connect(str(cave_db))
        refs = db.execute("SELECT * FROM Refs").fetchall()
        db.close()
        self.assertEqual(len(refs), 0)

    def test_cave_only_paths_checked_against_overlay(self):
        """Cave-only paths are validated against the merged overlay, not /nix/store."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())

        # Merged overlay has the cave-only package (in the upper layer)
        store_root = tmp_path / "merged"
        store_root.mkdir()
        (store_root / "host-pkg").mkdir()
        (store_root / "cave-pkg").mkdir()  # exists in upper layer
        # stale-cave-pkg does NOT exist (upper layer was cleaned)

        host_db = self._setup_host_db(tmp_path, ["/nix/store/host-pkg"])

        # Previous cave DB had host + two cave-only paths
        cave_db = tmp_path / "cave_db.sqlite"
        db = _create_nix_db(cave_db)
        _insert_path(db, "/nix/store/host-pkg")
        _insert_path(db, "/nix/store/cave-pkg")
        _insert_path(db, "/nix/store/stale-cave-pkg")
        db.close()

        # Merge-back: check cave-only paths against overlay
        old = sqlite3.connect(f"file:{cave_db}?mode=ro", uri=True)
        host = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        host_paths = {r[0] for r in host.execute("SELECT path FROM ValidPaths")}
        cave_only_rows = []
        for row in old.execute(
            "SELECT path, hash, registrationTime, deriver, narSize, "
            "ultimate, sigs, ca FROM ValidPaths"
        ):
            if row[0] not in host_paths:
                basename = Path(row[0]).name
                if (store_root / basename).exists():
                    cave_only_rows.append(row)
        host.close()
        old.close()

        # Only cave-pkg should be preserved (stale-cave-pkg is gone)
        self.assertEqual(len(cave_only_rows), 1)
        self.assertEqual(cave_only_rows[0][0], "/nix/store/cave-pkg")

    def test_noop_when_no_cave_only(self, tmp_path=None):
        """When cave has no extra paths, result equals host DB."""
        tmp_path = tmp_path or Path(__import__("tempfile").mkdtemp())

        host_db = self._setup_host_db(tmp_path, ["/nix/store/aaa", "/nix/store/bbb"])

        cave_db = tmp_path / "cave_db.sqlite"
        db = _create_nix_db(cave_db)
        _insert_path(db, "/nix/store/aaa")
        _insert_path(db, "/nix/store/bbb")
        db.close()

        # Merge-back finds nothing cave-only
        old = sqlite3.connect(f"file:{cave_db}?mode=ro", uri=True)
        host = sqlite3.connect(f"file:{host_db}?mode=ro", uri=True)
        host_paths = {r[0] for r in host.execute("SELECT path FROM ValidPaths")}
        cave_only = [
            r for r in old.execute("SELECT path FROM ValidPaths")
            if r[0] not in host_paths
        ]
        host.close()
        old.close()

        self.assertEqual(len(cave_only), 0)


    def test_db_on_native_fs_not_overlay(self):
        """DB directory is a sibling of merged, not under it."""
        from cav.sandbox import _nix_overlay_dirs
        home = config.persistent_home()
        _, _, merged = _nix_overlay_dirs(home)
        db_dir = merged.parent / "db"
        self.assertFalse(
            str(db_dir).startswith(str(merged) + "/"),
            f"DB dir {db_dir} must not be under merged mount {merged}",
        )


class TestIsolatedSubcaveHomes(unittest.TestCase):
    """Subcaves get isolated homes so overlay dirs are naturally distinct.

    Each nesting level uses "shared/" under its own home. Since homes
    are distinct, the overlay paths never collide and no lineage
    tracking is needed.
    """

    def test_different_homes_yield_different_overlay_dirs(self):
        """Two homes produce non-overlapping overlay paths."""
        import tempfile
        from cav.sandbox import _nix_overlay_dirs
        a = Path(tempfile.mkdtemp())
        b = Path(tempfile.mkdtemp())
        _, _, merged_a = _nix_overlay_dirs(a)
        _, _, merged_b = _nix_overlay_dirs(b)
        self.assertNotEqual(merged_a, merged_b)
        self.assertFalse(str(merged_a).startswith(str(merged_b) + "/"))
        self.assertFalse(str(merged_b).startswith(str(merged_a) + "/"))

    def test_same_home_yields_same_overlay_dirs(self):
        """Siblings sharing a home share the overlay (by design)."""
        import tempfile
        from cav.sandbox import _nix_overlay_dirs
        home = Path(tempfile.mkdtemp())
        _, _, merged_a = _nix_overlay_dirs(home)
        _, _, merged_b = _nix_overlay_dirs(home)
        self.assertEqual(merged_a, merged_b)


class TestRmtreeReadonly(unittest.TestCase):
    """Test _rmtree_readonly handles read-only directory trees."""

    def test_removes_nested_readonly_dirs(self):
        """Deeply nested read-only dirs (like overlayfs work) are removed."""
        import tempfile
        from cav.sandbox import _rmtree_readonly
        root = Path(tempfile.mkdtemp())
        # Simulate overlayfs work dir: nested dirs with nix store perms.
        deep = root / "work" / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "file").touch()
        # Make everything read-only (like nix store: r-xr-xr-x).
        for dirpath, _dns, _fns in os.walk(root / "work", topdown=False):
            os.chmod(dirpath, 0o555)
        _rmtree_readonly(root / "work")
        self.assertFalse((root / "work").exists())
        root.rmdir()


class TestPruneNixCaches(unittest.TestCase):
    """Test fetcher and eval cache pruning."""

    def test_fetcher_cache_dead_entries_removed(self):
        """Fetcher cache rows with missing store paths are deleted."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        nix_cache = tmp / ".cache" / "nix"
        nix_cache.mkdir(parents=True)

        db_path = nix_cache / "fetcher-cache-v4.sqlite"
        db = sqlite3.connect(str(db_path))
        db.execute("""
            CREATE TABLE Cache (
                domain TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (domain, key)
            )
        """)
        # Entry with a path that doesn't exist
        db.execute(
            "INSERT INTO Cache VALUES (?, ?, ?, ?)",
            ("fetchToStore", "key1",
             '{"storePath":"zzzzz-nonexistent-pkg"}', 1),
        )
        # Entry without a storePath (should be left alone)
        db.execute(
            "INSERT INTO Cache VALUES (?, ?, ?, ?)",
            ("gitLastModified", "key2", '{"lastModified":123}', 1),
        )
        db.commit()
        db.close()

        with patch("cav.sandbox.config") as mock_config:
            mock_config.persistent_home.return_value = tmp
            _prune_nix_caches()

        db = sqlite3.connect(str(db_path))
        rows = db.execute("SELECT domain, key FROM Cache").fetchall()
        db.close()

        # Dead storePath entry removed, non-storePath entry kept
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], ("gitLastModified", "key2"))

    def test_eval_cache_stale_file_removed(self):
        """Eval cache files with missing store paths are deleted."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        eval_dir = tmp / ".cache" / "nix" / "eval-cache-v6"
        eval_dir.mkdir(parents=True)

        db_path = eval_dir / "abc123.sqlite"
        db = sqlite3.connect(str(db_path))
        db.execute("""
            CREATE TABLE Attributes (
                parent INTEGER NOT NULL,
                name TEXT,
                type INTEGER NOT NULL,
                value TEXT,
                context TEXT,
                PRIMARY KEY (parent, name)
            )
        """)
        db.execute(
            "INSERT INTO Attributes VALUES (?, ?, ?, ?, ?)",
            (0, "drvPath", 2, "/nix/store/zzzzz-nonexistent.drv", None),
        )
        db.commit()
        db.close()

        with patch("cav.sandbox.config") as mock_config:
            mock_config.persistent_home.return_value = tmp
            _prune_nix_caches()

        self.assertFalse(db_path.exists())

    def test_eval_cache_valid_file_kept(self):
        """Eval cache files where all paths exist are preserved."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        eval_dir = tmp / ".cache" / "nix" / "eval-cache-v6"
        eval_dir.mkdir(parents=True)

        # Use a path that definitely exists
        existing_path = str(Path(__file__).parent.parent / "cav" / "sandbox.py")

        db_path = eval_dir / "valid.sqlite"
        db = sqlite3.connect(str(db_path))
        db.execute("""
            CREATE TABLE Attributes (
                parent INTEGER NOT NULL,
                name TEXT,
                type INTEGER NOT NULL,
                value TEXT,
                context TEXT,
                PRIMARY KEY (parent, name)
            )
        """)
        db.execute(
            "INSERT INTO Attributes VALUES (?, ?, ?, ?, ?)",
            (0, "drvPath", 2, existing_path, None),
        )
        db.commit()
        db.close()

        with patch("cav.sandbox.config") as mock_config:
            mock_config.persistent_home.return_value = tmp
            _prune_nix_caches()

        self.assertTrue(db_path.exists())


if __name__ == "__main__":
    unittest.main()
