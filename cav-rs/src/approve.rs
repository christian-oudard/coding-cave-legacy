use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, parse_dev_branch, resolve_base, shared_repo_path};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;
use crate::push;
use crate::registry;
use crate::sandbox;
use crate::style;

pub fn approve(
    base: &Path,
    agent_id: Option<&str>,
    yes: bool,
    commit: Option<&str>,
) -> Result<()> {
    let slug = config::project_slug(base);
    let verbose = config::verbose() >= 1;

    git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    // Drop shared dev branches whose tip already matches the base tip
    // (provably nothing to lose).
    sandbox::reconcile_merged_dev_branches(base, verbose);

    // Build flat list of (branch, base_name, agent_id) candidates,
    // filtering out branches with no commits ahead of base. Covers branches
    // already merged into base and branches that lag behind base.
    let mut candidates: Vec<(String, String, String)> = vec![];
    let mut has_any_agent_branches = false;
    for rb in git::remote_branches(CAVE_SHARED_REMOTE, Some(base))? {
        if let Some((_, aid)) = parse_dev_branch(&rb) {
            if agent_id.is_some_and(|a| a != aid) {
                continue;
            }
            has_any_agent_branches = true;
            let base_name = resolve_base(rb.clone());
            let remote_dev = format!("{CAVE_SHARED_REMOTE}/{rb}");
            let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
            let has_base = git::git(
                &["rev-parse", "--verify", &remote_base],
                Some(base),
                false,
                true,
                false,
            )
            .is_ok();
            if has_base
                && git::commits_ahead(&remote_dev, &remote_base, None, Some(base))
                    .unwrap_or(0)
                    == 0
            {
                continue;
            }
            candidates.push((rb, base_name, aid));
        }
    }

    if candidates.is_empty() {
        if let Some(aid) = agent_id {
            let hint = registry::other_project_hint(base, aid)
                .map(|h| format!("\n{h}"))
                .unwrap_or_default();
            bail!(
                "No commits to approve for agent {} in {}.{hint}",
                style::bold(aid),
                style::blue(&slug),
            );
        }
        if has_any_agent_branches {
            bail!("No unmerged commits to approve in {}.", style::blue(&slug));
        }
        bail!("No agent branches in {}.", style::blue(&slug));
    }

    // Resolve commit prefix against candidates' pending ranges.
    // This both narrows the candidate (when agent isn't given) and
    // produces the full SHA without relying on global rev-parse, which
    // can fail for short prefixes due to git's abbreviation minimum.
    let mut commit_sha: Option<String> = None;
    if let Some(prefix) = commit {
        let lower = prefix.to_ascii_lowercase();
        let mut matches: Vec<((String, String, String), String)> = vec![];
        for cand in &candidates {
            if let Some(sha) = candidate_match_commit(&cand.0, &cand.1, &lower, base) {
                matches.push((cand.clone(), sha));
            }
        }
        match matches.len() {
            0 => bail!(
                "No pending commit matches '{}' in {}.",
                style::yellow(prefix),
                style::blue(&slug)
            ),
            1 => {
                let (cand, sha) = matches.into_iter().next().unwrap();
                candidates = vec![cand];
                commit_sha = Some(sha);
            }
            _ => bail!(
                "Commit prefix '{}' is ambiguous in {}.\n\
                 Specify agent: {} -c {}",
                style::yellow(prefix),
                style::blue(&slug),
                style::bold_cyan("cav approve <agent-id>"),
                style::yellow(prefix),
            ),
        }
    }

    // Disambiguate by current branch if checked out.
    if candidates.len() > 1 {
        let current = git::current_branch(Some(base)).unwrap_or_default();
        // First try exact agent-branch match (user is on a reviewed branch).
        let by_branch: Vec<_> = candidates
            .iter()
            .filter(|(b, _, _)| b == &current)
            .cloned()
            .collect();
        if !by_branch.is_empty() {
            candidates = by_branch;
        } else {
            // Then try base-branch match: on 'main' with only one agent targeting 'main'.
            let by_base: Vec<_> = candidates
                .iter()
                .filter(|(_, base_name, _)| base_name == &current)
                .cloned()
                .collect();
            if by_base.len() == 1 {
                candidates = by_base;
            }
        }
    }

    let (branch, base_name, resolved_agent_id) = if candidates.len() == 1 {
        candidates.remove(0)
    } else {
        pick_candidate(candidates, base, &slug, yes)?
    };

    let name = registry::display_name(base, &resolved_agent_id);
    let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
    let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");

    let dev_tip = git::git_output(&["rev-parse", &remote_dev], Some(base))
        .map_err(|_| anyhow::anyhow!("No remote branch '{remote_dev}'."))?;

    let base_result = git::git(
        &["rev-parse", "--verify", &remote_base],
        Some(base),
        false,
        true,
        false,
    );
    let creating = base_result.is_err();
    let before_sha: Option<String> = if creating {
        None
    } else {
        Some(base_result?)
    };

    if !creating {
        let bs = before_sha.as_deref().unwrap();
        if bs == dev_tip {
            bail!(
                "No commits to approve for agent {} in {}.",
                style::bold(&resolved_agent_id),
                style::blue(&slug)
            );
        }
        if !git::is_ancestor(bs, &dev_tip, Some(base)) {
            bail!(
                "Cannot approve: {} is not a fast-forward from {}.\n\
                 The agent must rebase onto {} first.",
                style::yellow(&branch),
                style::yellow(&base_name),
                style::yellow(&base_name),
            );
        }
    }

    let after_sha = if let Some(sha) = commit_sha {
        sha
    } else {
        dev_tip.clone()
    };
    let partial = after_sha != dev_tip;

    let base_ref = if creating { None } else { Some(remote_base.as_str()) };
    let all_commits = pending_oneline(base_ref, &remote_dev, base)?;

    // Oldest first — the chain do_approve records, one row per commit.
    let mut approval_shas = git::pending_shas(base_ref, &after_sha, Some(base))?;
    approval_shas.reverse();

    if partial {
        let commits = pending_oneline(base_ref, &after_sha, base)?;
        let remaining = all_commits.len() - commits.len();
        let n = commits.len();
        let total = all_commits.len();
        let word = if total == 1 { "commit" } else { "commits" };
        println!(
            "\nApproving {} of {} {word} from {} into {}/{}:\n",
            style::bold(&n.to_string()),
            style::bold(&total.to_string()),
            style::agent_name(&name),
            style::blue(&slug),
            style::yellow(&base_name),
        );
        for line in &commits {
            println!("  {}", style::commit_line(line));
        }
        println!(
            "\n  {}",
            style::dim(&format!("({remaining} remaining on {branch})"))
        );
    } else {
        let n = all_commits.len();
        let word = if n == 1 { "commit" } else { "commits" };
        println!(
            "\n{} {word} from {} to merge into {}/{}:\n",
            style::bold(&n.to_string()),
            style::agent_name(&name),
            style::blue(&slug),
            style::yellow(&base_name),
        );
        for line in &all_commits {
            println!("  {}", style::commit_line(line));
        }
    }

    if !yes {
        print!("\nApprove? [y/N] ");
        io::stdout().flush()?;
        let mut answer = String::new();
        io::stdin().read_line(&mut answer).unwrap_or(0);
        let answer = answer.trim().to_lowercase();
        if answer != "y" && answer != "yes" {
            bail!("Aborted.");
        }
    }

    let notes = do_approve(
        &slug,
        base,
        &branch,
        &base_name,
        &resolved_agent_id,
        before_sha.as_deref(),
        &after_sha,
        &approval_shas,
        creating,
        partial,
    )?;

    println!("Approved.");
    for note in notes {
        eprintln!("{note}");
    }
    Ok(())
}

fn pick_candidate(
    candidates: Vec<(String, String, String)>,
    base: &Path,
    slug: &str,
    yes: bool,
) -> Result<(String, String, String)> {
    if yes {
        let ids: std::collections::BTreeSet<String> =
            candidates.iter().map(|(_, _, aid)| aid.clone()).collect();
        bail!(
            "Multiple choices in {}: agents {}\nSpecify which: {}",
            style::blue(slug),
            style::bold(&ids.into_iter().collect::<Vec<_>>().join(", ")),
            style::bold_cyan("cav approve <agent-id>"),
        );
    }

    println!("\nPending work in {}:\n", style::blue(slug));
    for (branch, base_name, aid) in candidates.iter() {
        let name = registry::display_name(base, aid);
        let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
        let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
        let has_base = git::git(
            &["rev-parse", "--verify", &remote_base],
            Some(base),
            false,
            true,
            false,
        )
        .is_ok();
        let commits = pending_oneline(has_base.then_some(remote_base.as_str()), &remote_dev, base)
            .unwrap_or_default();
        let n = commits.len();
        let word = if n == 1 { "commit" } else { "commits" };
        println!(
            "  {}) {} → {}    {} {word}",
            style::bold(aid),
            style::agent_name(&name),
            style::yellow(base_name),
            style::bold(&n.to_string()),
        );
        print!("{}", git::format_commit_list(&commits, "     ", Some(3)));
    }

    let ids: Vec<&str> = candidates.iter().map(|(_, _, aid)| aid.as_str()).collect();
    print!("\nApprove which agent? [{}] ", ids.join("/"));
    io::stdout().flush()?;
    let mut answer = String::new();
    match io::stdin().read_line(&mut answer) {
        Ok(0) | Err(_) => bail!("\nAborted."),
        Ok(_) => {}
    }
    let answer = answer.trim();
    candidates
        .into_iter()
        .find(|(_, _, aid)| aid == answer)
        .ok_or_else(|| anyhow::anyhow!("Invalid selection."))
}

/// Returns the full SHA of the unique commit in the candidate's pending
/// range whose hex starts with `prefix`. Returns None if no commit matches.
fn candidate_match_commit(
    branch: &str,
    base_name: &str,
    prefix: &str,
    base: &Path,
) -> Option<String> {
    let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
    let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
    let has_base = git::git(
        &["rev-parse", "--verify", &remote_base],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    let base_ref = has_base.then_some(remote_base.as_str());
    let shas = git::pending_shas(base_ref, &remote_dev, Some(base)).ok()?;
    shas.into_iter().find(|sha| sha.starts_with(prefix))
}

/// Commits pending delivery from `head` into the base, newest first.
/// `base_ref` is None when the target branch has never been pushed.
fn pending_oneline(base_ref: Option<&str>, head: &str, base: &Path) -> Result<Vec<String>> {
    let excludes = git::pending_excludes(base_ref, head, Some(base))?;
    let refs: Vec<&str> = excludes.iter().map(String::as_str).collect();
    git::commit_oneline(head, Some(&refs), Some(base))
}

fn do_approve(
    slug: &str,
    project_base: &Path,
    branch: &str,
    base_name: &str,
    agent_id: &str,
    before_sha: Option<&str>,
    after_sha: &str,
    approval_shas: &[String],
    creating: bool,
    partial: bool,
) -> Result<Vec<String>> {
    let verbose = config::verbose() >= 1;

    // Sync local base branch first. If the working tree is dirty in a
    // way that blocks the ff-merge, git's own message ('Please commit
    // your changes or stash them before you merge') is surfaced and
    // approve aborts before touching the cave or the registry.
    let current = git::current_branch(Some(project_base)).unwrap_or_default();
    if creating {
        git::git(
            &["checkout", "-B", base_name, after_sha],
            Some(project_base),
            false,
            true,
            false,
        )?;
    } else if current == base_name {
        git::git(
            &["merge", "--ff-only", after_sha],
            Some(project_base),
            false,
            true,
            false,
        )?;
    } else {
        git::git(
            &["branch", "-f", base_name, after_sha],
            Some(project_base),
            false,
            true,
            false,
        )?;
        git::git(
            &["checkout", base_name],
            Some(project_base),
            false,
            true,
            false,
        )?;
    }

    let (_, notes) = push::push_base(
        project_base,
        base_name,
        Some(&format!("{after_sha}:refs/heads/{base_name}")),
        Some(agent_id),
        false,
        verbose,
    )?;

    // Record one approval per commit
    let null_sha = "0".repeat(40);
    let mut prev = before_sha.unwrap_or(&null_sha).to_string();
    for sha in approval_shas {
        registry::record_approval(slug, base_name, agent_id, &prev, sha)?;
        prev = sha.to_string();
    }

    if !partial {
        // Delete local copy in the user's working repo.
        git::git(
            &["branch", "-D", branch],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
        // Delete from the shared bare repo so the branch doesn't linger as a
        // phantom (no pending commits, invisible in status, confusing in review).
        // The agent's next push recreates it automatically.
        let shared = shared_repo_path(project_base);
        git::git(
            &["branch", "-D", branch],
            Some(&shared),
            false,
            true,
            false,
        )
        .ok();
        // Prune the host's remote-tracking ref so `cave/<dev>` doesn't linger.
        git::git(
            &["fetch", "--prune", CAVE_SHARED_REMOTE],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
    }
    Ok(notes)
}

