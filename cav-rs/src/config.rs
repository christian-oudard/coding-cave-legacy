use std::env;
use std::path::{Path, PathBuf};

use anyhow::bail;

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
    let rest = branch.strip_prefix("agent-")?;
    let slash = rest.find('/')?;
    let (id, base) = (&rest[..slash], &rest[slash + 1..]);
    if id.is_empty() || base.is_empty() || !id.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    Some((base.to_string(), id.to_string()))
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
    let mut slug = String::with_capacity(name.len());
    let mut prev_dash = true;
    for c in name.chars() {
        if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
            slug.push(c);
            prev_dash = c == '-';
        } else if !prev_dash {
            slug.push('-');
            prev_dash = true;
        }
    }
    let slug = slug.trim_matches('-').to_string();
    if slug.is_empty() { "project".to_string() } else { slug }
}

pub fn inside_cave() -> bool {
    env::var("CODING_CAVE_VERSION").is_ok()
}

/// User has opted into subcave operations by exporting CODING_CAVE_NESTED.
/// Required inside a cave to run subcave-affecting subcommands; see
/// `require_subcave_opt_in` in main.rs for the enforcement.
pub fn nested_opt_in() -> bool {
    env::var("CODING_CAVE_NESTED").is_ok()
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

    fn p(s: &str) -> Option<(String, String)> {
        parse_dev_branch(s)
    }

    #[test]
    fn parse_dev_branch_simple() {
        assert_eq!(p("agent-1/main"), Some(("main".into(), "1".into())));
        assert_eq!(p("agent-42/release"), Some(("release".into(), "42".into())));
    }

    #[test]
    fn parse_dev_branch_base_with_slashes() {
        assert_eq!(
            p("agent-3/feature/foo"),
            Some(("feature/foo".into(), "3".into())),
        );
    }

    #[test]
    fn parse_dev_branch_nested() {
        assert_eq!(
            p("agent-1/agent-2/main"),
            Some(("agent-2/main".into(), "1".into())),
        );
    }

    #[test]
    fn parse_dev_branch_rejects_non_digit_id() {
        assert_eq!(p("agent-abc/main"), None);
        assert_eq!(p("agent-1a/main"), None);
        assert_eq!(p("agent-/main"), None);
    }

    #[test]
    fn parse_dev_branch_rejects_empty_base() {
        assert_eq!(p("agent-1/"), None);
    }

    #[test]
    fn parse_dev_branch_rejects_no_prefix() {
        assert_eq!(p("main"), None);
        assert_eq!(p(""), None);
        assert_eq!(p("agent-1main"), None);
    }

    fn slug(s: &str) -> String {
        project_slug(Path::new(s))
    }

    #[test]
    fn project_slug_lowercases_and_keeps_allowed_chars() {
        assert_eq!(slug("MyProject"), "myproject");
        assert_eq!(slug("a_b-c"), "a_b-c");
        assert_eq!(slug("repo123"), "repo123");
    }

    #[test]
    fn project_slug_collapses_runs_of_invalid_chars() {
        assert_eq!(slug("My Project"), "my-project");
        assert_eq!(slug("a@@@b"), "a-b");
        assert_eq!(slug("a   b   c"), "a-b-c");
    }

    #[test]
    fn project_slug_trims_leading_and_trailing_dashes() {
        assert_eq!(slug("  hello  "), "hello");
        assert_eq!(slug("---repo---"), "repo");
        assert_eq!(slug("@@foo@@"), "foo");
    }

    #[test]
    fn project_slug_falls_back_when_empty() {
        assert_eq!(slug(""), "project");
        assert_eq!(slug("@@@"), "project");
        assert_eq!(slug("   "), "project");
    }

    #[test]
    fn project_slug_strips_non_ascii() {
        assert_eq!(slug("café"), "caf");
        assert_eq!(slug("über-cool"), "ber-cool");
    }

    #[test]
    fn project_slug_uses_basename_for_full_paths() {
        assert_eq!(slug("/home/user/My Project"), "my-project");
    }
}
