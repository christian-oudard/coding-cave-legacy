use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, inside_cave, parse_dev_branch, resolve_base, self_agent_id};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE};
use crate::git;
use crate::style;
use crate::workspace::worktree_for;

pub fn pull(base: &Path, force: bool) -> Result<()> {
    let local_branch = git::current_branch(Some(base))?;

    if inside_cave() {
        let aid = self_agent_id().ok_or_else(|| {
            anyhow::anyhow!(
                "Cannot determine this cave's agent id (CODING_CAVE_AGENT_NAME not set)."
            )
        })?;
        pull_cave_self(base, &local_branch, &aid, force)?;
        return Ok(());
    }

    let parsed = parse_dev_branch(&local_branch);
    if parsed.is_none() {
        bail!(
            "Not on a dev branch (current: '{}').\n\
             cav pull is only valid on agent branches (e.g. agent-1/main).\n\
             To review agent changes, run: {}",
            style::yellow(&local_branch),
            style::bold_cyan("cav review <agent-id>"),
        );
    }
    let (base_name, agent_id) = parsed.unwrap();
    let slug = config::project_slug(base);

    sync_agent_to_shared(base, &slug, &local_branch, &agent_id);

    let remote_dev = format!("{CAVE_SHARED_REMOTE}/{local_branch}");
    let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
    let verbose = config::verbose() >= 1;

    git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    if git::git_output(&["rev-parse", &remote_dev], Some(base)).is_err() {
        println!("No remote branch '{}'.", style::yellow(&remote_dev));
        return Ok(());
    }

    // Courtesy: update local base branch if it exists
    if git::git_output(&["rev-parse", &remote_base], Some(base)).is_ok()
        && git::git_output(&["rev-parse", &base_name], Some(base)).is_ok()
    {
        git::git(
            &["branch", "-f", "--no-track", &base_name, &remote_base],
            Some(base),
            false,
            true,
            false,
        )
        .ok();
    }

    let local_sha = git::git_output(&["rev-parse", &local_branch], Some(base))?;
    let remote_sha = git::git_output(&["rev-parse", &remote_dev], Some(base))?;

    if local_sha == remote_sha {
        println!("Already up to date.");
        return Ok(());
    }

    let local_only = git::git_output(
        &["log", "--oneline", &format!("{remote_dev}..{local_branch}")],
        Some(base),
    )
    .unwrap_or_default();

    let mut actual_local_only = local_only.clone();
    if !local_only.is_empty() && !force {
        let shared = config::shared_repo_path(base);
        let mut user_lines = vec![];
        for line in local_only.lines() {
            let sha = line.split_whitespace().next().unwrap_or("");
            let in_shared = git::git(
                &["cat-file", "-t", sha],
                Some(&shared),
                false,
                true,
                false,
            )
            .is_ok();
            if !in_shared {
                user_lines.push(line.to_string());
            }
        }
        actual_local_only = user_lines.join("\n");
    }

    if !actual_local_only.is_empty() && !force {
        let count = actual_local_only.lines().count();
        let n = if count == 1 { "commit" } else { "commits" };
        bail!(
            "Local {} {n} on '{}' would be overwritten:\n\
             {}\n\
             Use {} to discard them.",
            style::bold(&count.to_string()),
            style::yellow(&local_branch),
            actual_local_only
                .lines()
                .map(style::commit_line)
                .collect::<Vec<_>>()
                .join("\n"),
            style::bold_cyan("cav pull --force"),
        );
    }

    git::git(
        &["reset", "--hard", &remote_dev],
        Some(base),
        true,
        verbose,
        verbose,
    )?;
    println!(
        "Reset '{}' to '{}'.",
        style::yellow(&local_branch),
        style::yellow(&remote_dev),
    );
    show_ahead(base, &remote_base, &remote_dev, &base_name);
    Ok(())
}

fn pull_cave_self(base: &Path, local_branch: &str, agent_id: &str, force: bool) -> Result<()> {
    let remote_branch = config::dev_branch(local_branch, agent_id);
    let verbose = config::verbose() >= 1;
    git::git(
        &["fetch", "--prune", HOST_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;

    let remote_dev = format!("{HOST_SHARED_REMOTE}/{remote_branch}");
    let remote_sha = match git::git_output(&["rev-parse", &remote_dev], Some(base)) {
        Ok(s) => s,
        Err(_) => {
            println!("No upstream dev ref '{}'.", style::yellow(&remote_dev));
            return Ok(());
        }
    };

    let local_sha = git::git_output(&["rev-parse", local_branch], Some(base))?;
    if local_sha == remote_sha {
        println!("Already up to date.");
        return Ok(());
    }

    let local_only = git::git_output(
        &["log", "--oneline", &format!("{remote_dev}..{local_branch}")],
        Some(base),
    )
    .unwrap_or_default();

    if !local_only.is_empty() && !force {
        let count = local_only.lines().count();
        let n = if count == 1 { "commit" } else { "commits" };
        bail!(
            "Local {} {n} on '{}' would be overwritten:\n\
             {}\n\
             Use {} to discard them.",
            style::bold(&count.to_string()),
            style::yellow(local_branch),
            local_only
                .lines()
                .map(style::commit_line)
                .collect::<Vec<_>>()
                .join("\n"),
            style::bold_cyan("cav pull --force"),
        );
    }

    git::git(
        &["reset", "--hard", &remote_dev],
        Some(base),
        true,
        verbose,
        verbose,
    )?;
    println!(
        "Reset '{}' to '{}'.",
        style::yellow(local_branch),
        style::yellow(&remote_dev),
    );
    Ok(())
}

pub(crate) fn sync_agent_to_shared(base: &Path, slug: &str, dev_branch: &str, agent_id: &str) {
    let ws = worktree_for(slug, agent_id);
    if !ws.exists() {
        return;
    }
    let shared = config::shared_repo_path(base);
    if !shared.exists() {
        return;
    }

    let local_ref = [dev_branch.to_string(), resolve_base(dev_branch.to_string())]
        .into_iter()
        .find(|c| {
            git::git(
                &["rev-parse", "--verify", &format!("refs/heads/{c}")],
                Some(&ws),
                false,
                true,
                false,
            )
            .is_ok()
        });
    let Some(local_ref) = local_ref else { return };

    let Ok(local_sha) =
        git::git_output(&["rev-parse", &format!("refs/heads/{local_ref}")], Some(&ws))
    else {
        return;
    };

    let dest = format!("refs/heads/{dev_branch}");
    let cave_sha = git::git_output(&["rev-parse", &dest], Some(&shared)).ok();
    if cave_sha.as_deref() == Some(local_sha.as_str()) {
        return;
    }

    // Stage the worktree's tip into shared so we can ancestor-check it,
    // then skip promotion if cave's tip isn't an ancestor (e.g. cave was
    // rewritten by cav reject and we must not clobber that).
    let staging = format!("refs/cav/sync-tmp/{dev_branch}");
    let staging_refspec = format!("+refs/heads/{local_ref}:{staging}");
    if git::git(
        &["fetch", &ws.to_string_lossy(), &staging_refspec],
        Some(&shared),
        false,
        true,
        false,
    )
    .is_err()
    {
        return;
    }

    let promote = match cave_sha.as_deref() {
        None => true,
        Some(c) => git::is_ancestor(c, &local_sha, Some(&shared)),
    };
    if promote {
        git::git(
            &["update-ref", &dest, &local_sha],
            Some(&shared),
            false,
            true,
            false,
        )
        .ok();
        git::update_agent_tip_to(&shared, dev_branch, &local_sha).ok();
    }
    git::git(
        &["update-ref", "-d", &staging],
        Some(&shared),
        false,
        true,
        false,
    )
    .ok();
}

fn show_ahead(base: &Path, remote_base: &str, remote_dev: &str, base_name: &str) {
    let log = git::git_output(
        &["log", "--oneline", &format!("{remote_base}..{remote_dev}")],
        Some(base),
    )
    .unwrap_or_default();
    if !log.is_empty() {
        let count = log.lines().count();
        let n = if count == 1 { "commit" } else { "commits" };
        println!(
            "{} {n} ahead of {}:",
            style::bold(&count.to_string()),
            style::yellow(base_name),
        );
        for line in log.lines() {
            println!("{}", style::commit_line(line));
        }
    }
}
