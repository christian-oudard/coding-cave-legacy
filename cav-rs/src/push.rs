use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::{self, dev_branch, inside_cave, parse_dev_branch, resolve_base, self_agent_id};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE};
use crate::git;
use crate::registry;
use crate::sandbox;
use crate::style;
use crate::workspace::worktree_for;

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
                   {}\n\
                   {}",
                style::bold_cyan("git rebase --continue"),
                style::bold_cyan("git rebase --abort"),
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
                eprintln!(
                    "\n  {}                {}",
                    style::bold_cyan("cav push --force"),
                    style::dim("# overwrite shared remote with local branch"),
                );
            }
            std::process::exit(1);
        }
        Ok((up_to_date, notes)) => {
            if !verbose {
                if up_to_date {
                    println!("Already up to date.");
                } else if force {
                    println!("Force-pushed {} to shared remote.", style::yellow(&branch));
                } else {
                    println!("Pushed {} to shared remote.", style::yellow(&branch));
                }
            }
            for note in notes {
                eprintln!("{note}");
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
) -> Result<(bool, Vec<String>)> {
    let remote_branch = config::dev_branch(local_branch, agent_id);
    // Compare local & remote SHAs directly. `git push` reports up-to-date on
    // stderr, not stdout, so parsing its output is unreliable.
    let local_sha = git::git_output(&["rev-parse", &format!("refs/heads/{local_branch}")], Some(base)).ok();
    let remote_sha = git::git_output(
        &["rev-parse", &format!("refs/remotes/{HOST_SHARED_REMOTE}/{remote_branch}")],
        Some(base),
    ).ok();
    if !force && local_sha.is_some() && local_sha == remote_sha {
        return Ok((true, vec![]));
    }
    let refspec = format!("refs/heads/{local_branch}:refs/heads/{remote_branch}");
    let mut args = vec!["push", HOST_SHARED_REMOTE, &refspec];
    let force_flag = "--force".to_string();
    if force {
        args.insert(1, &force_flag);
    }
    git::git(&args, Some(base), true, verbose, verbose)?;
    Ok((false, vec![]))
}

pub fn push_base(
    base: &Path,
    base_branch: &str,
    refspec: Option<&str>,
    force: bool,
    verbose: bool,
) -> Result<(bool, Vec<String>)> {
    let refspec_str = refspec.unwrap_or(base_branch).to_string();
    // refspec may be "sha:refs/heads/branch" (from approve) or just "branch".
    let local_sha = if let Some(colon) = refspec_str.find(':') {
        git::git_output(&["rev-parse", "--verify", &refspec_str[..colon]], Some(base)).ok()
    } else {
        git::git_output(&["rev-parse", &format!("refs/heads/{base_branch}")], Some(base)).ok()
    };
    let remote_sha = git::git_output(
        &["rev-parse", &format!("refs/remotes/{CAVE_SHARED_REMOTE}/{base_branch}")],
        Some(base),
    ).ok();
    if !force && local_sha.is_some() && local_sha == remote_sha {
        return Ok((true, vec![]));
    }
    let mut args = vec!["push", "--no-set-upstream", CAVE_SHARED_REMOTE, &refspec_str];
    let force_flag = "--force".to_string();
    if force {
        args.insert(1, &force_flag);
    }
    git::git(&args, Some(base), true, verbose, verbose)?;
    sandbox::reconcile_merged_dev_branches(base, verbose);
    let notes = rebase_agents(base, base_branch);
    Ok((false, notes))
}

fn push_dev(
    base: &Path,
    branch: &str,
    agent_id: &str,
    force: bool,
    verbose: bool,
) -> Result<(bool, Vec<String>)> {
    let local_sha = git::git_output(&["rev-parse", &format!("refs/heads/{branch}")], Some(base)).ok();
    let remote_sha = git::git_output(
        &["rev-parse", &format!("refs/remotes/{CAVE_SHARED_REMOTE}/{branch}")],
        Some(base),
    ).ok();
    let up_to_date = !force && local_sha.is_some() && local_sha == remote_sha;
    if !up_to_date {
        let mut args = vec!["push", "--no-set-upstream", CAVE_SHARED_REMOTE, branch];
        let force_flag = "--force".to_string();
        if force {
            args.insert(1, &force_flag);
        }
        git::git(&args, Some(base), true, verbose, verbose)?;
    }

    let slug = config::project_slug(base);
    let ws = worktree_for(&slug, agent_id);
    if !ws.exists() {
        return Ok((up_to_date, vec![]));
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
    let notes = if current == ws_branch {
        sync_agent_worktree(&ws, branch, force)
    } else {
        vec![]
    };

    git::update_agent_tip(&shared, branch).ok();
    Ok((up_to_date, notes))
}

fn sync_agent_worktree(ws: &Path, branch: &str, force: bool) -> Vec<String> {
    let remote_ref = format!("{HOST_SHARED_REMOTE}/{branch}");
    let has_changes = git::git_output(&["status", "--porcelain"], Some(ws))
        .map(|s| !s.is_empty())
        .unwrap_or(false);

    if has_changes {
        git::git(&["stash"], Some(ws), false, true, false).ok();
    }

    let mut notes = vec![];

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
                notes.push("Note: agent worktree could not be rebased; reset to pushed state.".to_string());
            }
        }
    }

    if has_changes {
        let pop_ok =
            git::git(&["stash", "pop"], Some(ws), false, true, false).is_ok();
        if !pop_ok {
            notes.push("Note: agent worktree has merge conflicts from pushed changes.".to_string());
        }
    }

    notes
}

fn rebase_agents(base: &Path, base_branch: &str) -> Vec<String> {
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
        let ws = worktree_for(&slug, &agent_id);
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
        let dev_ref = format!("refs/heads/{dev}");
        let base_ref = format!("refs/heads/{base_branch}");
        // Patch-id check: skip work already on base (whether by ancestry
        // or by patch-equivalent rebase). Avoids both the pointless
        // worktree rebase and the resurrection of refs that approve or
        // reconcile_merged_dev_branches already deleted.
        let dev_exists =
            git::git(&["rev-parse", "--verify", &dev_ref], Some(&shared), false, true, false)
                .is_ok();
        let undelivered = if dev_exists {
            git::cherry_undelivered(&base_ref, &dev_ref, Some(&shared)).unwrap_or_default()
        } else {
            vec![]
        };
        if undelivered.is_empty() {
            if dev_exists {
                git::git(&["update-ref", "-d", &dev_ref], Some(&shared), false, true, false).ok();
                let tip_ref = format!("refs/cav/agent-tip/{dev}");
                git::git(&["update-ref", "-d", &tip_ref], Some(&shared), false, true, false).ok();
                git::git(&["branch", "-D", &dev], Some(base), false, true, false).ok();
            }
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
            notes.push(format!(
                "Note: rebase of {} onto {} failed (conflicts). Agent must resolve them.",
                style::yellow(&dev),
                style::yellow(base_branch),
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
        notes.push(format!(
            "Rebased {} onto {}.",
            style::yellow(&dev),
            style::yellow(base_branch),
        ));
    }
    notes
}
