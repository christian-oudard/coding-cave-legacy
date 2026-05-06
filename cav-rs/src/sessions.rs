// Cross-cave session listing. Per-agent home isolation hides each
// agent's .claude/projects/ from every other agent. cav (host-side)
// can still see all of them — it has access to every per-agent home —
// so cross-cave session discovery happens here, not via the
// filesystem inside any cave.

use anyhow::Result;
use std::time::SystemTime;

use crate::config;

#[derive(Debug)]
pub struct SessionEntry {
    pub agent_id: String,
    pub project_slug: String,
    pub session_id: String,
    pub modified: SystemTime,
    pub size_bytes: u64,
}

/// Walk every per-agent home and collect session JSONLs.
/// Optionally filter to one agent.
pub fn list(filter_agent: Option<&str>) -> Result<Vec<SessionEntry>> {
    let agents_root = config::data_dir().join("agents");
    let mut out = Vec::new();
    if !agents_root.is_dir() {
        return Ok(out);
    }
    for agent_entry in std::fs::read_dir(&agents_root)? {
        let agent_entry = agent_entry?;
        if !agent_entry.file_type()?.is_dir() {
            continue;
        }
        let aid = agent_entry.file_name().to_string_lossy().to_string();
        if !config::is_agent_id(&aid) {
            continue;
        }
        if let Some(want) = filter_agent {
            if aid != want {
                continue;
            }
        }
        let projects_root = agent_entry.path().join("home/.claude/projects");
        if !projects_root.is_dir() {
            continue;
        }
        for proj_entry in std::fs::read_dir(&projects_root)?.flatten() {
            if !proj_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let slug = decode_project_slug(&proj_entry.file_name().to_string_lossy());
            for jsonl in std::fs::read_dir(proj_entry.path())?.flatten() {
                let path = jsonl.path();
                if path.extension().is_none_or(|e| e != "jsonl") {
                    continue;
                }
                let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
                    continue;
                };
                let meta = match std::fs::metadata(&path) {
                    Ok(m) => m,
                    Err(_) => continue,
                };
                out.push(SessionEntry {
                    agent_id: aid.clone(),
                    project_slug: slug.clone(),
                    session_id: stem.to_string(),
                    modified: meta.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                    size_bytes: meta.len(),
                });
            }
        }
    }
    out.sort_by(|a, b| b.modified.cmp(&a.modified));
    Ok(out)
}

/// Inverse of the encoding in config::claude_project_dir, which replaces
/// non-alphanumeric chars in `/projects/<slug>` with `-`. The encoding
/// is lossy so this returns the best-effort slug guess: strip the
/// leading "-projects-" prefix that always appears.
fn decode_project_slug(encoded: &str) -> String {
    encoded
        .strip_prefix("-projects-")
        .unwrap_or(encoded)
        .to_string()
}

pub fn print_list(entries: &[SessionEntry]) {
    if entries.is_empty() {
        println!("No sessions found.");
        return;
    }
    println!("{:<6} {:<20} {:<12} {:<10} {}", "AGENT", "PROJECT", "MODIFIED", "SIZE", "SESSION-UUID");
    let now = SystemTime::now();
    for e in entries {
        let age = now
            .duration_since(e.modified)
            .map(|d| format_age(d.as_secs()))
            .unwrap_or_else(|_| "future".to_string());
        let size = format_size(e.size_bytes);
        println!(
            "{:<6} {:<20} {:<12} {:<10} {}",
            e.agent_id, e.project_slug, age, size, e.session_id
        );
    }
}

fn format_age(secs: u64) -> String {
    if secs < 60 {
        format!("{secs}s ago")
    } else if secs < 3600 {
        format!("{}m ago", secs / 60)
    } else if secs < 86400 {
        format!("{}h ago", secs / 3600)
    } else {
        format!("{}d ago", secs / 86400)
    }
}

fn format_size(bytes: u64) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else if bytes < 1024 * 1024 {
        format!("{} KB", bytes / 1024)
    } else {
        format!("{} MB", bytes / (1024 * 1024))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_strips_projects_prefix() {
        // The encoding from claude_project_dir replaces non-alphanumeric
        // chars in /projects/<slug> with hyphens. Only the leading
        // "/projects/" → "-projects-" segment is recoverable; underscores
        // and other characters in the slug are already lost upstream, so
        // the decoder just returns whatever remains after the prefix.
        assert_eq!(decode_project_slug("-projects-coding-cave"), "coding-cave");
        assert_eq!(decode_project_slug("-projects-my-app"), "my-app");
    }

    #[test]
    fn format_age_units() {
        assert_eq!(format_age(5), "5s ago");
        assert_eq!(format_age(120), "2m ago");
        assert_eq!(format_age(7200), "2h ago");
        assert_eq!(format_age(172800), "2d ago");
    }

    #[test]
    fn format_size_units() {
        assert_eq!(format_size(500), "500 B");
        assert_eq!(format_size(2048), "2 KB");
        assert_eq!(format_size(2 * 1024 * 1024), "2 MB");
    }
}
