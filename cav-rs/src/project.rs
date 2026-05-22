//! Project-level operations: remove (nuclear, with confirm) and (later)
//! rename. Symmetric to status::remove_agent but operates across every
//! agent owned by the project.

use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config;
use crate::gc;
use crate::git;
use crate::registry;
use crate::style;
use crate::workspace;

/// `cav remove-project <slug>`. Drops the project's `.coding-cave/`, every
/// owned agent's worktree dir, nix overlay, and bare-repo refs, plus the
/// agent and project registry rows. Refuses if any agent is running.
/// Confirms `[y/N]` first unless `yes` is set. Source files and session
/// logs are NOT touched.
pub fn remove(slug: &str, yes: bool) -> Result<()> {
    let path = registry::project_path(slug)?
        .ok_or_else(|| anyhow::anyhow!("No project \"{}\" in registry.", slug))?;
    let base = PathBuf::from(&path);

    let agent_ids = registry::all_agent_ids(Some(slug))?;

    let running: Vec<String> = agent_ids
        .iter()
        .filter(|a| registry::is_agent_running(a).unwrap_or(false))
        .cloned()
        .collect();
    if !running.is_empty() {
        bail!(
            "Project \"{}\" has running agents: {}\nStop them first.",
            style::blue(slug),
            running
                .iter()
                .map(|a| style::bold(a))
                .collect::<Vec<_>>()
                .join(", "),
        );
    }

    let (owned, guests) = partition_agents(slug, &agent_ids);

    print_summary(slug, &base, &owned, &guests);

    if !yes {
        print!("Remove? [y/N] ");
        io::stdout().flush()?;
        let mut answer = String::new();
        io::stdin().read_line(&mut answer).unwrap_or(0);
        let answer = answer.trim().to_lowercase();
        if answer != "y" && answer != "yes" {
            bail!("Aborted.");
        }
    }

    // Owned agents: full removal across every slug they touched.
    for aid in &owned {
        let slugs = registry::agent_all_slugs(aid).unwrap_or_default();
        for s in &slugs {
            if let Ok(Some(p)) = registry::project_path(s) {
                let pb = PathBuf::from(&p);
                workspace::wipe_agent_refs(&pb, aid);
                wipe_host_local_branches(&pb, aid);
            }
            let ws = workspace::worktree_for(s, aid);
            if ws.exists() {
                workspace::force_remove_dir_all(&ws);
            }
        }
        let overlay = workspace::nix_overlay_base_for(&config::persistent_home(), aid);
        if overlay.exists() {
            workspace::force_remove_dir_all(&overlay);
        }
    }

    // Guests: only the slug-being-removed-side stuff. Agent row stays.
    for aid in &guests {
        workspace::wipe_agent_refs(&base, aid);
        wipe_host_local_branches(&base, aid);
        let ws = workspace::worktree_for(slug, aid);
        if ws.exists() {
            workspace::force_remove_dir_all(&ws);
        }
        prune_extra_from_agent(aid, slug)?;
    }

    // Drop the `.coding-cave/` metadata dir (bare repo + hooks). The rest
    // of the source tree is left alone.
    let cav_meta = config::cav_dir(&base);
    if cav_meta.exists() {
        workspace::force_remove_dir_all(&cav_meta);
    }

    // Finally, drop the registry rows. registry::remove_project deletes
    // every agent row whose primary == slug + the project row itself.
    registry::remove_project(slug)?;

    let total = owned.len() + guests.len();
    let noun = if total == 1 { "agent" } else { "agents" };
    println!(
        "Removed project \"{}\" ({} {noun}).",
        style::blue(slug),
        style::bold(&total.to_string()),
    );
    Ok(())
}

/// `cav rename-project <new-path>`. Moves the project's source dir to
/// the new path; if the new dir name slugs differently, cascades the
/// slug change through the registry and renames worktree + session log
/// dirs to match. Refuses if any agent in the project is running.
pub fn rename(old_base: &Path, new_path: &Path, yes: bool) -> Result<()> {
    let old_slug = config::project_slug(old_base);
    let registered_path = registry::project_path(&old_slug)?
        .ok_or_else(|| anyhow::anyhow!("No project \"{}\" in registry.", old_slug))?;
    let registered_pathbuf = PathBuf::from(&registered_path);
    if registered_pathbuf != old_base {
        bail!(
            "Project path mismatch: cwd resolves to {} but registry has {}.\n\
             Use cav from the project root.",
            old_base.display(),
            registered_pathbuf.display(),
        );
    }

    let new_abs = if new_path.is_absolute() {
        new_path.to_path_buf()
    } else {
        std::env::current_dir()?.join(new_path)
    };

    if new_abs.exists() {
        bail!(
            "Destination already exists: {}",
            style::yellow(&new_abs.display().to_string()),
        );
    }
    if let Some(parent) = new_abs.parent() {
        if !parent.is_dir() {
            bail!(
                "Destination parent does not exist: {}",
                style::yellow(&parent.display().to_string()),
            );
        }
    }

    let agent_ids = registry::all_agent_ids(Some(&old_slug))?;
    let running: Vec<String> = agent_ids
        .iter()
        .filter(|a| registry::is_agent_running(a).unwrap_or(false))
        .cloned()
        .collect();
    if !running.is_empty() {
        bail!(
            "Project \"{}\" has running agents: {}\nStop them first.",
            style::blue(&old_slug),
            running
                .iter()
                .map(|a| style::bold(a))
                .collect::<Vec<_>>()
                .join(", "),
        );
    }

    let new_slug = config::project_slug(&new_abs);
    print_rename_summary(&old_slug, old_base, &new_slug, &new_abs, &agent_ids);

    if !yes {
        print!("Rename? [y/N] ");
        io::stdout().flush()?;
        let mut answer = String::new();
        io::stdin().read_line(&mut answer).unwrap_or(0);
        let answer = answer.trim().to_lowercase();
        if answer != "y" && answer != "yes" {
            bail!("Aborted.");
        }
    }

    // Finish any pending display-id → uuid worktree migration before we
    // move the tree, otherwise legacy display-id-keyed dirs would survive
    // the rename in their old location.
    workspace::settle_uuid_keying(&config::persistent_home());

    // Source dir first. If this fails (cross-fs etc.) we bail before
    // touching anything else.
    std::fs::rename(old_base, &new_abs).map_err(|e| {
        anyhow::anyhow!(
            "Failed to move {} → {}: {e}",
            old_base.display(),
            new_abs.display()
        )
    })?;

    if new_slug != old_slug {
        let data = config::data_dir();
        let worktrees_old = data.join("worktrees").join(&old_slug);
        let worktrees_new = data.join("worktrees").join(&new_slug);
        if worktrees_old.exists() && !worktrees_new.exists() {
            std::fs::rename(&worktrees_old, &worktrees_new).map_err(|e| {
                anyhow::anyhow!(
                    "Failed to move worktrees: {} → {}: {e}",
                    worktrees_old.display(),
                    worktrees_new.display()
                )
            })?;
        }
        let logs_old = config::claude_project_dir(&old_slug, None);
        let logs_new = config::claude_project_dir(&new_slug, None);
        if logs_old.exists() && !logs_new.exists() {
            let _ = std::fs::rename(&logs_old, &logs_new);
        }
    }

    registry::rename_project(&old_slug, &new_slug, &new_abs)?;

    println!(
        "Renamed project: {} → {}{}",
        style::blue(&old_slug),
        style::blue(&new_slug),
        if new_slug == old_slug {
            format!(" {}", style::dim("(slug unchanged, source moved only)"))
        } else {
            String::new()
        },
    );
    Ok(())
}

fn print_rename_summary(
    old_slug: &str,
    old_base: &Path,
    new_slug: &str,
    new_path: &Path,
    agent_ids: &[String],
) {
    println!(
        "Renaming project \"{}\":",
        style::blue(old_slug),
    );
    println!(
        "  - Move source: {} → {}",
        style::dim(&old_base.display().to_string()),
        style::dim(&new_path.display().to_string()),
    );
    if new_slug != old_slug {
        println!(
            "  - Slug: {} → {}",
            style::blue(old_slug),
            style::blue(new_slug),
        );
        let agent_word = if agent_ids.len() == 1 { "agent" } else { "agents" };
        println!(
            "  - Update {} {} and rename worktree/session-log dirs to match",
            style::bold(&agent_ids.len().to_string()),
            agent_word,
        );
    } else {
        println!(
            "  - Slug unchanged ({}), no registry cascade needed",
            style::blue(old_slug),
        );
    }
}

fn partition_agents(slug: &str, agent_ids: &[String]) -> (Vec<String>, Vec<String>) {
    let mut owned = Vec::new();
    let mut guests = Vec::new();
    for aid in agent_ids {
        if registry::agent_project(aid).as_deref() == Some(slug) {
            owned.push(aid.clone());
        } else {
            guests.push(aid.clone());
        }
    }
    (owned, guests)
}

fn print_summary(slug: &str, base: &Path, owned: &[String], guests: &[String]) {
    let mut owned_disk = 0u64;
    for aid in owned {
        for s in registry::agent_all_slugs(aid).unwrap_or_default() {
            let ws = workspace::worktree_for(&s, aid);
            if ws.exists() {
                owned_disk += gc::dir_size(&ws);
            }
        }
        let overlay = workspace::nix_overlay_base_for(&config::persistent_home(), aid);
        if overlay.exists() {
            owned_disk += gc::dir_size(&overlay);
        }
    }
    let mut guest_disk = 0u64;
    for aid in guests {
        let ws = workspace::worktree_for(slug, aid);
        if ws.exists() {
            guest_disk += gc::dir_size(&ws);
        }
    }
    let total_disk = owned_disk + guest_disk;

    println!(
        "Removing project \"{}\" at {} will:",
        style::blue(slug),
        style::dim(&base.display().to_string()),
    );
    if !owned.is_empty() {
        let names: Vec<String> = owned.iter().map(|a| style::bold(a)).collect();
        println!(
            "  - Drop {} owned {} ({}): {}",
            style::bold(&owned.len().to_string()),
            if owned.len() == 1 { "agent" } else { "agents" },
            style::yellow(&gc::format_size(owned_disk)),
            names.join(", "),
        );
    }
    if !guests.is_empty() {
        let names: Vec<String> = guests.iter().map(|a| style::bold(a)).collect();
        println!(
            "  - Unmount from {} guest {} ({}): {}",
            style::bold(&guests.len().to_string()),
            if guests.len() == 1 { "agent" } else { "agents" },
            style::yellow(&gc::format_size(guest_disk)),
            names.join(", "),
        );
    }
    println!(
        "  - Remove {} (cave metadata)",
        style::dim(&config::cav_dir(base).display().to_string()),
    );
    println!(
        "  - Total reclaimed: {}",
        style::bold(&gc::format_size(total_disk)),
    );
    println!();
    println!(
        "{} the source tree at {} and any session logs are left in place.",
        style::dim("Untouched:"),
        style::dim(&base.display().to_string()),
    );
}

fn wipe_host_local_branches(base: &Path, agent_id: &str) {
    let pattern = format!("refs/heads/agent-{agent_id}/");
    let Ok(refs) = git::git_output(
        &[
            "for-each-ref",
            "--format=%(refname:short)",
            &pattern,
        ],
        Some(base),
    ) else {
        return;
    };
    for br in refs.lines() {
        git::git(&["branch", "-D", br], Some(base), false, true, false).ok();
    }
}

fn prune_extra_from_agent(agent_id: &str, slug_to_drop: &str) -> Result<()> {
    let slugs = registry::agent_all_slugs(agent_id).unwrap_or_default();
    let primary = registry::agent_project(agent_id);
    let extras: Vec<String> = slugs
        .into_iter()
        .filter(|s| primary.as_deref() != Some(s) && s != slug_to_drop)
        .collect();
    registry::save_agent_extras(agent_id, &extras)
}
