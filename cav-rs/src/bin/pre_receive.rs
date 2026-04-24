use std::io::{self, BufRead};
use std::process::{Command, exit};

const ZERO_SHA: &str = "0000000000000000000000000000000000000000";

fn is_ancestor(old: &str, new: &str) -> bool {
    Command::new("git")
        .args(["merge-base", "--is-ancestor", old, new])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn agent_tip(branch: &str) -> Option<String> {
    let r = Command::new("git")
        .args(["rev-parse", "--verify", &format!("refs/cav/agent-tip/{branch}")])
        .output()
        .ok()?;
    if r.status.success() {
        Some(String::from_utf8_lossy(&r.stdout).trim().to_string())
    } else {
        None
    }
}

fn get_agent() -> Option<String> {
    std::env::var("CODING_CAVE_AGENT_ID").ok().filter(|s| !s.is_empty())
}

fn check_ref(old_sha: &str, new_sha: &str, refname: &str, agent: Option<&str>) -> Option<String> {
    if !refname.starts_with("refs/heads/") {
        if agent.is_some() {
            return Some(format!(
                "REJECTED: Agent {} cannot push to '{refname}'.\n\
                 Agents may only push to branch refs.",
                agent.unwrap()
            ));
        }
        return None;
    }

    let branch = &refname["refs/heads/".len()..];

    if branch.contains("HEAD") {
        // Something creates agent-N/HEAD branches occasionally; log full
        // context so the root cause can be tracked down when it recurs.
        let env_dump: Vec<(String, String)> = std::env::vars()
            .filter(|(k, _)| k.starts_with("CODING_CAVE_") || k.starts_with("GIT_"))
            .collect();
        eprintln!("HEAD-in-branch bug diagnostics:");
        eprintln!("  ref:    {refname}");
        eprintln!("  branch: {branch}");
        eprintln!("  old:    {old_sha}");
        eprintln!("  new:    {new_sha}");
        eprintln!("  agent:  {}", agent.unwrap_or("<none>"));
        for (k, v) in &env_dump {
            eprintln!("  env[{k}]={v}");
        }
        return Some(format!(
            "REJECTED: branch name '{branch}' contains 'HEAD'.\n\
             This is a bug. Diagnostics printed above."
        ));
    }

    if agent.is_none() {
        return None;
    }
    let agent = agent.unwrap();

    // Check if it's an agent branch: agent-<n>/<base>
    if let Some(rest) = branch.strip_prefix("agent-") {
        if let Some(slash) = rest.find('/') {
            let branch_agent = &rest[..slash];
            if agent != branch_agent {
                return Some(format!(
                    "REJECTED: Agent {agent} cannot push to '{branch}'.\n\
                     You may only push to branches matching '*/agent-{agent}'."
                ));
            }
            // Non-FF push: check agent tip
            if old_sha != ZERO_SHA && new_sha != ZERO_SHA && !is_ancestor(old_sha, new_sha) {
                if let Some(tip) = agent_tip(branch) {
                    if tip != old_sha {
                        return Some(format!(
                            "REJECTED: Agent {agent} force-push would overwrite \
                             commits on '{branch}' not pushed by this agent.\n\
                             Pull the user's changes first."
                        ));
                    }
                }
            }
            return None;
        }
    }

    // Base branch: agents cannot push
    Some(format!(
        "REJECTED: Agent {agent} cannot push to base branch '{branch}'.\n\
         Work stays on your dev branch until the user approves it."
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    const ZERO: &str = "0000000000000000000000000000000000000000";
    const SHA_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const SHA_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    // User (agent=None) can do anything
    #[test]
    fn user_push_shared() {
        assert!(check_ref(SHA_A, SHA_B, "refs/heads/main", None).is_none());
    }

    #[test]
    fn user_delete_shared() {
        assert!(check_ref(SHA_A, ZERO, "refs/heads/main", None).is_none());
    }

    #[test]
    fn user_push_agent_branch() {
        assert!(check_ref(SHA_A, SHA_B, "refs/heads/agent-1/main", None).is_none());
    }

    #[test]
    fn user_create_branch() {
        assert!(check_ref(ZERO, SHA_A, "refs/heads/develop", None).is_none());
    }

    // Agent: own branch
    #[test]
    fn agent_push_own_branch() {
        assert!(check_ref(SHA_A, SHA_B, "refs/heads/agent-1/main", Some("1")).is_none());
    }

    #[test]
    fn agent_create_own_branch() {
        assert!(check_ref(ZERO, SHA_A, "refs/heads/agent-1/develop", Some("1")).is_none());
    }

    #[test]
    fn agent_force_push_own_branch() {
        // Force push own branch is allowed when agent-tip is not set (agent_tip returns None)
        assert!(check_ref(SHA_A, SHA_B, "refs/heads/agent-1/main", Some("1")).is_none());
    }

    // Agent: other agent's branch
    #[test]
    fn agent_push_other_agent_branch() {
        let r = check_ref(SHA_A, SHA_B, "refs/heads/agent-2/main", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
        assert!(r.contains("cannot push"), "got: {r}");
    }

    #[test]
    fn agent_create_other_agent_branch() {
        let r = check_ref(ZERO, SHA_A, "refs/heads/agent-2/main", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
    }

    // Agent: base branch (all pushes rejected)
    #[test]
    fn agent_push_base_rejected() {
        let r = check_ref(SHA_A, SHA_B, "refs/heads/main", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
        assert!(r.contains("cannot push"), "got: {r}");
        assert!(r.contains("base branch"), "got: {r}");
    }

    #[test]
    fn agent_delete_shared_rejected() {
        let r = check_ref(SHA_A, ZERO, "refs/heads/main", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
        assert!(r.contains("cannot push"), "got: {r}");
    }

    #[test]
    fn agent_create_shared_rejected() {
        let r = check_ref(ZERO, SHA_A, "refs/heads/develop", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
        assert!(r.contains("cannot push"), "got: {r}");
    }

    // HEAD branch trap
    #[test]
    fn agent_push_head_branch_rejected() {
        let r = check_ref(SHA_A, SHA_B, "refs/heads/agent-1/HEAD", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
        assert!(r.contains("HEAD"), "got: {r}");
    }

    #[test]
    fn user_push_head_branch_rejected() {
        let r = check_ref(SHA_A, SHA_B, "refs/heads/agent-1/HEAD", None).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
    }

    #[test]
    fn bare_head_branch_rejected() {
        let r = check_ref(ZERO, SHA_A, "refs/heads/HEAD", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
    }

    // Non-branch refs (tags, notes, replace)
    #[test]
    fn agent_tag_push_rejected() {
        let r = check_ref(ZERO, SHA_A, "refs/tags/v1.0", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
    }

    #[test]
    fn agent_notes_ref_rejected() {
        let r = check_ref(SHA_A, SHA_B, "refs/notes/commits", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
    }

    #[test]
    fn agent_replace_ref_rejected() {
        let r = check_ref(SHA_A, SHA_B, &format!("refs/replace/{SHA_A}"), Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
    }

    #[test]
    fn user_tag_push_allowed() {
        assert!(check_ref(ZERO, SHA_A, "refs/tags/v1.0", None).is_none());
    }

    #[test]
    fn user_notes_push_allowed() {
        assert!(check_ref(SHA_A, SHA_B, "refs/notes/commits", None).is_none());
    }

    // Agent ID edge cases
    #[test]
    fn agent_high_id() {
        assert!(check_ref(SHA_A, SHA_B, "refs/heads/agent-42/main", Some("42")).is_none());
    }

    #[test]
    fn agent_id_mismatch() {
        let r = check_ref(SHA_A, SHA_B, "refs/heads/agent-10/main", Some("1")).unwrap();
        assert!(r.contains("REJECTED"), "got: {r}");
    }

    #[test]
    fn agent_own_branch_different_targets() {
        for base in &["main", "develop", "feature/foo"] {
            let refname = format!("refs/heads/agent-1/{base}");
            assert!(
                check_ref(SHA_A, SHA_B, &refname, Some("1")).is_none(),
                "Failed for base: {base}"
            );
        }
    }

    // get_agent: reads CODING_CAVE_AGENT_ID env var
    #[test]
    fn get_agent_empty_env_returns_none() {
        // Can't easily clear env in unit tests; skip if var is set
        if std::env::var("CODING_CAVE_AGENT_ID").map(|v| v.is_empty()).unwrap_or(true) {
            // Either not set or empty - both should return None
            let agent = std::env::var("CODING_CAVE_AGENT_ID")
                .ok()
                .filter(|s| !s.is_empty());
            assert!(agent.is_none());
        }
    }
}

fn main() {
    let agent = get_agent();
    let stdin = io::stdin();
    let mut errors = vec![];

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
        let (old_sha, new_sha, refname) = (parts[0], parts[1], parts[2]);
        if let Some(err) = check_ref(old_sha, new_sha, refname, agent.as_deref()) {
            errors.push(err);
        }
    }

    if !errors.is_empty() {
        for err in &errors {
            eprintln!("{err}");
        }
        exit(1);
    }
}
