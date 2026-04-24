use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;

use crate::config::{self, inside_cave, parse_dev_branch, resolve_base, worktree_dir};
use crate::constants::CAVE_SHARED_REMOTE;
use crate::git;
use crate::registry;
use crate::sandbox;

fn format_age(timestamp: f64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let delta = (now - timestamp) as u64;
    if delta < 60 {
        "just now".to_string()
    } else if delta < 3600 {
        format!("{}m ago", delta / 60)
    } else if delta < 86400 {
        format!("{}h ago", delta / 3600)
    } else {
        format!("{}d ago", delta / 86400)
    }
}

struct AgentInfo {
    last_activity: f64,
    name: String,
    running: bool,
    state: String,
    unapproved: Vec<(String, usize, Vec<String>)>, // (branch, ahead, commit_lines)
    dirty_worktrees: Vec<PathBuf>,
}

pub fn show(base: &Path, agent_id: Option<&str>) -> Result<()> {
    // Inside a cave, the cave remote may not exist yet (before cav init).
    // Make the fetch non-fatal in that case.
    let fetch_result = git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        false,
        false,
        config::verbose() >= 1,
    );
    if let Err(e) = fetch_result {
        if !inside_cave() {
            return Err(e);
        }
        // Inside cave without a cave remote (not yet cav init'd): no sub-agents.
        let slug = config::project_slug(base);
        if let Some(aid) = agent_id {
            println!("No agent {aid} in registry.");
        } else {
            println!("No running agents in {slug}.");
        }
        return Ok(());
    }

    if agent_id.is_none() {
        show_branch_sync_status(base);
        sandbox::reap_dead_agents();
        sandbox::reconcile_merged_dev_branches(base, config::verbose() >= 1);
    }

    let slug = config::project_slug(base);
    let mut registered = registry::all_agent_ids(Some(&slug))?;
    if let Some(aid) = agent_id {
        registered.retain(|a| a == aid);
    }

    let branches = git::remote_branches(CAVE_SHARED_REMOTE, Some(base))?;
    let mut by_agent: std::collections::HashMap<String, Vec<(String, String)>> = registered
        .iter()
        .map(|aid| (aid.clone(), vec![]))
        .collect();

    for branch in &branches {
        if let Some((_, branch_agent_id)) = parse_dev_branch(branch) {
            if let Some(list) = by_agent.get_mut(&branch_agent_id) {
                let base_name = resolve_base(branch.clone());
                list.push((branch.clone(), base_name));
            }
        }
    }

    if by_agent.is_empty() {
        if let Some(aid) = agent_id {
            println!("No agent {aid} in registry.");
        } else {
            println!("No running agents in {slug}.");
        }
        return Ok(());
    }

    let verbose_mode = config::verbose() >= 1 || agent_id.is_some();
    let max_commits = if verbose_mode { None } else { Some(3usize) };

    let mut agents: Vec<AgentInfo> = vec![];

    for (aid, branches_list) in &by_agent {
        let name = registry::display_name(base, aid);
        let running = registry::is_agent_running(aid)?;

        let mut unapproved: Vec<(String, usize, Vec<String>)> = vec![];
        let mut last_activity = 0.0f64;

        for (branch, base_name) in branches_list {
            let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
            let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
            if let Some(ts) = git::commit_timestamp(&remote_dev, Some(base)) {
                if ts > last_activity {
                    last_activity = ts;
                }
            }
            if git::has_merge_base(&remote_dev, &remote_base, Some(base)) {
                if let Ok(lines) = git::cherry_undelivered(&remote_base, &remote_dev, Some(base)) {
                    if !lines.is_empty() {
                        unapproved.push((branch.clone(), lines.len(), lines));
                    }
                }
            }
        }

        if last_activity == 0.0 {
            if let Ok(Some(exit_t)) = registry::agent_exit_time(aid) {
                last_activity = exit_t;
            }
        }

        let total_ahead: usize = unapproved.iter().map(|(_, n, _)| n).sum();

        let mut dirty_worktrees: Vec<PathBuf> = vec![];
        for slug_s in registry::agent_all_slugs(aid)? {
            let ws = worktree_dir(&slug_s, aid);
            if !ws.is_dir() {
                continue;
            }
            let st = git::git(
                &["status", "--porcelain"],
                Some(&ws),
                false,
                true,
                false,
            ).unwrap_or_default();
            if !st.trim().is_empty() {
                dirty_worktrees.push(ws);
            }
        }

        let state = if running { "running" } else { "stopped" }.to_string();

        if !running && total_ahead == 0 && dirty_worktrees.is_empty() {
            continue;
        }

        agents.push(AgentInfo {
            last_activity,
            name,
            running,
            state,
            unapproved,
            dirty_worktrees,
        });
    }

    if agents.is_empty() {
        if let Some(aid) = agent_id {
            println!("No agent {aid} in registry.");
        } else {
            println!("No running agents in {slug}.");
        }
        return Ok(());
    }

    // Sort: running first, then stopped; within each group most recent first
    agents.sort_by(|a, b| {
        let order_a = if a.running { 0 } else { 1 };
        let order_b = if b.running { 0 } else { 1 };
        order_a
            .cmp(&order_b)
            .then(b.last_activity.partial_cmp(&a.last_activity).unwrap_or(std::cmp::Ordering::Equal))
    });

    print_agents(&agents, max_commits);
    Ok(())
}

fn show_branch_sync_status(base: &Path) {
    let local_branch = match git::current_branch(Some(base)) {
        Ok(b) => b,
        Err(_) => return,
    };
    let remote_ref = format!("{CAVE_SHARED_REMOTE}/{local_branch}");
    let local_sha = git::git_output(&["rev-parse", &local_branch], Some(base)).ok();
    let remote_sha = git::git_output(&["rev-parse", &remote_ref], Some(base)).ok();

    if let (Some(local), Some(remote)) = (local_sha, remote_sha) {
        if local != remote {
            let behind = git::commits_ahead(&remote_ref, &local_branch, None, Some(base))
                .unwrap_or(0);
            let local_ahead =
                git::commits_ahead(&local_branch, &remote_ref, None, Some(base)).unwrap_or(0);
            if behind > 0 && local_ahead > 0 {
                let nb = if behind == 1 { "commit" } else { "commits" };
                let na = if local_ahead == 1 { "commit" } else { "commits" };
                println!(
                    "{local_branch} and {remote_ref} have diverged,\n\
                     and have {local_ahead} and {behind} different {na} and {nb} each, respectively.\n"
                );
            } else if behind > 0 {
                let n = if behind == 1 { "commit" } else { "commits" };
                println!(
                    "{local_branch} is behind {remote_ref} by {behind} {n}, and can be fast-forwarded.\n"
                );
            } else if local_ahead > 0 {
                let n = if local_ahead == 1 { "commit" } else { "commits" };
                println!("{local_branch} is ahead of {remote_ref} by {local_ahead} {n}.\n");
            }
        }
    }
}

fn print_agents(agents: &[AgentInfo], max_commits: Option<usize>) {
    for (i, agent) in agents.iter().enumerate() {
        if i > 0 {
            println!();
        }
        let age = if agent.last_activity > 0.0 {
            format!("  {}", format_age(agent.last_activity))
        } else {
            String::new()
        };
        println!("{}  ({}){}", agent.name, agent.state, age);
        for (branch, ahead, commit_lines) in &agent.unapproved {
            let n = if *ahead == 1 { "commit" } else { "commits" };
            println!("  {ahead} {n} on {branch}");
            let show: &[String] = match max_commits {
                None => commit_lines,
                Some(m) => &commit_lines[..m.min(commit_lines.len())],
            };
            for line in show {
                println!("    {line}");
            }
            if let Some(m) = max_commits {
                if *ahead > m {
                    println!("    ... and {} more", ahead - m);
                }
            }
        }
        for ws in &agent.dirty_worktrees {
            println!("  uncommitted changes in {}", ws.display());
        }
    }
}

pub fn show_all(projects: Vec<(String, String)>) -> Result<()> {
    sandbox::reap_dead_agents();

    let mut entries: Vec<(f64, String, String)> = vec![];
    let mut problems: Vec<(String, String)> = vec![];

    for (slug, path) in &projects {
        let base = PathBuf::from(path);
        if !base.is_dir() {
            problems.push((
                slug.clone(),
                format!(
                    "registered at {path} but the directory is missing.\n\
                     Remove with: cav remove-project {slug}"
                ),
            ));
            continue;
        }
        let shared = config::shared_repo_path(&base);
        if !shared.exists() {
            problems.push((
                slug.clone(),
                format!(
                    "registered at {path} but {} is missing.\n\
                     Restore with: cav init {path}",
                    shared.display()
                ),
            ));
            continue;
        }
        match capture_status_output(&base) {
            Ok((timestamp, text)) => entries.push((timestamp, slug.clone(), text)),
            Err(e) => problems.push((slug.clone(), e.to_string())),
        }
    }

    // Print all collected output
    let active: Vec<_> = entries.iter().filter(|(ts, _, _)| *ts > 0.0).collect();
    let inactive: Vec<_> = entries
        .iter()
        .filter(|(ts, _, _)| *ts == 0.0)
        .map(|(_, s, _)| s.as_str())
        .collect();

    if active.is_empty() && problems.is_empty() && inactive.is_empty() {
        println!("No running agents in any project.");
        return Ok(());
    }

    let mut sorted_active = active.clone();
    sorted_active.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    for (_, slug, output) in &sorted_active {
        println!("=== {slug} ===");
        print!("{output}");
    }
    for (slug, message) in &problems {
        println!("=== {slug} ===");
        println!("{message}");
    }
    if !inactive.is_empty() {
        let mut sorted_inactive: Vec<&str> = inactive.clone();
        sorted_inactive.sort();
        println!("No running agents in: {}", sorted_inactive.join(", "));
    }
    Ok(())
}

fn capture_status_output(base: &Path) -> Result<(f64, String)> {
    // This fetches and builds status for the given project, returning
    // the last activity timestamp and formatted text.
    let _ = git::git(
        &["fetch", "--prune", CAVE_SHARED_REMOTE],
        Some(base),
        false,
        true,
        false,
    );
    sandbox::reconcile_merged_dev_branches(base, config::verbose() >= 1);

    let slug = config::project_slug(base);
    let registered = registry::all_agent_ids(Some(&slug))?;
    let branches = git::remote_branches(CAVE_SHARED_REMOTE, Some(base))?;
    let mut by_agent: std::collections::HashMap<String, Vec<(String, String)>> = registered
        .iter()
        .map(|aid| (aid.clone(), vec![]))
        .collect();
    for branch in &branches {
        if let Some((_, branch_agent_id)) = parse_dev_branch(branch) {
            if let Some(list) = by_agent.get_mut(&branch_agent_id) {
                let base_name = resolve_base(branch.clone());
                list.push((branch.clone(), base_name));
            }
        }
    }

    let mut agents: Vec<AgentInfo> = vec![];
    for (aid, branches_list) in &by_agent {
        let name = registry::display_name(base, aid);
        let running = registry::is_agent_running(aid)?;
        let mut unapproved = vec![];
        let mut last_activity = 0.0f64;
        for (branch, base_name) in branches_list {
            let remote_dev = format!("{CAVE_SHARED_REMOTE}/{branch}");
            let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
            if let Some(ts) = git::commit_timestamp(&remote_dev, Some(base)) {
                if ts > last_activity {
                    last_activity = ts;
                }
            }
            if git::has_merge_base(&remote_dev, &remote_base, Some(base)) {
                if let Ok(lines) = git::cherry_undelivered(&remote_base, &remote_dev, Some(base)) {
                    if !lines.is_empty() {
                        unapproved.push((branch.clone(), lines.len(), lines));
                    }
                }
            }
        }
        if last_activity == 0.0 {
            if let Ok(Some(exit_t)) = registry::agent_exit_time(aid) {
                last_activity = exit_t;
            }
        }
        let total_ahead: usize = unapproved.iter().map(|(_, n, _)| n).sum();
        let mut dirty_worktrees = vec![];
        for slug_s in registry::agent_all_slugs(aid)? {
            let ws = worktree_dir(&slug_s, aid);
            if ws.is_dir() {
                let st = git::git(
                    &["status", "--porcelain"],
                    Some(&ws),
                    false,
                    true,
                    false,
                ).unwrap_or_default();
                if !st.trim().is_empty() {
                    dirty_worktrees.push(ws);
                }
            }
        }
        let state = if running { "running" } else { "stopped" }.to_string();
        if !running && total_ahead == 0 && dirty_worktrees.is_empty() {
            continue;
        }
        agents.push(AgentInfo {
            last_activity,
            name,
            running,
            state,
            unapproved,
            dirty_worktrees,
        });
    }

    agents.sort_by(|a, b| {
        let order_a = if a.running { 0 } else { 1 };
        let order_b = if b.running { 0 } else { 1 };
        order_a
            .cmp(&order_b)
            .then(b.last_activity.partial_cmp(&a.last_activity).unwrap_or(std::cmp::Ordering::Equal))
    });

    let ts = agents.first().map(|a| a.last_activity).unwrap_or(0.0);
    let mut text = String::new();
    for (i, agent) in agents.iter().enumerate() {
        if i > 0 {
            text.push('\n');
        }
        let age = if agent.last_activity > 0.0 {
            format!("  {}", format_age(agent.last_activity))
        } else {
            String::new()
        };
        text.push_str(&format!("{}  ({}){}\n", agent.name, agent.state, age));
        for (branch, ahead, commit_lines) in &agent.unapproved {
            let n = if *ahead == 1 { "commit" } else { "commits" };
            text.push_str(&format!("  {ahead} {n} on {branch}\n"));
            for line in commit_lines.iter().take(3) {
                text.push_str(&format!("    {line}\n"));
            }
            if *ahead > 3 {
                text.push_str(&format!("    ... and {} more\n", ahead - 3));
            }
        }
        for ws in &agent.dirty_worktrees {
            text.push_str(&format!("  uncommitted changes in {}\n", ws.display()));
        }
    }
    Ok((ts, text))
}

pub fn remove_agent(base: &Path, agent_id: &str) -> Result<()> {
    let branches = git::remote_branches(CAVE_SHARED_REMOTE, Some(base))?;
    let agent_branches: Vec<String> = branches
        .into_iter()
        .filter(|b| {
            parse_dev_branch(b)
                .map(|(_, aid)| aid == agent_id)
                .unwrap_or(false)
        })
        .collect();

    for b in &agent_branches {
        let parsed = parse_dev_branch(b);
        if parsed.is_none() {
            continue;
        }
        let base_name = config::resolve_base(b.clone());
        let remote_dev = format!("{CAVE_SHARED_REMOTE}/{b}");
        let base_ref = format!("{CAVE_SHARED_REMOTE}/{base_name}");
        if git::has_merge_base(&remote_dev, &base_ref, Some(base)) {
            if let Ok(commits) = git::cherry_undelivered(&base_ref, &remote_dev, Some(base)) {
                if !commits.is_empty() {
                    let n = if commits.len() == 1 { "commit" } else { "commits" };
                    println!(
                        "Warning: discarding {} unapproved {n} on {b}:",
                        commits.len()
                    );
                    if let Ok(lines) = git::commit_oneline(&format!("{base_ref}..{remote_dev}"), None, Some(base)) {
                        for line in &lines {
                            println!("  {line}");
                        }
                    }
                }
            }
        }
    }

    _remove_agent(base, agent_id, &agent_branches)?;
    println!("Removed {}.", registry::display_name(base, agent_id));
    Ok(())
}

pub fn _remove_agent(base: &Path, agent_id: &str, branches: &[String]) -> Result<()> {
    for branch in branches {
        git::git(
            &["push", CAVE_SHARED_REMOTE, &format!(":refs/heads/{branch}")],
            Some(base),
            false,
            true,
            false,
        )
        .ok();
        git::git(&["branch", "-D", branch], Some(base), false, true, false).ok();
    }

    let primary_slug = config::project_slug(base);
    let slugs = registry::agent_all_slugs(agent_id)?;
    for slug in &slugs {
        let ws = config::worktree_dir(slug, agent_id);
        if ws.exists() {
            std::fs::remove_dir_all(&ws).ok();
        }
        if slug == &primary_slug {
            continue;
        }
        if let Ok(Some(path)) = registry::project_path(slug) {
            let sec_base = PathBuf::from(path);
            if let Ok(sec_branches) = git::remote_branches(CAVE_SHARED_REMOTE, Some(&sec_base)) {
                for b in sec_branches {
                    if let Some((_, aid)) = parse_dev_branch(&b) {
                        if aid == agent_id {
                            git::git(
                                &["push", CAVE_SHARED_REMOTE, &format!(":refs/heads/{b}")],
                                Some(&sec_base),
                                false,
                                true,
                                false,
                            )
                            .ok();
                            git::git(
                                &["branch", "-D", &b],
                                Some(&sec_base),
                                false,
                                true,
                                false,
                            )
                            .ok();
                        }
                    }
                }
            }
        }
    }
    registry::remove_agent(agent_id)?;
    Ok(())
}
