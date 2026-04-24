/// Integration tests for branch lifecycle across approve/reject.
///
/// Verifies that full approve tears down the agent branch on both host and
/// shared remote, that reject reconstructs it, and that partial approval
/// leaves the branch untouched.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const CAV: &str = env!("CARGO_BIN_EXE_cav");

struct Project {
    dir: PathBuf,
    data_dir: PathBuf,
}

impl Project {
    fn new(name: &str) -> Self {
        let uniq = format!("{}-{}", std::process::id(), name);
        let dir = std::env::temp_dir().join(format!("cav-lifecycle-{uniq}"));
        let data_dir = std::env::temp_dir().join(format!("cav-lifecycle-data-{uniq}"));
        let _ = fs::remove_dir_all(&dir);
        let _ = fs::remove_dir_all(&data_dir);
        fs::create_dir_all(&dir).unwrap();
        fs::create_dir_all(&data_dir).unwrap();

        git(&["init", "-b", "main"], &dir);
        git(&["config", "user.email", "test@test.com"], &dir);
        git(&["config", "user.name", "Test"], &dir);
        fs::write(dir.join("README.md"), "# test\n").unwrap();
        git(&["add", "README.md"], &dir);
        git(&["commit", "-m", "init"], &dir);

        let p = Self { dir, data_dir };
        let out = p.cav(&["init", p.dir.to_str().unwrap()]);
        assert_eq!(
            out.status.code(),
            Some(0),
            "init failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        p
    }

    /// Seed the registry with an agent row so reject's resolve_target_agent
    /// accepts it. Mirrors what `cav agent` does on sandbox launch, without
    /// requiring the sandbox.
    fn register_agent(&self, agent_id: &str) {
        let slug = self
            .dir
            .file_name()
            .unwrap()
            .to_string_lossy()
            .to_lowercase()
            .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-");
        let db_path = self.data_dir.join("coding-cave.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS agents (\
                 agent_id TEXT PRIMARY KEY,\
                 project TEXT NOT NULL,\
                 session_id TEXT UNIQUE,\
                 host_pid INTEGER,\
                 cave_pid INTEGER,\
                 started_at REAL,\
                 exited_at REAL,\
                 extra_projects TEXT,\
                 host_ports TEXT);",
        )
        .unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO agents \
             (agent_id, project, started_at, exited_at) \
             VALUES (?1, ?2, 0, 1)",
            rusqlite::params![agent_id, slug],
        )
        .unwrap();
    }

    fn cav(&self, args: &[&str]) -> Output {
        Command::new(CAV)
            .args(args)
            .current_dir(&self.dir)
            .env("CODING_CAVE_DATA_DIR", &self.data_dir)
            .env_remove("CODING_CAVE_VERSION")
            .env_remove("CODING_CAVE_AGENT_ID")
            .output()
            .expect("cav binary not found")
    }

    /// Simulate an agent committing on its branch. Returns the tip sha.
    fn push_agent_commits(&self, agent_id: &str, base: &str, files: &[(&str, &str)]) -> String {
        let dev = format!("agent-{agent_id}/{base}");
        let starting = git_out(&["rev-parse", base], &self.dir);
        // Create or move the dev branch to the base tip.
        git(&["branch", "-f", &dev, &starting], &self.dir);
        git(&["checkout", &dev], &self.dir);
        for (name, body) in files {
            fs::write(self.dir.join(name), body).unwrap();
            git(&["add", name], &self.dir);
            git(&["commit", "-m", &format!("agent {agent_id}: {name}")], &self.dir);
        }
        let tip = git_out(&["rev-parse", "HEAD"], &self.dir);
        git(&["push", "cave", &format!("{dev}:refs/heads/{dev}")], &self.dir);
        git(&["checkout", base], &self.dir);
        git(&["fetch", "cave"], &self.dir);
        tip
    }

    fn shared(&self) -> PathBuf {
        self.dir.join(".coding-cave").join("cave-shared.git")
    }

    fn shared_has_ref(&self, ref_name: &str) -> bool {
        Command::new("git")
            .args(["rev-parse", "--verify", ref_name])
            .current_dir(self.shared())
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }

    fn host_has_local_branch(&self, branch: &str) -> bool {
        Command::new("git")
            .args(["rev-parse", "--verify", &format!("refs/heads/{branch}")])
            .current_dir(&self.dir)
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }

    fn host_has_remote_tracking(&self, branch: &str) -> bool {
        Command::new("git")
            .args(["rev-parse", "--verify", &format!("refs/remotes/cave/{branch}")])
            .current_dir(&self.dir)
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }

    fn sha(&self, ref_name: &str) -> String {
        git_out(&["rev-parse", ref_name], &self.dir)
    }

    fn shared_sha(&self, ref_name: &str) -> String {
        let out = Command::new("git")
            .args(["rev-parse", ref_name])
            .current_dir(self.shared())
            .output()
            .unwrap();
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }
}

impl Drop for Project {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
        let _ = fs::remove_dir_all(&self.data_dir);
    }
}

fn git(args: &[&str], dir: &Path) {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap_or_else(|_| panic!("git {:?} failed", args));
    assert!(
        out.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&out.stderr)
    );
}

fn git_out(args: &[&str], dir: &Path) -> String {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

// --- Approve teardown ---

#[test]
fn full_approve_removes_agent_branch_everywhere() {
    let p = Project::new("full-approve");
    p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);

    assert!(p.shared_has_ref("refs/heads/agent-1/main"));
    assert!(p.host_has_remote_tracking("agent-1/main"));

    let out = p.cav(&["approve", "-y", "1"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "approve failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(
        !p.shared_has_ref("refs/heads/agent-1/main"),
        "shared agent branch should be deleted"
    );
    assert!(
        !p.host_has_local_branch("agent-1/main"),
        "local host agent branch should be deleted"
    );
    assert!(
        !p.host_has_remote_tracking("agent-1/main"),
        "remote-tracking ref should be pruned"
    );
}

#[test]
fn partial_approve_preserves_agent_branch() {
    let p = Project::new("partial-approve");
    let tip = p.push_agent_commits(
        "1",
        "main",
        &[("a.txt", "A\n"), ("b.txt", "B\n")],
    );
    // Approve only the first commit (one before tip).
    let first = git_out(&["rev-parse", &format!("{tip}^")], &p.dir);

    let out = p.cav(&["approve", "-y", "1", "--commit", &first]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "partial approve failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(
        p.shared_has_ref("refs/heads/agent-1/main"),
        "shared agent branch must survive partial approval"
    );
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        tip,
        "shared agent branch tip unchanged by partial approval"
    );
    assert_eq!(
        p.shared_sha("refs/heads/main"),
        first,
        "base advanced to partial tip"
    );
}

// --- Reject recreates state ---

#[test]
fn reject_after_full_approve_restores_branches() {
    let p = Project::new("reject-full");
    p.register_agent("1");
    let approved_tip = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let pre_approve_base = p.shared_sha("refs/heads/main");

    let out = p.cav(&["approve", "-y", "1"]);
    assert_eq!(out.status.code(), Some(0));
    // Post-approve: base advanced.
    assert_eq!(p.shared_sha("refs/heads/main"), approved_tip);
    assert!(!p.shared_has_ref("refs/heads/agent-1/main"));

    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(
        p.shared_has_ref("refs/heads/agent-1/main"),
        "shared agent branch must be restored"
    );
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        approved_tip,
        "restored branch points to pre-rollback tip"
    );
    assert!(
        p.host_has_local_branch("agent-1/main"),
        "local host agent branch must be recreated"
    );
    assert_eq!(
        p.sha("refs/heads/agent-1/main"),
        approved_tip,
        "local branch matches restored shared branch"
    );
    assert_eq!(
        p.shared_sha("refs/heads/main"),
        pre_approve_base,
        "base rolled back"
    );
}

// --- Reject uses registry membership, not process state ---

#[test]
fn reject_allows_non_running_agent_id() {
    // Reject validates that the agent-id exists in the registry.
    // A missing id is the only condition that fails. There is no check
    // for a running process (exited_at is set, host_pid NULL below).
    let p = Project::new("reject-nonrunning");
    p.register_agent("1");
    let approved_tip = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let _ = p.cav(&["approve", "-y", "1"]);

    // Rejecting without -a should find the recorded approval and succeed
    // (even though no agent-1 sandbox was ever running in this test).
    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject without running agent: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    // And the state is restored, same as any other reject.
    assert!(p.shared_has_ref("refs/heads/agent-1/main"));
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        approved_tip,
    );
}

// --- Re-approve after reject round-trips cleanly ---

#[test]
fn approve_reject_approve_cycle() {
    let p = Project::new("cycle");
    p.register_agent("1");
    let tip = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);

    assert_eq!(
        p.cav(&["approve", "-y", "1"]).status.code(),
        Some(0)
    );
    assert!(!p.shared_has_ref("refs/heads/agent-1/main"));

    assert_eq!(p.cav(&["reject", "-y"]).status.code(), Some(0));
    assert!(p.shared_has_ref("refs/heads/agent-1/main"));
    assert_eq!(p.shared_sha("refs/heads/agent-1/main"), tip);

    assert_eq!(
        p.cav(&["approve", "-y", "1"]).status.code(),
        Some(0)
    );
    assert_eq!(p.shared_sha("refs/heads/main"), tip);
    assert!(!p.shared_has_ref("refs/heads/agent-1/main"));
    assert!(!p.host_has_local_branch("agent-1/main"));
}
