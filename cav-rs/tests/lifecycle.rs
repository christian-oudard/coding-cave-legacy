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

    fn agent_registered(&self, agent_id: &str) -> bool {
        let db_path = self.data_dir.join("coding-cave.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.query_row(
            "SELECT 1 FROM agents WHERE agent_id = ?1",
            rusqlite::params![agent_id],
            |_| Ok(()),
        )
        .is_ok()
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
fn approve_disambiguates_by_base_branch() {
    // When on 'main' with agents targeting 'main' and 'develop', approve should
    // auto-select the one targeting 'main' without prompting.
    let p = Project::new("approve-disambig");
    p.register_agent("1");
    p.register_agent("2");
    // Create 'develop' branch
    git(&["checkout", "-b", "develop"], &p.dir);
    git(&["push", "cave", "develop"], &p.dir);
    git(&["checkout", "main"], &p.dir);
    git(&["fetch", "cave"], &p.dir);
    // Agent 1 targets main, agent 2 targets develop
    p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    p.push_agent_commits("2", "develop", &[("b.txt", "B\n")]);
    // push_agent_commits leaves us on the last base branch used, so switch back.
    git(&["checkout", "main"], &p.dir);

    // With -y, should auto-select agent 1 since we're on main
    let out = p.cav(&["approve", "-y"]);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        out.status.code(),
        Some(0),
        "approve should auto-select agent 1 when on main: {combined}"
    );
    assert!(
        !p.shared_has_ref("refs/heads/agent-1/main"),
        "agent-1/main should be deleted after approval: {combined}"
    );
    assert!(
        p.shared_has_ref("refs/heads/agent-2/develop"),
        "agent-2/develop should be untouched: {combined}"
    );
}

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

// --- Partial approve then reject rolls back base and preserves agent branch ---

#[test]
fn reject_after_partial_approve_restores_state() {
    let p = Project::new("partial-reject");
    p.register_agent("1");
    let tip = p.push_agent_commits(
        "1",
        "main",
        &[("a.txt", "A\n"), ("b.txt", "B\n")],
    );
    let original_base = p.shared_sha("refs/heads/main");
    let first = git_out(&["rev-parse", &format!("{tip}^")], &p.dir);

    // Partial approve: base advances to first commit, agent branch survives at tip.
    let out = p.cav(&["approve", "-y", "1", "--commit", &first]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "partial approve failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(p.shared_sha("refs/heads/main"), first);
    assert_eq!(p.shared_sha("refs/heads/agent-1/main"), tip);

    // Reject undoes the partial approval.
    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject after partial approve failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert_eq!(
        p.shared_sha("refs/heads/main"),
        original_base,
        "base must roll back to pre-approval sha"
    );
    assert!(
        p.shared_has_ref("refs/heads/agent-1/main"),
        "agent branch must remain"
    );
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        tip,
        "agent branch tip preserved at full dev tip"
    );
    assert!(
        p.host_has_local_branch("agent-1/main"),
        "local host agent branch must exist"
    );

    // Approval record must be gone: a second reject with no approvals should fail.
    let out = p.cav(&["reject", "-y"]);
    assert_ne!(
        out.status.code(),
        Some(0),
        "second reject should fail since approval was deleted"
    );
}

// --- Reaper handles crashes that skipped teardown ---

#[test]
fn reaper_removes_dead_trivially_clean_agent() {
    // Simulates: agent launched, crashed (SIGKILL) before it ever pushed
    // anything. The registry row was never cleaned up because run_agent's
    // exit path didn't run. On the next `cav status`, the reaper should
    // spot an exited agent with no branches / no approvals / no worktree
    // and remove the stale row.
    let p = Project::new("reaper-clean");
    p.register_agent("7");
    assert!(p.agent_registered("7"));

    let out = p.cav(&["status"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "status failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(
        !p.agent_registered("7"),
        "trivially-clean exited agent should have been reaped"
    );
}

#[test]
fn reconciler_drops_dev_branch_matching_base_tip() {
    // Simulates: a prior approve advanced the base tip but failed to delete
    // the dev branch. The reconciler should drop any shared agent-N/* whose
    // tip matches the corresponding base, since identical tips means there
    // is no unique work to lose.
    let p = Project::new("reconcile-merged");
    git(
        &["push", "cave", "main:refs/heads/agent-99/main"],
        &p.dir,
    );
    git(&["fetch", "cave"], &p.dir);
    assert!(
        p.shared_has_ref("refs/heads/agent-99/main"),
        "test setup: stale dev branch should exist"
    );

    let out = p.cav(&["status"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "status failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(
        !p.shared_has_ref("refs/heads/agent-99/main"),
        "reconciler should drop dev branch whose tip matches base"
    );
}

#[test]
fn reconciler_preserves_dev_branch_with_unique_commits() {
    // Opposite check: a dev branch with commits ahead of base must not be
    // dropped. Only matched-tip branches are safe to delete.
    let p = Project::new("reconcile-unique");
    p.register_agent("3");
    p.push_agent_commits("3", "main", &[("a.txt", "A\n")]);
    assert!(p.shared_has_ref("refs/heads/agent-3/main"));

    let out = p.cav(&["status"]);
    assert_eq!(out.status.code(), Some(0));

    assert!(
        p.shared_has_ref("refs/heads/agent-3/main"),
        "dev branch ahead of base must survive reconciler"
    );
}

#[test]
fn reaper_preserves_agent_with_unapproved_work() {
    // Opposite of the above: an exited agent that did push commits must NOT
    // be reaped, because their shared branch is salvageable work that the
    // user still needs to approve or reject.
    let p = Project::new("reaper-preserve");
    p.register_agent("7");
    p.push_agent_commits("7", "main", &[("a.txt", "A\n")]);

    let out = p.cav(&["status"]);
    assert_eq!(out.status.code(), Some(0));

    assert!(
        p.agent_registered("7"),
        "agent with unapproved commits must survive"
    );
    assert!(
        p.shared_has_ref("refs/heads/agent-7/main"),
        "shared agent branch must survive"
    );
}

// --- Agent ID lifecycle decoupled from session archive ---

#[test]
fn resume_unknown_agent_id_points_to_session_archive() {
    // cav -r N for a missing/reclaimed agent should error with a hint
    // pointing at cav --resume <session-uuid>, not silently substitute
    // a different agent.
    let p = Project::new("resume-missing-agent");
    let out = p.cav(&["agent", "-r", "999"]);
    assert_ne!(out.status.code(), Some(0));
    let err = String::from_utf8_lossy(&out.stderr).to_string();
    assert!(
        err.contains("--resume <session-uuid>"),
        "error should hint at --resume <session-uuid>, got: {err}"
    );
    assert!(
        err.contains("999"),
        "error should mention the requested agent, got: {err}"
    );
}

#[test]
fn continue_with_no_agents_points_to_session_archive() {
    // cav -c with no agents should error with a hint pointing at
    // cav --resume <session-uuid> for archive recovery.
    let p = Project::new("continue-empty");
    let out = p.cav(&["agent", "-c"]);
    assert_ne!(out.status.code(), Some(0));
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        combined.contains("cav --resume") || combined.contains("Nothing to continue"),
        "error should mention --resume or 'Nothing to continue', got: {combined}"
    );
}

#[test]
fn resume_unknown_session_uuid_mints_fresh_agent() {
    // cav --resume <uuid> for an unrecognized uuid should mint a fresh
    // agent id, bind the uuid as that agent's session, and try to launch.
    // We can't run all the way through (sandbox config absent), but the
    // registry side-effect should land before the launch fails.
    let p = Project::new("resume-mint-uuid");
    let fake_uuid = "deadbeef-0000-0000-0000-000000000000";
    let _ = p.cav(&["agent", "-r", fake_uuid]); // expected to fail at sandbox launch
    // The agent row should now exist with the uuid as its session_id.
    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let bound: Option<String> = conn
        .query_row(
            "SELECT agent_id FROM agents WHERE session_id = ?1",
            rusqlite::params![fake_uuid],
            |row| row.get(0),
        )
        .ok();
    assert!(
        bound.is_some(),
        "an agent row should bind the unknown uuid; none found"
    );
}

#[test]
fn dead_pid_sweep_uses_session_mtime_for_exit_time() {
    // When we discover an agent's pid is dead, exited_at should be set to
    // the session log's mtime (when the agent last did work) rather than
    // now() (when we noticed). Verifies the mtime-based exit estimate.
    let p = Project::new("mtime-exit");
    let slug = p
        .dir
        .file_name()
        .unwrap()
        .to_string_lossy()
        .to_lowercase();

    // Insert an agent row that claims to be running (host_pid set, no
    // exited_at) with a known session id and a definitely-dead pid.
    let sid = "11111111-1111-1111-1111-111111111111";
    let dead_pid: u32 = 999_999_999;
    let db_path = p.data_dir.join("coding-cave.db");
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
         (agent_id, project, session_id, host_pid, started_at, exited_at) \
         VALUES (?1, ?2, ?3, ?4, 0, NULL)",
        rusqlite::params!["3", slug, sid, dead_pid],
    )
    .unwrap();

    // Create a fake session jsonl with mtime backdated 7 days. Path matches
    // config::claude_project_dir(slug, None) -> data_dir/home/.claude/projects/<encoded>
    // where encoded replaces every non-alphanumeric char in /projects/<slug>.
    let encoded = format!("/projects/{slug}")
        .chars()
        .map(|c| if c.is_alphanumeric() { c } else { '-' })
        .collect::<String>();
    let session_dir = p.data_dir
        .join("home")
        .join(".claude")
        .join("projects")
        .join(&encoded);
    fs::create_dir_all(&session_dir).unwrap();
    let session_file = session_dir.join(format!("{sid}.jsonl"));
    fs::write(&session_file, "{}\n").unwrap();

    let week_ago = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
        - 7 * 86400;
    let _ = Command::new("touch")
        .arg("-d")
        .arg(format!("@{week_ago}"))
        .arg(&session_file)
        .output()
        .unwrap();

    // Trigger the dead-pid sweep via `cav status 3` (per-agent path that
    // calls is_agent_running, but skips the reaper that would delete a
    // trivially-clean row before we can read exited_at back).
    let _ = p.cav(&["status", "3"]);

    // Read back exited_at; should be approximately week_ago, not now.
    let exited_at: f64 = conn
        .query_row(
            "SELECT exited_at FROM agents WHERE agent_id = '3'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let now_secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as f64;
    assert!(
        exited_at < now_secs - 6.0 * 86400.0,
        "exited_at should be near session mtime (week ago), got {exited_at}, now {now_secs}"
    );

    // Cleanup
    let _ = fs::remove_dir_all(session_dir);
}

#[test]
fn continue_after_pre_ttl_exit_still_works() {
    // An agent whose row exists with exited_at = 1 (epoch second) should
    // still be continuable -- old rows are reclaimable, not auto-expired.
    // We can't run cav -c to completion (would need sandbox launch), but
    // we can confirm cav status sees the agent normally.
    let p = Project::new("old-agent-still-visible");
    p.register_agent("3");
    p.push_agent_commits("3", "main", &[("a.txt", "A\n")]);
    let out = p.cav(&["status", "3"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "status should succeed for old agent: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        combined.contains("@3") || combined.contains("agent-3"),
        "status should show agent 3, got: {combined}"
    );
}

// --- Checkout (retarget) ---

#[test]
fn checkout_retargets_agent_branch_in_shared_remote() {
    // `cav checkout develop 1` renames agent-1/main -> agent-1/develop in the
    // shared remote (from outside the cave, specifying agent id explicitly).
    let p = Project::new("checkout-retarget");
    p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    assert!(p.shared_has_ref("refs/heads/agent-1/main"));

    // Create develop branch in the shared remote (from main).
    let main_sha = p.shared_sha("refs/heads/main");
    git(
        &["push", "cave", &format!("{main_sha}:refs/heads/develop")],
        &p.dir,
    );
    git(&["fetch", "cave"], &p.dir);
    assert!(p.host_has_remote_tracking("develop"));

    // Run checkout from host (on main branch, agent id explicit).
    let out = p.cav(&["checkout", "develop", "1"]);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        out.status.code(),
        Some(0),
        "checkout should succeed: {combined}"
    );

    // agent-1/main should be gone, agent-1/develop should exist.
    assert!(
        !p.shared_has_ref("refs/heads/agent-1/main"),
        "old agent branch must be removed: {combined}"
    );
    assert!(
        p.shared_has_ref("refs/heads/agent-1/develop"),
        "new agent branch must exist: {combined}"
    );
}

// --- Push idempotency ---

#[test]
fn push_already_up_to_date_on_second_push() {
    let p = Project::new("push-idem");
    // Make a new commit so first push has something to send.
    fs::write(p.dir.join("x.txt"), "x\n").unwrap();
    git(&["add", "x.txt"], &p.dir);
    git(&["commit", "-m", "add x"], &p.dir);

    let out1 = p.cav(&["push"]);
    let stdout1 = String::from_utf8_lossy(&out1.stdout);
    assert_eq!(
        out1.status.code(),
        Some(0),
        "first push should succeed: {}{}",
        stdout1,
        String::from_utf8_lossy(&out1.stderr)
    );
    assert!(
        stdout1.contains("Pushed"),
        "first push should say 'Pushed': {stdout1}"
    );

    let out2 = p.cav(&["push"]);
    let stdout2 = String::from_utf8_lossy(&out2.stdout);
    assert_eq!(
        out2.status.code(),
        Some(0),
        "second push should succeed: {}{}",
        stdout2,
        String::from_utf8_lossy(&out2.stderr)
    );
    assert!(
        stdout2.contains("Already up to date"),
        "second push should say 'Already up to date': {stdout2}"
    );
}

// --- Partial approve remaining count message ---

#[test]
fn partial_approve_prints_remaining_count() {
    let p = Project::new("partial-remaining");
    let tip = p.push_agent_commits(
        "1",
        "main",
        &[("a.txt", "A\n"), ("b.txt", "B\n"), ("c.txt", "C\n")],
    );
    // Approve only the first commit (two before tip).
    let first = git_out(&["rev-parse", &format!("{tip}^^")], &p.dir);

    let out = p.cav(&["approve", "-y", "1", "--commit", &first]);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        out.status.code(),
        Some(0),
        "partial approve failed: {combined}"
    );
    assert!(
        combined.contains("remain") || combined.contains("future approval"),
        "should mention remaining commits: {combined}"
    );
}
