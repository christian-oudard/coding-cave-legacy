use std::io::{self, BufRead};
use std::process::Command;

const ZERO_SHA: &str = "0000000000000000000000000000000000000000";

fn main() {
    let agent = std::env::var("CODING_CAVE_AGENT_ID")
        .ok()
        .filter(|s| !s.is_empty());
    let agent = match agent {
        Some(a) => a,
        None => return,
    };

    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        let parts: Vec<&str> = line.splitn(3, ' ').collect();
        if parts.len() != 3 {
            continue;
        }
        let (_old, new_sha, refname) = (parts[0], parts[1], parts[2]);
        if !refname.starts_with("refs/heads/") {
            continue;
        }
        let branch = &refname["refs/heads/".len()..];
        let tip_ref = format!("refs/cav/agent-tip/{branch}");
        if new_sha == ZERO_SHA {
            Command::new("git")
                .args(["update-ref", "-d", &tip_ref])
                .output()
                .ok();
        } else {
            Command::new("git")
                .args(["update-ref", &tip_ref, new_sha])
                .output()
                .ok();
        }
        drop(agent.as_str()); // suppress unused warning
    }
}
