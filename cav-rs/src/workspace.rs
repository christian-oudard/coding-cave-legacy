use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::{self, dev_branch, is_agent_id, parse_dev_branch, resolve_base};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR};
use crate::git;

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

/// Create workspace for an agent. Returns (clone_path, shared_repo).
pub fn create(base: &Path, agent_id: &str) -> Result<(PathBuf, PathBuf)> {
    if !is_agent_id(agent_id) {
        bail!("Invalid agent ID '{agent_id}'. Must be a number (e.g. 1, 2, 3).");
    }
    let slug = config::project_slug(base);
    let ws_path = config::worktree_dir(&slug, agent_id);
    let shared = config::shared_repo_path(base);

    if ws_path.exists() {
        return Ok((ws_path, shared));
    }

    let empty = repo_is_empty(base);
    let base_name = if empty {
        "main".to_string()
    } else {
        let user_branch = git::current_branch(Some(base))?;
        resolve_base(user_branch)
    };

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
