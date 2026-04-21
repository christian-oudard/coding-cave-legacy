use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, parse_dev_branch, resolve_base};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;

pub fn review(base: &Path, agent_id: &str) -> Result<()> {
    let verbose = config::verbose() >= 1;
    git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    let branches = git::remote_branches(CAVE_SHARED_REMOTE, Some(base))?;
    let agent_branch = branches
        .into_iter()
        .find(|b| parse_dev_branch(b).map_or(false, |(_, aid)| aid == agent_id));

    let agent_branch = match agent_branch {
        Some(b) => b,
        None => bail!("No agent branch found for agent {agent_id}."),
    };

    let remote_ref = format!("{CAVE_SHARED_REMOTE}/{agent_branch}");
    let target = resolve_base(agent_branch.clone());
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
