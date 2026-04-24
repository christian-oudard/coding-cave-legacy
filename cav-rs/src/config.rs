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

pub fn persistent_home() -> PathBuf {
    data_dir().join("home")
}

pub fn launch_home(agent_name: &str) -> PathBuf {
    if inside_cave() {
        data_dir().join("subcave-homes").join(agent_name)
    } else {
        persistent_home()
    }
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

pub fn latest_session_file(slug: &str, home: Option<&Path>) -> Option<String> {
    let dir = claude_project_dir(slug, home);
    if !dir.is_dir() {
        return None;
    }
    let mut best: Option<(String, std::time::SystemTime)> = None;
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().is_some_and(|e| e == "jsonl") {
                if let Ok(meta) = entry.metadata() {
                    if let Ok(mtime) = meta.modified() {
                        let stem = path.file_stem().unwrap().to_string_lossy().to_string();
                        match &best {
                            None => best = Some((stem, mtime)),
                            Some((_, best_mtime)) if mtime > *best_mtime => {
                                best = Some((stem, mtime))
                            }
                            _ => {}
                        }
                    }
                }
            }
        }
    }
    best.map(|(s, _)| s)
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
