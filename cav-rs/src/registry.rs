use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{bail, Result};
use rusqlite::{params, Connection};
use uuid::Uuid;

use crate::config;
use crate::workspace;

const SCHEMA: &str = include_str!("schema.sql");

/// Past this many seconds since exit, an agent ID becomes reclaimable: the
/// next `acquire_agent_id` may reuse it instead of minting a new number.
/// Agents are NOT eagerly expired or removed; they remain continuable via
/// `cav -c` and `cav -r N` until their slot is actually reclaimed by a new
/// agent. After reclamation, the old session is still recoverable via
/// `cav --resume <uuid>` against the permanent session archive.
const RECLAIMABLE_AFTER_SECS: f64 = 24.0 * 60.0 * 60.0;

fn registry_path() -> std::path::PathBuf {
    config::data_dir().join("coding-cave.db")
}

/// Collect rows from a prepared statement, avoiding the `?`-on-iterator lifetime issue.
fn collect_rows<T, F>(db: &Connection, sql: &str, f: F) -> Result<Vec<T>>
where
    F: FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<T>,
{
    let mut stmt = db.prepare(sql)?;
    let rows = stmt.query_map([], f);
    match rows {
        Ok(mapped) => {
            let v: rusqlite::Result<Vec<T>> = mapped.collect();
            Ok(v?)
        }
        Err(e) => Err(e.into()),
    }
}

fn open() -> Result<Connection> {
    let data_dir = config::data_dir();
    std::fs::create_dir_all(&data_dir)?;
    let db = Connection::open(registry_path())?;
    db.execute_batch("PRAGMA journal_mode=WAL;")?;
    let has_tables: bool = db
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='agents'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .map(|n| n > 0)
        .unwrap_or(false);
    if !has_tables {
        db.execute_batch(SCHEMA)?;
    } else {
        let cols: Vec<String> =
            collect_rows(&db, "PRAGMA table_info(agents)", |row| row.get::<_, String>(1))?;
        for dead in ["host_ports", "host_devices", "comms", "inner_pid"] {
            if cols.iter().any(|c| c == dead) {
                db.execute_batch(&format!("ALTER TABLE agents DROP COLUMN {dead};"))?;
            }
        }
        // Backfill the new `uuid` column on existing rows. SQLite can't add
        // a NOT NULL UNIQUE column to a populated table in one shot, so add
        // nullable, fill, then enforce via a unique index.
        if !cols.iter().any(|c| c == "uuid") {
            db.execute_batch("ALTER TABLE agents ADD COLUMN uuid TEXT;")?;
            let existing: Vec<String> = collect_rows(
                &db,
                "SELECT agent_id FROM agents WHERE uuid IS NULL",
                |row| row.get::<_, String>(0),
            )?;
            for aid in existing {
                db.execute(
                    "UPDATE agents SET uuid = ?1 WHERE agent_id = ?2",
                    params![Uuid::new_v4().to_string(), aid],
                )?;
            }
            db.execute_batch(
                "CREATE UNIQUE INDEX IF NOT EXISTS agents_uuid_unique ON agents(uuid);",
            )?;
        }
    }
    Ok(db)
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn is_pid_alive(pid: u32) -> bool {
    let Some(pid) = rustix::process::Pid::from_raw(pid as i32) else {
        return false;
    };
    match rustix::process::test_kill_process(pid) {
        Ok(()) => true,
        Err(e) => e == rustix::io::Errno::PERM,
    }
}

/// Best estimate of when an agent actually exited. Used when we discover a
/// dead pid: prefer the session log's mtime (when the agent last did work)
/// over now() (when we noticed). Falls back to now if no session file.
fn estimate_exit_time(slug: &str, session_id: Option<&str>, now_ts: f64) -> f64 {
    if let Some(sid) = session_id {
        let path = config::claude_project_dir(slug, None).join(format!("{sid}.jsonl"));
        if let Ok(secs) = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .map(|m| m.duration_since(std::time::UNIX_EPOCH).ok())
        {
            if let Some(d) = secs {
                let t = d.as_secs_f64();
                if t > 0.0 && t <= now_ts {
                    return t;
                }
            }
        }
    }
    now_ts
}

fn sweep_dead_pids(db: &Connection, ts: f64) -> Result<()> {
    let rows: Vec<(String, u32, String, Option<String>)> = collect_rows(
        db,
        "SELECT agent_id, host_pid, project, session_id FROM agents \
         WHERE exited_at IS NULL AND host_pid IS NOT NULL",
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, u32>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
            ))
        },
    )?;
    for (aid, pid, slug, sid) in rows {
        if !is_pid_alive(pid) {
            let exited = estimate_exit_time(&slug, sid.as_deref(), ts);
            db.execute(
                "UPDATE agents SET exited_at = ?1, host_pid = NULL, cave_pid = NULL \
                 WHERE agent_id = ?2",
                params![exited, aid],
            )?;
        }
    }
    Ok(())
}

pub fn display_name(base: &Path, agent_id: &str) -> String {
    let slug = agent_project(agent_id).unwrap_or_else(|| config::project_slug(base));
    format!("{slug}@{agent_id}")
}

// -- Session management --

pub fn write_session_id(agent_id: &str, session_id: &str) -> Result<()> {
    let db = open()?;
    db.execute(
        "UPDATE agents SET session_id = NULL WHERE session_id = ?1 AND agent_id != ?2",
        params![session_id, agent_id],
    )?;
    db.execute(
        "UPDATE agents SET session_id = ?1 WHERE agent_id = ?2",
        params![session_id, agent_id],
    )?;
    Ok(())
}

pub fn clear_session_id(agent_id: &str) -> Result<()> {
    let db = open()?;
    db.execute(
        "UPDATE agents SET session_id = NULL WHERE agent_id = ?1",
        params![agent_id],
    )?;
    Ok(())
}

pub fn find_agent_by_session(session_id: &str) -> Result<Option<String>> {
    let db = open()?;
    Ok(db
        .query_row(
            "SELECT agent_id FROM agents WHERE session_id = ?1",
            params![session_id],
            |row| row.get(0),
        )
        .ok())
}

pub fn read_session_id(agent_id: &str) -> Result<Option<String>> {
    let db = open()?;
    Ok(db
        .query_row(
            "SELECT session_id FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| row.get::<_, Option<String>>(0),
        )
        .ok()
        .flatten())
}

// -- Extra projects --

pub fn save_agent_extras(agent_id: &str, extra_slugs: &[String]) -> Result<()> {
    let db = open()?;
    let extra = if extra_slugs.is_empty() {
        None
    } else {
        Some(serde_json::to_string(extra_slugs)?)
    };
    db.execute(
        "UPDATE agents SET extra_projects = ?1 WHERE agent_id = ?2",
        params![extra, agent_id],
    )?;
    Ok(())
}

// -- Agent lifecycle --

pub fn is_agent_running(agent_id: &str) -> Result<bool> {
    let db = open()?;
    let row = db
        .query_row(
            "SELECT host_pid, exited_at, project, session_id FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| {
                Ok((
                    row.get::<_, Option<u32>>(0)?,
                    row.get::<_, Option<f64>>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            },
        )
        .ok();
    match row {
        None | Some((None, _, _, _)) | Some((_, Some(_), _, _)) => Ok(false),
        Some((Some(pid), None, slug, sid)) => {
            if is_pid_alive(pid) {
                return Ok(true);
            }
            let exited = estimate_exit_time(&slug, sid.as_deref(), now());
            db.execute(
                "UPDATE agents SET exited_at = ?1, host_pid = NULL, cave_pid = NULL \
                 WHERE agent_id = ?2",
                params![exited, agent_id],
            )?;
            Ok(false)
        }
    }
}

pub fn agent_exit_time(agent_id: &str) -> Result<Option<f64>> {
    let db = open()?;
    Ok(db
        .query_row(
            "SELECT exited_at FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| row.get::<_, Option<f64>>(0),
        )
        .ok()
        .flatten())
}

pub fn acquire_agent_id(base: &Path) -> Result<String> {
    // Push any stale paused agents in the active range up to 1000+ before
    // we shop for a free int, so the new agent gets a clean low int rather
    // than reusing a slot whose previous occupant's session/log/worktree
    // is still around.
    workspace::archive_sweep();

    let slug = config::project_slug(base);
    let ts = now();
    let pid = std::process::id();
    let db = open()?;
    db.execute_batch("BEGIN IMMEDIATE")?;
    sweep_dead_pids(&db, ts)?;

    // An ID is in use if its row exists AND it's not yet reclaimable. A row
    // becomes reclaimable when the agent has exited and the exit was more
    // than RECLAIMABLE_AFTER_SECS ago. Reclaimed slots get overwritten so
    // the lowest free number is reused, preventing IDs from drifting upward.
    let cutoff = ts - RECLAIMABLE_AFTER_SECS;
    let in_use: std::collections::HashSet<String> = {
        let mut stmt = db.prepare(
            "SELECT agent_id FROM agents \
             WHERE exited_at IS NULL OR exited_at > ?1",
        )?;
        let rows = stmt.query_map(params![cutoff], |row| row.get::<_, String>(0))?;
        rows.filter_map(|r| r.ok()).collect()
    };
    let mut n = 1u32;
    while in_use.contains(&n.to_string()) {
        n += 1;
    }
    let aid = n.to_string();
    // If this slot was previously occupied (reclaimable row still in the
    // table), gather the old occupant's projects so we can wipe their
    // worktrees and dev branch refs after committing the new row.
    let reclaim_targets = collect_reclaim_targets(&db, &aid);
    let sid = Uuid::new_v4().to_string();
    let uuid = Uuid::new_v4().to_string();
    db.execute(
        "INSERT OR REPLACE INTO agents \
         (agent_id, uuid, project, host_pid, started_at, exited_at, session_id) \
         VALUES (?1, ?2, ?3, ?4, ?5, NULL, ?6)",
        params![aid, uuid, slug, pid, ts, sid],
    )?;
    db.execute_batch("COMMIT")?;
    if !reclaim_targets.is_empty() {
        workspace::reclaim(&aid, &reclaim_targets);
    }
    Ok(aid)
}

fn collect_reclaim_targets(db: &Connection, aid: &str) -> Vec<(String, PathBuf)> {
    let row: Option<(String, Option<String>)> = db
        .query_row(
            "SELECT project, extra_projects FROM agents WHERE agent_id = ?1",
            params![aid],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?)),
        )
        .ok();
    let Some((primary, extra)) = row else {
        return vec![];
    };
    let mut slugs = vec![primary];
    if let Some(e) = extra {
        if let Ok(more) = serde_json::from_str::<Vec<String>>(&e) {
            slugs.extend(more);
        }
    }
    slugs
        .into_iter()
        .filter_map(|s| {
            db.query_row(
                "SELECT path FROM projects WHERE slug = ?1",
                params![&s],
                |row| row.get::<_, String>(0),
            )
            .ok()
            .map(|p| (s, PathBuf::from(p)))
        })
        .collect()
}

pub fn continue_agent_id(base: &Path, agent_id: Option<&str>) -> Result<Option<String>> {
    let slug = config::project_slug(base);
    let ts = now();
    let pid = std::process::id();
    let db = open()?;
    db.execute_batch("BEGIN IMMEDIATE")?;
    sweep_dead_pids(&db, ts)?;

    if let Some(aid) = agent_id {
        let row = db
            .query_row(
                "SELECT host_pid, exited_at FROM agents WHERE agent_id = ?1",
                params![aid],
                |row| {
                    Ok((
                        row.get::<_, Option<u32>>(0)?,
                        row.get::<_, Option<f64>>(1)?,
                    ))
                },
            )
            .ok();
        match row {
            None => bail!(
                "Agent {aid} not found (may have been reclaimed). To recover an\n\
                 archived session, run: cav --resume <session-uuid>\n\
                 (find uuids in ~/.claude/projects/)"
            ),
            Some((Some(p), None)) if is_pid_alive(p) => {
                bail!("Agent {aid} is still running.")
            }
            _ => {}
        }
        db.execute(
            "UPDATE agents SET host_pid = ?1, cave_pid = NULL, \
             started_at = ?2, exited_at = NULL WHERE agent_id = ?3",
            params![pid, ts, aid],
        )?;
        db.execute_batch("COMMIT")?;
        return Ok(Some(aid.to_string()));
    }

    let row = db
        .query_row(
            "SELECT agent_id FROM agents \
             WHERE project = ?1 AND exited_at IS NOT NULL \
             ORDER BY exited_at DESC LIMIT 1",
            params![slug],
            |row| row.get::<_, String>(0),
        )
        .ok();
    match row {
        None => {
            db.execute_batch("COMMIT")?;
            Ok(None)
        }
        Some(aid) => {
            db.execute(
                "UPDATE agents SET host_pid = ?1, cave_pid = NULL, \
                 started_at = ?2, exited_at = NULL WHERE agent_id = ?3",
                params![pid, ts, aid],
            )?;
            db.execute_batch("COMMIT")?;
            Ok(Some(aid))
        }
    }
}

pub struct ResolvedContinue {
    pub agent_id: String,
    pub session_id: String,
    pub resuming: bool,
}

pub fn resolve_continue(
    base: &Path,
    agent_id_hint: Option<&str>,
) -> Result<ResolvedContinue> {
    let slug = config::project_slug(base);
    let agent_id = continue_agent_id(base, agent_id_hint)?;

    if agent_id.is_none() {
        let d = config::claude_project_dir(&slug, None);
        if d.is_dir() {
            bail!(
                "No agents to continue. To recover an archived session, find\n\
                 its uuid in\n  {}\n\
                 then run: cav --resume <session-uuid>",
                d.display()
            );
        }
        bail!(
            "Nothing to continue. No agents in registry and no session archive at {}\n\
             Start a new session with: cav agent",
            d.display()
        );
    }

    let aid = agent_id.unwrap();
    let home = config::persistent_home();
    let session_id = read_session_id(&aid)?;

    if let Some(ref sid) = session_id {
        if config::session_file_exists(&slug, sid, Some(&home)) {
            return Ok(ResolvedContinue {
                agent_id: aid,
                session_id: sid.clone(),
                resuming: true,
            });
        }
    }

    if session_id.is_some() {
        // No .jsonl at the recorded uuid -- claude exited without
        // persisting (early exit, no messages sent). Reuse the slot fresh.
        clear_session_id(&aid)?;
    }
    let new_sid = Uuid::new_v4().to_string();
    write_session_id(&aid, &new_sid)?;
    Ok(ResolvedContinue {
        agent_id: aid,
        session_id: new_sid,
        resuming: false,
    })
}

pub fn mark_agent_exited(agent_id: &str) -> Result<()> {
    let db = open()?;
    db.execute(
        "UPDATE agents SET exited_at = ?1, host_pid = NULL, cave_pid = NULL \
         WHERE agent_id = ?2",
        params![now(), agent_id],
    )?;
    Ok(())
}

pub fn set_cave_pid(agent_id: &str, pid: u32) -> Result<()> {
    let db = open()?;
    db.execute(
        "UPDATE agents SET cave_pid = ?1 WHERE agent_id = ?2",
        params![pid, agent_id],
    )?;
    Ok(())
}

pub fn cave_pid(agent_id: &str) -> Result<Option<u32>> {
    let db = open()?;
    Ok(db
        .query_row(
            "SELECT cave_pid FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| row.get::<_, Option<u32>>(0),
        )
        .ok()
        .flatten())
}

pub fn agent_all_slugs(agent_id: &str) -> Result<Vec<String>> {
    let db = open()?;
    let row = db
        .query_row(
            "SELECT project, extra_projects FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Option<String>>(1)?,
                ))
            },
        )
        .ok();
    match row {
        None => Ok(vec![]),
        Some((primary, extra)) => {
            let mut slugs = vec![primary];
            if let Some(e) = extra {
                if let Ok(more) = serde_json::from_str::<Vec<String>>(&e) {
                    slugs.extend(more);
                }
            }
            Ok(slugs)
        }
    }
}

pub fn remove_agent(agent_id: &str) -> Result<()> {
    let db = open()?;
    db.execute("DELETE FROM agents WHERE agent_id = ?1", params![agent_id])?;
    Ok(())
}

pub fn all_agent_ids(project: Option<&str>) -> Result<Vec<String>> {
    let db = open()?;
    let all: Vec<(String, String, Option<String>)> = collect_rows(
        &db,
        "SELECT agent_id, project, extra_projects FROM agents",
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        },
    )?;
    let mut ids: Vec<String> = if let Some(proj) = project {
        all.into_iter()
            .filter(|(_, primary, extras)| {
                primary == proj
                    || extras
                        .as_deref()
                        .and_then(|e| serde_json::from_str::<Vec<String>>(e).ok())
                        .is_some_and(|v| v.iter().any(|s| s == proj))
            })
            .map(|(aid, _, _)| aid)
            .collect()
    } else {
        all.into_iter().map(|(aid, _, _)| aid).collect()
    };
    ids.sort_by(|a, b| {
        a.parse::<u32>()
            .unwrap_or(u32::MAX)
            .cmp(&b.parse::<u32>().unwrap_or(u32::MAX))
    });
    Ok(ids)
}

// -- Project registry --

pub fn register_project(base: &Path) -> Result<String> {
    let slug = config::project_slug(base);
    let path = base.canonicalize()?.to_string_lossy().to_string();
    let db = open()?;
    let existing: Option<String> = db
        .query_row(
            "SELECT path FROM projects WHERE slug = ?1",
            params![slug],
            |row| row.get(0),
        )
        .ok();
    if let Some(existing_path) = existing {
        if existing_path != path {
            // If the old path is gone, allow re-registration silently.
            if !std::path::Path::new(&existing_path).is_dir() {
                db.execute(
                    "DELETE FROM projects WHERE slug = ?1",
                    params![slug],
                )?;
            } else {
                bail!(
                    "Slug collision: '{slug}' is already registered to {existing_path}\n\
                     If that project was moved or deleted, run: cav remove-project {slug}"
                );
            }
        }
    }
    db.execute(
        "INSERT OR REPLACE INTO projects (slug, path) VALUES (?1, ?2)",
        params![slug, path],
    )?;
    Ok(slug)
}

pub fn project_path(slug: &str) -> Result<Option<String>> {
    let db = open()?;
    Ok(db
        .query_row(
            "SELECT path FROM projects WHERE slug = ?1",
            params![slug],
            |row| row.get(0),
        )
        .ok())
}

pub fn all_projects() -> Result<Vec<(String, String)>> {
    let db = open()?;
    collect_rows(&db, "SELECT slug, path FROM projects", |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })
}

/// Relabel a project. Slug change cascades: project row, every agent's
/// primary `project` field, every agent's `extra_projects` JSON entries,
/// and every approvals row. Path is unconditionally updated even when
/// the slug is unchanged (a pure source-dir move). Caller is responsible
/// for moving on-disk state (worktrees, session logs).
pub fn rename_project(old_slug: &str, new_slug: &str, new_path: &Path) -> Result<()> {
    let mut db = open()?;
    let path_str = new_path.to_string_lossy().to_string();
    let tx = db.transaction()?;
    tx.execute(
        "UPDATE projects SET slug = ?1, path = ?2 WHERE slug = ?3",
        params![new_slug, path_str, old_slug],
    )?;
    if new_slug != old_slug {
        tx.execute(
            "UPDATE agents SET project = ?1 WHERE project = ?2",
            params![new_slug, old_slug],
        )?;
        tx.execute(
            "UPDATE approvals SET project = ?1 WHERE project = ?2",
            params![new_slug, old_slug],
        )?;
        // extra_projects is a JSON array per agent; iterate and rewrite
        // entries that match old_slug.
        let rows: Vec<(String, String)> = {
            let mut stmt = tx.prepare(
                "SELECT agent_id, extra_projects FROM agents \
                 WHERE extra_projects IS NOT NULL",
            )?;
            let mapped = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            mapped.filter_map(|r| r.ok()).collect()
        };
        for (aid, extras_json) in rows {
            let Ok(extras) = serde_json::from_str::<Vec<String>>(&extras_json) else {
                continue;
            };
            if !extras.iter().any(|s| s == old_slug) {
                continue;
            }
            let new_extras: Vec<String> = extras
                .into_iter()
                .map(|s| if s == old_slug { new_slug.to_string() } else { s })
                .collect();
            let new_json = serde_json::to_string(&new_extras)?;
            tx.execute(
                "UPDATE agents SET extra_projects = ?1 WHERE agent_id = ?2",
                params![new_json, aid],
            )?;
        }
    }
    tx.commit()?;
    Ok(())
}

pub fn remove_project(slug: &str) -> Result<()> {
    let db = open()?;
    db.execute("DELETE FROM agents WHERE project = ?1", params![slug])?;
    db.execute("DELETE FROM projects WHERE slug = ?1", params![slug])?;
    Ok(())
}

/// Relabel an agent's display int. Only the `agent_id` column moves; the
/// uuid (and therefore worktree dir + nix overlay paths) stays put.
/// Errors if `new_id` is already taken.
pub fn bump_agent_id(old_id: &str, new_id: &str) -> Result<()> {
    let db = open()?;
    db.execute(
        "UPDATE agents SET agent_id = ?1 WHERE agent_id = ?2",
        params![new_id, old_id],
    )?;
    db.execute(
        "UPDATE approvals SET agent_id = ?1 WHERE agent_id = ?2",
        params![new_id, old_id],
    )?;
    Ok(())
}

pub fn agent_uuid(agent_id: &str) -> Result<Option<String>> {
    let db = open()?;
    Ok(db
        .query_row(
            "SELECT uuid FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| row.get::<_, String>(0),
        )
        .ok())
}

pub fn agent_project(agent_id: &str) -> Option<String> {
    let db = open().ok()?;
    db.query_row(
        "SELECT project FROM agents WHERE agent_id = ?1",
        params![agent_id],
        |row| row.get(0),
    )
    .ok()
}

/// If `agent_id` is registered to a project other than the one rooted at
/// `base`, return a hint pointing at the project's on-disk path. Caller
/// appends it to a "not found here" error. None when the agent belongs to
/// this project, isn't in the registry, has no resolvable path, or we're
/// inside a cave (where the registry only knows about subcaves of self).
pub fn other_project_hint(base: &Path, agent_id: &str) -> Option<String> {
    if config::inside_cave() {
        return None;
    }
    let agent_slug = agent_project(agent_id)?;
    if agent_slug == config::project_slug(base) {
        return None;
    }
    let path = project_path(&agent_slug).ok().flatten()?;
    Some(format!(
        "Agent {agent_id} is in {agent_slug}:\n  cd {path}"
    ))
}

// -- Approvals --

pub fn record_approval(
    project: &str,
    shared_branch: &str,
    agent_id: &str,
    before_sha: &str,
    after_sha: &str,
) -> Result<()> {
    let db = open()?;
    db.execute(
        "INSERT INTO approvals \
         (project, shared_branch, agent_id, before_sha, after_sha, approved_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![project, shared_branch, agent_id, before_sha, after_sha, now()],
    )?;
    Ok(())
}

pub fn lookup_approval_by_after(
    project: &str,
    shared_branch: &str,
    after_sha: &str,
) -> Result<Option<serde_json::Value>> {
    let db = open()?;
    let row = db
        .query_row(
            "SELECT id, agent_id, before_sha, after_sha, approved_at FROM approvals \
             WHERE project = ?1 AND shared_branch = ?2 AND after_sha = ?3",
            params![project, shared_branch, after_sha],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, f64>(4)?,
                ))
            },
        )
        .ok();
    Ok(
        row.map(|(id, agent_id, before_sha, after_sha, approved_at)| {
            serde_json::json!({
                "id": id,
                "agent_id": agent_id,
                "before_sha": before_sha,
                "after_sha": after_sha,
                "approved_at": approved_at,
            })
        }),
    )
}

pub fn agent_started_at(agent_id: &str) -> Result<Option<f64>> {
    let db = open()?;
    Ok(db
        .query_row(
            "SELECT started_at FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| row.get::<_, f64>(0),
        )
        .ok())
}

pub fn delete_approvals(approval_ids: &[i64]) -> Result<()> {
    if approval_ids.is_empty() {
        return Ok(());
    }
    let mut db = open()?;
    let tx = db.transaction()?;
    {
        let mut stmt = tx.prepare("DELETE FROM approvals WHERE id = ?1")?;
        for id in approval_ids {
            stmt.execute(params![id])?;
        }
    }
    tx.commit()?;
    Ok(())
}
