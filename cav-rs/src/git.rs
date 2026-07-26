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

/// Returns the agent id when HEAD is on a dev branch like `agent-N/<base>`.
pub fn current_agent_id(cwd: Option<&Path>) -> Option<String> {
    let branch = current_branch(cwd).ok()?;
    crate::config::parse_dev_branch(&branch).map(|(_, aid)| aid)
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

pub fn is_ancestor(ancestor: &str, descendant: &str, cwd: Option<&Path>) -> bool {
    git(
        &["merge-base", "--is-ancestor", ancestor, descendant],
        cwd,
        false,
        true,
        false,
    )
    .is_ok()
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
            let (sha, subject) = rest.split_once(' ').unwrap_or((rest, ""));
            let sha_short: String = sha.chars().take(7).collect();
            format!("{sha_short} {}", subject.trim())
        })
        .collect();
    lines.reverse();
    Ok(lines)
}

/// Commits on `head` not yet delivered to `base`, newest first. When the
/// two share a merge base this is `cherry_undelivered`. When they don't —
/// the named `base` ref is absent (a dev branch forked from a
/// differently-named mainline, so `host-cave/<base>` never existed) or the
/// histories are unrelated — fall back to the commits unique to `head`.
pub fn undelivered_commits(base: &str, head: &str, cwd: Option<&Path>) -> Result<Vec<String>> {
    if has_merge_base(head, base, cwd) {
        cherry_undelivered(base, head, cwd)
    } else {
        commits_unique_to(head, cwd)
    }
}

/// Commits on `head` past its fork point, newest first, when `head` has no
/// merge base with its named base. The dev branch sits on top of some
/// mainline (e.g. `develop`) under a target name (`experimental`) that was
/// never pushed, so its real base is whatever other refs already contain.
///
/// The fork point is found by excluding every ref that does *not* contain
/// `head` — the base lines and other branches. Refs that *do* contain
/// `head` are the dev branch's own mirrors (its local base-name branch, the
/// `refs/cav/agent-tip/*` tip ref, `head` itself); excluding those would
/// subtract the agent's own commits and report nothing. In a brand-new repo
/// where `head` is the only ref, nothing is excluded, so this is every
/// commit on `head`.
fn commits_unique_to(head: &str, cwd: Option<&Path>) -> Result<Vec<String>> {
    let excludes = pending_excludes(None, head, cwd)?;
    let negated: Vec<String> = excludes.iter().map(|r| format!("^{r}")).collect();
    let mut args: Vec<&str> = vec!["log", "--format=%H %s", head];
    args.extend(negated.iter().map(|s| s.as_str()));
    let out = git_output(&args, cwd)?;
    Ok(format_log_lines(&out))
}

/// Refs to exclude when listing the commits on `head` pending delivery to
/// `base_ref`, for callers that build their own rev-list. With the base
/// present that is just the base. Without it — a target branch that has
/// never been pushed — it is the fork point described above, so callers
/// list the agent's own commits instead of the branch's whole history.
pub fn pending_excludes(
    base_ref: Option<&str>,
    head: &str,
    cwd: Option<&Path>,
) -> Result<Vec<String>> {
    if let Some(base) = base_ref {
        return Ok(vec![base.to_string()]);
    }
    let refs = git_output(&["for-each-ref", "--format=%(refname)"], cwd)?;
    Ok(refs
        .lines()
        .filter(|r| !is_ancestor(head, r, cwd))
        .map(str::to_string)
        .collect())
}

/// SHAs of the commits on `head` pending delivery to `base_ref`, newest
/// first.
pub fn pending_shas(
    base_ref: Option<&str>,
    head: &str,
    cwd: Option<&Path>,
) -> Result<Vec<String>> {
    let excludes = pending_excludes(base_ref, head, cwd)?;
    let negated: Vec<String> = excludes.iter().map(|r| format!("^{r}")).collect();
    let mut args: Vec<&str> = vec!["rev-list", head];
    args.extend(negated.iter().map(|s| s.as_str()));
    Ok(git_output(&args, cwd)?
        .lines()
        .map(str::to_string)
        .collect())
}

fn format_log_lines(out: &str) -> Vec<String> {
    out.lines()
        .filter(|l| !l.is_empty())
        .map(|l| {
            let (sha, subject) = l.split_once(' ').unwrap_or((l, ""));
            let sha_short: String = sha.chars().take(7).collect();
            format!("{sha_short} {}", subject.trim())
        })
        .collect()
}

pub fn commit_timestamp(ref_name: &str, cwd: Option<&Path>) -> Option<f64> {
    git_output(&["log", "-1", "--format=%ct", ref_name], cwd)
        .ok()
        .and_then(|s| s.parse().ok())
}

pub fn update_agent_tip(shared: &Path, branch: &str) -> Result<()> {
    let sha = git_output(&["rev-parse", &format!("refs/heads/{branch}")], Some(shared))?;
    update_agent_tip_to(shared, branch, &sha)
}

pub fn update_agent_tip_to(shared: &Path, branch: &str, sha: &str) -> Result<()> {
    let tip_ref = format!("refs/cav/agent-tip/{branch}");
    git(&["update-ref", &tip_ref, sha], Some(shared), false, true, false)?;
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


/// Render a list of "<sha> <subject>" commit lines.
/// `indent` is prepended to every line. `max` caps the number rendered;
/// `None` shows them all. The "and N more" tail is suppressed when only
/// one would be hidden (we just show it).
pub fn format_commit_list(lines: &[String], indent: &str, max: Option<usize>) -> String {
    let n = lines.len();
    let show = match max {
        None => n,
        Some(m) => if n == m + 1 { n } else { m.min(n) },
    };
    let mut out = String::new();
    for line in &lines[..show] {
        out.push_str(&format!("{indent}{}\n", crate::style::commit_line(line)));
    }
    let hidden = n - show;
    if hidden >= 2 {
        out.push_str(&format!(
            "{indent}{}\n",
            crate::style::dim(&format!("... and {hidden} more"))
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;
    use std::process::Command;

    fn run(repo: &Path, args: &[&str]) {
        let out = Command::new("git").args(args).current_dir(repo).output().unwrap();
        assert!(out.status.success(), "git {args:?} failed: {out:?}");
    }

    fn commit(repo: &Path, msg: &str) {
        run(repo, &["commit", "-q", "--allow-empty", "-m", msg]);
    }

    fn tmp_repo(label: &str) -> PathBuf {
        let base = std::env::temp_dir();
        for i in 0..1000 {
            let p = base.join(format!("cav-git-test-{label}-{i}"));
            if fs::create_dir(&p).is_ok() {
                run(&p, &["init", "-q", "-b", "main"]);
                run(&p, &["config", "user.email", "t@t"]);
                run(&p, &["config", "user.name", "T"]);
                return p;
            }
        }
        panic!("could not allocate tmp dir");
    }

    fn subjects(lines: &[String]) -> Vec<String> {
        lines.iter().map(|l| l.split_once(' ').unwrap().1.to_string()).collect()
    }

    #[test]
    fn undelivered_lists_all_commits_when_base_ref_missing() {
        // A brand-new repo whose only commits live on the agent branch: the
        // base ref doesn't exist, so every commit on the dev branch counts
        // as undelivered. This is the `cav status` "shows nothing" bug.
        let repo = tmp_repo("missing-base");
        // Branch off the unborn HEAD so `main` never comes into existence.
        run(&repo, &["checkout", "-q", "-b", "agent-1/main"]);
        commit(&repo, "root on agent branch only");
        commit(&repo, "Add Severance episode list");

        let lines = undelivered_commits("main", "agent-1/main", Some(&repo)).unwrap();
        assert_eq!(
            subjects(&lines),
            vec!["Add Severance episode list", "root on agent branch only"],
            "newest first, all commits surfaced",
        );
        fs::remove_dir_all(&repo).ok();
    }

    #[test]
    fn undelivered_lists_only_agent_commits_when_base_forked_from_mainline() {
        // The dev branch targets a name (`experimental`) that was never
        // pushed as a shared branch; it was actually forked from `develop`.
        // With no `experimental` ref, the old fallback dumped the entire
        // history (every commit on develop + the agent's). Undelivered must
        // be only the commits unique to the dev branch.
        let repo = tmp_repo("forked-base");
        run(&repo, &["checkout", "-q", "-b", "develop"]);
        commit(&repo, "develop 1");
        commit(&repo, "develop 2");
        commit(&repo, "develop 3");
        // Dev branch forks from develop, named after a non-existent target.
        run(&repo, &["checkout", "-q", "-b", "agent-1/experimental"]);
        commit(&repo, "agent work 1");
        commit(&repo, "agent work 2");

        let lines = undelivered_commits("experimental", "agent-1/experimental", Some(&repo))
            .unwrap();
        assert_eq!(
            subjects(&lines),
            vec!["agent work 2", "agent work 1"],
            "only the agent's own commits, not all of develop's history",
        );
        fs::remove_dir_all(&repo).ok();
    }

    #[test]
    fn undelivered_ignores_refs_that_mirror_the_dev_tip() {
        // Same missing-base setup, but another ref points at the dev branch's
        // own tip — its local base-name branch and the `refs/cav/agent-tip/*`
        // ref both mirror it in a real cave. Excluding those mirrors would
        // subtract the agent's own commits and report nothing. Undelivered
        // must still be the two commits unique to the dev branch.
        let repo = tmp_repo("mirror-tip");
        run(&repo, &["checkout", "-q", "-b", "develop"]);
        commit(&repo, "develop 1");
        commit(&repo, "develop 2");
        run(&repo, &["checkout", "-q", "-b", "agent-1/experimental"]);
        commit(&repo, "agent work 1");
        commit(&repo, "agent work 2");
        // Mirrors of the dev tip: the agent's local base-name branch and its
        // tip ref, both pointing at HEAD. The base cav compares against is the
        // shared ref `host-cave/experimental`, which was never pushed.
        run(&repo, &["branch", "experimental"]);
        run(&repo, &["update-ref", "refs/cav/agent-tip/agent-1/experimental", "HEAD"]);

        let lines =
            undelivered_commits("host-cave/experimental", "agent-1/experimental", Some(&repo))
                .unwrap();
        assert_eq!(
            subjects(&lines),
            vec!["agent work 2", "agent work 1"],
            "mirror refs at the dev tip must not zero out the agent's commits",
        );
        fs::remove_dir_all(&repo).ok();
    }

    #[test]
    fn undelivered_lists_only_commits_ahead_of_base() {
        // With a real shared base, only the commits not yet on the base are
        // undelivered, newest first.
        let repo = tmp_repo("with-base");
        commit(&repo, "shared root");
        run(&repo, &["checkout", "-q", "-b", "agent-1/main"]);
        commit(&repo, "first agent commit");
        commit(&repo, "second agent commit");

        let lines = undelivered_commits("main", "agent-1/main", Some(&repo)).unwrap();
        assert_eq!(
            subjects(&lines),
            vec!["second agent commit", "first agent commit"],
            "base commit excluded, newest first",
        );
        fs::remove_dir_all(&repo).ok();
    }
}
