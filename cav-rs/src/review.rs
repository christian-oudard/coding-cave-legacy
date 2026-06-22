use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, parse_dev_branch, resolve_base};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;
use crate::pull;
use crate::registry;
use crate::style;

pub fn review(base: &Path, agent_id: &str) -> Result<()> {
    let verbose = config::verbose() >= 1;
    let slug = config::project_slug(base);
    let shared = config::shared_repo_path(base);

    let bare_branches = git::git_output(
        &["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        Some(&shared),
    )
    .unwrap_or_default();
    let candidates: Vec<String> = bare_branches
        .lines()
        .filter(|b| parse_dev_branch(b).is_some_and(|(_, aid)| aid == agent_id))
        .map(str::to_string)
        .collect();

    if candidates.is_empty() {
        if let Some(hint) = registry::other_project_hint(base, agent_id) {
            bail!(
                "No dev branch found for agent {} in {}.\n{hint}",
                style::bold(agent_id),
                style::blue(&slug),
            );
        }
        if registry::agent_project(agent_id).as_deref() == Some(slug.as_str())
            && registry::is_agent_running(agent_id).unwrap_or(false)
        {
            let name = registry::display_name(base, agent_id);
            bail!(
                "Agent {} is running with no pending commits to review.",
                style::agent_name(&name),
            );
        }
        bail!(
            "No dev branch found for agent {} in {}.",
            style::bold(agent_id),
            style::blue(&slug),
        );
    }

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
        let lines = git::undelivered_commits(&remote_target, &remote_dev, Some(base))
            .unwrap_or_default();
        if !lines.is_empty() {
            pending.push((branch.clone(), target, lines));
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

    println!("Checked out {}", style::yellow(&agent_branch));

    let log = git::undelivered_commits(&remote_target, &remote_ref, Some(base))
        .unwrap_or_default();
    if !log.is_empty() {
        let count = log.len();
        let n = if count == 1 { "commit" } else { "commits" };
        println!(
            "{} {n} ahead of {}:",
            style::bold(&count.to_string()),
            style::yellow(&target),
        );
        for line in &log {
            println!("{}", style::commit_line(line));
        }
    }
    Ok(())
}

fn pick(
    pending: Vec<(String, String, Vec<String>)>,
    base: &Path,
    agent_id: &str,
) -> Result<(String, String)> {
    let name = registry::display_name(base, agent_id);
    println!(
        "\nMultiple branches with pending work for {}:\n",
        style::agent_name(&name),
    );
    for (i, (branch, target, lines)) in pending.iter().enumerate() {
        let n = lines.len();
        let word = if n == 1 { "commit" } else { "commits" };
        println!(
            "  {}) {} -> {}    {} {word}",
            style::bold(&(i + 1).to_string()),
            style::yellow(branch),
            style::yellow(target),
            style::bold(&n.to_string()),
        );
        print!("{}", git::format_commit_list(lines, "  ", Some(3)));
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
