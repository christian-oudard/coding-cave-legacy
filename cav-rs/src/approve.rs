use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, parse_dev_branch, resolve_base, shared_repo_path};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;
use crate::push;
use crate::registry;
use crate::sandbox;

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

    // Reconcile leftovers from prior crashes or half-done commands:
    // reap any trivially-clean dead agents, and drop shared dev branches
    // whose tip already matches the base tip (provably nothing to lose).
    sandbox::reap_dead_agents();
    sandbox::reconcile_merged_dev_branches(base, verbose);

    // Build flat list of (branch, base_name, agent_id) candidates,
    // filtering out branches that are already fully merged (no new commits).
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
            // Skip branches that are already merged (tip == base tip)
            if let (Ok(dev_sha), Ok(base_sha)) = (
                git::git_output(&["rev-parse", &remote_dev], Some(base)),
                git::git_output(&["rev-parse", &remote_base], Some(base)),
            ) {
                if dev_sha == base_sha {
                    continue;
                }
            }
            candidates.push((rb, base_name, aid));
        }
    }

    if candidates.is_empty() {
        if let Some(aid) = agent_id {
            bail!("No commits to approve for agent {aid} in {slug}.");
        }
        if has_any_agent_branches {
            bail!("No unmerged commits to approve in {slug}.");
        }
        bail!("No agent branches in {slug}.");
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
            0 => bail!("No pending commit matches '{prefix}' in {slug}."),
            1 => {
                let (cand, sha) = matches.into_iter().next().unwrap();
                candidates = vec![cand];
                commit_sha = Some(sha);
            }
            _ => bail!(
                "Commit prefix '{prefix}' is ambiguous in {slug}.\n\
                 Specify agent: cav approve <agent-id> -c {prefix}"
            ),
        }
    }

    // Disambiguate by current branch if checked out.
    if candidates.len() > 1 {
        let current = git::current_branch(Some(base)).unwrap_or_default();
        let matching: Vec<_> = candidates
            .iter()
            .filter(|(b, _, _)| b == &current)
            .cloned()
            .collect();
        if !matching.is_empty() {
            candidates = matching;
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
            bail!("No commits to approve for agent {resolved_agent_id} in {slug}.");
        }
        let ancestor_ok = git::git(
            &["merge-base", "--is-ancestor", bs, &dev_tip],
            Some(base),
            false,
            true,
            false,
        )
        .is_ok();
        if !ancestor_ok {
            bail!(
                "Cannot approve: {branch} is not a fast-forward from {base_name}.\n\
                 The agent must rebase onto {base_name} first."
            );
        }
    }

    let after_sha = if let Some(sha) = commit_sha {
        sha
    } else {
        dev_tip.clone()
    };
    let partial = after_sha != dev_tip;

    let all_commits = if creating {
        git::commit_oneline(&remote_dev, None, Some(base))?
    } else {
        git::commit_oneline(
            &format!("{remote_base}..{remote_dev}"),
            None,
            Some(base),
        )?
    };

    if partial {
        let commits = if creating {
            git::commit_oneline(&after_sha, None, Some(base))?
        } else {
            git::commit_oneline(
                &format!("{remote_base}..{after_sha}"),
                None,
                Some(base),
            )?
        };
        let remaining = all_commits.len() - commits.len();
        let n = commits.len();
        let total = all_commits.len();
        let word = if total == 1 { "commit" } else { "commits" };
        println!("\nApproving {n} of {total} {word} from {name} into {slug}/{base_name}:\n");
        for line in &commits {
            println!("  {line}");
        }
        println!("\n  ({remaining} remaining on {branch})");
    } else {
        let n = all_commits.len();
        let word = if n == 1 { "commit" } else { "commits" };
        println!("\n{n} {word} from {name} to merge into {slug}/{base_name}:\n");
        for line in &all_commits {
            println!("  {line}");
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
            "Multiple choices in {slug}: agents {}\nSpecify which: cav approve <agent-id>",
            ids.into_iter().collect::<Vec<_>>().join(", ")
        );
    }

    println!("\nPending work in {slug}:\n");
    for (i, (branch, base_name, aid)) in candidates.iter().enumerate() {
        let name = registry::display_name(base, aid);
        let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
        let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
        let commits = if git::git(
            &["rev-parse", "--verify", &remote_base],
            Some(base),
            false,
            true,
            false,
        )
        .is_ok()
        {
            git::commit_oneline(&format!("{remote_base}..{remote_dev}"), None, Some(base))
                .unwrap_or_default()
        } else {
            git::commit_oneline(&remote_dev, None, Some(base)).unwrap_or_default()
        };
        let n = commits.len();
        let word = if n == 1 { "commit" } else { "commits" };
        println!("  {}) {name} → {base_name}    {n} {word}", i + 1);
        let show = if n == 4 { 4 } else { 3.min(n) };
        for line in commits.iter().take(show) {
            println!("     {line}");
        }
        let hidden = n - show;
        if hidden >= 2 {
            println!("     ... and {} more", hidden);
        }
    }

    print!("\nApprove which? [1-{}] ", candidates.len());
    io::stdout().flush()?;
    let mut answer = String::new();
    io::stdin().read_line(&mut answer).map_err(|_| anyhow::anyhow!("\nAborted."))?;
    let idx: usize = answer
        .trim()
        .parse::<usize>()
        .ok()
        .filter(|&n| n >= 1 && n <= candidates.len())
        .ok_or_else(|| anyhow::anyhow!("Invalid selection."))?;
    Ok(candidates.into_iter().nth(idx - 1).unwrap())
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
    let range = if has_base {
        format!("{remote_base}..{remote_dev}")
    } else {
        remote_dev
    };
    let shas = git::git_output(&["rev-list", &range], Some(base)).ok()?;
    shas.lines().find(|sha| sha.starts_with(prefix)).map(String::from)
}

fn do_approve(
    slug: &str,
    project_base: &Path,
    branch: &str,
    base_name: &str,
    agent_id: &str,
    before_sha: Option<&str>,
    after_sha: &str,
    creating: bool,
    partial: bool,
) -> Result<Vec<String>> {
    let verbose = config::verbose() >= 1;
    let (_, mut notes) = push::push_base(
        project_base,
        base_name,
        Some(&format!("{after_sha}:refs/heads/{base_name}")),
        false,
        verbose,
    )?;

    // Record one approval per commit
    let range_spec = if creating {
        after_sha.to_string()
    } else {
        format!("{}..{}", before_sha.unwrap(), after_sha)
    };
    let shas = git::git_output(
        &["rev-list", "--reverse", &range_spec],
        Some(project_base),
    )?;
    let null_sha = "0".repeat(40);
    let mut prev = before_sha.unwrap_or(&null_sha).to_string();
    for sha in shas.lines() {
        registry::record_approval(slug, base_name, agent_id, &prev, sha)?;
        prev = sha.to_string();
    }

    // Courtesy: sync local base branch
    let current = git::current_branch(Some(project_base)).unwrap_or_default();
    if creating {
        git::git(
            &["checkout", "-B", base_name, after_sha],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
    } else if current == base_name {
        let ok = git::git(
            &["merge", "--ff-only", after_sha],
            Some(project_base),
            false,
            true,
            false,
        )
        .is_ok();
        if !ok {
            notes.push(format!(
                "Note: local branch '{base_name}' could not be updated."
            ));
        }
    } else {
        git::git(
            &["branch", "-f", base_name, after_sha],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
        git::git(
            &["checkout", base_name],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
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

