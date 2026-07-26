use std::io::{self, BufRead};
use std::process::Command;

const ZERO_SHA: &str = "0000000000000000000000000000000000000000";

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// `agent-<n>/<target>` -> `<target>`. Mirrors `config::parse_dev_branch`.
fn dev_branch_target(branch: &str) -> Option<&str> {
    let rest = branch.strip_prefix("agent-")?;
    let slash = rest.find('/')?;
    let (id, target) = (&rest[..slash], &rest[slash + 1..]);
    if id.is_empty() || target.is_empty() || !id.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    Some(target)
}

/// Every non-dev branch in the repo: the lines an agent's work forks from.
/// Other agents' dev branches are deliberately absent — a second agent
/// building on the first one's commits still forks from the mainline.
fn base_lines() -> Vec<String> {
    let Some(out) = git(&["for-each-ref", "--format=%(refname)", "refs/heads/"]) else {
        return vec![];
    };
    out.lines()
        .filter(|r| {
            r.strip_prefix("refs/heads/")
                .is_some_and(|b| dev_branch_target(b).is_none())
        })
        .map(str::to_string)
        .collect()
}

/// Where `new_sha`'s history leaves the existing base lines: the commit just
/// outside the set of commits this branch adds. None when the branch adds
/// nothing, or shares no history with any base line (an unrelated or
/// brand-new line) — neither has a fork point to name.
///
/// A branch that merges several lines has several boundary commits. Any of
/// them gives a correct `target..dev` range, since that range is by
/// definition what the target gains; the nearest one lists the fewest
/// commits the agent did not write.
fn fork_point(new_sha: &str) -> Option<String> {
    let lines = base_lines();
    if lines.is_empty() {
        return None;
    }
    let mut args: Vec<&str> = vec!["rev-list", "--boundary", new_sha, "--not"];
    args.extend(lines.iter().map(String::as_str));
    let out = git(&args)?;
    let mut boundary: Vec<&str> = out.lines().filter_map(|l| l.strip_prefix('-')).collect();
    boundary.sort_unstable();
    boundary.dedup();
    match boundary.len() {
        0 => None,
        1 => Some(boundary[0].to_string()),
        _ => boundary
            .into_iter()
            .min_by_key(|b| {
                git(&["rev-list", "--count", &format!("{b}..{new_sha}")])
                    .and_then(|s| s.parse::<usize>().ok())
                    .unwrap_or(usize::MAX)
            })
            .map(str::to_string),
    }
}

/// A dev branch only means something against its target branch: `cav approve`
/// merges `target..dev`, `cav status` measures against it. When a push
/// creates `agent-<n>/<target>` and the repo has no `<target>` yet, record
/// where the work began by creating the target at the fork point. Both
/// ranges are then exact for the life of the branch, instead of being
/// re-derived later from a ref set that has since moved.
fn ensure_target_branch(branch: &str, new_sha: &str) {
    let Some(target) = dev_branch_target(branch) else {
        return;
    };
    let target_ref = format!("refs/heads/{target}");
    if git(&["rev-parse", "--verify", "--quiet", &target_ref]).is_some() {
        return;
    }
    if let Some(fork) = fork_point(new_sha) {
        git(&["update-ref", &target_ref, &fork]);
    }
}

fn main() {
    // Agent-tip refs are only maintained for pushes from inside a cave.
    let from_agent = std::env::var("CODING_CAVE_AGENT_ID")
        .ok()
        .filter(|s| !s.is_empty())
        .is_some();

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

        if new_sha != ZERO_SHA {
            ensure_target_branch(branch, new_sha);
        }

        if !from_agent {
            continue;
        }
        let tip_ref = format!("refs/cav/agent-tip/{branch}");
        if new_sha == ZERO_SHA {
            git(&["update-ref", "-d", &tip_ref]);
        } else {
            git(&["update-ref", &tip_ref, new_sha]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::dev_branch_target;

    #[test]
    fn dev_branch_targets() {
        assert_eq!(dev_branch_target("agent-1/main"), Some("main"));
        assert_eq!(dev_branch_target("agent-12/feature/x"), Some("feature/x"));
        assert_eq!(dev_branch_target("main"), None);
        assert_eq!(dev_branch_target("agent-x/main"), None);
        assert_eq!(dev_branch_target("agent-1/"), None);
    }
}
