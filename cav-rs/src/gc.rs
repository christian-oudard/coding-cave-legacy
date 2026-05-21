use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Result};

use crate::config;
use crate::git;
use crate::registry;
use crate::style;
use crate::workspace;

const DEFAULT_OLDER_THAN: &str = "24h";

/// One paused agent's GC-able on-disk footprint.
pub struct Candidate {
    pub agent_id: String,
    /// (slug, project_base) for the agent's primary and extra projects.
    pub targets: Vec<(String, PathBuf)>,
    /// Sum of worktree + nix overlay sizes, in bytes.
    pub size: u64,
}

pub fn default_older_than() -> &'static str {
    DEFAULT_OLDER_THAN
}

pub fn parse_older_than(s: &str) -> Result<Duration> {
    let s = s.trim();
    if s.is_empty() {
        bail!("empty duration; try e.g. 24h, 7d, 1w");
    }
    let split_at = s
        .find(|c: char| !c.is_ascii_digit() && c != '.')
        .unwrap_or(s.len());
    let (num, unit) = s.split_at(split_at);
    let num: f64 = num
        .parse()
        .map_err(|_| anyhow::anyhow!("not a number: '{num}'"))?;
    let mul = match unit {
        "" | "s" => 1.0,
        "m" => 60.0,
        "h" => 3600.0,
        "d" => 86400.0,
        "w" => 604800.0,
        other => bail!("unknown duration unit '{other}'; use s, m, h, d, or w"),
    };
    Ok(Duration::from_secs_f64(num * mul))
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn dir_size(path: &Path) -> u64 {
    let mut total = 0u64;
    let Ok(entries) = std::fs::read_dir(path) else {
        return 0;
    };
    for entry in entries.flatten() {
        let Ok(meta) = entry.metadata() else { continue };
        if meta.is_symlink() {
            total += meta.len();
        } else if meta.is_dir() {
            total += dir_size(&entry.path());
        } else {
            total += meta.len();
        }
    }
    total
}

pub fn format_size(bytes: u64) -> String {
    const KB: u64 = 1024;
    const MB: u64 = 1024 * KB;
    const GB: u64 = 1024 * MB;
    if bytes >= GB {
        format!("{:.1} GB", bytes as f64 / GB as f64)
    } else if bytes >= MB {
        format!("{:.1} MB", bytes as f64 / MB as f64)
    } else if bytes >= KB {
        format!("{:.1} KB", bytes as f64 / KB as f64)
    } else {
        format!("{bytes} B")
    }
}

fn nix_overlay_dir(agent_id: &str) -> PathBuf {
    workspace::nix_overlay_base_for(&config::persistent_home(), agent_id)
}

/// Collect everything currently eligible for `cav gc --older-than <dur>`.
/// An agent is eligible when it's paused (exited_at is set), was last alive
/// at least `older_than` ago, and has any on-disk artifacts still around.
pub fn candidates(older_than: Duration) -> Vec<Candidate> {
    let cutoff = now_secs() - older_than.as_secs_f64();
    let Ok(all) = registry::all_agent_ids(None) else {
        return vec![];
    };
    let mut out = Vec::new();
    for aid in all {
        // Running agents and freshly-paused agents are off limits.
        let exited = registry::agent_exit_time(&aid).ok().flatten();
        let Some(exited_at) = exited else { continue };
        if exited_at > cutoff {
            continue;
        }
        let targets = resolve_targets(&aid);
        let mut size = 0u64;
        for (slug, _) in &targets {
            let ws = workspace::worktree_for(slug, &aid);
            if ws.exists() {
                size += dir_size(&ws);
            }
        }
        let overlay = nix_overlay_dir(&aid);
        if overlay.exists() {
            size += dir_size(&overlay);
        }
        if size == 0 {
            // Already collected, nothing left to free.
            continue;
        }
        out.push(Candidate {
            agent_id: aid,
            targets,
            size,
        });
    }
    out
}

fn resolve_targets(agent_id: &str) -> Vec<(String, PathBuf)> {
    let slugs = registry::agent_all_slugs(agent_id).unwrap_or_default();
    slugs
        .into_iter()
        .filter_map(|s| {
            registry::project_path(&s)
                .ok()
                .flatten()
                .map(|p| (s, PathBuf::from(p)))
        })
        .collect()
}

/// Remove on-disk artifacts for one agent. Registry row and session log
/// are left in place: the row stays as a tombstone so `cav -r N` produces
/// a useful "this agent was GC'd, session at <path>" message instead of
/// silently re-cloning a fresh worktree.
pub fn collect(c: &Candidate) {
    workspace::reclaim(&c.agent_id, &c.targets);
    // Also drop the host-side local agent branches; workspace::reclaim
    // only touches the bare repo's refs.
    for (_, base) in &c.targets {
        let prefix = format!("agent-{}/", c.agent_id);
        if let Ok(local_refs) = git::git_output(
            &[
                "for-each-ref",
                "--format=%(refname:short)",
                &format!("refs/heads/{prefix}"),
            ],
            Some(base),
        ) {
            for br in local_refs.lines() {
                git::git(&["branch", "-D", br], Some(base), false, true, false).ok();
            }
        }
    }
    let overlay = nix_overlay_dir(&c.agent_id);
    if overlay.exists() {
        let _ = std::fs::remove_dir_all(&overlay);
    }
}

pub fn run(older_than: Duration, dry_run: bool) -> Result<()> {
    let cands = candidates(older_than);
    if cands.is_empty() {
        println!("Nothing to GC.");
        return Ok(());
    }
    let mut total = 0u64;
    for c in &cands {
        let name = registry::display_name(Path::new("/"), &c.agent_id);
        total += c.size;
        if dry_run {
            println!(
                "Would GC {} ({})",
                style::agent_name(&name),
                style::yellow(&format_size(c.size)),
            );
        } else {
            collect(c);
            println!(
                "Collected {} ({})",
                style::agent_name(&name),
                style::yellow(&format_size(c.size)),
            );
        }
    }
    if dry_run {
        println!("Total: {}", style::bold(&format_size(total)));
    } else {
        println!("Reclaimed: {}", style::bold(&format_size(total)));
    }
    Ok(())
}
