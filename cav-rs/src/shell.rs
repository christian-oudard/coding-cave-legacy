use std::io::{self, Write};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Command;

use anyhow::{bail, Result};

use crate::config;
use crate::registry;

pub fn enter(
    base: &Path,
    agent_id: Option<&str>,
    cmd: &[String],
) -> Result<()> {
    let interactive = cmd.is_empty();
    let slug = config::project_slug(base);

    let (resolved_aid, target_pid) = resolve_agent_pid(base, &slug, agent_id)?;

    let project_path = format!("/projects/{slug}");
    let name = registry::display_name(base, &resolved_aid);

    if let Some(pid) = rustix::process::Pid::from_raw(target_pid as i32) {
        match rustix::process::test_kill_process(pid) {
            Ok(()) => {}
            Err(e) if e == rustix::io::Errno::SRCH => {
                bail!("Cave {name} (pid {target_pid}) is no longer running.");
            }
            Err(rustix::io::Errno::PERM) => {}
            Err(e) => {
                bail!("Cannot check cave process (pid {target_pid}): {e}");
            }
        }
    }

    if interactive {
        eprintln!("Entering cave {name}...");
    }

    // Move our own cwd to / so nsenter's pre-namespace open(".") succeeds
    // (the host cwd typically does not exist inside the cave's mount ns).
    std::env::set_current_dir("/").ok();

    // Source the cave env from a descendant's /proc/<pid>/environ. bwrap's
    // own environ is pre-setenv (just supervisor's clean env); its bwrap-
    // exec'd child (claude) has the post-setenv cave env that --setenv
    // produced. Pick the descendant whose environ contains the cave marker
    // CODING_CAVE_AGENT_NAME — that's a cave-side process started under
    // bwrap's setenv. Avoids landing on a worker subprocess that cleared
    // its env (e.g. a daemon that did env_clear before fork).
    let cave_env = find_cave_env(target_pid).unwrap_or_default();

    let inner = if interactive {
        // Honor $SHELL from the cave's env (sourced from the agent's
        // /proc/<pid>/environ above) so a user who set
        // `home.sessionVariables.SHELL = "${pkgs.zsh}/bin/zsh"` in cave.nix
        // gets zsh. Falls back to /bin/bash when SHELL is unset. `-l` works
        // as a login flag for bash, zsh, and fish.
        format!("cd {project_path} && exec \"${{SHELL:-/bin/bash}}\" -l")
    } else {
        let joined = cmd.iter()
            .map(|s| shell_quote(s))
            .collect::<Vec<_>>()
            .join(" ");
        format!("cd {project_path} && exec {joined}")
    };
    let target_arg = format!("--target={target_pid}");

    // Resolve nsenter's absolute path BEFORE env_clear: Command::exec()
    // mutates the calling process's env in-place before execvp, so a
    // cleared/replaced PATH would make execvp fail to find "nsenter".
    let nsenter_path = which_in_path("nsenter")
        .ok_or_else(|| anyhow::anyhow!("nsenter not found on PATH"))?;

    // env_clear drops the host's PATH/HOME/USER. Then we restore the cave's
    // env from the descendant. TERM stays from the host so the new shell
    // knows the terminal type (cave processes don't always set it).
    let term = std::env::var("TERM").unwrap_or_else(|_| "xterm-256color".to_string());
    let mut cmd_builder = Command::new(&nsenter_path);
    cmd_builder.env_clear();
    cmd_builder.env("TERM", term);
    for (k, v) in cave_env {
        if k != "TERM" && k != "_" && k != "PWD" && k != "OLDPWD" && k != "SHLVL" {
            cmd_builder.env(k, v);
        }
    }
    // --pid is required: without it our shell stays in cav's outer pid ns
    // while /proc is mounted for the cave's inner pid ns, so /proc/self
    // resolves to nothing and tools like direnv (readlink /proc/self/exe)
    // break. Joining --pid aligns /proc with our pid.
    cmd_builder.args([
        "--preserve-credentials",
        &target_arg,
        "--mount",
        "--user",
        "--net",
        "--pid",
        "--",
        "/bin/bash",
        "-c",
        &inner,
    ]);
    let err = cmd_builder.exec();
    bail!("Failed to exec nsenter: {err}");
}

/// BFS through descendants of `pid`; return the env of the first descendant
/// whose /proc/<pid>/environ contains the cave-marker var
/// CODING_CAVE_AGENT_NAME. That marker is set by bwrap --setenv, so any
/// process matching it has the post-setenv cave env (PATH, HOME=/home/cave-
/// coder, USER, SHELL, ...). Walking shallow-first means we prefer claude
/// itself rather than a helper subprocess.
fn find_cave_env(start: u32) -> Option<Vec<(String, String)>> {
    let mut queue: std::collections::VecDeque<u32> = std::collections::VecDeque::new();
    queue.push_back(start);
    while let Some(pid) = queue.pop_front() {
        let env = read_environ(pid);
        if env.iter().any(|(k, _)| k == "CODING_CAVE_AGENT_NAME") {
            return Some(env);
        }
        for child in read_children(pid) {
            queue.push_back(child);
        }
    }
    None
}

/// Parse /proc/<pid>/environ (NUL-separated KEY=VALUE pairs).
fn read_environ(pid: u32) -> Vec<(String, String)> {
    let bytes = match std::fs::read(format!("/proc/{pid}/environ")) {
        Ok(b) => b,
        Err(_) => return vec![],
    };
    bytes
        .split(|&b| b == 0)
        .filter_map(|chunk| {
            let s = std::str::from_utf8(chunk).ok()?;
            let eq = s.find('=')?;
            Some((s[..eq].to_string(), s[eq + 1..].to_string()))
        })
        .collect()
}

/// Returns (agent_id, target_pid). The supervisor lives in its own pid ns,
/// so it can't report a host-visible pid for bwrap. We walk the proc tree
/// from cav's pid ns instead and pick the first descendant whose mount
/// namespace differs from the outer unshare process. That's the sandboxed
/// target.
fn resolve_agent_pid(base: &Path, slug: &str, agent_id: Option<&str>) -> Result<(String, u32)> {
    let aid = match agent_id {
        Some(aid) => aid.to_string(),
        None => pick_running_agent(base, slug)?,
    };
    let cave_pid = registry::cave_pid(&aid)?
        .ok_or_else(|| anyhow::anyhow!("No cave pid for agent {aid}. Is it running?"))?;
    let pid = find_inner_pid(cave_pid).ok_or_else(|| {
        anyhow::anyhow!(
            "Cannot find sandboxed target inside cave (pid {cave_pid}): no descendant \
             has a different mount namespace. The cave may still be initializing or \
             may have crashed during setup."
        )
    })?;
    Ok((aid, pid))
}

fn find_inner_pid(outer: u32) -> Option<u32> {
    let outer_mnt = mnt_inode(outer)?;
    let mut queue: std::collections::VecDeque<u32> = std::collections::VecDeque::new();
    queue.push_back(outer);
    while let Some(pid) = queue.pop_front() {
        for child in read_children(pid) {
            if let Some(mnt) = mnt_inode(child) {
                if mnt != outer_mnt {
                    return Some(child);
                }
            }
            queue.push_back(child);
        }
    }
    None
}

fn mnt_inode(pid: u32) -> Option<u64> {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(format!("/proc/{pid}/ns/mnt"))
        .ok()
        .map(|m| m.ino())
}

fn read_children(pid: u32) -> Vec<u32> {
    std::fs::read_to_string(format!("/proc/{pid}/task/{pid}/children"))
        .unwrap_or_default()
        .split_whitespace()
        .filter_map(|s| s.parse().ok())
        .collect()
}

fn pick_running_agent(base: &Path, slug: &str) -> Result<String> {
    let ids = registry::all_agent_ids(Some(slug))?;
    let mut running: Vec<String> = vec![];
    for aid in &ids {
        if registry::is_agent_running(aid)? {
            running.push(aid.clone());
        }
    }

    match running.len() {
        0 => bail!("No running agents in {slug}.\nStart one with: cav agent"),
        1 => Ok(running.into_iter().next().unwrap()),
        _ => {
            eprintln!("Multiple running agents in {slug}:");
            for aid in &running {
                let name = registry::display_name(base, aid);
                eprintln!("  {name}");
            }
            eprint!("Agent ID: ");
            io::stderr().flush()?;
            let mut answer = String::new();
            io::stdin().read_line(&mut answer).map_err(|_| anyhow::anyhow!("Aborted."))?;
            let chosen = answer.trim();
            running.into_iter()
                .find(|aid| {
                    aid == chosen
                        || chosen.ends_with(&format!("@{aid}"))
                        || chosen.trim_start_matches('@') == aid
                })
                .ok_or_else(|| anyhow::anyhow!("Invalid selection."))
        }
    }
}

fn which_in_path(name: &str) -> Option<String> {
    std::env::var_os("PATH")?
        .to_string_lossy()
        .split(':')
        .filter(|s| !s.is_empty())
        .map(|d| std::path::Path::new(d).join(name))
        .find(|p| p.is_file())
        .map(|p| p.to_string_lossy().into_owned())
}

fn shell_quote(s: &str) -> String {
    if s.chars().all(|c| c.is_ascii_alphanumeric() || "_-./=:".contains(c)) {
        s.to_string()
    } else {
        format!("'{}'", s.replace('\'', "'\\''"))
    }
}
