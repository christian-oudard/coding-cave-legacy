use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{bail, Result};
use rusqlite::{params, Connection};
use uuid::Uuid;

use crate::config;

const SCHEMA: &str = include_str!("schema.sql");

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
        if !cols.iter().any(|c| c == "host_ports") {
            db.execute_batch("ALTER TABLE agents ADD COLUMN host_ports TEXT;")?;
        }
        if !cols.iter().any(|c| c == "comms") {
            db.execute_batch("ALTER TABLE agents ADD COLUMN comms INTEGER;")?;
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

fn sweep_dead_pids(db: &Connection, ts: f64) -> Result<()> {
    let rows: Vec<(String, u32)> = collect_rows(
        db,
        "SELECT agent_id, host_pid FROM agents \
         WHERE exited_at IS NULL AND host_pid IS NOT NULL",
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, u32>(1)?)),
    )?;
    for (aid, pid) in rows {
        if !is_pid_alive(pid) {
            db.execute(
                "UPDATE agents SET exited_at = ?1, host_pid = NULL, cave_pid = NULL \
                 WHERE agent_id = ?2",
                params![ts, aid],
            )?;
        }
    }
    Ok(())
}

pub fn display_name(base: &Path, agent_id: &str) -> String {
    let slug = agent_project(agent_id).unwrap_or_else(|| config::project_slug(base));
    format!("{slug}@{agent_id}")
}

pub fn launch_name(base: &Path, agent_id: &str) -> String {
    let name = display_name(base, agent_id);
    let lineage = std::env::var("CODING_CAVE_LINEAGE").unwrap_or_default();
    if lineage.is_empty() {
        name
    } else {
        format!("{name} (parent {lineage})")
    }
}

pub fn agent_launch_home(base: &Path, agent_id: &str) -> std::path::PathBuf {
    config::launch_home(&launch_name(base, agent_id))
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

// -- Launch options --

pub fn save_agent_launch_opts(
    agent_id: &str,
    extra_slugs: &[String],
    host_ports: Option<&[u16]>,
    comms: bool,
) -> Result<()> {
    let db = open()?;
    let extra = if extra_slugs.is_empty() {
        None
    } else {
        Some(serde_json::to_string(extra_slugs)?)
    };
    let ports = host_ports.and_then(|p| {
        if p.is_empty() {
            None
        } else {
            serde_json::to_string(p).ok()
        }
    });
    db.execute(
        "UPDATE agents SET extra_projects = ?1, host_ports = ?2, comms = ?3 \
         WHERE agent_id = ?4",
        params![extra, ports, comms as i64, agent_id],
    )?;
    Ok(())
}

pub fn read_agent_launch_opts(agent_id: &str) -> Result<(Vec<String>, Vec<u16>, bool)> {
    let db = open()?;
    let row = db
        .query_row(
            "SELECT extra_projects, host_ports, comms FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<i64>>(2)?,
                ))
            },
        )
        .ok();
    match row {
        None => Ok((vec![], vec![], false)),
        Some((extra, ports, comms)) => {
            let slugs = extra
                .as_deref()
                .and_then(|s| serde_json::from_str(s).ok())
                .unwrap_or_default();
            let port_vec = ports
                .as_deref()
                .and_then(|s| serde_json::from_str(s).ok())
                .unwrap_or_default();
            Ok((slugs, port_vec, comms.unwrap_or(0) != 0))
        }
    }
}

// -- Agent lifecycle --

pub fn is_agent_running(agent_id: &str) -> Result<bool> {
    let db = open()?;
    let row = db
        .query_row(
            "SELECT host_pid, exited_at FROM agents WHERE agent_id = ?1",
            params![agent_id],
            |row| {
                Ok((
                    row.get::<_, Option<u32>>(0)?,
                    row.get::<_, Option<f64>>(1)?,
                ))
            },
        )
        .ok();
    match row {
        None | Some((None, _)) | Some((_, Some(_))) => Ok(false),
        Some((Some(pid), None)) => {
            if is_pid_alive(pid) {
                return Ok(true);
            }
            db.execute(
                "UPDATE agents SET exited_at = ?1, host_pid = NULL, cave_pid = NULL \
                 WHERE agent_id = ?2",
                params![now(), agent_id],
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
    let slug = config::project_slug(base);
    let ts = now();
    let pid = std::process::id();
    let db = open()?;
    db.execute_batch("BEGIN IMMEDIATE")?;
    sweep_dead_pids(&db, ts)?;

    let occupied: Vec<String> = collect_rows(&db, "SELECT agent_id FROM agents", |row| row.get(0))?;
    let occupied_set: std::collections::HashSet<String> = occupied.into_iter().collect();
    let mut n = 1u32;
    while occupied_set.contains(&n.to_string()) {
        n += 1;
    }
    let aid = n.to_string();
    let sid = Uuid::new_v4().to_string();
    db.execute(
        "INSERT OR REPLACE INTO agents \
         (agent_id, project, host_pid, started_at, exited_at, session_id) \
         VALUES (?1, ?2, ?3, ?4, NULL, ?5)",
        params![aid, slug, pid, ts, sid],
    )?;
    db.execute_batch("COMMIT")?;
    Ok(aid)
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
            None => bail!("No agent {aid} found."),
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
        let found = config::latest_session_file(&slug, None);
        if let Some(session_file) = found {
            let aid = acquire_agent_id(base)?;
            write_session_id(&aid, &session_file)?;
            return Ok(ResolvedContinue {
                agent_id: aid,
                session_id: session_file,
                resuming: true,
            });
        }
        let d = config::claude_project_dir(&slug, None);
        bail!(
            "Nothing to continue. No agents in registry and no session files in {}\n\
             Start a new session with: cav agent",
            d.display()
        );
    }

    let aid = agent_id.unwrap();
    let home = agent_launch_home(base, &aid);
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

    if let Some(ref sid) = session_id {
        let d = config::claude_project_dir(&slug, Some(&home));
        eprintln!(
            "Warning: session file missing ({}), starting fresh.",
            d.join(format!("{sid}.jsonl")).display()
        );
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
            bail!(
                "Slug collision: '{slug}' is already registered to {existing_path}\n\
                 Rename one of the project directories to avoid this."
            );
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

pub fn remove_project(slug: &str) -> Result<()> {
    let db = open()?;
    db.execute("DELETE FROM agents WHERE project = ?1", params![slug])?;
    db.execute("DELETE FROM projects WHERE slug = ?1", params![slug])?;
    Ok(())
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
            "SELECT id, agent_id, before_sha, after_sha FROM approvals \
             WHERE project = ?1 AND shared_branch = ?2 AND after_sha = ?3",
            params![project, shared_branch, after_sha],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .ok();
    Ok(row.map(|(id, agent_id, before_sha, after_sha)| {
        serde_json::json!({
            "id": id,
            "agent_id": agent_id,
            "before_sha": before_sha,
            "after_sha": after_sha,
        })
    }))
}

pub fn delete_approval(approval_id: i64) -> Result<()> {
    let db = open()?;
    db.execute("DELETE FROM approvals WHERE id = ?1", params![approval_id])?;
    Ok(())
}
