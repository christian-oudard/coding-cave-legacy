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
    let canon = registry::canonical_slug(slug)?;
    let slug = canon.as_str();
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
        let store = workspace::cave_store_root_for(aid);
        if store.exists() {
            workspace::force_remove_dir_all(&store);
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

    // The per-project worktrees dir is now empty; drop it too.
    let _ = std::fs::remove_dir(config::worktree_slug_dir(slug));

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

/// `cav rename-project <old-slug> <new-path>`. The project directory has
/// already been moved or renamed on disk; cav only reconciles its records.
/// Resolves the new location (a bare name is taken as an in-place rename
/// next to the old registered path), then cascades the slug change through
/// the registry and renames cav's worktree + session log dirs to match.
/// Never touches the source tree. Refuses if any agent is running or if the
/// new slug collides with a different registered project.
pub fn rename(old_slug: &str, new_arg: &str, yes: bool) -> Result<()> {
    let canon = registry::canonical_slug(old_slug)?;
    let old_slug = canon.as_str();
    let registered_path = registry::project_path(old_slug)?
        .ok_or_else(|| anyhow::anyhow!("No project \"{}\" in registry.", old_slug))?;
    let old_path = PathBuf::from(&registered_path);

    let new_abs = if new_arg.contains('/') || Path::new(new_arg).is_absolute() {
        let p = PathBuf::from(new_arg);
        if p.is_absolute() {
            p
        } else {
            std::env::current_dir()?.join(p)
        }
    } else {
        // A bare name renames in place: same parent, new basename.
        match old_path.parent() {
            Some(parent) => parent.join(new_arg),
            None => PathBuf::from(new_arg),
        }
    };

    if !new_abs.is_dir() {
        if old_path.is_dir() {
            bail!(
                "Directory not moved yet. Move it on disk first, then rerun:\n\
                 \x20 mv {} {}\n\
                 \x20 cav rename-project {} {}",
                style::dim(&old_path.display().to_string()),
                style::dim(&new_abs.display().to_string()),
                old_slug,
                new_arg,
            );
        }
        bail!(
            "Project directory not found at {}.\n\
             Neither the old nor the new path exists on disk.",
            style::yellow(&new_abs.display().to_string()),
        );
    }
    let new_abs = new_abs.canonicalize()?;
    if !config::cav_dir(&new_abs).is_dir() {
        bail!(
            "{} has no .coding-cave/ metadata; it is not the moved project.",
            style::yellow(&new_abs.display().to_string()),
        );
    }

    let agent_ids = registry::all_agent_ids(Some(old_slug))?;
    let running: Vec<String> = agent_ids
        .iter()
        .filter(|a| registry::is_agent_running(a).unwrap_or(false))
        .cloned()
        .collect();
    if !running.is_empty() {
        bail!(
            "Project \"{}\" has running agents: {}\nStop them first.",
            style::blue(old_slug),
            running
                .iter()
                .map(|a| style::bold(a))
                .collect::<Vec<_>>()
                .join(", "),
        );
    }

    let new_slug = config::project_slug(&new_abs);
    // If the new slug is already registered to this same directory (e.g. cav
    // init was run in the moved dir), fold old_slug's history into it rather
    // than renaming the row: its agents, worktrees, and logs join whatever is
    // already there. A different path is a genuine collision.
    let mut fold = false;
    let mut dup_agents = 0;
    if new_slug != old_slug {
        if let Some(p) = registry::project_path(&new_slug)? {
            let same_dir = PathBuf::from(&p).canonicalize().ok().as_deref()
                == Some(new_abs.as_path());
            if !same_dir {
                bail!(
                    "Slug \"{}\" is already registered to {}.\n\
                     Pick a different directory name.",
                    style::blue(&new_slug),
                    style::dim(&p),
                );
            }
            fold = true;
            dup_agents = registry::all_agent_ids(Some(&new_slug))?.len();
        }
    }

    // A plain rename (destination not registered) is frictionless. Merging or
    // adopting an already-registered destination asks first, unless -y given.
    if fold {
        print_merge_summary(old_slug, &new_slug, &new_abs, dup_agents, &agent_ids);
        if !yes {
            print!("Proceed? [y/N] ");
            io::stdout().flush()?;
            let mut answer = String::new();
            io::stdin().read_line(&mut answer).unwrap_or(0);
            let answer = answer.trim().to_lowercase();
            if answer != "y" && answer != "yes" {
                bail!("Aborted.");
            }
        }
    }

    if new_slug != old_slug {
        let data = config::data_dir();
        merge_dir(
            &data.join("worktrees").join(old_slug),
            &data.join("worktrees").join(&new_slug),
        )
        .map_err(|e| anyhow::anyhow!("Failed to move worktrees: {e}"))?;
        let _ = merge_dir(
            &config::claude_project_dir(old_slug, None),
            &config::claude_project_dir(&new_slug, None),
        );
    }

    if fold {
        registry::merge_project(old_slug, &new_slug, &new_abs)?;
        let total = agent_ids.len() + dup_agents;
        println!(
            "Merged {} into {} ({} {} now under {}).",
            style::blue(old_slug),
            style::blue(&new_slug),
            style::bold(&total.to_string()),
            if total == 1 { "agent" } else { "agents" },
            style::blue(&new_slug),
        );
    } else {
        registry::rename_project(old_slug, &new_slug, &new_abs)?;
        println!(
            "Renamed project: {} → {}{}",
            style::blue(old_slug),
            style::blue(&new_slug),
            if new_slug == old_slug {
                format!(" {}", style::dim("(slug unchanged, path updated only)"))
            } else {
                String::new()
            },
        );
    }
    Ok(())
}

/// Summary shown before a merge/adopt into an already-registered destination.
/// Only reached when the new slug differs from the old and already exists.
fn print_merge_summary(
    old_slug: &str,
    new_slug: &str,
    new_path: &Path,
    dup_agents: usize,
    agent_ids: &[String],
) {
    if dup_agents == 0 {
        println!(
            "Adopting the empty \"{}\" project at {}:",
            style::blue(new_slug),
            style::dim(&new_path.display().to_string()),
        );
    } else {
        println!(
            "Merging \"{}\" into \"{}\" at {}:",
            style::blue(old_slug),
            style::blue(new_slug),
            style::dim(&new_path.display().to_string()),
        );
        println!(
            "  - {} already has {} {} here",
            style::blue(new_slug),
            style::bold(&dup_agents.to_string()),
            if dup_agents == 1 { "agent" } else { "agents" },
        );
    }
    let agent_word = if agent_ids.len() == 1 { "agent" } else { "agents" };
    println!(
        "  - Move {} {} (worktrees + session logs) from {} into {}",
        style::bold(&agent_ids.len().to_string()),
        agent_word,
        style::blue(old_slug),
        style::blue(new_slug),
    );
}

/// Move `old` into `new`. If `new` does not exist, renames wholesale; if it
/// does, moves each child not already present (worktree and session dirs are
/// uuid-keyed, so they don't collide), then drops the emptied `old`.
fn merge_dir(old: &Path, new: &Path) -> std::io::Result<()> {
    if !old.exists() {
        return Ok(());
    }
    if !new.exists() {
        return std::fs::rename(old, new);
    }
    for entry in std::fs::read_dir(old)? {
        let entry = entry?;
        let dest = new.join(entry.file_name());
        if !dest.exists() {
            std::fs::rename(entry.path(), &dest)?;
        }
    }
    let _ = std::fs::remove_dir(old);
    Ok(())
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
        let store = workspace::cave_store_root_for(aid);
        if store.exists() {
            owned_disk += gc::dir_size(&store);
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
