//! Read helpers for a nix store database.
//!
//! `#[path]`-included into the cav-store binary, which has no access to the
//! lib crate. Just the two readers the store manager's GC needs.

use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags};
use std::collections::HashSet;
use std::path::Path;

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
