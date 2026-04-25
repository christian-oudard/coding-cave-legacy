use std::path::Path;
use std::process::{Command, Stdio};

use anyhow::{bail, Result};

/// Run a git command. Echoes to stderr when `echo` is true.
/// Returns stdout as a string if `capture` is true, otherwise empty string.
pub fn git(
    args: &[&str],
    cwd: Option<&Path>,
    _check: bool,
    capture: bool,
    echo: bool,
) -> Result<String> {
    if echo {
        let cmd_str = std::iter::once("git")
            .chain(args.iter().copied())
            .collect::<Vec<_>>()
            .join(" ");
        eprintln!("+ {cmd_str}");
    }

    let mut cmd = Command::new("git");
    cmd.args(args);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    if capture {
        cmd.stdout(Stdio::piped());
        cmd.stderr(Stdio::piped());
    }

    let output = cmd.output()?;
    if !output.status.success() {
        let code = output.status.code().unwrap_or(-1);
        let stderr_text = String::from_utf8_lossy(&output.stderr);
        let stderr_trimmed = stderr_text.trim();
        if capture && !stderr_trimmed.is_empty() {
            bail!(
                "git {} failed with exit code {}\n{}",
                args.join(" "),
                code,
                stderr_trimmed
            );
        }
        bail!("git {} failed with exit code {}", args.join(" "), code);
    }
    if capture {
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    } else {
        Ok(String::new())
    }
}

pub fn git_output(args: &[&str], cwd: Option<&Path>) -> Result<String> {
    git(args, cwd, true, true, false)
}

pub fn repo_root(cwd: Option<&Path>) -> Result<String> {
    git_output(&["rev-parse", "--show-toplevel"], cwd)
}

pub fn current_branch(cwd: Option<&Path>) -> Result<String> {
    let branch = match git_output(&["rev-parse", "--abbrev-ref", "HEAD"], cwd) {
        Ok(b) => b,
        Err(_) => {
            // Orphan branch: HEAD is symbolic but has no commit yet
            return git_output(&["symbolic-ref", "--short", "HEAD"], cwd);
        }
    };
    if branch != "HEAD" {
        return Ok(branch);
    }
    // Detached HEAD: check if sitting on (or just above) a branch tip.
    Ok(detached_branch(cwd).unwrap_or(branch))
}

fn git_operation_in_progress(cwd: Option<&Path>) -> bool {
    let git_dir = match git_output(&["rev-parse", "--git-dir"], cwd) {
        Ok(d) => d,
        Err(_) => return false,
    };
    let base = if git_dir.starts_with('/') {
        std::path::PathBuf::from(&git_dir)
    } else {
        let wd = cwd
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| std::env::current_dir().unwrap_or_default());
        wd.join(&git_dir)
    };
    for sentinel in &["rebase-merge", "rebase-apply", "BISECT_LOG", "CHERRY_PICK_HEAD"] {
        if base.join(sentinel).exists() {
            return true;
        }
    }
    false
}

fn detached_branch(cwd: Option<&Path>) -> Option<String> {
    if git_operation_in_progress(cwd) {
        return None;
    }
    for ref_name in &["HEAD", "HEAD^"] {
        let out = git_output(
            &[
                "for-each-ref",
                "--points-at",
                ref_name,
                "refs/heads/",
                "--format=%(refname:short)",
            ],
            cwd,
        )
        .ok()?;
        if out.is_empty() {
            continue;
        }
        let candidates: Vec<&str> = out.lines().collect();
        if candidates.len() == 1 {
            return Some(candidates[0].to_string());
        }
    }
    None
}

pub fn remote_branches(remote: &str, cwd: Option<&Path>) -> Result<Vec<String>> {
    let out = git_output(&["branch", "-r", "--list", &format!("{remote}/*")], cwd)?;
    if out.is_empty() {
        return Ok(vec![]);
    }
    let prefix = format!("{remote}/");
    let branches = out
        .lines()
        .map(str::trim)
        .filter(|l| !l.contains(" -> "))
        .filter_map(|l| l.strip_prefix(&prefix))
        .map(str::to_string)
        .collect();
    Ok(branches)
}

pub fn commits_ahead(
    branch_a: &str,
    branch_b: &str,
    exclude: Option<&[&str]>,
    cwd: Option<&Path>,
) -> Result<usize> {
    let not_b = format!("^{branch_b}");
    let excludes: Vec<String> = exclude
        .unwrap_or(&[])
        .iter()
        .map(|r| format!("^{r}"))
        .collect();
    let mut args: Vec<&str> = vec!["rev-list", "--count", branch_a, &not_b];
    args.extend(excludes.iter().map(|s| s.as_str()));
    let out = git_output(&args, cwd)?;
    Ok(out.parse().unwrap_or(0))
}

pub fn has_merge_base(ref_a: &str, ref_b: &str, cwd: Option<&Path>) -> bool {
    git(
        &["merge-base", ref_a, ref_b],
        cwd,
        false,
        true,
        false,
    )
    .map(|_| true)
    .unwrap_or(false)
}

pub fn cherry_undelivered(
    upstream: &str,
    head: &str,
    cwd: Option<&Path>,
) -> Result<Vec<String>> {
    let out = git_output(&["cherry", "-v", upstream, head], cwd)?;
    if out.is_empty() {
        return Ok(vec![]);
    }
    let mut lines: Vec<String> = out
        .lines()
        .filter(|l| l.starts_with("+ "))
        .map(|l| {
            let rest = &l[2..];
            let sha_short: String = rest.chars().take(7).collect();
            let subject = rest.get(8..).unwrap_or("").trim().to_string();
            format!("{sha_short} {subject}")
        })
        .collect();
    lines.reverse();
    Ok(lines)
}

pub fn commit_timestamp(ref_name: &str, cwd: Option<&Path>) -> Option<f64> {
    git_output(&["log", "-1", "--format=%ct", ref_name], cwd)
        .ok()
        .and_then(|s| s.parse().ok())
}

pub fn update_agent_tip(shared: &Path, branch: &str) -> Result<()> {
    let rev_ref = format!("refs/heads/{branch}");
    let sha = git_output(&["rev-parse", &rev_ref], Some(shared))?;
    let tip_ref = format!("refs/cav/agent-tip/{branch}");
    git(&["update-ref", &tip_ref, &sha], Some(shared), false, true, false)?;
    Ok(())
}

pub fn commit_oneline(
    range_spec: &str,
    exclude: Option<&[&str]>,
    cwd: Option<&Path>,
) -> Result<Vec<String>> {
    let excludes: Vec<String> = exclude
        .unwrap_or(&[])
        .iter()
        .map(|r| format!("^{r}"))
        .collect();
    let mut args: Vec<&str> = vec!["log", "--oneline", range_spec];
    args.extend(excludes.iter().map(|s| s.as_str()));
    let out = git_output(&args, cwd)?;
    if out.is_empty() {
        return Ok(vec![]);
    }
    Ok(out.lines().map(str::to_string).collect())
}


/// Format a list of commit lines for display, showing up to max items.
/// If only 1 item would be hidden, shows it too (avoids "... and 1 more").
pub fn format_commit_list(lines: &[String], max: usize) -> String {
    let n = lines.len();
    let show = if n == max + 1 { n } else { max.min(n) };
    let mut out = String::new();
    for line in &lines[..show] {
        out.push_str(&format!("  {line}\n"));
    }
    let hidden = n - show;
    if hidden >= 2 {
        out.push_str(&format!("  ... and {hidden} more\n"));
    }
    out
}
