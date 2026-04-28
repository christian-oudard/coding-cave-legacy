use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::{self, inside_cave, parse_dev_branch};
use crate::constants::{CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR};
use crate::git;
use crate::registry;
use crate::workspace;

fn install_hooks(shared: &Path) -> Result<()> {
    // In the Rust build, hooks are installed as compiled binaries.
    // The cav build places cav-pre-receive and cav-post-receive next to cav.
    let cav_bin = crate::constants::build().cav_bin.clone();
    let hooks_dir = shared.join("hooks");
    std::fs::create_dir_all(&hooks_dir)?;

    for (hook_name, binary) in &[
        ("pre-receive", "cav-pre-receive"),
        ("post-receive", "cav-post-receive"),
    ] {
        let dst = hooks_dir.join(hook_name);
        let src_bin = PathBuf::from(&cav_bin).join(binary);
        if src_bin.exists() {
            std::fs::copy(&src_bin, &dst)?;
        } else {
            // Fallback: write a wrapper shell script (useful during dev/test)
            std::fs::write(&dst, format!("#!/bin/sh\nexec {}\n", src_bin.display()))?;
        }
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&dst, std::fs::Permissions::from_mode(0o755))?;
    }
    Ok(())
}

fn require_commits(base: &Path) -> Result<()> {
    let ok = git::git(&["rev-parse", "HEAD"], Some(base), false, true, false).is_ok();
    if !ok {
        bail!(
            "No commits yet. Commit your files first:\n  git add -A && git commit -m 'Initial commit'"
        );
    }
    Ok(())
}

fn warn_dirty(base: &Path) {
    let dirty = git::git_output(
        &["status", "--porcelain", "--ignore-submodules"],
        Some(base),
    )
    .unwrap_or_default();
    let filtered: Vec<&str> = dirty
        .lines()
        .filter(|l| !l.ends_with(".coding-cave/") && !l.ends_with(".coding-cave"))
        .collect();
    if !filtered.is_empty() {
        eprintln!(
            "Warning: uncommitted changes will not be visible to agents. Commit them first, or use 'cav cp' to copy files in later.\n"
        );
    }
}

fn is_bare_repo(path: &Path) -> bool {
    path.is_dir() && (path.join("HEAD").exists() || path.join("config").exists())
}

fn publish_bases(base: &Path) -> Result<()> {
    let has_host_cave = git::git(
        &["remote", "get-url", HOST_SHARED_REMOTE],
        Some(base),
        false,
        true,
        false,
    )
    .is_ok();

    let prefix = if has_host_cave {
        git::git(
            &["fetch", "--prune", HOST_SHARED_REMOTE],
            Some(base),
            false,
            true,
            false,
        )
        .ok();
        format!("refs/remotes/{HOST_SHARED_REMOTE}/")
    } else {
        "refs/heads/".to_string()
    };

    let refs = git::git_output(&["for-each-ref", "--format=%(refname)", &prefix], Some(base))?;
    for refline in refs.lines() {
        let name = &refline[prefix.len()..];
        if name == "HEAD" || parse_dev_branch(name).is_some() {
            continue;
        }
        git::git(
            &[
                "push",
                "--no-set-upstream",
                CAVE_SHARED_REMOTE,
                &format!("{refline}:refs/heads/{name}"),
            ],
            Some(base),
            false,
            true,
            false,
        )
        .ok();
    }
    Ok(())
}

fn offer_cave_nix() {
    let host_home = std::env::var("CODING_CAVE_HOST_HOME")
        .unwrap_or_else(|_| std::env::var("HOME").unwrap_or_default());
    let cave_nix = PathBuf::from(&host_home)
        .join(".config")
        .join("coding-cave")
        .join("cave.nix");
    if cave_nix.exists() {
        return;
    }
    if !rustix::termios::isatty(rustix::stdio::stdin()) {
        return;
    }
    print!("Create default cave.nix? [y/N] ");
    io::stdout().flush().ok();
    let mut answer = String::new();
    if io::stdin().read_line(&mut answer).is_err() {
        return;
    }
    let answer = answer.trim().to_lowercase();
    if answer != "y" && answer != "yes" {
        return;
    }
    if let Some(parent) = cave_nix.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    if let Ok(content) = crate::user_env::default_cave_nix() {
        if std::fs::write(&cave_nix, content).is_ok() {
            println!("Created {}", cave_nix.display());
        }
    }
}

pub fn init(path: &Path, reinitialize: bool) -> Result<()> {
    let base = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());

    // If we're inside an existing git repo but not at its root, redirect.
    if let Ok(root) = git::repo_root(Some(&base)) {
        let root_path = PathBuf::from(&root);
        if root_path != base && !base.join(".git").exists() {
            bail!(
                "This directory is inside an existing git repo at {}.\n\
                 Run 'cav init {}' to initialize there instead.",
                root_path.display(),
                root_path.display()
            );
        }
    }

    if !base.join(".git").exists() {
        git::git(&["init", &base.to_string_lossy()], None, true, false, true)?;
        println!();
    }

    let cav = config::cav_dir(&base);
    let shared = config::shared_repo_path(&base);

    if inside_cave() {
        let host_shared = cav.join(HOST_SHARED_REPO_DIR);
        if !host_shared.exists() {
            bail!(
                "{} missing. Expected a bind mount from the parent cave. \
                 Relaunch via 'cav agent' on the host.",
                host_shared.display()
            );
        }
    }

    if is_bare_repo(&shared) {
        if !reinitialize {
            registry::register_project(&base)?;
            println!("Coding Cave already initialized at {}", cav.display());
            return Ok(());
        }
        install_hooks(&shared)?;
        git::git(
            &["config", "receive.advertisePushOptions", "true"],
            Some(&shared),
            false,
            true,
            false,
        )?;
        git::git(
            &["config", "receive.shallowUpdate", "true"],
            Some(&shared),
            false,
            true,
            false,
        )?;
        registry::register_project(&base)?;
        workspace::reconfigure(&base)?;
        require_commits(&base)?;
        warn_dirty(&base);
        publish_bases(&base)?;
        println!("Coding Cave reinitialized at {}", cav.display());
        return Ok(());
    }

    // Fresh init. Remove any stub directory at shared.
    if shared.exists() {
        std::fs::remove_dir_all(&shared)?;
    }

    // Refuse if 'cave' remote already exists pointing elsewhere.
    if git::git(
        &["remote", "get-url", CAVE_SHARED_REMOTE],
        Some(&base),
        false,
        true,
        false,
    )
    .is_ok()
    {
        bail!(
            "Remote '{CAVE_SHARED_REMOTE}' already exists.\n\
             Remove it first: git remote remove {CAVE_SHARED_REMOTE}"
        );
    }

    require_commits(&base)?;

    std::fs::create_dir_all(&cav)?;
    git::git(
        &["init", "--bare", &shared.to_string_lossy()],
        None,
        true,
        true,
        false,
    )?;
    git::git(
        &["config", "receive.advertisePushOptions", "true"],
        Some(&shared),
        false,
        true,
        false,
    )?;
    git::git(
        &["config", "receive.shallowUpdate", "true"],
        Some(&shared),
        false,
        true,
        false,
    )?;
    install_hooks(&shared)?;
    git::git(
        &["remote", "add", CAVE_SHARED_REMOTE, &shared.to_string_lossy()],
        Some(&base),
        true,
        true,
        false,
    )?;

    warn_dirty(&base);
    publish_bases(&base)?;
    registry::register_project(&base)?;
    println!("Coding Cave initialized at {}", cav.display());

    if !inside_cave() {
        offer_cave_nix();
    }
    Ok(())
}

