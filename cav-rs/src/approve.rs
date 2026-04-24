use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, parse_dev_branch, resolve_base};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;
use crate::push;
use crate::registry;

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

    // Build flat list of (branch, base_name, agent_id) candidates,
    // filtering out branches that are already fully merged (no new commits).
    let mut candidates: Vec<(String, String, String)> = vec![];
    for rb in git::remote_branches(CAVE_SHARED_REMOTE, Some(base))? {
        if let Some((_, aid)) = parse_dev_branch(&rb) {
            if agent_id.is_some_and(|a| a != aid) {
                continue;
            }
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
        bail!("No agent branches in {slug}.");
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

    let after_sha = if let Some(sha_prefix) = commit {
        resolve_partial(sha_prefix, before_sha.as_deref(), &dev_tip, base)?
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
        let word = if n == 1 { "commit" } else { "commits" };
        println!("\nApproving {n} of {} {word} from {name} into {slug}/{base_name}:\n", all_commits.len());
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
        print!("\nApprove? [Y/n] ");
        io::stdout().flush()?;
        let mut answer = String::new();
        if io::stdin().read_line(&mut answer).is_err() { bail!("\nAborted.") }
        let answer = answer.trim().to_lowercase();
        if answer == "n" || answer == "no" {
            bail!("Aborted.");
        }
    }

    do_approve(
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
        for line in commits.iter().take(3) {
            println!("     {line}");
        }
        if n > 3 {
            println!("     ... and {} more", n - 3);
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

fn resolve_partial(
    sha_prefix: &str,
    before_sha: Option<&str>,
    dev_tip: &str,
    base: &Path,
) -> Result<String> {
    let full_sha = git::git_output(&["rev-parse", "--verify", sha_prefix], Some(base))
        .map_err(|_| anyhow::anyhow!("Cannot resolve commit: {sha_prefix}"))?;

    // Must be an ancestor of dev tip
    let ok = git::git(
        &["merge-base", "--is-ancestor", &full_sha, dev_tip],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    if !ok {
        bail!("Commit {sha_prefix} is not on the dev branch.");
    }

    if let Some(bs) = before_sha {
        let ok = git::git(
            &["merge-base", "--is-ancestor", bs, &full_sha],
            Some(base),
            false,
            true,
            false,
        )
        .is_ok();
        if !ok {
            bail!("Commit {sha_prefix} is not after the base branch tip.");
        }
        if full_sha == bs {
            bail!("Nothing to approve: commit is the current base.");
        }
    }
    Ok(full_sha)
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
) -> Result<()> {
    let verbose = config::verbose() >= 1;
    push::push_base(
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
            println!("Approved, but local '{base_name}' could not be updated. Run 'cav pull' to sync.");
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
        git::git(
            &["branch", "-D", branch],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
    }
    Ok(())
}
