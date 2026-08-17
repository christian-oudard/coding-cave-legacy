use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{
    self, dev_branch, inside_cave, parse_dev_branch, self_agent_id, shared_repo_path,
};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE};
use crate::git;
use crate::registry;
use crate::style;
use crate::workspace;

pub fn checkout(base: &Path, name: &str, agent_id: Option<&str>) -> Result<()> {
    if parse_dev_branch(name).is_some() {
        bail!(
            "Cannot use dev branch '{}' as base.\n\
             Use a base branch name (e.g. main, develop).",
            style::yellow(name),
        );
    }

    let current = git::current_branch(Some(base))?;

    if inside_cave() {
        let aid = self_agent_id().ok_or_else(|| {
            anyhow::anyhow!(
                "Cannot determine this cave's agent id (CODING_CAVE_AGENT_NAME not set)."
            )
        })?;
        if let Some(explicit) = agent_id {
            if explicit != aid {
                bail!(
                    "This cave is agent {}, but targeting agent {}.",
                    style::bold(&aid),
                    style::bold(explicit),
                );
            }
        }
        let old_base = current.clone();
        if old_base == name {
            bail!(
                "Agent {} already targeting '{}'.",
                style::bold(&aid),
                style::yellow(name),
            );
        }
        let old_branch = dev_branch(&old_base, &aid);
        let new_branch = dev_branch(name, &aid);
        ensure_base_exists(base, name, &old_base, HOST_SHARED_REMOTE)?;
        retarget_local(base, &old_base, name, &old_branch, &new_branch, &aid)?;
        println!(
            "Retargeted agent {}: {} -> {}",
            style::bold(&aid),
            style::yellow(&old_base),
            style::yellow(name),
        );
    } else {
        let parsed = parse_dev_branch(&current);
        let (old_base, aid) = if let Some((ob, a)) = parsed {
            if let Some(explicit) = agent_id {
                if explicit != a {
                    bail!(
                        "On branch for agent {}, but targeting agent {}.\n\
                         Switch to the correct dev branch first.",
                        style::bold(&a),
                        style::bold(explicit),
                    );
                }
            }
            (ob, a)
        } else if let Some(explicit_aid) = agent_id {
            let ob = find_agent_base(base, explicit_aid)?;
            (ob, explicit_aid.to_string())
        } else {
            bail!(
                "Not on a dev branch, please specify agent id.\nUsage: {}",
                style::bold_cyan("cav checkout <branch> <agent_id>"),
            );
        };
        if old_base == name {
            bail!(
                "Agent {} already targeting '{}'.",
                style::bold(&aid),
                style::yellow(name),
            );
        }
        let old_branch = dev_branch(&old_base, &aid);
        let new_branch = dev_branch(name, &aid);
        ensure_base_exists(base, name, &old_base, CAVE_SHARED_REMOTE)?;
        retarget_remote(base, &old_base, name, &old_branch, &new_branch, &aid)?;
        println!(
            "Retargeted agent {}: {} -> {}",
            style::bold(&aid),
            style::yellow(&old_base),
            style::yellow(name),
        );
    }
    Ok(())
}

fn find_agent_base(base: &Path, agent_id: &str) -> Result<String> {
    let verbose = config::verbose() >= 1;
    git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        true,
        verbose,
        verbose,
    )?;
    let branches = git::remote_branches(CAVE_SHARED_REMOTE, Some(base))?;
    let matches: Vec<String> = branches
        .into_iter()
        .filter(|b| parse_dev_branch(b).is_some_and(|(_, aid)| aid == agent_id))
        .collect();
    match matches.len() {
        0 => {
            let hint = registry::other_project_hint(base, agent_id)
                .map(|h| format!("\n{h}"))
                .unwrap_or_default();
            bail!(
                "No dev branch found for agent {} in {}.{hint}",
                style::bold(agent_id),
                style::blue(&config::project_slug(base)),
            )
        }
        1 => Ok(parse_dev_branch(&matches[0]).unwrap().0),
        _ => {
            let list = matches
                .iter()
                .map(|b| style::yellow(b))
                .collect::<Vec<_>>()
                .join(", ");
            bail!(
                "Agent {} has multiple dev branches: {list}\n\
                 Check out the branch to retarget first:\n\
                 \n  {}",
                style::bold(agent_id),
                style::bold_cyan(&format!("git checkout {}", &matches[0])),
            )
        }
    }
}

fn ensure_base_exists(base: &Path, name: &str, old_base: &str, remote: &str) -> Result<()> {
    let verbose = config::verbose() >= 1;
    git::git(
        &["fetch", "--prune", remote],
        Some(base),
        true,
        verbose,
        verbose,
    )?;
    let remote_ref = format!("{remote}/{name}");
    let exists = git::git(
        &["rev-parse", "--verify", &remote_ref],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    if exists {
        return Ok(());
    }
    let old_ref = format!("{remote}/{old_base}");
    let sha = git::git_output(&["rev-parse", &old_ref], Some(base)).map_err(|_| {
        anyhow::anyhow!(
            "Cannot resolve '{}' to create '{}'.",
            style::yellow(&old_ref),
            style::yellow(name),
        )
    })?;
    git::git(
        &["push", remote, &format!("{sha}:refs/heads/{name}")],
        Some(base),
        true,
        verbose,
        verbose,
    )?;
    Ok(())
}

fn retarget_local(
    base: &Path,
    old_base: &str,
    new_base: &str,
    old_branch: &str,
    new_branch: &str,
    agent_id: &str,
) -> Result<()> {
    git::git(
        &["branch", "-D", new_base],
        Some(base),
        false,
        true,
        false,
    )
    .ok();
    git::git(
        &["branch", "-m", old_base, new_base],
        Some(base),
        false,
        true,
        false,
    )?;
    workspace::set_agent_push_refspec(base, new_base, agent_id)?;

    let remote_base = format!("{HOST_SHARED_REMOTE}/{new_base}");
    git::git(
        &["rebase", &remote_base],
        Some(base),
        false,
        true,
        false,
    )
    .unwrap_or_else(|_| {
        git::git(
            &["rebase", "--abort"],
            Some(base),
            false,
            true,
            false,
        )
        .ok();
        String::new()
    });

    git::git(
        &["branch", "--set-upstream-to", &remote_base, new_base],
        Some(base),
        false,
        true,
        false,
    )
    .ok();

    let verbose = config::verbose() >= 1;
    let new_branch_ref = format!("refs/heads/{new_base}:refs/heads/{new_branch}");
    let del_ref = format!(":refs/heads/{old_branch}");
    git::git(
        &["push", "--force", HOST_SHARED_REMOTE, &new_branch_ref, &del_ref],
        Some(base),
        true,
        verbose,
        verbose,
    )?;
    Ok(())
}

fn retarget_remote(
    base: &Path,
    old_base: &str,
    new_base: &str,
    old_branch: &str,
    new_branch: &str,
    agent_id: &str,
) -> Result<()> {
    let shared = shared_repo_path(base);
    git::git(
        &["branch", "-m", old_branch, new_branch],
        Some(&shared),
        false,
        true,
        false,
    )?;

    let slug = config::project_slug(base);
    let ws = workspace::worktree_for(&slug, agent_id);
    if !ws.exists() {
        return Ok(());
    }

    git::git(
        &["fetch", "--prune", HOST_SHARED_REMOTE],
        Some(&ws),
        false,
        true,
        false,
    )
    .ok();
    git::git(
        &["branch", "-D", new_base],
        Some(&ws),
        false,
        true,
        false,
    )
    .ok();
    git::git(
        &["branch", "-m", old_base, new_base],
        Some(&ws),
        false,
        true,
        false,
    )?;
    workspace::set_agent_push_refspec(&ws, new_base, agent_id)?;
    git::git(
        &[
            "branch",
            "--set-upstream-to",
            &format!("{HOST_SHARED_REMOTE}/{new_base}"),
            new_base,
        ],
        Some(&ws),
        false,
        true,
        false,
    )
    .ok();
    Ok(())
}
