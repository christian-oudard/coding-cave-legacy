use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::{self, dev_branch, is_agent_id, parse_dev_branch, resolve_base};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR};
use crate::git;
use crate::registry;

/// Resolve where an agent's worktree currently lives. Prefers the
/// canonical (uuid-keyed) path; falls back to the legacy (display-id-
/// keyed) location if a pre-migration dir is still around. Pure: no
/// filesystem side effects, safe for dry-run paths.
pub fn worktree_for(slug: &str, agent_id: &str) -> PathBuf {
    let Some(uuid) = registry::agent_uuid(agent_id).ok().flatten() else {
        return config::worktree_dir(slug, agent_id);
    };
    let canonical = config::worktree_dir(slug, &uuid);
    if canonical.exists() {
        return canonical;
    }
    let legacy = config::worktree_dir(slug, agent_id);
    if legacy != canonical && legacy.exists() {
        return legacy;
    }
    canonical
}

/// Same shape as `worktree_for` for the per-agent nix overlay base
/// (`<home>/.local/share/coding-cave/nix-overlay/agent-<key>`). Pure.
pub fn nix_overlay_base_for(home: &Path, agent_id: &str) -> PathBuf {
    let nix_root = home
        .join(".local")
        .join("share")
        .join("coding-cave")
        .join("nix-overlay");
    let Some(uuid) = registry::agent_uuid(agent_id).ok().flatten() else {
        return nix_root.join(format!("agent-{agent_id}"));
    };
    let canonical = nix_root.join(format!("agent-{uuid}"));
    if canonical.exists() {
        return canonical;
    }
    let legacy = nix_root.join(format!("agent-{agent_id}"));
    if legacy != canonical && legacy.exists() {
        return legacy;
    }
    canonical
}

/// First display int reserved for archived (bumped) agents. Active
/// agents allocate from 1.. up to but not including this boundary; once
/// they hit it, the next free int continues from here for archives.
pub const ARCHIVE_INT_BASE: u32 = 1000;

/// Default age after which a Paused agent is eligible to be bumped into
/// the archived range. Matches the default `cav gc --older-than` so the
/// two operations stay in step: by the time an agent's artifacts can be
/// reclaimed, its display int has rotated out of the active range.
pub const ARCHIVE_AGE_SECS: f64 = 24.0 * 3600.0;

/// Drop every `refs/heads/agent-<old_id>/*` and
/// `refs/cav/agent-tip/agent-<old_id>/*` in the project's bare repo.
/// Called from archive bumping (where the agent gets a new display int
/// and shouldn't keep the old namespace) and from cleanup paths.
pub fn wipe_agent_refs(base: &Path, agent_id: &str) {
    let shared = config::shared_repo_path(base);
    if !shared.is_dir() {
        return;
    }
    let prefix = format!("agent-{agent_id}/");
    for area in ["refs/heads", "refs/cav/agent-tip"] {
        let pattern = format!("{area}/{prefix}");
        let Ok(refs) = git::git_output(
            &["for-each-ref", "--format=%(refname)", &pattern],
            Some(&shared),
        ) else {
            continue;
        };
        for refname in refs.lines() {
            git::git(
                &["update-ref", "-d", refname],
                Some(&shared),
                false,
                true,
                false,
            )
            .ok();
        }
    }
}

/// Scan every paused agent and bump any with a display int < ARCHIVE_INT_BASE
/// that's been paused at least ARCHIVE_AGE_SECS into the archived range.
/// The agent keeps its uuid (and therefore its worktree dir + nix overlay
/// + session log), drops its old `agent-N/*` refs in the bare repo, and
/// gets its push refspec rewritten so future pushes from the surviving
/// worktree land at `agent-<new_id>/<base>`. Safe to call repeatedly;
/// each call is a no-op once everything in 1..ARCHIVE_INT_BASE that's
/// past the threshold has been bumped.
pub fn archive_sweep() {
    // Settle any pending display-id → uuid renames first. Otherwise an
    // about-to-be-bumped agent's legacy display-id-keyed dir would never
    // get migrated — settle keys on the current display int, which we're
    // about to change.
    settle_uuid_keying(&config::persistent_home());

    let Ok(agents) = registry::all_agent_ids(None) else {
        return;
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let cutoff = now - ARCHIVE_AGE_SECS;

    let used: std::collections::HashSet<u32> = agents
        .iter()
        .filter_map(|a| a.parse().ok())
        .collect();
    let mut next_archived: u32 = ARCHIVE_INT_BASE;
    let mut alloc_archived = |used: &mut std::collections::HashSet<u32>| -> u32 {
        while used.contains(&next_archived) {
            next_archived += 1;
        }
        let n = next_archived;
        used.insert(n);
        next_archived += 1;
        n
    };
    let mut used = used;

    for aid in &agents {
        let Ok(n) = aid.parse::<u32>() else { continue };
        if n == 0 || n >= ARCHIVE_INT_BASE {
            continue;
        }
        if registry::is_agent_running(aid).unwrap_or(false) {
            continue;
        }
        let exited = match registry::agent_exit_time(aid).ok().flatten() {
            Some(t) => t,
            None => continue,
        };
        if exited > cutoff {
            continue;
        }

        let new_id = alloc_archived(&mut used).to_string();
        used.remove(&n);

        // Drop old agent-<n>/* and agent-tip refs in every bare repo that
        // this agent touched. Branches are part of the interface; on
        // display int change they go away.
        let slugs = registry::agent_all_slugs(aid).unwrap_or_default();
        for slug in &slugs {
            if let Ok(Some(p)) = registry::project_path(slug) {
                wipe_agent_refs(Path::new(&p), aid);
            }
        }

        if registry::bump_agent_id(aid, &new_id).is_err() {
            continue;
        }

        eprintln!(
            "{} {} {} {}",
            crate::style::dim("Archived agent"),
            crate::style::bold(aid),
            crate::style::dim("→"),
            crate::style::bold(&new_id),
        );

        // Refresh push refspec in any surviving worktrees so future pushes
        // from the bumped agent target agent-<new_id>/<base>.
        for slug in &slugs {
            let ws = worktree_for(slug, &new_id);
            if ws.exists() {
                let _ = verify_layout(&ws, &new_id);
            }
        }
    }
}

/// Sweep every non-running agent's on-disk state, renaming display-id-
/// keyed worktrees and nix overlays into their canonical uuid-keyed
/// locations. One-shot upgrade migration that completes lazily as the
/// user runs cav after picking up the uuid column. Skips running agents
/// since renaming their dirs would yank state out from under them. Safe
/// to call repeatedly — each call is a no-op once everything's settled.
pub fn settle_uuid_keying(home: &Path) {
    let Ok(agents) = registry::all_agent_ids(None) else {
        return;
    };
    for aid in agents {
        if registry::is_agent_running(&aid).unwrap_or(false) {
            continue;
        }
        let Some(uuid) = registry::agent_uuid(&aid).ok().flatten() else {
            continue;
        };
        for slug in registry::agent_all_slugs(&aid).unwrap_or_default() {
            let canonical = config::worktree_dir(&slug, &uuid);
            let legacy = config::worktree_dir(&slug, &aid);
            if legacy != canonical && legacy.exists() && !canonical.exists() {
                if let Some(parent) = canonical.parent() {
                    let _ = std::fs::create_dir_all(parent);
                }
                let _ = std::fs::rename(&legacy, &canonical);
            }
        }
        let nix_root = home
            .join(".local")
            .join("share")
            .join("coding-cave")
            .join("nix-overlay");
        let canonical_nix = nix_root.join(format!("agent-{uuid}"));
        let legacy_nix = nix_root.join(format!("agent-{aid}"));
        if legacy_nix != canonical_nix && legacy_nix.exists() && !canonical_nix.exists() {
            let _ = std::fs::rename(&legacy_nix, &canonical_nix);
        }
    }
}

fn repo_is_empty(path: &Path) -> bool {
    git::git(&["rev-parse", "HEAD"], Some(path), false, true, false).is_err()
}

pub fn configure(ws: &Path, shared: &Path) -> Result<()> {
    let alt = ws.join(".git").join("objects").join("info").join("alternates");
    if !alt.exists() {
        return Ok(());
    }
    let mount_point = ws.join(".coding-cave").join(HOST_SHARED_REPO_DIR);
    std::fs::create_dir_all(&mount_point)?;
    let objects_link = mount_point.join("objects");
    if !objects_link.exists() && !objects_link.is_symlink() {
        std::os::unix::fs::symlink(shared.join("objects"), &objects_link)?;
    }
    std::fs::write(
        &alt,
        format!("../../.coding-cave/{HOST_SHARED_REPO_DIR}/objects\n"),
    )?;
    let exclude = ws.join(".git").join("info").join("exclude");
    let content = if exclude.exists() {
        std::fs::read_to_string(&exclude)?
    } else {
        String::new()
    };
    let lines: Vec<&str> = content.lines().collect();
    if !lines.contains(&"/.coding-cave") {
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(&exclude)?;
        use std::io::Write;
        writeln!(f, "/.coding-cave")?;
    }
    Ok(())
}

pub fn verify_layout(ws: &Path, agent_id: &str) -> Result<()> {
    let current = match git::current_branch(Some(ws)) {
        Ok(b) => b,
        Err(_) => return Ok(()),
    };
    let base_name = if let Some((base, _)) = parse_dev_branch(&current) {
        git::git(
            &["branch", "-D", &base],
            Some(ws),
            false,
            true,
            false,
        )
        .ok();
        git::git(
            &["branch", "-m", &current, &base],
            Some(ws),
            false,
            true,
            false,
        )
        .ok();
        base
    } else {
        current
    };
    set_agent_push_refspec(ws, &base_name, agent_id)?;
    Ok(())
}

pub fn reconfigure(user_repo: &Path) -> Result<()> {
    let slug = config::project_slug(user_repo);
    let worktrees = config::data_dir().join("worktrees").join(&slug);
    if !worktrees.is_dir() {
        return Ok(());
    }
    let shared = config::shared_repo_path(user_repo);
    for entry in std::fs::read_dir(&worktrees)?.flatten() {
        if entry.file_type()?.is_dir() {
            let ws = entry.path();
            let id = ws.file_name().unwrap().to_string_lossy().to_string();
            configure(&ws, &shared)?;
            verify_layout(&ws, &id)?;
        }
    }
    Ok(())
}

pub fn agent_push_refspec(base_name: &str, agent_id: &str) -> String {
    format!(
        "refs/heads/{base_name}:refs/heads/{}",
        dev_branch(base_name, agent_id)
    )
}

pub fn set_agent_push_refspec(ws: &Path, base_name: &str, agent_id: &str) -> Result<()> {
    let key = format!("remote.{HOST_SHARED_REMOTE}.push");
    git::git(&["config", "--unset-all", &key], Some(ws), false, true, false).ok();
    git::git(
        &["config", "--add", &key, &agent_push_refspec(base_name, agent_id)],
        Some(ws),
        true,
        true,
        false,
    )?;
    Ok(())
}

/// Erase on-disk state belonging to a previous occupant of an agent slot:
/// the worktree dir, plus `refs/heads/agent-N/*` and
/// `refs/cav/agent-tip/agent-N/*` in each project's bare repo. Called when
/// `acquire_agent_id` reuses a slot, so a new agent doesn't inherit a stale
/// branch, push refspec, or force-push tip from its namesake.
pub fn reclaim(agent_id: &str, targets: &[(String, PathBuf)]) {
    for (slug, base) in targets {
        let ws = worktree_for(slug, agent_id);
        if ws.exists() {
            let _ = std::fs::remove_dir_all(&ws);
        }
        let shared = config::shared_repo_path(base);
        if !shared.is_dir() {
            continue;
        }
        let prefix = format!("agent-{agent_id}/");
        for area in ["refs/heads", "refs/cav/agent-tip"] {
            let pattern = format!("{area}/{prefix}");
            let listing = match git::git_output(
                &["for-each-ref", "--format=%(refname)", &pattern],
                Some(&shared),
            ) {
                Ok(s) => s,
                Err(_) => continue,
            };
            for refname in listing.lines() {
                git::git(
                    &["update-ref", "-d", refname],
                    Some(&shared),
                    false,
                    true,
                    false,
                )
                .ok();
            }
        }
    }
}

/// Create workspace for an agent. Returns (clone_path, shared_repo).
pub fn create(base: &Path, agent_id: &str) -> Result<(PathBuf, PathBuf)> {
    if !is_agent_id(agent_id) {
        bail!("Invalid agent ID '{agent_id}'. Must be a number (e.g. 1, 2, 3).");
    }
    let slug = config::project_slug(base);
    let ws_path = worktree_for(&slug, agent_id);
    let shared = config::shared_repo_path(base);

    if ws_path.exists() {
        return Ok((ws_path, shared));
    }

    let empty = repo_is_empty(base);
    let base_name = resolve_base(git::current_branch(Some(base))?);

    if base_name == "HEAD" {
        bail!(
            "Cannot launch a new agent from detached HEAD.\n\
             Switch to a branch first (e.g. 'git checkout main')."
        );
    }

    std::fs::create_dir_all(ws_path.parent().unwrap())?;

    git::git(
        &["config", "receive.shallowUpdate", "true"],
        Some(&shared),
        false,
        true,
        false,
    )
    .ok();

    if !empty {
        let branches = git::git_output(
            &["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            Some(base),
        )?;
        for branch in branches.lines() {
            if parse_dev_branch(branch).is_some() {
                continue;
            }
            git::git(
                &["push", "--no-set-upstream", CAVE_SHARED_REMOTE, branch],
                Some(base),
                false,
                true,
                false,
            )
            .ok();
        }
    }

    git::git(
        &["clone", "--shared", &shared.to_string_lossy(), &ws_path.to_string_lossy()],
        None,
        true,
        true,
        false,
    )?;
    configure(&ws_path, &shared)?;

    git::git(
        &["remote", "rename", "origin", HOST_SHARED_REMOTE],
        Some(&ws_path),
        true,
        true,
        false,
    )?;
    set_agent_push_refspec(&ws_path, &base_name, agent_id)?;

    if empty {
        git::git(
            &["checkout", "--orphan", &base_name],
            Some(&ws_path),
            true,
            true,
            false,
        )?;
        return Ok((ws_path, shared));
    }

    let dev_name = dev_branch(&base_name, agent_id);
    let dev_ref = format!("refs/remotes/{HOST_SHARED_REMOTE}/{dev_name}");
    let base_ref = format!("refs/remotes/{HOST_SHARED_REMOTE}/{base_name}");

    let dev_exists = git::git(
        &["rev-parse", "--verify", &dev_ref],
        Some(&ws_path),
        false,
        true,
        false,
    )
    .is_ok();

    if dev_exists {
        git::git(
            &["checkout", "-B", &base_name, &dev_ref],
            Some(&ws_path),
            true,
            true,
            false,
        )?;
        git::git(
            &[
                "branch",
                "--set-upstream-to",
                &format!("{HOST_SHARED_REMOTE}/{base_name}"),
                &base_name,
            ],
            Some(&ws_path),
            false,
            true,
            false,
        )
        .ok();
    } else {
        git::git(
            &["checkout", "-B", &base_name, &base_ref],
            Some(&ws_path),
            true,
            true,
            false,
        )?;
        let result = git::git(
            &["push", HOST_SHARED_REMOTE, &agent_push_refspec(&base_name, agent_id)],
            Some(&ws_path),
            false,
            true,
            false,
        );
        if result.is_err() && !config::inside_cave() {
            bail!("Failed to push initial branch '{dev_name}' to shared repo.");
        }
    }

    Ok((ws_path, shared))
}
