/// Git wrapper installed inside the cave at ~/bin/git.
/// Syncs changes to the parent shared remote after each git command.
use std::env;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{exit, Command};

fn find_real_git() -> Option<String> {
    let wrapper = env::current_exe().ok()?;
    let wrapper_dir = wrapper.parent()?.to_string_lossy().to_string();
    for dir in env::var("PATH").unwrap_or_default().split(':') {
        if dir == wrapper_dir {
            continue;
        }
        let p = Path::new(dir).join("git");
        if p.is_file() {
            return Some(p.to_string_lossy().to_string());
        }
    }
    None
}

fn git_out(real_git: &str, args: &[&str], cwd: Option<&str>) -> Option<String> {
    let mut cmd = Command::new(real_git);
    cmd.args(args);
    if let Some(d) = cwd {
        cmd.current_dir(d);
    }
    let out = cmd.output().ok()?;
    if out.status.success() {
        Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
    } else {
        None
    }
}

fn branch_tip(real_git: &str, cwd: Option<&str>) -> (Option<String>, Option<String>) {
    let branch = git_out(real_git, &["symbolic-ref", "--short", "HEAD"], cwd);
    let sha = branch.as_ref().and_then(|b| {
        git_out(real_git, &["rev-parse", &format!("refs/heads/{b}")], cwd)
    });
    (branch, sha)
}

fn parse_chdir(args: &[String]) -> Option<String> {
    let mut prev = "";
    for arg in args {
        if prev == "-C" {
            return Some(arg.clone());
        }
        prev = arg;
    }
    None
}

fn is_ancestor(real_git: &str, old: &str, new: &str, cwd: Option<&str>) -> bool {
    let mut cmd = Command::new(real_git);
    cmd.args(["merge-base", "--is-ancestor", old, new]);
    if let Some(d) = cwd {
        cmd.current_dir(d);
    }
    cmd.output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn rebase_in_progress(real_git: &str, cwd: Option<&str>) -> bool {
    if let Some(git_dir) = git_out(real_git, &["rev-parse", "--git-dir"], cwd) {
        let base = cwd.map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
        let d = if git_dir.starts_with('/') {
            PathBuf::from(&git_dir)
        } else {
            base.join(&git_dir)
        };
        return d.join("rebase-merge").exists() || d.join("rebase-apply").exists();
    }
    false
}

fn sync(real_git: &str, args: Vec<String>) {
    let agent_name = env::var("CODING_CAVE_AGENT_NAME").unwrap_or_default();
    let agent_id: String = agent_name.rsplit('@').next().unwrap_or("").to_string();
    if agent_id.is_empty() || !agent_id.chars().all(|c| c.is_ascii_digit()) {
        run_and_exit(real_git, &args);
    }

    let cwd = parse_chdir(&args);
    let cwd_ref = cwd.as_deref();

    let (_, sha_before) = branch_tip(real_git, cwd_ref);

    // Run the real command
    let status = Command::new(real_git)
        .args(&args)
        .status()
        .unwrap_or_else(|e| {
            eprintln!("git: exec failed: {e}");
            exit(1);
        });

    if !status.success() {
        exit(status.code().unwrap_or(1));
    }

    let (branch, sha_after) = branch_tip(real_git, cwd_ref);
    let branch = match branch {
        Some(b) => b,
        None => exit(0),
    };

    // Don't sync if we're on a dev branch
    if branch.starts_with("agent-") {
        exit(0);
    }

    if sha_before == sha_after {
        exit(0);
    }

    // Prevent recursion
    unsafe { env::set_var("_CAV_GIT_SYNC", "1") };

    let sha_after = sha_after.unwrap_or_default();

    // Forward motion: fetch + rebase
    if let Some(ref sb) = sha_before {
        if is_ancestor(real_git, sb, &sha_after, cwd_ref)
            && !rebase_in_progress(real_git, cwd_ref)
        {
            let mut fetch_cmd = Command::new(real_git);
            fetch_cmd.arg("fetch").arg("host-cave");
            if let Some(d) = cwd_ref {
                fetch_cmd.current_dir(d);
            }
            let _ = fetch_cmd.output();

            let remote_ref = format!("host-cave/{branch}");
            if git_out(real_git, &["rev-parse", &remote_ref], cwd_ref).is_some() {
                let mut rebase_cmd = Command::new(real_git);
                rebase_cmd.arg("rebase").arg(&remote_ref);
                if let Some(d) = cwd_ref {
                    rebase_cmd.current_dir(d);
                }
                if !rebase_cmd.status().map(|s| s.success()).unwrap_or(false) {
                    let mut abort = Command::new(real_git);
                    abort.args(["rebase", "--abort"]);
                    if let Some(d) = cwd_ref {
                        abort.current_dir(d);
                    }
                    let _ = abort.output();
                    eprintln!("WARNING: rebase onto host-cave/{branch} failed (conflicts).");
                }
            }
            // Re-read tip after rebase
            let (_, new_sha) = branch_tip(real_git, cwd_ref);
            let _ = new_sha; // we'll use push with sha_after below
        }
    }

    // Re-read tip after possible rebase
    let (_, final_sha) = branch_tip(real_git, cwd_ref);
    let push_sha = final_sha.as_deref().unwrap_or(&sha_after);

    // Push
    let remote_ref = format!("agent-{agent_id}/{branch}");
    let refspec = format!("{push_sha}:refs/heads/{remote_ref}");
    let mut push_cmd = Command::new(real_git);
    push_cmd.args(["push", "--force", "host-cave", &refspec]);
    if let Some(d) = cwd_ref {
        push_cmd.current_dir(d);
    }
    match push_cmd.output() {
        Ok(out) if !out.status.success() => {
            eprintln!("cav-sync: push {branch} -> host-cave/{remote_ref} failed:");
            let msg = String::from_utf8_lossy(if out.stderr.is_empty() { &out.stdout } else { &out.stderr });
            for line in msg.trim().lines() {
                eprintln!("  {line}");
            }
        }
        Err(e) => eprintln!("cav-sync: push failed: {e}"),
        _ => {}
    }

    exit(0);
}

fn run_and_exit(real_git: &str, args: &[String]) -> ! {
    let err = Command::new(real_git).args(args).exec();
    eprintln!("git: exec failed: {err}");
    exit(1);
}

fn main() {
    // Recursion guard
    if env::var("_CAV_GIT_SYNC").is_ok() {
        let real_git = env::var("_CAV_REAL_GIT")
            .ok()
            .or_else(find_real_git)
            .unwrap_or_else(|| "git".to_string());
        let args: Vec<String> = env::args().skip(1).collect();
        run_and_exit(&real_git, &args);
    }

    let real_git = match find_real_git() {
        Some(g) => g,
        None => {
            eprintln!("git: real git not found on PATH");
            exit(1);
        }
    };
    unsafe { env::set_var("_CAV_REAL_GIT", &real_git) };

    let args: Vec<String> = env::args().skip(1).collect();
    sync(&real_git, args);
}
