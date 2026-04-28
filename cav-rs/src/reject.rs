use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, dev_branch, parse_dev_branch};
use crate::constants::CAVE_SHARED_REMOTE;
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

    let remote_base = format!("{CAVE_SHARED_REMOTE}/{branch}");
    let tip_sha = git::git_output(&["rev-parse", &remote_base], Some(base))
        .map_err(|_| anyhow::anyhow!("No remote branch '{remote_base}'."))?;

    let local_sha = git::git_output(&["rev-parse", "HEAD"], Some(base))?;
    if local_sha != tip_sha {
        bail!(
            "Local '{branch}' is not in sync with '{remote_base}'.\n\
             Run 'cav pull' first."
        );
    }

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

    let base_agent = resolve_target_agent(base, &branch, agent_id, &original_agent_id, &tip_sha)?;

    let commits =
        git::commit_oneline(&format!("{before_sha}..{tip_sha}"), None, Some(base))?;
    let n = commits.len();
    let word = if n == 1 { "commit" } else { "commits" };
    let target_name = registry::display_name(base, &base_agent);
    let target_running = registry::is_agent_running(&base_agent).unwrap_or(false);
    println!("\nRejecting {n} {word} from tip of {branch}:\n");
    for line in &commits {
        println!("  {line}");
    }
    println!("\nWork will be assigned to {target_name}.");
    if !target_running {
        println!(
            "  ({target_name} is stopped. Resume with 'cav --resume {base_agent}' to continue the work.)"
        );
    }

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

    let ab = dev_branch(&branch, &base_agent);
    if !agent_covers_tip(base, &ab, &tip_sha) {
        git::git(
            &[
                "push",
                "--force",
                CAVE_SHARED_REMOTE,
                &format!("{tip_sha}:refs/heads/{ab}"),
            ],
            Some(base),
            true,
            verbose,
            verbose,
        )?;
    }

    // Recreate the local agent branch on host if approve deleted it.
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
            &["branch", &ab, &tip_sha],
            Some(base),
            false,
            true,
            false,
        )
        .ok();
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

    if target_running {
        println!("\nRejected. {target_name} will pick up '{ab}' on its next pull.");
    } else {
        println!(
            "\nRejected. Resume {target_name} to continue the rejected work:\n  cav --resume {base_agent}"
        );
    }
    Ok(())
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
    println!("\nApproved commits on {branch}:\n");
    for (i, approval) in approvals.iter().enumerate() {
        let sha = approval["after_sha"].as_str().unwrap_or("");
        let aid = approval["agent_id"].as_str().unwrap_or("");
        let name = registry::display_name(base, aid);
        let msg = git::git_output(&["log", "--oneline", "-1", sha], Some(base))
            .unwrap_or_default();
        println!("  {}) {msg}  ({name})", i + 1);
    }

    print!(
        "\nReject from? [1=tip only, {}=all] ",
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
        .map_err(|_| anyhow::anyhow!("Cannot resolve commit: {sha_prefix}"))?;
    let on_branch = git::git(
        &["merge-base", "--is-ancestor", &full_sha, tip_sha],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    if !on_branch {
        bail!("Commit {sha_prefix} is not on this branch.");
    }
    if full_sha == tip_sha {
        bail!(
            "Commit {sha_prefix} is the current tip. \
             Use 'cav reject' without --commit."
        );
    }
    Ok(full_sha)
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

fn agent_covers_tip(base: &Path, agent_branch_name: &str, tip_sha: &str) -> bool {
    let remote_agent = format!("{CAVE_SHARED_REMOTE}/{agent_branch_name}");
    if git::git_output(&["rev-parse", &remote_agent], Some(base)).is_err() {
        return false;
    }
    git::git(
        &["merge-base", "--is-ancestor", tip_sha, &remote_agent],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok()
}

fn resolve_target_agent(
    base: &Path,
    branch: &str,
    agent_id: Option<&str>,
    original_agent_id: &str,
    tip_sha: &str,
) -> Result<String> {
    if let Some(aid) = agent_id {
        let all_ids = registry::all_agent_ids(None)?;
        if !all_ids.contains(&aid.to_string()) {
            bail!("Agent {aid} not found in registry.");
        }
        check_no_unapproved(base, branch, aid, tip_sha)?;
        return Ok(aid.to_string());
    }

    let all_ids = registry::all_agent_ids(None)?;
    if !all_ids.contains(&original_agent_id.to_string()) {
        bail!(
            "Original agent {original_agent_id} has been removed from the registry.\n\
             Specify a different agent to receive the rejected work:\n  cav reject <agent-id>"
        );
    }
    check_no_unapproved(base, branch, original_agent_id, tip_sha)?;
    Ok(original_agent_id.to_string())
}

fn check_no_unapproved(base: &Path, base_branch: &str, agent_id: &str, tip_sha: &str) -> Result<()> {
    let ab = dev_branch(base_branch, agent_id);
    let remote_agent = format!("{CAVE_SHARED_REMOTE}/{ab}");
    let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_branch}");

    if git::git_output(&["rev-parse", &remote_agent], Some(base)).is_err() {
        return Ok(());
    }
    if !git::has_merge_base(&remote_agent, &remote_base, Some(base)) {
        bail!(
            "Agent {agent_id}'s branch '{ab}' has diverged from '{base_branch}'.\n\
             Choose an agent with no existing work on '{base_branch}'."
        );
    }
    let has_undelivered = git::cherry_undelivered(&remote_base, &remote_agent, Some(base))
        .map(|v| !v.is_empty())
        .unwrap_or(false);
    if has_undelivered {
        if agent_covers_tip(base, &ab, tip_sha) {
            return Ok(());
        }
        bail!(
            "Agent {agent_id} has unapproved work on '{ab}'.\n\
             Approve or remove it first, or choose a different agent."
        );
    }
    Ok(())
}
