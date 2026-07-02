use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::{self, dev_branch, is_agent_id, parse_dev_branch, resolve_base};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR};
use crate::git;
use crate::registry;

/// Resolve where an agent's worktree lives: the uuid-keyed path. Falls back to
/// the display id only for an id with no registry uuid (an unregistered agent).
/// Pure: no filesystem side effects, safe for dry-run paths.
pub fn worktree_for(slug: &str, agent_id: &str) -> PathBuf {
    let key = registry::agent_uuid(agent_id).ok().flatten();
    config::worktree_dir(slug, key.as_deref().unwrap_or(agent_id))
}

/// The base dir holding every per-cave nix store. Resolves through
/// [`crate::store_paths::cave_stores_base`] — the single definition the host
/// manager (`cav-store`) also watches, so the two can't desync. A fixed
/// `/var/lib/coding-cave/nix-stores` (not under `$HOME`): nix stores are
/// system-level, and a fixed path lets the root daemon find them without
/// knowing which user runs caves.
pub fn cave_store_base() -> PathBuf {
    crate::store_paths::cave_stores_base()
}

/// One cave's private nix chroot store: `<data>/store/agent-<key>`. Keyed by
/// agent uuid (like the worktree and overlay dirs) so the host manager's
/// per-agent quota dirs line up. The cave runs `nix` against this as
/// `local?root=<this>`, so its paths are logically `/nix/store/...`. Pure.
pub fn cave_store_root_for(agent_id: &str) -> PathBuf {
    let base = cave_store_base();
    let key = registry::agent_uuid(agent_id).ok().flatten();
    base.join(format!("agent-{}", key.as_deref().unwrap_or(agent_id)))
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

/// `std::fs::remove_dir_all` with a chmod-writable preorder pass. Nix
/// store paths land in the overlay upper layer as mode-0555 dirs; without
/// the chmod, std bails with EACCES partway through and silently leaves
/// most of the tree on disk. Warnings on failure are surfaced — callers
/// here can't usefully react, but the user needs to see when collection
/// didn't actually free what we claimed.
pub fn force_remove_dir_all(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    fn chmod_writable(path: &Path) {
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
        let Ok(entries) = std::fs::read_dir(path) else { return };
        for entry in entries.flatten() {
            let Ok(meta) = entry.metadata() else { continue };
            if meta.is_dir() && !meta.is_symlink() {
                chmod_writable(&entry.path());
            }
        }
    }
    chmod_writable(path);
    if let Err(e) = std::fs::remove_dir_all(path) {
        eprintln!("warning: failed to remove {}: {e}", path.display());
    }
}

/// Erase a paused agent's on-disk state: its worktree dir, plus its refs in
/// each project's bare repo. Called from `cav gc` when collecting a paused
/// agent (the caller removes the nix store separately). Not used for slot
/// reuse in `acquire_agent_id`, which wipes only refs and leaves disk to
/// `cav gc` — a reused slot gets a fresh uuid dir, so there is no stale
/// worktree to clear eagerly.
pub fn reclaim(agent_id: &str, targets: &[(String, PathBuf)]) {
    for (slug, base) in targets {
        let ws = worktree_for(slug, agent_id);
        if ws.exists() {
            force_remove_dir_all(&ws);
        }
        wipe_agent_refs(base, agent_id);
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn force_remove_dir_all_clears_readonly_dirs() {
        // Mimics the nix-overlay upper-layer shape: a nested tree where
        // intermediate dirs are mode 0555. Plain remove_dir_all bails on
        // these with EACCES.
        let tmp = std::env::temp_dir().join(format!("cav-rm-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let nested = tmp.join("ro_outer").join("ro_inner");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::write(nested.join("leaf"), b"x").unwrap();
        let perms = std::fs::Permissions::from_mode(0o555);
        std::fs::set_permissions(&nested, perms.clone()).unwrap();
        std::fs::set_permissions(nested.parent().unwrap(), perms.clone()).unwrap();
        std::fs::set_permissions(&tmp, perms).unwrap();

        assert!(std::fs::remove_dir_all(&tmp).is_err(), "baseline: std fails");

        force_remove_dir_all(&tmp);
        assert!(!tmp.exists(), "force_remove_dir_all should clear the tree");
    }

    #[test]
    fn cave_store_root_matches_manager_layout() {
        // The host manager (cav-store) watches `<base>/agent-<id>` as a chroot
        // store; the cave side must produce the same path. Serialize on the
        // shared data-dir lock and use an agent id with no registry uuid so
        // the fallback (display-id keying) is deterministic without a database.
        let g = crate::test_util::DataDirGuard::new("cave-store-root");
        assert_eq!(cave_store_base(), g.stores());
        assert_eq!(
            cave_store_root_for("__no_such_agent__"),
            g.stores().join("agent-__no_such_agent__"),
        );
    }
}
