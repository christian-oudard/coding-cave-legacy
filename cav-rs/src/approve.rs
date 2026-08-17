use std::io::{self, Write};
use std::path::Path;

use anyhow::{bail, Result};

use crate::config::{self, parse_dev_branch, resolve_base, shared_repo_path};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;
use crate::push;
use crate::registry;
use crate::sandbox;
use crate::style;

/// (dev branch, base branch, agent id)
type Candidate = (String, String, String);

pub fn approve(
    base: &Path,
    agent_id: Option<&str>,
    agent_base: Option<&str>,
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

    // Drop shared dev branches whose tip already matches the base tip
    // (provably nothing to lose).
    sandbox::reconcile_merged_dev_branches(base, verbose);

    // Build flat list of (branch, base_name, agent_id) candidates,
    // filtering out branches with no commits ahead of base. Covers branches
    // already merged into base and branches that lag behind base.
    let mut candidates: Vec<Candidate> = vec![];
    let mut has_any_agent_branches = false;
    for rb in git::remote_branches(CAVE_SHARED_REMOTE, Some(base))? {
        if let Some((_, aid)) = parse_dev_branch(&rb) {
            if agent_id.is_some_and(|a| a != aid) {
                continue;
            }
            has_any_agent_branches = true;
            let base_name = resolve_base(rb.clone());
            if agent_base.is_some_and(|b| b != base_name) {
                continue;
            }
            let remote_dev = format!("{CAVE_SHARED_REMOTE}/{rb}");
            let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
            let has_base = git::git(
                &["rev-parse", "--verify", &remote_base],
                Some(base),
                false,
                true,
                false,
            )
            .is_ok();
            if has_base
                && git::commits_ahead(&remote_dev, &remote_base, None, Some(base))
                    .unwrap_or(0)
                    == 0
            {
                continue;
            }
            candidates.push((rb, base_name, aid));
        }
    }

    sort_candidates(&mut candidates);

    if candidates.is_empty() {
        if let (Some(aid), Some(agent_base)) = (agent_id, agent_base) {
            bail!(
                "No commits to approve for agent {} on {} in {}.",
                style::bold(aid),
                style::yellow(agent_base),
                style::blue(&slug),
            );
        }
        if let Some(aid) = agent_id {
            let hint = registry::other_project_hint(base, aid)
                .map(|h| format!("\n{h}"))
                .unwrap_or_default();
            bail!(
                "No commits to approve for agent {} in {}.{hint}",
                style::bold(aid),
                style::blue(&slug),
            );
        }
        if has_any_agent_branches {
            bail!("No unmerged commits to approve in {}.", style::blue(&slug));
        }
        bail!("No agent branches in {}.", style::blue(&slug));
    }

    // Resolve commit prefix against candidates' pending ranges.
    // This both narrows the candidate (when agent isn't given) and
    // produces the full SHA without relying on global rev-parse, which
    // can fail for short prefixes due to git's abbreviation minimum.
    let mut commit_sha: Option<String> = None;
    if let Some(prefix) = commit {
        let lower = prefix.to_ascii_lowercase();
        let mut matches: Vec<(Candidate, String)> = vec![];
        for cand in &candidates {
            if let Some(sha) = candidate_match_commit(&cand.0, &cand.1, &lower, base) {
                matches.push((cand.clone(), sha));
            }
        }
        match matches.len() {
            0 => bail!(
                "No pending commit matches '{}' in {}.",
                style::yellow(prefix),
                style::blue(&slug)
            ),
            1 => {
                let (cand, sha) = matches.into_iter().next().unwrap();
                candidates = vec![cand];
                commit_sha = Some(sha);
            }
            _ => bail!(
                "Commit prefix '{}' is ambiguous in {}.\n\
                 Specify agent: {} -c {}",
                style::yellow(prefix),
                style::blue(&slug),
                style::bold_cyan("cav approve <agent_id>"),
                style::yellow(prefix),
            ),
        }
    }

    // Disambiguate by current branch if checked out.
    if candidates.len() > 1 {
        let current = git::current_branch(Some(base)).unwrap_or_default();
        // First try exact agent-branch match (user is on a reviewed branch).
        let by_branch: Vec<_> = candidates
            .iter()
            .filter(|(b, _, _)| b == &current)
            .cloned()
            .collect();
        if !by_branch.is_empty() {
            candidates = by_branch;
        } else {
            // Then try base-branch match: on 'main' with only one agent targeting 'main'.
            let by_base: Vec<_> = candidates
                .iter()
                .filter(|(_, base_name, _)| base_name == &current)
                .cloned()
                .collect();
            if by_base.len() == 1 {
                candidates = by_base;
            }
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
            bail!(
                "No commits to approve for agent {} in {}.",
                style::bold(&resolved_agent_id),
                style::blue(&slug)
            );
        }
        if !git::is_ancestor(bs, &dev_tip, Some(base)) {
            bail!(
                "Cannot approve: {} is not a fast-forward from {}.\n\
                 The agent must rebase onto {} first.",
                style::yellow(&branch),
                style::yellow(&base_name),
                style::yellow(&base_name),
            );
        }
    }

    let after_sha = if let Some(sha) = commit_sha {
        sha
    } else {
        dev_tip.clone()
    };
    let partial = after_sha != dev_tip;

    // One exclude set for the whole branch, derived from its tip: the full
    // list, the partial list, and the approval chain all measure from the
    // same fork point, so the "remaining" count can't drift.
    let base_ref = if creating { None } else { Some(remote_base.as_str()) };
    let excludes = git::pending_excludes(base_ref, &remote_dev, Some(base))?;
    let excludes: Vec<&str> = excludes.iter().map(String::as_str).collect();
    let all_commits = git::commit_oneline(&remote_dev, Some(&excludes), Some(base))?;

    // Oldest first — the chain do_approve records, one row per commit.
    let mut approval_shas = git::rev_list(&after_sha, &excludes, Some(base))?;
    approval_shas.reverse();

    if partial {
        let commits = git::commit_oneline(&after_sha, Some(&excludes), Some(base))?;
        let remaining = all_commits.len() - commits.len();
        let n = commits.len();
        let total = all_commits.len();
        let word = if total == 1 { "commit" } else { "commits" };
        println!(
            "\nApproving {} of {} {word} from {} into {}/{}:\n",
            style::bold(&n.to_string()),
            style::bold(&total.to_string()),
            style::agent_name(&name),
            style::blue(&slug),
            style::yellow(&base_name),
        );
        for line in &commits {
            println!("  {}", style::commit_line(line));
        }
        println!(
            "\n  {}",
            style::dim(&format!("({remaining} remaining on {branch})"))
        );
    } else {
        let n = all_commits.len();
        let word = if n == 1 { "commit" } else { "commits" };
        println!(
            "\n{} {word} from {} to merge into {}/{}:\n",
            style::bold(&n.to_string()),
            style::agent_name(&name),
            style::blue(&slug),
            style::yellow(&base_name),
        );
        for line in &all_commits {
            println!("  {}", style::commit_line(line));
        }
    }

    if !yes {
        print!("\nApprove? [y/N] ");
        io::stdout().flush()?;
        let mut answer = String::new();
        io::stdin().read_line(&mut answer).unwrap_or(0);
        let answer = answer.trim().to_lowercase();
        if answer != "y" && answer != "yes" {
            bail!("Aborted.");
        }
    }

    let notes = do_approve(
        &slug,
        base,
        &branch,
        &base_name,
        &resolved_agent_id,
        before_sha.as_deref(),
        &after_sha,
        &approval_shas,
        creating,
        partial,
    )?;

    println!("Approved.");
    for note in notes {
        eprintln!("{note}");
    }
    Ok(())
}

/// Numeric agent order, then base branch: remote branches sort as text,
/// which puts agent 19 ahead of agent 2.
fn sort_candidates(candidates: &mut [Candidate]) {
    candidates.sort_by(|a, b| {
        let key = |c: &Candidate| {
            (c.2.parse::<u64>().unwrap_or(u64::MAX), c.1.clone(), c.0.clone())
        };
        key(a).cmp(&key(b))
    });
}

/// One entry per agent, in candidate order. An agent with several dev
/// branches gets a single menu entry; the branch is a second question.
fn group_by_agent(
    candidates: Vec<Candidate>,
) -> Vec<(String, Vec<Candidate>)> {
    let mut groups: Vec<(String, Vec<Candidate>)> = vec![];
    for cand in candidates {
        match groups.last_mut() {
            Some((aid, group)) if *aid == cand.2 => group.push(cand),
            _ => groups.push((cand.2.clone(), vec![cand])),
        }
    }
    groups
}

/// Pending commit subjects on a candidate's dev branch, newest first.
fn candidate_commits(
    (branch, base_name, _): &Candidate,
    base: &Path,
) -> Vec<String> {
    let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
    let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
    let has_base = git::git(
        &["rev-parse", "--verify", &remote_base],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    let base_ref = has_base.then_some(remote_base.as_str());
    git::pending_excludes(base_ref, &remote_dev, Some(base))
        .and_then(|ex| {
            let ex: Vec<&str> = ex.iter().map(String::as_str).collect();
            git::commit_oneline(&remote_dev, Some(&ex), Some(base))
        })
        .unwrap_or_default()
}

fn ask(question: &str, options: &[String]) -> Result<String> {
    print!("\n{question} [{}] ", options.join("/"));
    io::stdout().flush()?;
    let mut answer = String::new();
    match io::stdin().read_line(&mut answer) {
        Ok(0) | Err(_) => bail!("\nAborted."),
        Ok(_) => {}
    }
    Ok(answer.trim().to_string())
}

fn count(n: usize, singular: &str, plural: &str) -> String {
    let noun = if n == 1 { singular } else { plural };
    format!("{} {noun}", style::bold(&n.to_string()))
}

fn pick_candidate(
    candidates: Vec<Candidate>,
    base: &Path,
    slug: &str,
    yes: bool,
) -> Result<Candidate> {
    let groups = group_by_agent(candidates);

    if yes {
        let ids: Vec<&str> = groups.iter().map(|(aid, _)| aid.as_str()).collect();
        if let [aid] = ids.as_slice() {
            let bases: Vec<&str> =
                groups[0].1.iter().map(|(_, b, _)| b.as_str()).collect();
            bail!(
                "Agent {} has pending work on {} in {}: {}\nSpecify which: {}",
                style::bold(aid),
                count(bases.len(), "branch", "branches"),
                style::blue(slug),
                style::yellow(&bases.join(", ")),
                style::bold_cyan(&format!("cav approve {aid}/<branch_name>")),
            );
        }
        bail!(
            "Multiple choices in {}: agents {}\nSpecify which: {}",
            style::blue(slug),
            style::bold(&ids.join(", ")),
            style::bold_cyan("cav approve <agent_id>"),
        );
    }

    println!("\nPending work in {}:\n", style::blue(slug));
    for (aid, group) in &groups {
        let name = registry::display_name(base, aid);
        if let [cand] = group.as_slice() {
            let commits = candidate_commits(cand, base);
            println!(
                "  {}) {} → {}    {}",
                style::bold(aid),
                style::agent_name(&name),
                style::yellow(&cand.1),
                count(commits.len(), "commit", "commits"),
            );
            print!("{}", git::format_commit_list(&commits, "     ", Some(3)));
        } else {
            println!(
                "  {}) {}    {}",
                style::bold(aid),
                style::agent_name(&name),
                count(group.len(), "branch", "branches"),
            );
            for cand in group {
                println!(
                    "     {}    {}",
                    style::yellow(&cand.1),
                    count(candidate_commits(cand, base).len(), "commit", "commits"),
                );
            }
        }
    }

    let ids: Vec<String> = groups.iter().map(|(aid, _)| aid.clone()).collect();
    let answer = ask("Approve which agent?", &ids)?;
    let group = groups
        .into_iter()
        .find(|(aid, _)| *aid == answer)
        .map(|(_, group)| group)
        .ok_or_else(|| anyhow::anyhow!("Invalid selection."))?;

    if group.len() == 1 {
        return Ok(group.into_iter().next().unwrap());
    }
    pick_branch(group, base, &registry::display_name(base, &answer))
}

fn pick_branch(
    group: Vec<Candidate>,
    base: &Path,
    name: &str,
) -> Result<Candidate> {
    println!("\n{} has pending work on:\n", style::agent_name(name));
    for (i, cand) in group.iter().enumerate() {
        let commits = candidate_commits(cand, base);
        println!(
            "  {}) {}    {}",
            style::bold(&(i + 1).to_string()),
            style::yellow(&cand.1),
            count(commits.len(), "commit", "commits"),
        );
        print!("{}", git::format_commit_list(&commits, "     ", Some(3)));
    }

    let numbers: Vec<String> = (1..=group.len()).map(|i| i.to_string()).collect();
    let answer = ask("Approve which branch?", &numbers)?;
    let index: usize = answer
        .parse()
        .ok()
        .filter(|i| (1..=group.len()).contains(i))
        .ok_or_else(|| anyhow::anyhow!("Invalid selection."))?;
    Ok(group.into_iter().nth(index - 1).unwrap())
}

/// Returns the full SHA of the unique commit in the candidate's pending
/// range whose hex starts with `prefix`. Returns None if no commit matches.
fn candidate_match_commit(
    branch: &str,
    base_name: &str,
    prefix: &str,
    base: &Path,
) -> Option<String> {
    let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
    let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
    let has_base = git::git(
        &["rev-parse", "--verify", &remote_base],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();
    let base_ref = has_base.then_some(remote_base.as_str());
    let excludes = git::pending_excludes(base_ref, &remote_dev, Some(base)).ok()?;
    let excludes: Vec<&str> = excludes.iter().map(String::as_str).collect();
    let shas = git::rev_list(&remote_dev, &excludes, Some(base)).ok()?;
    shas.into_iter().find(|sha| sha.starts_with(prefix))
}

fn do_approve(
    slug: &str,
    project_base: &Path,
    branch: &str,
    base_name: &str,
    agent_id: &str,
    before_sha: Option<&str>,
    after_sha: &str,
    approval_shas: &[String],
    creating: bool,
    partial: bool,
) -> Result<Vec<String>> {
    let verbose = config::verbose() >= 1;

    // Sync local base branch first. If the working tree is dirty in a
    // way that blocks the ff-merge, git's own message ('Please commit
    // your changes or stash them before you merge') is surfaced and
    // approve aborts before touching the cave or the registry.
    let current = git::current_branch(Some(project_base)).unwrap_or_default();
    if creating {
        git::git(
            &["checkout", "-B", base_name, after_sha],
            Some(project_base),
            false,
            true,
            false,
        )?;
    } else if current == base_name {
        git::git(
            &["merge", "--ff-only", after_sha],
            Some(project_base),
            false,
            true,
            false,
        )?;
    } else {
        git::git(
            &["branch", "-f", base_name, after_sha],
            Some(project_base),
            false,
            true,
            false,
        )?;
        git::git(
            &["checkout", base_name],
            Some(project_base),
            false,
            true,
            false,
        )?;
    }

    let (_, notes) = push::push_base(
        project_base,
        base_name,
        Some(&format!("{after_sha}:refs/heads/{base_name}")),
        Some(agent_id),
        false,
        verbose,
    )?;

    // Record one approval per commit
    let null_sha = "0".repeat(40);
    let mut prev = before_sha.unwrap_or(&null_sha).to_string();
    for sha in approval_shas {
        registry::record_approval(slug, base_name, agent_id, &prev, sha)?;
        prev = sha.to_string();
    }

    if !partial {
        // Delete local copy in the user's working repo.
        git::git(
            &["branch", "-D", branch],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
        // Delete from the shared bare repo so the branch doesn't linger as a
        // phantom (no pending commits, invisible in status, confusing in review).
        // The agent's next push recreates it automatically.
        let shared = shared_repo_path(project_base);
        git::git(
            &["branch", "-D", branch],
            Some(&shared),
            false,
            true,
            false,
        )
        .ok();
        // Prune the host's remote-tracking ref so `cave/<dev>` doesn't linger.
        git::git(
            &["fetch", "--prune", CAVE_SHARED_REMOTE],
            Some(project_base),
            false,
            true,
            false,
        )
        .ok();
    }
    Ok(notes)
}


#[cfg(test)]
mod tests {
    use super::*;

    fn cand(aid: &str, base_name: &str) -> Candidate {
        (
            format!("agent-{aid}/{base_name}"),
            base_name.to_string(),
            aid.to_string(),
        )
    }

    #[test]
    fn sorts_agents_numerically() {
        let mut c = vec![cand("18", "develop"), cand("19", "main"), cand("2", "main")];
        sort_candidates(&mut c);
        let ids: Vec<&str> = c.iter().map(|(_, _, aid)| aid.as_str()).collect();
        assert_eq!(ids, ["2", "18", "19"]);
    }

    #[test]
    fn an_agent_gets_one_menu_entry_however_many_branches_it_has() {
        let c = vec![
            cand("18", "develop"),
            cand("18", "worktree-div"),
            cand("19", "main"),
        ];
        let groups = group_by_agent(c);
        let ids: Vec<&str> = groups.iter().map(|(aid, _)| aid.as_str()).collect();
        assert_eq!(ids, ["18", "19"]);
        assert_eq!(groups[0].1.len(), 2);
        assert_eq!(groups[1].1.len(), 1);
    }
}
