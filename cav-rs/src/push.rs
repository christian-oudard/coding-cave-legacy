use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::{self, dev_branch, inside_cave, parse_dev_branch, resolve_base, self_agent_id, worktree_dir};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE};
use crate::git;
use crate::registry;

pub fn push(base: &Path, force: bool) -> Result<()> {
    let branch = git::current_branch(Some(base))?;
    if branch == "HEAD" {
        let git_dir = git::git_output(&["rev-parse", "--git-dir"], Some(base))?;
        let git_dir_path = if git_dir.starts_with('/') {
            PathBuf::from(&git_dir)
        } else {
            base.join(&git_dir)
        };
        if git_dir_path.join("rebase-merge").exists()
            || git_dir_path.join("rebase-apply").exists()
        {
            bail!(
                "Rebase in progress. Complete or abort it before pushing:\n\
                   git rebase --continue\n\
                   git rebase --abort"
            );
        }
        bail!("Detached HEAD. Check out a branch before pushing.");
    }

    let parsed = parse_dev_branch(&branch);
    let verbose = config::verbose() >= 1;
    let result = if inside_cave() {
        let aid = self_agent_id()
            .ok_or_else(|| anyhow::anyhow!("Cannot determine this cave's agent id (CODING_CAVE_AGENT_NAME not set)."))?;
        push_cave_self(base, &branch, &aid, force, verbose)
    } else if let Some((_, agent_id)) = parsed {
        push_dev(base, &branch, &agent_id, force, verbose)
    } else {
        push_base(base, &branch, None, force, verbose)
    };

    match result {
        Err(e) => {
            eprintln!("{e}");
            if !force {
                eprintln!("\n  cav push --force                # overwrite shared remote with local branch");
            }
            std::process::exit(1);
        }
        Ok(up_to_date) => {
            if !verbose {
                if up_to_date {
                    println!("Already up to date.");
                } else {
                    println!("Pushed {branch} to shared remote.");
                }
            }
            Ok(())
        }
    }
}

fn push_cave_self(
    base: &Path,
    local_branch: &str,
    agent_id: &str,
    force: bool,
    verbose: bool,
) -> Result<bool> {
    let remote_branch = config::dev_branch(local_branch, agent_id);
    let refspec = format!("refs/heads/{local_branch}:refs/heads/{remote_branch}");
    let mut args = vec!["push", HOST_SHARED_REMOTE, &refspec];
    let force_flag = "--force".to_string();
    if force {
        args.insert(1, &force_flag);
    }
    let out = git::git(&args, Some(base), true, verbose, verbose)?;
    Ok(out.contains("up-to-date"))
}

pub fn push_base(
    base: &Path,
    base_branch: &str,
    refspec: Option<&str>,
    force: bool,
    verbose: bool,
) -> Result<bool> {
    let refspec_str = refspec.unwrap_or(base_branch).to_string();
    let mut args = vec!["push", "--no-set-upstream", CAVE_SHARED_REMOTE, &refspec_str];
    let force_flag = "--force".to_string();
    if force {
        args.insert(1, &force_flag);
    }
    let out = git::git(&args, Some(base), true, verbose, verbose)?;
    rebase_agents(base, base_branch)?;
    Ok(out.contains("up-to-date"))
}

fn push_dev(
    base: &Path,
    branch: &str,
    agent_id: &str,
    force: bool,
    verbose: bool,
) -> Result<bool> {
    let mut args = vec!["push", "--no-set-upstream", CAVE_SHARED_REMOTE, branch];
    let force_flag = "--force".to_string();
    if force {
        args.insert(1, &force_flag);
    }
    let out = git::git(&args, Some(base), true, verbose, verbose)?;
    let up_to_date = out.contains("up-to-date");

    let slug = config::project_slug(base);
    let ws = worktree_dir(&slug, agent_id);
    if !ws.exists() {
        return Ok(up_to_date);
    }

    let shared = config::shared_repo_path(base);
    git::git(
        &[
            "fetch",
            &shared.to_string_lossy(),
            &format!("+refs/heads/{branch}:refs/remotes/{HOST_SHARED_REMOTE}/{branch}"),
        ],
        Some(&ws),
        false,
        true,
        false,
    )
    .ok();

    let ws_branch = resolve_base(branch.to_string());
    let current = git::current_branch(Some(&ws)).unwrap_or_default();
    if current == ws_branch {
        sync_agent_worktree(&ws, branch, force);
    }

    git::update_agent_tip(&shared, branch).ok();
    Ok(up_to_date)
}

fn sync_agent_worktree(ws: &Path, branch: &str, force: bool) {
    let remote_ref = format!("{HOST_SHARED_REMOTE}/{branch}");
    let has_changes = git::git_output(&["status", "--porcelain"], Some(ws))
        .map(|s| !s.is_empty())
        .unwrap_or(false);

    if has_changes {
        git::git(&["stash"], Some(ws), false, true, false).ok();
    }

    if force {
        git::git(&["reset", "--hard", &remote_ref], Some(ws), false, true, false).ok();
    } else {
        let merge_ok =
            git::git(&["merge", "--ff-only", &remote_ref], Some(ws), false, true, false).is_ok();
        if !merge_ok {
            let rebase_ok =
                git::git(&["rebase", &remote_ref], Some(ws), false, true, false).is_ok();
            if !rebase_ok {
                git::git(&["rebase", "--abort"], Some(ws), false, true, false).ok();
                git::git(&["reset", "--hard", &remote_ref], Some(ws), false, true, false).ok();
                println!("Warning: could not rebase agent worktree, reset to pushed state.");
            }
        }
    }

    if has_changes {
        let pop_ok =
            git::git(&["stash", "pop"], Some(ws), false, true, false).is_ok();
        if !pop_ok {
            println!("Warning: agent worktree has merge conflicts from pushed changes.");
        }
    }
}

fn rebase_agents(base: &Path, base_branch: &str) -> Result<()> {
    let slug = config::project_slug(base);
    let shared = config::shared_repo_path(base);
    if !shared.exists() {
        return Ok(());
    }
    for agent_id in registry::all_agent_ids(Some(&slug))? {
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
        let dev = dev_branch(&branch, &agent_id);
        let ahead = git::commits_ahead(
            &format!("refs/heads/{dev}"),
            &format!("refs/heads/{base_branch}"),
            None,
            Some(&shared),
        )
        .unwrap_or(0);
        if ahead == 0 {
            continue;
        }
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
            &["rebase", &format!("{HOST_SHARED_REMOTE}/{base_branch}")],
            Some(&ws),
            false,
            true,
            false,
        )
        .is_ok();
        if !rebase_ok {
            git::git(&["rebase", "--abort"], Some(&ws), false, true, false).ok();
            println!("Warning: rebase of {dev} onto {base_branch} failed (conflicts).");
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
        println!("Rebased {dev} onto {base_branch}.");
    }
    Ok(())
}
