use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, parse_dev_branch, resolve_base};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;
use crate::pull;
use crate::registry;

pub fn review(base: &Path, agent_id: &str) -> Result<()> {
    let verbose = config::verbose() >= 1;
    git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    // Gather all dev branches for this agent.
    let all_branches = git::remote_branches(CAVE_SHARED_REMOTE, Some(base))?;
    let candidates: Vec<String> = all_branches
        .into_iter()
        .filter(|b| parse_dev_branch(b).is_some_and(|(_, aid)| aid == agent_id))
        .collect();

    if candidates.is_empty() {
        bail!("No agent branch found for agent {agent_id}.");
    }

    // Pull the agent's worktree commits into the cave so review reflects
    // anything the agent committed but hasn't pushed yet.
    let slug = config::project_slug(base);
    for branch in &candidates {
        pull::sync_agent_to_shared(base, &slug, branch, agent_id);
    }
    git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    // Prefer branches with unapproved commits.
    let mut pending: Vec<(String, String, Vec<String>)> = vec![];
    for branch in &candidates {
        let target = resolve_base(branch.clone());
        let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
        let remote_target = format!("{CAVE_SHARED_REMOTE}/{target}");
        if git::has_merge_base(&remote_dev, &remote_target, Some(base)) {
            let lines = git::cherry_undelivered(&remote_target, &remote_dev, Some(base))
                .unwrap_or_default();
            if !lines.is_empty() {
                pending.push((branch.clone(), target, lines));
            }
        }
    }

    let (agent_branch, target) = if pending.len() == 1 {
        let (b, t, _) = pending.remove(0);
        (b, t)
    } else if pending.len() > 1 {
        let current = git::current_branch(Some(base)).unwrap_or_default();
        if let Some(idx) = pending.iter().position(|(b, _, _)| *b == current) {
            let (b, t, _) = pending.remove(idx);
            (b, t)
        } else {
            pick(pending, base, agent_id)?
        }
    } else {
        // No pending work; fall back to first candidate.
        let b = candidates.into_iter().next().unwrap();
        let t = resolve_base(b.clone());
        (b, t)
    };

    let remote_ref = format!("{CAVE_SHARED_REMOTE}/{agent_branch}");
    let remote_target = format!("{CAVE_SHARED_REMOTE}/{target}");

    let current = git::current_branch(Some(base)).unwrap_or_default();
    if current == agent_branch {
        git::git(
            &["reset", "--hard", &remote_ref],
            Some(base),
            true,
            verbose,
            verbose,
        )?;
    } else {
        git::git(
            &["checkout", "-B", &agent_branch, &remote_ref],
            Some(base),
            true,
            verbose,
            verbose,
        )?;
    }

    println!("Checked out {agent_branch}");

    let log = git::git_output(
        &["log", "--oneline", &format!("{remote_target}..{remote_ref}")],
        Some(base),
    )
    .unwrap_or_default();
    if !log.is_empty() {
        let count = log.lines().count();
        let n = if count == 1 { "commit" } else { "commits" };
        println!("{count} {n} ahead of {target}:");
        println!("{log}");
    }
    Ok(())
}

fn pick(
    pending: Vec<(String, String, Vec<String>)>,
    base: &Path,
    agent_id: &str,
) -> Result<(String, String)> {
    let name = registry::display_name(base, agent_id);
    println!("\nMultiple branches with pending work for {name}:\n");
    for (i, (branch, target, lines)) in pending.iter().enumerate() {
        let n = lines.len();
        let word = if n == 1 { "commit" } else { "commits" };
        println!("  {}) {branch} -> {target}    {n} {word}", i + 1);
        print!("{}", git::format_commit_list(lines, 3));
    }

    print!("\nReview which? [1-{}] ", pending.len());
    io::stdout().flush().ok();
    let mut answer = String::new();
    match io::stdin().read_line(&mut answer) {
        Ok(0) | Err(_) => bail!("\nAborted."),
        Ok(_) => {}
    }
    let idx: usize = answer.trim().parse().unwrap_or(0);
    if idx >= 1 && idx <= pending.len() {
        let (b, t, _) = &pending[idx - 1];
        return Ok((b.clone(), t.clone()));
    }
    bail!("Invalid selection.")
}
