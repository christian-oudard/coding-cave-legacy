use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, dev_branch, parse_dev_branch, resolve_base, worktree_dir};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE};
use crate::git;
use crate::registry;
use crate::sandbox;

pub fn reject(base: &Path, agent_id: Option<&str>, yes: bool, commit: Option<&str>) -> Result<()> {
    let branch = git::current_branch(Some(base))?;
    if parse_dev_branch(&branch).is_some() {
        bail!(
            "Cannot reject from dev branch '{branch}'.\n\
             Switch to a base branch first (e.g. 'git checkout main')."
        );
    }

    let verbose = config::verbose() >= 1;
    git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    sandbox::reap_dead_agents();
    sandbox::reconcile_merged_dev_branches(base, verbose);

    // Local '{branch}' is the source of truth; reject force-pushes it to
    // the cave at the end, so any drift between local and cave/{branch}
    // resolves itself.
    let tip_sha = git::git_output(&["rev-parse", "HEAD"], Some(base))?;

    let slug = config::project_slug(base);

    let approvals = if let Some(sha_prefix) = commit {
        let target_sha = resolve_reject_target(sha_prefix, &tip_sha, base)?;
        collect_approval_chain(&slug, &branch, &tip_sha, &target_sha)?
    } else {
        let all_approvals = collect_all_approvals(&slug, &branch, &tip_sha);
        if all_approvals.is_empty() {
            bail!(
                "No approval record found for the tip of '{branch}'.\n\
                 Only approved commits can be rejected."
            );
        }
        if all_approvals.len() > 1 && !yes {
            pick_reject(all_approvals, base, &branch)?
        } else {
            vec![all_approvals.into_iter().next().unwrap()]
        }
    };

    let before_sha = approvals.last().unwrap()["before_sha"]
        .as_str()
        .unwrap()
        .to_string();
    let original_agent_id = approvals.last().unwrap()["agent_id"]
        .as_str()
        .unwrap()
        .to_string();
    let original_approved_at = approvals
        .last()
        .unwrap()
        .get("approved_at")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);

    // Check none of the rejected commits have been pushed to origin.
    let on_origin = git::git(
        &[
            "merge-base",
            "--is-ancestor",
            &tip_sha,
            &format!("origin/{branch}"),
        ],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    if on_origin {
        bail!(
            "Cannot reject: commits are already on origin/{branch}.\n\
             Use 'git revert' instead."
        );
    }

    let base_agent = resolve_target_agent(agent_id, &original_agent_id)?;

    let commits =
        git::commit_oneline(&format!("{before_sha}..{tip_sha}"), None, Some(base))?;
    let n = commits.len();
    let word = if n == 1 { "commit" } else { "commits" };
    println!("\nRejecting {n} {word} from tip of {branch}:\n");
    for line in &commits {
        println!("  {line}");
    }

    block_if_agent_branch_diverged(base, &branch, &base_agent, &before_sha)?;

    if !yes {
        print!("\nReject? [y/N] ");
        io::stdout().flush()?;
        let mut answer = String::new();
        io::stdin().read_line(&mut answer).unwrap_or(0);
        let answer = answer.trim().to_lowercase();
        if answer != "y" && answer != "yes" {
            bail!("Aborted.");
        }
    }

    git::git(
        &[
            "push",
            "--force",
            CAVE_SHARED_REMOTE,
            &format!("{before_sha}:refs/heads/{branch}"),
        ],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    git::git(
        &["reset", "--hard", &before_sha],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    let approval_ids: Vec<i64> = approvals.iter().filter_map(|a| a["id"].as_i64()).collect();
    registry::delete_approvals(&approval_ids)?;

    let mut notes = drop_rejected_from_secondary_agents(
        base,
        &branch,
        &base_agent,
        &before_sha,
        &tip_sha,
    );
    if let Some(note) = recreate_target_branch_if_missing(
        base,
        &branch,
        &base_agent,
        &tip_sha,
        original_approved_at,
        verbose,
    )? {
        notes.push(note);
    }

    println!("\nRejected.");
    for note in notes {
        println!("  {note}");
    }
    Ok(())
}

/// When the target agent's dev branch is missing on cave (typically
/// because it was torn down by full approve), create it at the rejected
/// tip so the agent has somewhere to continue from. Skipped when the
/// agent ID has been re-used for an unrelated session (registered
/// agent's started_at is later than the original approval).
fn recreate_target_branch_if_missing(
    base: &Path,
    branch: &str,
    target_agent_id: &str,
    rejected_tip: &str,
    original_approved_at: f64,
    verbose: bool,
) -> Result<Option<String>> {
    let ab = dev_branch(branch, target_agent_id);
    let remote_agent = format!("{CAVE_SHARED_REMOTE}/{ab}");
    if git::git_output(&["rev-parse", &remote_agent], Some(base)).is_ok() {
        return Ok(None);
    }
    let started = match registry::agent_started_at(target_agent_id)? {
        Some(t) => t,
        None => return Ok(None),
    };
    if started > original_approved_at {
        return Ok(None);
    }
    git::git(
        &[
            "push",
            CAVE_SHARED_REMOTE,
            &format!("{rejected_tip}:refs/heads/{ab}"),
        ],
        Some(base),
        true,
        verbose,
        verbose,
    )?;
    let local_exists = git::git(
        &["rev-parse", "--verify", &format!("refs/heads/{ab}")],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    if !local_exists {
        git::git(
            &["branch", &ab, rejected_tip],
            Some(base),
            false,
            true,
            false,
        )
        .ok();
    }
    let target = registry::display_name(base, target_agent_id);
    Ok(Some(format!(
        "Recreated {target}'s branch '{ab}' at {short}.",
        short = &rejected_tip[..8.min(rejected_tip.len())],
    )))
}

/// Symmetric counterpart to approve's rebase_agents: every other agent's
/// worktree on this base may have been rebased onto the rejected commits
/// during the original approve. Drop the rejected commits from each via
/// `git rebase --onto <new_base> <rejected_tip>`. The target agent (the
/// committer of the rejected work) is left alone.
fn drop_rejected_from_secondary_agents(
    base: &Path,
    base_branch: &str,
    target_agent_id: &str,
    new_base_sha: &str,
    rejected_tip: &str,
) -> Vec<String> {
    let mut notes = vec![];
    let slug = config::project_slug(base);
    let shared = config::shared_repo_path(base);
    if !shared.exists() {
        return notes;
    }
    let agent_ids = match registry::all_agent_ids(Some(&slug)) {
        Ok(ids) => ids,
        Err(_) => return notes,
    };
    for agent_id in agent_ids {
        if agent_id == target_agent_id {
            continue;
        }
        let ws = worktree_dir(&slug, &agent_id);
        if !ws.exists() {
            continue;
        }
        let branch = match git::current_branch(Some(&ws)) {
            Ok(b) => b,
            Err(_) => continue,
        };
        if resolve_base(branch.clone()) != base_branch {
            continue;
        }
        let head_sha = match git::git_output(&["rev-parse", "HEAD"], Some(&ws)) {
            Ok(s) => s,
            Err(_) => continue,
        };
        if !git::is_ancestor(rejected_tip, &head_sha, Some(&ws)) {
            continue;
        }
        let dev = dev_branch(&branch, &agent_id);
        git::git(
            &[
                "fetch",
                &shared.to_string_lossy(),
                &format!(
                    "+refs/heads/{base_branch}:refs/remotes/{HOST_SHARED_REMOTE}/{base_branch}"
                ),
            ],
            Some(&ws),
            false,
            true,
            false,
        )
        .ok();
        let rebase_ok = git::git(
            &["rebase", "--onto", new_base_sha, rejected_tip],
            Some(&ws),
            false,
            true,
            false,
        )
        .is_ok();
        if !rebase_ok {
            git::git(&["rebase", "--abort"], Some(&ws), false, true, false).ok();
            notes.push(format!(
                "Note: dropping rejected commits from {dev} failed (conflicts). Agent must resolve them."
            ));
            continue;
        }
        git::git(
            &[
                "fetch",
                &ws.to_string_lossy(),
                &format!("+refs/heads/{branch}:refs/heads/{dev}"),
            ],
            Some(&shared),
            false,
            true,
            false,
        )
        .ok();
        git::update_agent_tip(&shared, &dev).ok();
        notes.push(format!("Dropped rejected commits from {dev}."));
    }
    notes
}

// Reject can only proceed when the agent's branch can fast-forward from
// (or is at/behind) the rolled-back main. Diverged history would leave
// approve permanently failing, so block and direct the agent to rebase.
fn block_if_agent_branch_diverged(
    base: &Path,
    branch: &str,
    agent_id: &str,
    new_base_sha: &str,
) -> Result<()> {
    let ab = dev_branch(branch, agent_id);
    let remote_agent = format!("{CAVE_SHARED_REMOTE}/{ab}");
    if git::git_output(&["rev-parse", &remote_agent], Some(base)).is_err() {
        return Ok(());
    }
    if git::is_ancestor(new_base_sha, &remote_agent, Some(base))
        || git::is_ancestor(&remote_agent, new_base_sha, Some(base))
    {
        return Ok(());
    }
    let target = registry::display_name(base, agent_id);
    let short_base = &new_base_sha[..8.min(new_base_sha.len())];
    bail!(
        "{target}'s branch '{ab}' has diverged from where main will be after this reject.\n\
         The agent must rebase their branch onto the new base first:\n\
         \n\
         \x20   (in the agent's session) git rebase {short_base}\n\
         \n\
         Resolve any conflicts the standard way, then retry 'cav reject'.\n\
         Alternative: 'cav remove-agent {agent_id}' to discard the dev branch entirely."
    );
}

fn collect_all_approvals(slug: &str, branch: &str, tip_sha: &str) -> Vec<serde_json::Value> {
    let mut approvals = vec![];
    let mut current = tip_sha.to_string();
    let null_sha = "0".repeat(40);
    while let Ok(Some(approval)) = registry::lookup_approval_by_after(slug, branch, &current) {
        let before = approval["before_sha"]
            .as_str()
            .unwrap_or("")
            .to_string();
        approvals.push(approval);
        if before == null_sha {
            break;
        }
        current = before;
    }
    approvals
}

fn pick_reject(
    approvals: Vec<serde_json::Value>,
    base: &Path,
    branch: &str,
) -> Result<Vec<serde_json::Value>> {
    println!("\nApproved commits on {branch} (newest first):\n");
    for (i, approval) in approvals.iter().enumerate() {
        let sha = approval["after_sha"].as_str().unwrap_or("");
        let aid = approval["agent_id"].as_str().unwrap_or("");
        let name = registry::display_name(base, aid);
        let msg = git::git_output(&["log", "--oneline", "-1", sha], Some(base))
            .unwrap_or_default();
        println!("  {}) {msg}  ({name})", i + 1);
    }

    print!(
        "\nReject how many commits from the tip? [1-{}] ",
        approvals.len()
    );
    io::stdout().flush()?;
    let mut answer = String::new();
    io::stdin()
        .read_line(&mut answer)
        .map_err(|_| anyhow::anyhow!("\nAborted."))?;
    let idx: usize = answer
        .trim()
        .parse::<usize>()
        .ok()
        .filter(|&n| n >= 1 && n <= approvals.len())
        .ok_or_else(|| anyhow::anyhow!("Invalid selection."))?;
    Ok(approvals.into_iter().take(idx).collect())
}

fn resolve_reject_target(sha_prefix: &str, tip_sha: &str, base: &Path) -> Result<String> {
    let full_sha = git::git_output(&["rev-parse", "--verify", sha_prefix], Some(base))
        .map_err(|_| short_prefix_error(sha_prefix, base))?;
    if !git::is_ancestor(&full_sha, tip_sha, Some(base)) {
        bail!("Commit {sha_prefix} is not on this branch.");
    }
    Ok(full_sha)
}

fn short_prefix_error(sha_prefix: &str, base: &Path) -> anyhow::Error {
    let lower = sha_prefix.to_ascii_lowercase();
    let matches = git::git_output(&["rev-parse", "--disambiguate", &lower], Some(base))
        .unwrap_or_default();
    let n = matches.lines().filter(|l| !l.is_empty()).count();
    if n > 1 {
        anyhow::anyhow!(
            "Commit prefix '{sha_prefix}' is ambiguous ({n} matches). Use a longer prefix."
        )
    } else {
        anyhow::anyhow!("Cannot resolve commit: {sha_prefix}")
    }
}

fn collect_approval_chain(
    slug: &str,
    branch: &str,
    tip_sha: &str,
    target_sha: &str,
) -> Result<Vec<serde_json::Value>> {
    let mut approvals = vec![];
    let mut current = tip_sha.to_string();
    let null_sha = "0".repeat(40);
    loop {
        let approval = registry::lookup_approval_by_after(slug, branch, &current)?
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "No approval record for {}.\n\
                     Cannot reject past unapproved commits.",
                    &current[..8]
                )
            })?;
        let is_target = current == target_sha;
        let before = approval["before_sha"]
            .as_str()
            .unwrap_or("")
            .to_string();
        approvals.push(approval);
        if is_target {
            break;
        }
        if before == null_sha {
            bail!(
                "Commit {} not found in approval chain.\n\
                 Only approved commits can be rejected.",
                &target_sha[..8]
            );
        }
        current = before;
    }
    Ok(approvals)
}

fn resolve_target_agent(
    agent_id: Option<&str>,
    original_agent_id: &str,
) -> Result<String> {
    if let Some(aid) = agent_id {
        let all_ids = registry::all_agent_ids(None)?;
        if !all_ids.contains(&aid.to_string()) {
            bail!("Agent {aid} not found in registry.");
        }
        return Ok(aid.to_string());
    }
    Ok(original_agent_id.to_string())
}
