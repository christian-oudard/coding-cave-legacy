//! Shared nix DB reconcile helpers.
//!
//! Both the launcher (initial seed, in `sandbox.rs`) and the host daemon
//! (live sync, in `bin/cav_store.rs`) keep a cave's nix DB consistent with
//! its parent store: the cave DB's `ValidPaths` should be the parent-valid
//! paths that exist on disk, plus the agent's own cave-only installs. This
//! module holds the pieces they share so the two stay in lockstep.
//!
//! Pure std + anyhow + rusqlite so it compiles into both binary crates via
//! a `#[path]` include without dragging in crate-specific code.

// This file is included into two binary crates. Each exercises a subset of
// the helpers (the launcher seeds and deletes; the daemon also registers and
// reconciles), so dead-code analysis in either crate alone sees the other's
// half as unused.
#![allow(dead_code)]

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use std::collections::HashSet;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

/// The cave DB has two writers now (the agent's own nix and the host
/// daemon's live sync), so every write path must tolerate the other holding
/// the lock briefly.
const BUSY_TIMEOUT: Duration = Duration::from_secs(10);

/// `nix-store --dump-db` takes paths on argv; chunk so a large first
/// reconcile can't blow past ARG_MAX. load-db is idempotent, so chunking is
/// free.
const REGISTER_CHUNK: usize = 256;

/// The set of store paths recorded valid in a nix DB.
pub fn valid_paths(db: &Path) -> Result<HashSet<String>> {
    let conn = Connection::open_with_flags(db, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("open nix db {}", db.display()))?;
    let mut stmt = conn.prepare("SELECT path FROM ValidPaths")?;
    let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
    Ok(rows.filter_map(|r| r.ok()).collect())
}

/// The `<hash>-<name>` basename of a store path.
pub fn store_name(path: &str) -> String {
    Path::new(path)
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned()
}

/// Register `paths` and their closure into the DB under `target_state_dir`
/// by piping `nix-store --dump-db` (reading the DB under `source_state_dir`)
/// into `nix-store --load-db`. A `None` state dir means nix's default (the
/// host `/nix/var/nix`). Schema knowledge stays inside nix.
pub fn register_paths(
    target_state_dir: Option<&Path>,
    source_state_dir: Option<&Path>,
    paths: &[String],
) -> Result<()> {
    for chunk in paths.chunks(REGISTER_CHUNK) {
        register_chunk(target_state_dir, source_state_dir, chunk)?;
    }
    Ok(())
}

fn register_chunk(
    target_state_dir: Option<&Path>,
    source_state_dir: Option<&Path>,
    paths: &[String],
) -> Result<()> {
    if paths.is_empty() {
        return Ok(());
    }
    let mut dump = Command::new("nix-store");
    dump.arg("--dump-db")
        .args(paths)
        .env("NIX_REMOTE", "")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(d) = source_state_dir {
        dump.env("NIX_STATE_DIR", d);
    }
    let mut dump = dump.spawn().context("spawn nix-store --dump-db")?;
    let dump_stdout = dump.stdout.take().expect("piped stdout");

    let mut load = Command::new("nix-store");
    load.arg("--load-db")
        .env("NIX_REMOTE", "")
        .stdin(Stdio::from(dump_stdout))
        .stderr(Stdio::piped());
    if let Some(d) = target_state_dir {
        load.env("NIX_STATE_DIR", d);
    }
    let mut load = load.spawn().context("spawn nix-store --load-db")?;

    let load_status = load.wait().context("wait load-db")?;
    let dump_status = dump.wait().context("wait dump-db")?;
    if !dump_status.success() {
        bail!("nix-store --dump-db failed: {}", read_stderr(dump.stderr));
    }
    if !load_status.success() {
        bail!("nix-store --load-db failed: {}", read_stderr(load.stderr));
    }
    Ok(())
}

fn read_stderr(stderr: Option<impl Read>) -> String {
    let mut s = String::new();
    if let Some(mut e) = stderr {
        let _ = e.read_to_string(&mut s);
    }
    s
}

/// Delete `paths` and their `Refs` rows from a nix DB by direct SQL. Used
/// for parent-GC'd paths. Does not touch files on disk: the files are the
/// parent's, and the parent has already removed them.
pub fn delete_paths(db: &Path, paths: &[String]) -> Result<()> {
    if paths.is_empty() {
        return Ok(());
    }
    let conn = Connection::open(db).with_context(|| format!("open nix db {}", db.display()))?;
    conn.busy_timeout(BUSY_TIMEOUT)?;
    conn.execute_batch("BEGIN")?;
    for p in paths {
        let id: Option<i64> = conn
            .query_row("SELECT id FROM ValidPaths WHERE path = ?1", params![p], |r| r.get(0))
            .optional()?;
        if let Some(id) = id {
            conn.execute("DELETE FROM Refs WHERE referrer = ?1 OR reference = ?1", params![id])?;
            conn.execute("DELETE FROM ValidPaths WHERE id = ?1", params![id])?;
        }
    }
    conn.execute_batch("COMMIT")?;
    Ok(())
}

/// The paths to add to and remove from a cave DB to match its parent.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct ReconcilePlan {
    /// Parent-valid paths the cave lacks. Registered with their closure.
    pub adds: Vec<String>,
    /// Paths the parent no longer has and that are not the agent's own
    /// cave-only installs. Removed from the cave DB.
    pub removes: Vec<String>,
}

/// Pure set logic of a reconcile: what the cave DB must gain and lose to
/// match the parent store. A cave path missing from the parent is kept only
/// if its files still exist in the agent's `upper_dir` (a cave-only install
/// not yet promoted); otherwise the parent garbage-collected it and it goes.
/// No I/O beyond the per-path `upper_dir` existence check, so this is the
/// unit-tested core.
pub fn reconcile_plan(
    parent: &HashSet<String>,
    cave: &HashSet<String>,
    upper_dir: &Path,
) -> ReconcilePlan {
    let adds = parent.difference(cave).cloned().collect();
    let removes = cave
        .difference(parent)
        .filter(|p| !upper_dir.join(store_name(p)).exists())
        .cloned()
        .collect();
    ReconcilePlan { adds, removes }
}

/// Reconcile a running agent's cave DB against the parent store.
///
/// `cave_state_dir` is the `NIX_STATE_DIR` whose `db/db.sqlite` is the cave
/// DB; `parent_db` is the parent's `db.sqlite` (host or parent cave);
/// `upper_dir` is the agent's overlay upper layer.
///
/// Adds parent-valid paths the cave lacks (with their closure) and removes
/// the parent-GC'd paths. Idempotent: a no-op once the cave DB matches.
pub fn reconcile_cave_db(
    cave_state_dir: &Path,
    cave_db: &Path,
    parent_db: &Path,
    upper_dir: &Path,
) -> Result<ReconcilePlan> {
    let parent = valid_paths(parent_db)?;
    let cave = valid_paths(cave_db)?;
    let plan = reconcile_plan(&parent, &cave, upper_dir);

    // The dump source is the parent's state dir, two levels up from its
    // db.sqlite (`<state>/db/db.sqlite`).
    let source = parent_db.parent().and_then(|db_dir| db_dir.parent());
    register_paths(Some(cave_state_dir), source, &plan.adds)?;
    delete_paths(cave_db, &plan.removes)?;
    Ok(plan)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn set(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn plan_adds_parent_paths_the_cave_lacks() {
        let parent = set(&["/nix/store/aaa-x", "/nix/store/bbb-y"]);
        let cave = set(&["/nix/store/aaa-x"]);
        let empty_upper = std::env::temp_dir().join("cav-nodir-empty");
        let plan = reconcile_plan(&parent, &cave, &empty_upper);
        assert_eq!(plan.adds, vec!["/nix/store/bbb-y".to_string()]);
        assert!(plan.removes.is_empty());
    }

    #[test]
    fn plan_removes_parent_gcd_paths_not_in_upper() {
        let parent = set(&["/nix/store/aaa-x"]);
        let cave = set(&["/nix/store/aaa-x", "/nix/store/ccc-gone"]);
        let empty_upper = std::env::temp_dir().join("cav-nodir-empty");
        let plan = reconcile_plan(&parent, &cave, &empty_upper);
        assert!(plan.adds.is_empty());
        assert_eq!(plan.removes, vec!["/nix/store/ccc-gone".to_string()]);
    }

    #[test]
    fn plan_keeps_cave_only_installs_present_in_upper() {
        let upper = std::env::temp_dir().join(format!("cav-upper-{}", std::process::id()));
        let _ = std::fs::create_dir_all(upper.join("ddd-local"));
        let parent = set(&["/nix/store/aaa-x"]);
        let cave = set(&["/nix/store/aaa-x", "/nix/store/ddd-local"]);
        let plan = reconcile_plan(&parent, &cave, &upper);
        assert!(plan.adds.is_empty());
        assert!(
            plan.removes.is_empty(),
            "a cave-only install present in upper must be preserved"
        );
        let _ = std::fs::remove_dir_all(&upper);
    }

    #[test]
    fn delete_paths_drops_validpaths_and_refs() {
        let dir = std::env::temp_dir().join(format!("cav-db-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("db.sqlite");
        let conn = Connection::open(&db).unwrap();
        conn.execute_batch(
            "CREATE TABLE ValidPaths (id INTEGER PRIMARY KEY, path TEXT UNIQUE);
             CREATE TABLE Refs (referrer INTEGER, reference INTEGER);
             INSERT INTO ValidPaths (id, path) VALUES (1, '/nix/store/keep'), (2, '/nix/store/gone');
             INSERT INTO Refs (referrer, reference) VALUES (1, 2), (2, 2);",
        )
        .unwrap();
        drop(conn);

        delete_paths(&db, &["/nix/store/gone".to_string()]).unwrap();

        let conn = Connection::open(&db).unwrap();
        let paths: Vec<String> = conn
            .prepare("SELECT path FROM ValidPaths")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();
        assert_eq!(paths, vec!["/nix/store/keep".to_string()]);
        let ref_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM Refs", [], |r| r.get(0))
            .unwrap();
        assert_eq!(ref_count, 0, "refs touching the deleted path must be gone");
        drop(conn);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
