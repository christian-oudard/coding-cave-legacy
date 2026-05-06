use std::env;
use std::path::{Path, PathBuf};

use anyhow::bail;
use regex::Regex;

use crate::constants::{HOST_SHARED_REPO_DIR, SHARED_REPO_DIR};

pub fn data_dir() -> PathBuf {
    if let Ok(v) = env::var("CODING_CAVE_DATA_DIR") {
        return PathBuf::from(v);
    }
    home_dir().join(".local").join("share").join("coding-cave")
}

fn home_dir() -> PathBuf {
    // std::env::home_dir is deprecated but functional on Linux;
    // use HOME env var directly.
    env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/"))
}

pub fn dev_branch(base: &str, agent_id: &str) -> String {
    format!("agent-{agent_id}/{base}")
}

/// Returns (base, agent_id) if the branch is an agent dev branch.
pub fn parse_dev_branch(branch: &str) -> Option<(String, String)> {
    let re = Regex::new(r"^agent-(\d+)/(.+)$").unwrap();
    re.captures(branch).map(|c| (c[2].to_string(), c[1].to_string()))
}

pub fn is_agent_id(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit())
}

/// Resolve the ultimate base branch, unwrapping nested agent branches.
pub fn resolve_base(mut branch: String) -> String {
    loop {
        match parse_dev_branch(&branch) {
            Some((base, _)) => branch = base,
            None => return branch,
        }
    }
}

// -- Project-level paths --

pub fn cav_dir(base: &Path) -> PathBuf {
    base.join(".coding-cave")
}

pub fn shared_repo_path(base: &Path) -> PathBuf {
    cav_dir(base).join(SHARED_REPO_DIR)
}

// -- Data dir paths --

/// Per-agent home directory on the host. Lives outside the legacy
/// shared home/ so one agent's cave cannot reach another's via the
/// home bind.
pub fn agent_home(agent_id: &str) -> PathBuf {
    data_dir().join("agents").join(agent_id).join("home")
}

/// Legacy shared home. Pre-per-agent layouts kept registry, sessions,
/// and overlay state here, all bind-mounted into every cave wholesale.
/// Retained as a fallback for code paths that haven't been migrated to
/// per-agent resolution yet.
pub fn persistent_home() -> PathBuf {
    data_dir().join("home")
}

pub fn launch_home(agent_name: &str) -> PathBuf {
    if inside_cave() {
        data_dir().join("subcave-homes").join(agent_name)
    } else {
        match agent_id_from_name(agent_name) {
            Some(aid) => agent_home(&aid),
            None => persistent_home(),
        }
    }
}

/// Extract the agent id from a launch/display name. Format is
/// `<slug>@<id>` optionally followed by ` (parent ...)`. Splits on the
/// first `@` so a parent name embedded in the lineage suffix doesn't
/// hijack the result.
fn agent_id_from_name(agent_name: &str) -> Option<String> {
    let after_first_at = agent_name.split_once('@').map(|(_, after)| after)?;
    let aid: String = after_first_at.chars().take_while(|c| c.is_ascii_digit()).collect();
    if aid.is_empty() { None } else { Some(aid) }
}

pub fn worktree_dir(slug: &str, agent_id: &str) -> PathBuf {
    data_dir().join("worktrees").join(slug).join(agent_id)
}

pub fn claude_project_dir(slug: &str, home: Option<&Path>) -> PathBuf {
    let home = home.map(|p| p.to_path_buf()).unwrap_or_else(persistent_home);
    let encoded = format!("/projects/{slug}")
        .chars()
        .map(|c| if c.is_alphanumeric() { c } else { '-' })
        .collect::<String>();
    home.join(".claude").join("projects").join(encoded)
}

pub fn session_file_exists(slug: &str, session_id: &str, home: Option<&Path>) -> bool {
    claude_project_dir(slug, home)
        .join(format!("{session_id}.jsonl"))
        .is_file()
}

pub fn project_slug(base: &Path) -> String {
    let name = base
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_lowercase();
    let re = Regex::new(r"[^a-z0-9_-]+").unwrap();
    let slug = re.replace_all(&name, "-").trim_matches('-').to_string();
    if slug.is_empty() { "project".to_string() } else { slug }
}

pub fn inside_cave() -> bool {
    env::var("CODING_CAVE_VERSION").is_ok()
}

static VERBOSE: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(0);

pub fn verbose() -> u8 {
    VERBOSE.load(std::sync::atomic::Ordering::Relaxed)
}

pub fn set_verbose(v: u8) {
    VERBOSE.store(v, std::sync::atomic::Ordering::Relaxed);
}

#[cfg(debug_assertions)]
static START_NS: std::sync::OnceLock<u128> = std::sync::OnceLock::new();

#[cfg(debug_assertions)]
fn now_ns() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

/// Anchor wall-clock time for verbose logs. Honors CODING_CAVE_TS_START so
/// child processes (supervisor, entry script) share the cav process's t=0.
#[cfg(debug_assertions)]
pub fn log_anchor() -> u128 {
    *START_NS.get_or_init(|| {
        if let Ok(s) = env::var("CODING_CAVE_TS_START") {
            if let Ok(n) = s.parse::<u128>() {
                return n;
            }
        }
        let n = now_ns();
        env::set_var("CODING_CAVE_TS_START", n.to_string());
        n
    })
}

pub fn log_prefix() -> String {
    #[cfg(debug_assertions)]
    {
        let elapsed_ns = now_ns().saturating_sub(log_anchor());
        let secs = elapsed_ns as f64 / 1e9;
        format!("[cav +{secs:6.3}s]")
    }
    #[cfg(not(debug_assertions))]
    "[cav]".to_string()
}

#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => {
        if $crate::config::verbose() >= 1 {
            eprintln!("{} {}", $crate::config::log_prefix(), format_args!($($arg)*));
        }
    };
}

#[macro_export]
macro_rules! log_debug {
    ($($arg:tt)*) => {
        if $crate::config::verbose() >= 2 {
            eprintln!("{} {}", $crate::config::log_prefix(), format_args!($($arg)*));
        }
    };
}

pub fn self_agent_id() -> Option<String> {
    let name = env::var("CODING_CAVE_AGENT_NAME").unwrap_or_default();
    if !name.contains('@') {
        return None;
    }
    let aid = name.rsplit('@').next()?.to_string();
    if aid.chars().all(|c| c.is_ascii_digit()) {
        Some(aid)
    } else {
        None
    }
}

/// Check that agent workspace alternates point at the right objects dir.
pub fn check_workspace_consistent(base: &Path) -> anyhow::Result<()> {
    if inside_cave() {
        check_alternates(base)?;
        return Ok(());
    }
    let slug = project_slug(base);
    let worktrees = data_dir().join("worktrees").join(&slug);
    if !worktrees.is_dir() {
        return Ok(());
    }
    for entry in std::fs::read_dir(&worktrees)?.flatten() {
        if entry.file_type()?.is_dir() {
            check_alternates(&entry.path())?;
        }
    }
    Ok(())
}

fn check_alternates(ws: &Path) -> anyhow::Result<()> {
    let alt = ws.join(".git").join("objects").join("info").join("alternates");
    if !alt.exists() {
        return Ok(());
    }
    let expected = format!("../../.coding-cave/{HOST_SHARED_REPO_DIR}/objects");
    let content = std::fs::read_to_string(&alt)?;
    if content.trim() == expected {
        return Ok(());
    }
    bail!(
        "Inconsistent workspace: {} does not match current layout.\n\
         Run: cav init --reinitialize",
        alt.display()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_id_extracts_from_simple_name() {
        assert_eq!(agent_id_from_name("project@5"), Some("5".to_string()));
        assert_eq!(agent_id_from_name("coding-cave@12"), Some("12".to_string()));
    }

    #[test]
    fn agent_id_ignores_lineage_suffix() {
        // Inside a subcave the launch name carries a "(parent ...)"
        // suffix containing another @<id>. Must read the first @<id>,
        // not the parent's.
        assert_eq!(
            agent_id_from_name("child@2 (parent project@1)"),
            Some("2".to_string())
        );
        assert_eq!(
            agent_id_from_name("a@7 (parent b@3 (parent c@1))"),
            Some("7".to_string())
        );
    }

    #[test]
    fn agent_id_rejects_non_numeric() {
        assert_eq!(agent_id_from_name("noatsign"), None);
        assert_eq!(agent_id_from_name("project@abc"), None);
        assert_eq!(agent_id_from_name(""), None);
    }

    #[test]
    fn host_launch_home_is_per_agent() {
        // For host launches (CODING_CAVE_VERSION unset), launch_home
        // must resolve to data_dir/agents/<id>/home so each agent has
        // its own backing dir.
        let tmp = std::env::temp_dir()
            .join(format!("cav-cfg-{}-host", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).unwrap();
        let key = "CODING_CAVE_DATA_DIR";
        let saved = std::env::var(key).ok();
        // SAFETY: tests run single-threaded by default, but if the test
        // harness opts into parallel the env mutation can race with
        // other tests that read CODING_CAVE_DATA_DIR. Acceptable here
        // because no other test in this crate touches that var.
        unsafe {
            std::env::set_var(key, &tmp);
            std::env::remove_var("CODING_CAVE_VERSION");
        }
        let h = launch_home("project@4");
        unsafe {
            match saved {
                Some(v) => std::env::set_var(key, v),
                None => std::env::remove_var(key),
            }
        }
        assert_eq!(h, tmp.join("agents").join("4").join("home"));
    }

    #[test]
    fn agents_get_distinct_homes() {
        // Critical invariant: two agents must not share a home dir.
        let tmp = std::env::temp_dir()
            .join(format!("cav-cfg-{}-distinct", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).unwrap();
        let key = "CODING_CAVE_DATA_DIR";
        let saved = std::env::var(key).ok();
        unsafe {
            std::env::set_var(key, &tmp);
            std::env::remove_var("CODING_CAVE_VERSION");
        }
        let h1 = launch_home("project@1");
        let h2 = launch_home("project@2");
        unsafe {
            match saved {
                Some(v) => std::env::set_var(key, v),
                None => std::env::remove_var(key),
            }
        }
        assert_ne!(h1, h2);
        assert!(!h1.starts_with(&h2));
        assert!(!h2.starts_with(&h1));
    }
}
