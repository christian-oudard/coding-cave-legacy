/// Integration tests for branch lifecycle across approve/reject.
use std::fs;
use std::path::{Path, PathBuf};
use std::io::Write;
use std::process::{Command, Output, Stdio};

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
        // Default to "just exited" so the agent stays in the active range
        // (not bumped to 1000+ by archive_sweep). Tests that want a stale
        // agent past the archive threshold use register_agent_exited_at
        // with epoch 1 directly.
        let recent = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        self.register_agent_exited_at(agent_id, recent);
    }

    fn register_agent_exited_at(&self, agent_id: &str, exited_at: f64) {
        self.register_agent_row(agent_id, None, Some(exited_at));
    }

    /// Mark the agent's row as running by pointing host_pid at this
    /// test process (which stays alive for the duration of the test)
    /// and clearing exited_at. Used by tests that exercise the
    /// auto-rebase path, which only fires for running agents.
    fn register_agent_running(&self, agent_id: &str) {
        self.register_agent_row(agent_id, Some(std::process::id()), None);
    }

    fn register_agent_row(
        &self,
        agent_id: &str,
        host_pid: Option<u32>,
        exited_at: Option<f64>,
    ) {
        let slug = self.slug();
        let db_path = self.data_dir.join("coding-cave.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS agents (\
                 agent_id TEXT PRIMARY KEY,\
                 uuid TEXT NOT NULL UNIQUE,\
                 project TEXT NOT NULL,\
                 session_id TEXT UNIQUE,\
                 host_pid INTEGER,\
                 cave_pid INTEGER,\
                 started_at REAL,\
                 exited_at REAL,\
                 extra_projects TEXT);",
        )
        .unwrap();
        let uuid = uuid::Uuid::new_v4().to_string();
        conn.execute(
            "INSERT OR REPLACE INTO agents \
             (agent_id, uuid, project, host_pid, started_at, exited_at) \
             VALUES (?1, ?2, ?3, ?4, 0, ?5)",
            rusqlite::params![agent_id, uuid, slug, host_pid, exited_at],
        )
        .unwrap();
    }

    fn cav_cmd(&self, args: &[&str]) -> Command {
        let mut cmd = Command::new(CAV);
        cmd.args(args)
            .current_dir(&self.dir)
            .env("PATH", hook_path())
            .env("CODING_CAVE_DATA_DIR", &self.data_dir)
            // Per-cave nix stores live at a fixed /var/lib path in production;
            // redirect them under the tmp data dir so the spawned cav never
            // touches the real system store.
            .env("CODING_CAVE_NIX_STORES_DIR", self.nix_stores())
            .env_remove("CODING_CAVE_VERSION")
            .env_remove("CODING_CAVE_AGENT_ID");
        cmd
    }

    fn cav(&self, args: &[&str]) -> Output {
        self.cav_cmd(args).output().expect("cav binary not found")
    }

    /// Run cav with `input` on stdin, for the interactive prompts.
    fn cav_answering(&self, args: &[&str], input: &str) -> Output {
        let mut child = self
            .cav_cmd(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("cav binary not found");
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.as_bytes())
            .unwrap();
        child.wait_with_output().unwrap()
    }

    /// The per-cave nix-store base the spawned cav uses (redirected for tests).
    fn nix_stores(&self) -> PathBuf {
        self.data_dir.join("nix-stores")
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

    fn slug(&self) -> String {
        self.dir
            .file_name()
            .unwrap()
            .to_string_lossy()
            .to_lowercase()
            .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-")
    }

    /// Where the agent's worktree lives: keyed by its uuid, like production.
    /// Falls back to the display id for an unregistered agent.
    fn worktree_dir(&self, agent_id: &str) -> PathBuf {
        let key = self.agent_uuid(agent_id).unwrap_or_else(|| agent_id.to_string());
        self.data_dir.join("worktrees").join(self.slug()).join(key)
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

    fn shared_sha(&self, ref_name: &str) -> String {
        let out = Command::new("git")
            .args(["rev-parse", ref_name])
            .current_dir(self.shared())
            .output()
            .unwrap();
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }

    fn agent_uuid(&self, agent_id: &str) -> Option<String> {
        let db_path = self.data_dir.join("coding-cave.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.query_row(
            "SELECT uuid FROM agents WHERE agent_id = ?1",
            rusqlite::params![agent_id],
            |row| row.get::<_, String>(0),
        )
        .ok()
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

/// PATH with the built binaries' dir first, so the receive hooks in a test
/// repo resolve to the binaries this test run built. The devShell also has an
/// installed cav on PATH; without this the hooks would silently exercise that
/// one instead.
fn hook_path() -> String {
    let bin_dir = Path::new(CAV).parent().unwrap().display().to_string();
    match std::env::var("PATH") {
        Ok(p) => format!("{bin_dir}:{p}"),
        Err(_) => bin_dir,
    }
}

fn git(args: &[&str], dir: &Path) {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .env("PATH", hook_path())
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
        .env("PATH", hook_path())
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
fn approve_menu_lists_each_agent_once_then_asks_which_branch() {
    // An agent with pending work on two bases used to appear twice in the
    // menu under the same id, making the second entry unselectable.
    let p = Project::new("approve-two-branches");
    p.register_agent("1");
    p.register_agent("2");
    git(&["checkout", "-b", "develop"], &p.dir);
    git(&["push", "cave", "develop"], &p.dir);
    git(&["checkout", "main"], &p.dir);
    git(&["fetch", "cave"], &p.dir);
    p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    p.push_agent_commits("1", "develop", &[("b.txt", "B\n")]);
    p.push_agent_commits("2", "main", &[("c.txt", "C\n")]);
    // Sit on a branch that matches no candidate, so the menu appears.
    git(&["checkout", "-b", "side"], &p.dir);

    // Agent 1, its second branch (bases sort alphabetically: develop, main).
    let out = p.cav_answering(&["approve"], "1\n2\ny\n");
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(out.status.code(), Some(0), "approve failed: {combined}");
    assert!(
        combined.contains("[1/2]") && !combined.contains("[1/1/2]"),
        "agent 1 should appear once in the menu: {combined}"
    );
    assert!(
        !p.shared_has_ref("refs/heads/agent-1/main"),
        "agent-1/main should be approved and deleted: {combined}"
    );
    assert!(
        p.shared_has_ref("refs/heads/agent-1/develop"),
        "agent-1/develop should be untouched: {combined}"
    );
    assert!(
        p.shared_has_ref("refs/heads/agent-2/main"),
        "agent-2/main should be untouched: {combined}"
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

#[test]
fn approve_uses_cwd_when_agent_registered_to_other_project() {
    // Regression: approve_reject_base used to look up the agent's home project
    // in the registry and operate there, ignoring the user's CWD. That broke
    // approving an agent's commits from a project where the same agent ID had
    // pending work but was registered (in the global registry) to a different
    // project. CWD must win whenever it's a real coding-cave project.
    let p = Project::new("approve-cwd-wins");
    p.push_agent_commits("7", "main", &[("a.txt", "A\n")]);

    // Redirect agent 7 to a "ghost" project pointing to a non-existent path.
    // With the old behavior, approve would chase that path and fail.
    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let ghost_path = std::env::temp_dir().join("cav-ghost-nonexistent");
    conn.execute(
        "INSERT OR REPLACE INTO projects (slug, path) VALUES (?1, ?2)",
        rusqlite::params!["ghost", ghost_path.to_string_lossy()],
    )
    .unwrap();
    conn.execute(
        "INSERT OR REPLACE INTO agents (agent_id, uuid, project, started_at, exited_at) \
         VALUES (?1, ?2, 'ghost', 0, 1)",
        rusqlite::params!["7", uuid::Uuid::new_v4().to_string()],
    )
    .unwrap();
    drop(conn);

    let out = p.cav(&["approve", "-y", "7"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "approve should use CWD, not chase the ghost project: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !p.shared_has_ref("refs/heads/agent-7/main"),
        "agent branch should be deleted after full approval in CWD project"
    );
}

// --- Reject rolls back main; agent's dev branch is left to the agent ---

#[test]
fn reject_after_full_approve_recreates_branch_at_rejected_tip() {
    let p = Project::new("reject-full");
    p.register_agent("1");
    let approved_tip = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let pre_approve_base = p.shared_sha("refs/heads/main");

    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));
    assert_eq!(p.shared_sha("refs/heads/main"), approved_tip);
    assert!(!p.shared_has_ref("refs/heads/agent-1/main"));

    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert_eq!(
        p.shared_sha("refs/heads/main"),
        pre_approve_base,
        "base rolled back"
    );
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        approved_tip,
        "agent's dev branch recreated at the rejected tip"
    );
    assert!(
        p.host_has_local_branch("agent-1/main"),
        "host's local branch ref recreated to track cave"
    );
}

#[test]
fn reject_allows_non_running_agent_id() {
    // Reject validates that the agent-id exists in the registry.
    // A missing id is the only condition that fails. There is no check
    // for a running process (exited_at is set, host_pid NULL below).
    let p = Project::new("reject-nonrunning");
    p.register_agent("1");
    let _ = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let _ = p.cav(&["approve", "-y", "1"]);

    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject without running agent: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
fn approve_reject_recreates_dev_branch_for_torn_down_agent() {
    let p = Project::new("cycle");
    p.register_agent("1");
    let tip = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);

    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));
    assert!(!p.shared_has_ref("refs/heads/agent-1/main"));

    assert_eq!(p.cav(&["reject", "-y"]).status.code(), Some(0));
    // Reject recreated the dev branch at the rejected tip so the agent
    // can continue iterating on the work.
    assert_eq!(p.shared_sha("refs/heads/agent-1/main"), tip);
    assert!(p.host_has_local_branch("agent-1/main"));
}

// --- Reject with built-on-top branch proceeds (the FF-from-new-main case) ---

#[test]
fn reject_after_partial_approve_proceeds() {
    // Partial approve of A leaves agent-1/main at B (B built on A on init).
    // After rejecting A, main = init. B is still a descendant of init, so
    // approve later can FF main forward to B. Reject is allowed; agent's
    // branch is left untouched.
    let p = Project::new("partial-reject");
    p.register_agent("1");
    let tip = p.push_agent_commits(
        "1",
        "main",
        &[("a.txt", "A\n"), ("b.txt", "B\n")],
    );
    let first = git_out(&["rev-parse", &format!("{tip}^")], &p.dir);
    let init = p.shared_sha("refs/heads/main");

    assert_eq!(
        p.cav(&["approve", "-y", "1", "--commit", &first]).status.code(),
        Some(0)
    );

    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject should proceed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert_eq!(p.shared_sha("refs/heads/main"), init, "main rolled back");
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        tip,
        "agent's branch left as-is"
    );
}

#[test]
fn reject_proceeds_when_original_agent_was_gc_d() {
    // After the original committer's registry row was reaped, full approve
    // had also torn down their dev branch. Reject still works: there's no
    // branch left to FF-check, so no block fires.
    let p = Project::new("reject-removed");
    p.register_agent("1");
    let tip_a = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let pre_a = p.shared_sha("refs/heads/main");
    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));
    assert_eq!(p.shared_sha("refs/heads/main"), tip_a);

    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute("DELETE FROM agents WHERE agent_id = '1'", [])
        .unwrap();

    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject should proceed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(p.shared_sha("refs/heads/main"), pre_a);
}

#[test]
fn reject_explicit_agent_id_errors_when_not_in_registry() {
    // Typo protection: explicit `cav reject <id>` validates the id exists.
    let p = Project::new("reject-explicit-typo");
    p.register_agent("1");
    let _ = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));

    let out = p.cav(&["reject", "-y", "9999"]);
    assert_ne!(out.status.code(), Some(0));
    let err = String::from_utf8_lossy(&out.stderr).to_string();
    assert!(
        err.contains("Agent 9999 not found in registry"),
        "expected typo-protection error, got: {err}"
    );
}

#[test]
fn reject_blocks_when_agent_branch_diverged_from_new_main() {
    // Agent's branch is on a totally separate history (orphan commit) so
    // it shares no FF-able relationship with the rolled-back main. Reject
    // must block and direct the operator to have the agent rebase.
    let p = Project::new("reject-diverged");
    p.register_agent("1");

    let tip_a = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));

    // Create a parentless commit and point cave/agent-1/main at it.
    let tree = git_out(&["write-tree"], &p.dir);
    let orphan = git_out(&["commit-tree", &tree, "-m", "orphan"], &p.dir);
    git(
        &["push", "cave", &format!("{orphan}:refs/heads/agent-1/main")],
        &p.dir,
    );
    git(&["fetch", "cave"], &p.dir);

    let out = p.cav(&["reject", "-y"]);
    assert_ne!(out.status.code(), Some(0), "reject should be blocked");
    let err = String::from_utf8_lossy(&out.stderr).to_string();
    assert!(
        err.contains("diverged from where main will be"),
        "expected divergence explanation, got: {err}"
    );
    assert!(
        err.contains("git rebase"),
        "expected rebase guidance, got: {err}"
    );

    // Nothing changed: main still at the approved tip, branch still orphan.
    assert_eq!(p.shared_sha("refs/heads/main"), tip_a);
    assert_eq!(p.shared_sha("refs/heads/agent-1/main"), orphan);
}

#[test]
fn reject_proceeds_when_agent_branch_is_unrelated() {
    // Setup: agent commits A, gets approved, then re-creates their branch
    // at a sibling commit W that doesn't include A in its history.
    let p = Project::new("reject-unrelated");
    p.register_agent("1");

    let tip_a = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let pre_a = git_out(&["rev-parse", &format!("{tip_a}^")], &p.dir);
    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));

    git(&["checkout", "-B", "tmp-w", &pre_a], &p.dir);
    fs::write(p.dir.join("w.txt"), "W\n").unwrap();
    git(&["add", "w.txt"], &p.dir);
    git(&["commit", "-m", "agent 1: w"], &p.dir);
    let tip_w = git_out(&["rev-parse", "HEAD"], &p.dir);
    git(&["push", "cave", "tmp-w:refs/heads/agent-1/main"], &p.dir);
    git(&["checkout", "main"], &p.dir);
    git(&["fetch", "cave"], &p.dir);

    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject should proceed when branch is on a sibling: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(p.shared_sha("refs/heads/main"), pre_a);
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        tip_w,
        "agent's branch must be left as-is"
    );
}

#[test]
fn reject_proceeds_when_agent_branch_is_at_or_behind_new_base() {
    // Agent's branch is at an ancestor of cave/main (already merged by the
    // reconciler's lights). At reject time the reconciler deletes the
    // branch, so the cutoff sees no branch to check; the Create case
    // then recreates the branch at the rejected tip.
    let p = Project::new("reject-behind");
    p.register_agent("1");

    let tip_a = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let pre_a = git_out(&["rev-parse", &format!("{tip_a}^")], &p.dir);
    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));

    git(
        &["push", "cave", &format!("{pre_a}:refs/heads/agent-1/main")],
        &p.dir,
    );
    git(&["fetch", "cave"], &p.dir);

    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "reject should proceed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(p.shared_sha("refs/heads/main"), pre_a);
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        tip_a,
        "Create case recreated the branch at the rejected tip"
    );
}

#[test]
fn reject_retries_successfully_after_agent_rebases_away_divergence() {
    // Once-blocked reject can retry after the agent rebases their branch.
    // Verifies the approval row is preserved on the blocked reject so retry
    // sees the same approval, and that the FF check passes after the agent
    // moves their branch onto the rolled-back main.
    let p = Project::new("reject-retry-after-rebase");
    p.register_agent("1");

    let tip_a = p.push_agent_commits("1", "main", &[("a.txt", "A\n")]);
    let pre_a = git_out(&["rev-parse", &format!("{tip_a}^")], &p.dir);
    assert_eq!(p.cav(&["approve", "-y", "1"]).status.code(), Some(0));

    // Diverged state: parentless orphan on agent-1/main.
    let tree = git_out(&["write-tree"], &p.dir);
    let orphan = git_out(&["commit-tree", &tree, "-m", "orphan"], &p.dir);
    git(
        &["push", "cave", &format!("{orphan}:refs/heads/agent-1/main")],
        &p.dir,
    );
    git(&["fetch", "cave"], &p.dir);

    // First attempt is blocked; nothing changed.
    let out = p.cav(&["reject", "-y"]);
    assert_ne!(out.status.code(), Some(0));
    assert_eq!(p.shared_sha("refs/heads/main"), tip_a);
    assert_eq!(p.shared_sha("refs/heads/agent-1/main"), orphan);

    // Agent rebases their branch onto pre_a (the future main).
    git(
        &["push", "-f", "cave", &format!("{pre_a}:refs/heads/agent-1/main")],
        &p.dir,
    );
    git(&["fetch", "cave"], &p.dir);

    // Retry succeeds.
    let out = p.cav(&["reject", "-y"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "retry should succeed after rebase: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(p.shared_sha("refs/heads/main"), pre_a);
}

// --- Pull's FF-only sync protects host-side rewrites ---

#[test]
fn pull_does_not_overwrite_cave_when_worktree_diverged() {
    // After a host-side rewrite of cave/agent-N/main (e.g. via cav reject),
    // the agent's worktree may still hold the pre-rewrite tip. The host's
    // sync_agent_to_shared must not force-push that stale tip back over
    // cave's new state.
    let p = Project::new("pull-divergent");
    p.register_agent("1");

    // Worktree main = V (unrelated to anything on host).
    let ws = p.worktree_dir("1");
    fs::create_dir_all(&ws).unwrap();
    git(&["init", "-b", "main"], &ws);
    git(&["config", "user.email", "test@test.com"], &ws);
    git(&["config", "user.name", "Test"], &ws);
    fs::write(ws.join("v.txt"), "V\n").unwrap();
    git(&["add", "v.txt"], &ws);
    git(&["commit", "-m", "v"], &ws);
    let tip_v = git_out(&["rev-parse", "HEAD"], &ws);

    // Cave/agent-1/main = W (different commit).
    git(&["checkout", "-B", "tmp-w"], &p.dir);
    fs::write(p.dir.join("w.txt"), "W\n").unwrap();
    git(&["add", "w.txt"], &p.dir);
    git(&["commit", "-m", "w"], &p.dir);
    let tip_w = git_out(&["rev-parse", "HEAD"], &p.dir);
    git(&["push", "cave", "tmp-w:refs/heads/agent-1/main"], &p.dir);
    git(&["fetch", "cave"], &p.dir);
    git(&["checkout", "-B", "agent-1/main", "cave/agent-1/main"], &p.dir);

    let out = p.cav(&["pull"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "pull failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        tip_w,
        "FF-only sync must keep cave at W, not overwrite with worktree's V"
    );
    assert_eq!(git_out(&["rev-parse", "main"], &ws), tip_v);
}

/// Helper: clone cave's bare repo into the worktree-dir for `agent_id` and
/// check out `main`, matching the shape `cav agent` would produce.
fn seed_agent_worktree(p: &Project, agent_id: &str) {
    let ws = p.worktree_dir(agent_id);
    fs::create_dir_all(&ws).unwrap();
    git(&["init", "-q", "-b", "main"], &ws);
    git(&["config", "user.email", "test@test.com"], &ws);
    git(&["config", "user.name", "Test"], &ws);
    git(
        &[
            "remote",
            "add",
            "host-cave",
            p.shared().to_str().unwrap(),
        ],
        &ws,
    );
    git(&["fetch", "-q", "host-cave"], &ws);
    git(&["checkout", "-q", "-B", "main", "host-cave/main"], &ws);
}

/// Helper: simulate the agent making a commit in their worktree and
/// pushing it to cave/agent-N/main (host-side push, host-cave remote).
fn agent_commit_and_push(p: &Project, agent_id: &str, file: &str, content: &str) -> String {
    let ws = p.worktree_dir(agent_id);
    fs::write(ws.join(file), content).unwrap();
    git(&["add", file], &ws);
    git(
        &["commit", "-m", &format!("agent {agent_id}: {file}")],
        &ws,
    );
    let tip = git_out(&["rev-parse", "HEAD"], &ws);
    git(
        &[
            "push",
            "-f",
            "host-cave",
            &format!("main:refs/heads/agent-{agent_id}/main"),
        ],
        &ws,
    );
    tip
}

/// Returns true if `ancestor` is an ancestor of `descendant` within `cwd`.
fn is_ancestor(ancestor: &str, descendant: &str, cwd: &Path) -> bool {
    Command::new("git")
        .args(["merge-base", "--is-ancestor", ancestor, descendant])
        .current_dir(cwd)
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[test]
fn reject_drops_rejected_commits_from_secondary_agent_worktrees() {
    // Scenario:
    //   1. agent 1 commits CA, doesn't get approved (touches a.txt).
    //   2. agent 2 commits CB (touches b.txt — no conflict with CA),
    //      cav approve advances main to CB and auto-rebases agent 1 on
    //      top of CB. Agent 1's worktree main is now CB + CA'.
    //   3. cav reject CB. main rolls back to before_CB. Agent 1's
    //      worktree is left at CB + CA' — still containing the rejected
    //      commit.
    //   4. agent 2 amends CB into CB' that touches the same file as CA
    //      (a.txt with different content).
    //   5. cav approve agent 2 — main FFs to CB' and auto-rebases agent 1
    //      onto CB'. CA' replays a.txt creation but CB' already created
    //      a.txt, so the rebase conflicts.
    //
    // Test asserts the desired behavior: step 5 produces no rebase
    // conflict. Currently fails because cav reject doesn't run the
    // equivalent of approve's rebase_agents step (which would have
    // dropped CB from agent 1's history at step 3).
    let p = Project::new("reject-secondary-rebase");
    // Both agents must be running for the auto-rebase to fire — that's
    // the scenario this test represents (live agents continuing work
    // through approve/reject churn).
    p.register_agent_running("1");
    p.register_agent_running("2");
    seed_agent_worktree(&p, "1");
    seed_agent_worktree(&p, "2");

    // Step 1: agent 1 commits CA (creates a.txt).
    let tip_ca = agent_commit_and_push(&p, "1", "a.txt", "A\n");

    // Step 2: agent 2 commits CB (creates b.txt, doesn't conflict with CA).
    let tip_cb = agent_commit_and_push(&p, "2", "b.txt", "B\n");
    assert_eq!(p.cav(&["approve", "-y", "2"]).status.code(), Some(0));
    assert_eq!(p.shared_sha("refs/heads/main"), tip_cb);

    // Approve auto-rebased agent 1's worktree onto CB.
    let ws_a = p.worktree_dir("1");
    assert!(
        is_ancestor(&tip_cb, "HEAD", &ws_a),
        "expected agent 1's worktree to be rebased onto CB after approve"
    );
    assert!(ws_a.join("a.txt").exists(), "agent 1 still has its work");

    // Step 3: reject CB. With the fix, agent 1's worktree gets the
    // rejected commits dropped via rebase --onto. Their work content
    // (a.txt) is preserved.
    assert_eq!(p.cav(&["reject", "-y"]).status.code(), Some(0));
    assert!(
        ws_a.join("a.txt").exists(),
        "agent 1's CA work content (a.txt) must survive reject"
    );

    // Step 4: agent 2 amends CB into CB'. CB' touches the same file CB
    // touched (b.txt) but with different content — so replaying CB on
    // top of CB' will conflict. CA is on a different file (a.txt) and
    // would NOT conflict if CB were dropped from agent 1's history.
    let ws_b = p.worktree_dir("2");
    git(&["reset", "--hard", "host-cave/main"], &ws_b);
    fs::write(ws_b.join("b.txt"), "B-amended\n").unwrap();
    git(&["add", "b.txt"], &ws_b);
    git(&["commit", "-m", "agent 2: amended (touches b.txt)"], &ws_b);
    git(
        &["push", "-f", "host-cave", "main:refs/heads/agent-2/main"],
        &ws_b,
    );

    // Step 5: approve agent 2's amended commit. The auto-rebase of
    // agent 1 onto CB' should succeed cleanly.
    let out = p.cav(&["approve", "-y", "2"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "approve should succeed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        !stdout.contains("conflicts") && !stderr.contains("conflicts"),
        "expected clean rebase of agent 1's worktree, got conflicts:\nstdout: {stdout}\nstderr: {stderr}"
    );
}

// --- Reaper handles crashes that skipped teardown ---

#[test]
fn cav_status_preserves_exited_agent() {
    // Previously, a "trivially clean" exited agent (no branches, no
    // approvals) was reaped by `cav status`. That heuristic discarded
    // sessions whose only artifact was the conversation log, so it was
    // removed entirely. Now status leaves every exited agent in place
    // until explicit `cav gc` or `cav remove-agent`.
    let p = Project::new("status-preserves-exited");
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
        p.agent_registered("7"),
        "exited agent must survive cav status"
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
             uuid TEXT NOT NULL UNIQUE,\
             project TEXT NOT NULL,\
             session_id TEXT UNIQUE,\
             host_pid INTEGER,\
             cave_pid INTEGER,\
             started_at REAL,\
             exited_at REAL,\
             extra_projects TEXT);",
    )
    .unwrap();
    conn.execute(
        "INSERT OR REPLACE INTO agents \
         (agent_id, uuid, project, session_id, host_pid, started_at, exited_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, 0, NULL)",
        rusqlite::params!["3", uuid::Uuid::new_v4().to_string(), slug, sid, dead_pid],
    )
    .unwrap();

    // Create a fake session jsonl with mtime backdated 7 days, in the
    // permanent store at data_dir/sessions/<session-id>.jsonl. That record is
    // hardlinked to the live transcript, so its mtime tracks the session
    // whichever agent directory the transcript itself lives in.
    let session_dir = p.data_dir.join("sessions");
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

#[test]
fn acquire_wipes_stale_branches_on_slot_reuse() {
    // When acquire_agent_id reuses a slot, the previous occupant's bare-repo
    // refs (refs/heads/agent-N/* and refs/cav/agent-tip/agent-N/*) must be
    // wiped so the reused display int doesn't hand the new agent a stale branch
    // or force-push tip. The stale worktree is NOT removed here — it is keyed by
    // the old occupant's uuid, so slot reuse can't collide with it, and its disk
    // is reclaimed by `cav gc`. We trigger acquire by invoking `cav agent`; it
    // bails at load_sandbox_config (no CODING_CAVE_BUILD_CONFIG) only after the
    // ref cleanup has run.
    let p = Project::new("reclaim-stale-state");

    // Seed: an old agent-1 row marked exited well past the reclaim cutoff.
    p.register_agent_exited_at("1", 1.0);

    // Stale worktree dir at the old occupant's uuid path.
    let ws = p.worktree_dir("1");
    fs::create_dir_all(&ws).unwrap();
    fs::write(ws.join("MARKER"), "from a previous incarnation\n").unwrap();

    // Stale agent-1/main branch + agent-tip ref in the bare repo.
    let main_sha = git_out(&["rev-parse", "main"], &p.dir);
    git(
        &["push", "cave", &format!("{main_sha}:refs/heads/agent-1/main")],
        &p.dir,
    );
    git(
        &[
            "update-ref",
            "refs/cav/agent-tip/agent-1/main",
            &main_sha,
        ],
        &p.shared(),
    );
    assert!(p.shared_has_ref("refs/heads/agent-1/main"));
    assert!(p.shared_has_ref("refs/cav/agent-tip/agent-1/main"));

    // Trigger acquire_agent_id. Expected to fail later in run_agent without
    // a sandbox config; that's fine — the ref wipe runs first.
    let _ = p.cav(&["agent"]);

    // The uuid-keyed worktree is left for `cav gc`, not removed on reuse.
    assert!(
        ws.exists(),
        "uuid-keyed worktree should be left in place for cav gc; {} vanished",
        ws.display()
    );
    assert!(
        !p.shared_has_ref("refs/heads/agent-1/main"),
        "stale agent-1/main branch should be removed from bare repo"
    );
    assert!(
        !p.shared_has_ref("refs/cav/agent-tip/agent-1/main"),
        "stale agent-tip ref should be removed from bare repo"
    );
}

#[test]
fn acquire_assigns_uuid_to_new_agent() {
    // acquire_agent_id stamps every new row with a v4 UUID. cav agent runs
    // acquire first, then bails at load_sandbox_config — that's enough to
    // populate the row.
    let p = Project::new("uuid-acquire");
    let _ = p.cav(&["agent"]);
    let uuid = p
        .agent_uuid("1")
        .expect("acquire should have created agent 1");
    assert_eq!(
        uuid.len(),
        36,
        "uuid should be a standard v4 string (8-4-4-4-12), got: {uuid}"
    );
    assert!(uuid.contains('-'), "uuid should contain dashes: {uuid}");
}

// --- Archive bump ---

#[test]
fn acquire_bumps_old_paused_agent_to_archive_range() {
    // Paused agent at display int 3 with exited_at well past 24h should be
    // renamed to 1000+ during the next acquire. The worktree (keyed by
    // uuid) stays put; the agent-3/* refs in the bare repo are wiped; the
    // bumped agent is reachable as agent 1000 (or next free in that range).
    let p = Project::new("archive-bump-old");
    p.register_agent_exited_at("3", 1.0);
    let main_sha = git_out(&["rev-parse", "main"], &p.dir);
    git(
        &["push", "cave", &format!("{main_sha}:refs/heads/agent-3/main")],
        &p.dir,
    );
    assert!(p.shared_has_ref("refs/heads/agent-3/main"));

    let _ = p.cav(&["agent"]);

    assert!(
        !p.agent_registered("3"),
        "old display int 3 should be vacated by the bump"
    );
    assert!(
        p.agent_registered("1000"),
        "bumped agent should land at 1000"
    );
    assert!(
        !p.shared_has_ref("refs/heads/agent-3/main"),
        "agent-3/* refs should be wiped on bump (branches are interface)"
    );
}

#[test]
fn acquire_does_not_bump_recently_paused_agent() {
    let p = Project::new("archive-bump-recent");
    let recent = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
        - 60.0;
    p.register_agent_exited_at("3", recent);

    let _ = p.cav(&["agent"]);

    assert!(
        p.agent_registered("3"),
        "recently-paused agent must keep its display int"
    );
    assert!(
        !p.agent_registered("1000"),
        "no archived agent should have been minted"
    );
}

#[test]
fn cav_status_bumps_old_paused_agents() {
    // cav status calls archive_sweep too, so a stale paused agent gets
    // rotated even without an acquire.
    let p = Project::new("archive-bump-status");
    p.register_agent_exited_at("4", 1.0);

    let out = p.cav(&["status"]);
    assert_eq!(out.status.code(), Some(0));

    assert!(!p.agent_registered("4"), "status should bump stale agent 4");
    assert!(p.agent_registered("1000"), "bumped agent should land at 1000");

    // The user gets a one-line notice on stderr so the display int rotation
    // isn't silent. Strip ANSI before matching.
    let stderr = strip_ansi(&String::from_utf8_lossy(&out.stderr));
    assert!(
        stderr.contains("Archived agent 4") && stderr.contains("1000"),
        "expected 'Archived agent 4 → 1000' notice on stderr, got: {stderr}"
    );
}

fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            // Skip until 'm' (end of CSI sequence).
            for c2 in chars.by_ref() {
                if c2 == 'm' {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

// --- cav gc ---

#[test]
fn gc_removes_worktree_and_branches_for_old_paused_agent() {
    let p = Project::new("gc-old-paused");
    // Exited at epoch 1 -> well past any sensible --older-than default.
    p.register_agent_exited_at("5", 1.0);
    let ws = p.worktree_dir("5");
    fs::create_dir_all(&ws).unwrap();
    fs::write(ws.join("MARKER"), "old\n").unwrap();
    let main_sha = git_out(&["rev-parse", "main"], &p.dir);
    git(
        &["push", "cave", &format!("{main_sha}:refs/heads/agent-5/main")],
        &p.dir,
    );
    git(
        &[
            "update-ref",
            "refs/cav/agent-tip/agent-5/main",
            &main_sha,
        ],
        &p.shared(),
    );

    let out = p.cav(&["gc"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "gc failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(!ws.exists(), "worktree dir should be removed by gc");
    assert!(
        !p.shared_has_ref("refs/heads/agent-5/main"),
        "agent branch should be removed by gc"
    );
    assert!(
        !p.shared_has_ref("refs/cav/agent-tip/agent-5/main"),
        "agent-tip ref should be removed by gc"
    );
    assert!(
        p.agent_registered("5"),
        "registry row must survive gc (only artifacts are reclaimed)"
    );
}

#[test]
fn gc_preserves_recent_paused_agent() {
    let p = Project::new("gc-recent-preserve");
    // Exited 5 minutes ago -> inside the default 24h window.
    let recent = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
        - 5.0 * 60.0;
    p.register_agent_exited_at("5", recent);
    let ws = p.worktree_dir("5");
    fs::create_dir_all(&ws).unwrap();
    fs::write(ws.join("MARKER"), "recent\n").unwrap();

    let out = p.cav(&["gc"]);
    assert_eq!(out.status.code(), Some(0));

    assert!(
        ws.exists(),
        "recently-paused agent's worktree must survive default gc"
    );
    assert!(p.agent_registered("5"));
}

#[test]
fn gc_dry_run_reports_but_removes_nothing() {
    let p = Project::new("gc-dry-run");
    p.register_agent_exited_at("5", 1.0);
    let ws = p.worktree_dir("5");
    fs::create_dir_all(&ws).unwrap();
    fs::write(ws.join("MARKER"), "x\n").unwrap();
    let main_sha = git_out(&["rev-parse", "main"], &p.dir);
    git(
        &["push", "cave", &format!("{main_sha}:refs/heads/agent-5/main")],
        &p.dir,
    );

    let out = p.cav(&["gc", "--dry-run"]);
    assert_eq!(out.status.code(), Some(0));

    assert!(ws.exists(), "dry-run must not touch the worktree");
    assert!(
        p.shared_has_ref("refs/heads/agent-5/main"),
        "dry-run must not touch shared refs"
    );
}

// --- cav remove-agent ---

#[test]
fn remove_agent_drops_worktree_and_cave_store() {
    let p = Project::new("ra-drops-store");
    p.register_agent("1");
    let uuid = p.agent_uuid("1").unwrap();

    let ws = p.worktree_dir("1");
    fs::create_dir_all(&ws).unwrap();
    fs::write(ws.join("MARKER"), "x\n").unwrap();

    // Seed a per-cave nix store dir; remove-agent should reclaim it
    // alongside the worktree.
    let store = p.nix_stores().join(format!("agent-{uuid}"));
    fs::create_dir_all(store.join("nix").join("store")).unwrap();
    fs::write(store.join("nix").join("store").join("FILE"), "y\n").unwrap();

    let out = p.cav(&["remove-agent", "1", "--yes"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "remove-agent failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(!ws.exists(), "worktree dir must be reclaimed");
    assert!(!store.exists(), "cave store dir must be reclaimed");
    assert!(!p.agent_registered("1"), "registry row must be dropped");
}

#[test]
fn remove_agent_refuses_running_even_with_missing_project_dir() {
    // Previously the project-dir-missing fast path skipped the is_running
    // check and dropped the row out from under a live process.
    let p = Project::new("ra-running-missing-dir");
    let slug = p.slug();
    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let uuid = uuid::Uuid::new_v4().to_string();
    // Register against a slug whose path won't resolve to a real dir.
    conn.execute(
        "INSERT OR REPLACE INTO projects (slug, path) VALUES (?1, ?2)",
        rusqlite::params!["ghost", "/nonexistent/ghost"],
    )
    .unwrap();
    conn.execute(
        "INSERT OR REPLACE INTO agents \
         (agent_id, uuid, project, host_pid, started_at, exited_at) \
         VALUES ('9', ?1, 'ghost', ?2, 0, NULL)",
        rusqlite::params![uuid, std::process::id()],
    )
    .unwrap();
    drop(conn);
    let _ = slug;

    let out = p.cav(&["remove-agent", "9", "--yes"]);
    assert_ne!(
        out.status.code(),
        Some(0),
        "should refuse: agent is running"
    );
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        err.contains("running"),
        "error should mention running, got: {err}"
    );

    // Row must still exist.
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let exists = conn
        .query_row(
            "SELECT 1 FROM agents WHERE agent_id = ?1",
            rusqlite::params!["9"],
            |_| Ok(()),
        )
        .is_ok();
    assert!(exists, "running agent must survive the refusal");
}

// --- cav remove-project ---

#[test]
fn remove_project_drops_owned_agents_and_metadata() {
    let p = Project::new("rp-owned-agents");
    let slug = p.slug();
    p.register_agent("1");
    p.register_agent("2");
    let ws1 = p.worktree_dir("1");
    let ws2 = p.worktree_dir("2");
    fs::create_dir_all(&ws1).unwrap();
    fs::create_dir_all(&ws2).unwrap();
    fs::write(ws1.join("MARKER"), "a\n").unwrap();
    fs::write(ws2.join("MARKER"), "b\n").unwrap();

    let main_sha = git_out(&["rev-parse", "main"], &p.dir);
    git(
        &["push", "cave", &format!("{main_sha}:refs/heads/agent-1/main")],
        &p.dir,
    );

    let cav_meta = p.dir.join(".coding-cave");
    assert!(cav_meta.is_dir(), "test precondition");
    let src_marker = p.dir.join("README.md");
    assert!(src_marker.is_file(), "test precondition");

    let out = p.cav(&["remove-project", &slug, "--yes"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "remove-project failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(!ws1.exists(), "agent 1 worktree should be gone");
    assert!(!ws2.exists(), "agent 2 worktree should be gone");
    assert!(!p.agent_registered("1"), "agent 1 row should be dropped");
    assert!(!p.agent_registered("2"), "agent 2 row should be dropped");
    assert!(
        !cav_meta.exists(),
        ".coding-cave/ metadata dir should be removed"
    );
    assert!(
        src_marker.is_file(),
        "source files must NOT be touched"
    );
}

#[test]
fn remove_project_refuses_when_agent_running() {
    let p = Project::new("rp-running");
    let slug = p.slug();
    // Pose as running by setting host_pid to this test's own pid (alive).
    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let uuid = uuid::Uuid::new_v4().to_string();
    conn.execute(
        "INSERT OR REPLACE INTO agents \
         (agent_id, uuid, project, host_pid, started_at, exited_at) \
         VALUES ('1', ?1, ?2, ?3, 0, NULL)",
        rusqlite::params![uuid, slug, std::process::id()],
    )
    .unwrap();
    drop(conn);

    let out = p.cav(&["remove-project", &slug, "--yes"]);
    assert_ne!(out.status.code(), Some(0), "should refuse with running agent");
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        err.contains("running agents") || err.contains("running"),
        "error should mention running agents, got: {err}"
    );

    assert!(p.agent_registered("1"), "agent must survive refusal");
}

#[test]
fn rename_project_refuses_when_agent_running() {
    let p = Project::new("rp-rename-running");
    let slug = p.slug();
    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let uuid = uuid::Uuid::new_v4().to_string();
    conn.execute(
        "INSERT OR REPLACE INTO agents \
         (agent_id, uuid, project, host_pid, started_at, exited_at) \
         VALUES ('1', ?1, ?2, ?3, 0, NULL)",
        rusqlite::params![uuid, slug, std::process::id()],
    )
    .unwrap();
    drop(conn);

    // Directory already moved; rename-project should still refuse because
    // an agent is running, and leave the registry untouched.
    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("running-{}", std::process::id()));
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_ne!(out.status.code(), Some(0));

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let project: String = conn
        .query_row(
            "SELECT project FROM agents WHERE agent_id = '1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(project, slug, "registry must be untouched on refusal");
    let _ = fs::remove_dir_all(&new_path);
}

#[test]
fn rename_project_after_manual_move_cascades_slug() {
    let p = Project::new("rp-rename-moved");
    let old_slug = p.slug();
    p.register_agent("1");
    let old_ws = p.worktree_dir("1");
    fs::create_dir_all(&old_ws).unwrap();
    fs::write(old_ws.join("MARKER"), "x\n").unwrap();
    let uuid = p
        .agent_uuid("1")
        .expect("registered agent should have uuid");

    // User moves the source dir outside cav (a plain `mv`); .coding-cave
    // travels with it. The registry now points at a missing old path.
    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("moved-{}", std::process::id()));
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &old_slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_eq!(
        out.status.code(),
        Some(0),
        "rename failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let new_slug = new_path
        .file_name()
        .unwrap()
        .to_string_lossy()
        .to_lowercase()
        .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-");
    assert_ne!(old_slug, new_slug);

    // Worktree dir follows the slug; agent history (the MARKER) survives.
    let new_ws = p.data_dir.join("worktrees").join(&new_slug).join(&uuid);
    assert!(!old_ws.exists(), "old worktree dir should be moved");
    assert!(new_ws.join("MARKER").is_file(), "worktree content preserved");

    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let project: String = conn
        .query_row(
            "SELECT project FROM agents WHERE agent_id = ?1",
            rusqlite::params!["1"],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(project, new_slug);
    let registered: String = conn
        .query_row(
            "SELECT path FROM projects WHERE slug = ?1",
            rusqlite::params![new_slug],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(registered, new_path.canonicalize().unwrap().to_str().unwrap());

    let _ = fs::remove_dir_all(&new_path);
}

#[test]
fn rename_project_carries_memory_and_agent_transcripts_to_the_new_slug() {
    let p = Project::new("rp-rename-memory");
    let old_slug = p.slug();
    p.register_agent("1");
    let uuid = p
        .agent_uuid("1")
        .expect("registered agent should have uuid");

    let encoded = |slug: &str| format!("-projects-{slug}");
    let old_memory = p.data_dir.join("memory").join(&old_slug);
    fs::create_dir_all(&old_memory).unwrap();
    fs::write(old_memory.join("fact.md"), "---\nname: fact\n---\nbody\n").unwrap();

    let old_transcripts = p
        .data_dir
        .join("claude/agents")
        .join(&uuid)
        .join("projects")
        .join(encoded(&old_slug));
    fs::create_dir_all(&old_transcripts).unwrap();
    fs::write(old_transcripts.join("sess-1.jsonl"), "{}\n").unwrap();

    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("moved-mem-{}", std::process::id()));
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &old_slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_eq!(
        out.status.code(),
        Some(0),
        "rename failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let new_slug = new_path
        .file_name()
        .unwrap()
        .to_string_lossy()
        .to_lowercase()
        .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-");
    assert_ne!(old_slug, new_slug);

    let new_memory = p.data_dir.join("memory").join(&new_slug);
    assert!(!old_memory.exists(), "old memory dir should be moved");
    assert!(new_memory.join("fact.md").is_file(), "memory should follow the rename");

    let new_transcripts = p
        .data_dir
        .join("claude/agents")
        .join(&uuid)
        .join("projects")
        .join(encoded(&new_slug));
    assert!(!old_transcripts.exists(), "old transcript dir should be moved");
    assert!(
        new_transcripts.join("sess-1.jsonl").is_file(),
        "agent transcript should follow the rename, or the resume picker goes empty",
    );

    let _ = fs::remove_dir_all(&new_path);
}

#[test]
fn rename_project_reminds_when_not_moved_yet() {
    let p = Project::new("rp-rename-not-moved");
    let old_slug = p.slug();

    // Directory still at the old path, target doesn't exist yet: cav should
    // remind the user to move it first, and change nothing.
    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("not-moved-{}", std::process::id()));
    assert!(!new_path.exists());

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &old_slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&p.data_dir)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_ne!(out.status.code(), Some(0));
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        err.contains("not moved yet"),
        "error should remind the user to move the dir, got: {err}"
    );
    assert!(p.dir.is_dir(), "source must be untouched");
    assert!(!new_path.exists());
}

#[test]
fn rename_project_errors_when_neither_path_exists() {
    let p = Project::new("rp-rename-neither");
    let old_slug = p.slug();

    // Old dir gone, new dir never created: the names match nothing on disk.
    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("ghost-{}", std::process::id()));
    fs::remove_dir_all(&p.dir).unwrap();

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &old_slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&p.data_dir)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_ne!(out.status.code(), Some(0));
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        err.contains("not found") || err.contains("Neither"),
        "error should report no matching directory, got: {err}"
    );
}

#[test]
fn rename_project_refuses_slug_collision() {
    let p = Project::new("rp-rename-collide");
    let old_slug = p.slug();

    // A different project already owns the target slug.
    let other = p.data_dir.join("other-proj");
    fs::create_dir_all(&other).unwrap();
    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute(
        "INSERT INTO projects (slug, path) VALUES ('taken', ?1)",
        rusqlite::params![other.to_str().unwrap()],
    )
    .unwrap();
    drop(conn);

    // Move our project to a dir whose name slugs to the taken slug.
    let new_path = p.dir.parent().unwrap().join("taken");
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &old_slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_ne!(out.status.code(), Some(0));
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        err.contains("already registered"),
        "error should report the slug collision, got: {err}"
    );
    let _ = fs::remove_dir_all(&new_path);
    let _ = fs::remove_dir_all(&other);
}

#[test]
fn rename_project_adopts_empty_duplicate_from_cav_init() {
    let p = Project::new("rp-rename-adopt");
    let old_slug = p.slug();
    p.register_agent("1");
    let old_ws = p.worktree_dir("1");
    fs::create_dir_all(&old_ws).unwrap();
    fs::write(old_ws.join("MARKER"), "x\n").unwrap();
    let uuid = p
        .agent_uuid("1")
        .expect("registered agent should have uuid");

    // User moved the dir, then ran cav/cav init in it, which auto-registered
    // an empty duplicate project pointing at the same directory.
    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("adopt-{}", std::process::id()));
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();
    let new_slug = new_path
        .file_name()
        .unwrap()
        .to_string_lossy()
        .to_lowercase()
        .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-");
    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute(
        "INSERT INTO projects (slug, path) VALUES (?1, ?2)",
        rusqlite::params![new_slug, new_path.canonicalize().unwrap().to_str().unwrap()],
    )
    .unwrap();
    drop(conn);

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &old_slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_eq!(
        out.status.code(),
        Some(0),
        "adopt failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    // History migrated onto the adopted slug.
    let new_ws = p.data_dir.join("worktrees").join(&new_slug).join(&uuid);
    assert!(new_ws.join("MARKER").is_file(), "worktree content preserved");

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let project: String = conn
        .query_row(
            "SELECT project FROM agents WHERE agent_id = '1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(project, new_slug, "agent should follow the adopted slug");
    let stale: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM projects WHERE slug = ?1",
            rusqlite::params![old_slug],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(stale, 0, "old slug row should be gone");
    let dup: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM projects WHERE slug = ?1",
            rusqlite::params![new_slug],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(dup, 1, "exactly one project row under the new slug");
    let _ = fs::remove_dir_all(&new_path);
}

#[test]
fn rename_project_merges_into_populated_destination() {
    let p = Project::new("rp-rename-merge");
    let old_slug = p.slug();
    // Old project: one agent with history.
    p.register_agent("1");
    let old_ws = p.worktree_dir("1");
    fs::create_dir_all(&old_ws).unwrap();
    fs::write(old_ws.join("MARKER"), "old\n").unwrap();
    let old_uuid = p.agent_uuid("1").expect("agent 1 should have uuid");

    // The dir was moved and reinitialized as `diktat`, where two new agents
    // have since done work.
    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("merge-{}", std::process::id()));
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();
    let new_slug = new_path
        .file_name()
        .unwrap()
        .to_string_lossy()
        .to_lowercase()
        .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-");
    let canon = new_path.canonicalize().unwrap();

    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute(
        "INSERT INTO projects (slug, path) VALUES (?1, ?2)",
        rusqlite::params![new_slug, canon.to_str().unwrap()],
    )
    .unwrap();
    let mut new_uuids = Vec::new();
    for aid in ["2", "3"] {
        let u = uuid::Uuid::new_v4().to_string();
        conn.execute(
            "INSERT INTO agents (agent_id, uuid, project, started_at, exited_at) \
             VALUES (?1, ?2, ?3, 0, 1)",
            rusqlite::params![aid, u, new_slug],
        )
        .unwrap();
        let ws = p.data_dir.join("worktrees").join(&new_slug).join(&u);
        fs::create_dir_all(&ws).unwrap();
        fs::write(ws.join("MARKER"), format!("new-{aid}\n")).unwrap();
        new_uuids.push(u);
    }
    drop(conn);

    let out = Command::new(CAV)
        .args([
            "rename-project",
            &old_slug,
            new_path.to_str().unwrap(),
            "--yes",
        ])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav");
    assert_eq!(
        out.status.code(),
        Some(0),
        "merge failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    // All three agents now live under the new slug.
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM agents WHERE project = ?1",
            rusqlite::params![new_slug],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 3, "old agent should join the two existing ones");
    let stale: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM projects WHERE slug = ?1",
            rusqlite::params![old_slug],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(stale, 0, "old slug row should be gone");

    // Every agent's worktree is preserved under the merged slug.
    let base = p.data_dir.join("worktrees").join(&new_slug);
    assert!(base.join(&old_uuid).join("MARKER").is_file(), "old history kept");
    for u in &new_uuids {
        assert!(base.join(u).join("MARKER").is_file(), "new history kept");
    }
    let _ = fs::remove_dir_all(&new_path);
}

#[test]
fn rename_project_plain_needs_no_confirmation() {
    let p = Project::new("rp-rename-nofriction");
    let old_slug = p.slug();
    p.register_agent("1");

    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("nofriction-{}", std::process::id()));
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();
    let new_slug = new_path
        .file_name()
        .unwrap()
        .to_string_lossy()
        .to_lowercase()
        .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-");

    // No --yes and no stdin: a plain rename must still go through.
    let out = Command::new(CAV)
        .args(["rename-project", &old_slug, new_path.to_str().unwrap()])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .stdin(std::process::Stdio::null())
        .output()
        .expect("cav");
    assert_eq!(
        out.status.code(),
        Some(0),
        "plain rename should not prompt: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let project: String = conn
        .query_row(
            "SELECT project FROM agents WHERE agent_id = '1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(project, new_slug);
    let _ = fs::remove_dir_all(&new_path);
}

#[test]
fn rename_project_merge_requires_confirmation() {
    let p = Project::new("rp-rename-merge-confirm");
    let old_slug = p.slug();
    p.register_agent("1");

    let new_path = p
        .dir
        .parent()
        .unwrap()
        .join(format!("mergeconfirm-{}", std::process::id()));
    let _ = fs::remove_dir_all(&new_path);
    fs::rename(&p.dir, &new_path).unwrap();
    let new_slug = new_path
        .file_name()
        .unwrap()
        .to_string_lossy()
        .to_lowercase()
        .replace(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-', "-");
    let canon = new_path.canonicalize().unwrap();

    let db_path = p.data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute(
        "INSERT INTO projects (slug, path) VALUES (?1, ?2)",
        rusqlite::params![new_slug, canon.to_str().unwrap()],
    )
    .unwrap();
    let u = uuid::Uuid::new_v4().to_string();
    conn.execute(
        "INSERT INTO agents (agent_id, uuid, project, started_at, exited_at) \
         VALUES ('2', ?1, ?2, 0, 1)",
        rusqlite::params![u, new_slug],
    )
    .unwrap();
    drop(conn);

    // No --yes, empty stdin: the merge must be declined, leaving records intact.
    let out = Command::new(CAV)
        .args(["rename-project", &old_slug, new_path.to_str().unwrap()])
        .current_dir(&new_path)
        .env("CODING_CAVE_DATA_DIR", &p.data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .stdin(std::process::Stdio::null())
        .output()
        .expect("cav");
    assert_ne!(out.status.code(), Some(0), "merge without -y should abort");

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let project: String = conn
        .query_row(
            "SELECT project FROM agents WHERE agent_id = '1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(project, old_slug, "registry must be untouched when declined");
    let _ = fs::remove_dir_all(&new_path);
}

#[test]
fn remove_project_unknown_slug_errors() {
    let p = Project::new("rp-unknown");
    let out = p.cav(&["remove-project", "does-not-exist", "--yes"]);
    assert_ne!(out.status.code(), Some(0));
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        err.contains("No project") || err.contains("does-not-exist"),
        "error should mention the missing project, got: {err}"
    );
}

#[test]
fn gc_zero_threshold_collects_freshly_paused() {
    // --older-than 0 makes every paused agent eligible, regardless of age.
    let p = Project::new("gc-zero-threshold");
    let recent = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    p.register_agent_exited_at("5", recent);
    let ws = p.worktree_dir("5");
    fs::create_dir_all(&ws).unwrap();
    fs::write(ws.join("MARKER"), "fresh\n").unwrap();

    let out = p.cav(&["gc", "--older-than", "0s"]);
    assert_eq!(out.status.code(), Some(0));

    assert!(!ws.exists(), "0-threshold gc should collect even fresh agents");
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

// --- Target branch that was never pushed to the cave ---

#[test]
fn status_and_approve_agree_on_an_unpushed_target() {
    // An agent targeting a branch that exists nowhere on the cave remote.
    // status counts the commits past the dev branch's fork point; approve
    // must count the same ones, not everything reachable from the dev tip.
    let p = Project::new("unpushed-target");
    p.register_agent("1");
    git(&["push", "cave", "main"], &p.dir);

    git(&["checkout", "-b", "agent-1/writing"], &p.dir);
    fs::write(p.dir.join("a.txt"), "A\n").unwrap();
    git(&["add", "a.txt"], &p.dir);
    git(&["commit", "-m", "agent 1: a.txt"], &p.dir);
    git(
        &["push", "cave", "agent-1/writing:refs/heads/agent-1/writing"],
        &p.dir,
    );
    git(&["checkout", "main"], &p.dir);
    git(&["branch", "-D", "agent-1/writing"], &p.dir);
    git(&["fetch", "cave"], &p.dir);

    // The push itself recorded where the work began.
    assert!(
        p.shared_has_ref("refs/heads/writing"),
        "post-receive should have created the target at the fork point"
    );

    let st = String::from_utf8_lossy(&p.cav(&["status", "1"]).stdout).to_string();
    assert!(st.contains("1 commit on agent-1/writing"), "status: {st}");

    let out = p.cav(&["approve", "-y", "1"]);
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    assert_eq!(
        out.status.code(),
        Some(0),
        "approve failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        text.contains("1 commit from"),
        "approve must count only the agent's commit: {text}"
    );
    assert!(
        !text.contains("init"),
        "approve must not list the seed history: {text}"
    );
    assert!(p.shared_has_ref("refs/heads/writing"));
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

// --- Auto-rebase scope ---

#[test]
fn push_does_not_rebase_paused_agent_worktree() {
    // Paused agents are off-limits to the host's auto-rebase. Reaching
    // into a paused worktree races nothing today, but the agent isn't
    // there to care, and resume will rebase the dev branch when it
    // matters. Only running agents get their worktrees rebased.
    let p = Project::new("push-skip-paused");
    p.register_agent("1");  // paused (default exited_at = recent)
    seed_agent_worktree(&p, "1");
    let ws = p.worktree_dir("1");
    // Agent has real undelivered work (touches a different file from main).
    let agent_tip = agent_commit_and_push(&p, "1", "a.txt", "A\n");

    // User advances main with an unrelated change.
    fs::write(p.dir.join("b.txt"), "B\n").unwrap();
    git(&["add", "b.txt"], &p.dir);
    git(&["commit", "-m", "user: b.txt"], &p.dir);
    let out = p.cav(&["push"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "push failed: {}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );

    // Paused agent's worktree must still be at its old tip; the host
    // must not have rebased it.
    let ws_head = git_out(&["rev-parse", "HEAD"], &ws);
    assert_eq!(
        ws_head, agent_tip,
        "paused agent's worktree must not be rebased by host-side push"
    );
    // Shared dev ref also stays at the agent's tip (no fetch-back from
    // a rebase that didn't happen).
    assert_eq!(
        p.shared_sha("refs/heads/agent-1/main"),
        agent_tip,
        "shared agent ref must not be moved by paused-agent auto-rebase"
    );
}

// --- Patch-equivalent dev ref cleanup ---

#[test]
fn push_drops_dev_ref_patch_equivalent_to_base() {
    // Agent 4 pushed CA on a.txt. Independently, the user lands the same
    // patch on main as CA' (different SHA, same content). agent-4/main
    // is now "delivered" by patch-id but not by ancestry. cav push of
    // main should sweep the stale dev ref, not noisily rebase it.
    let p = Project::new("push-patch-equiv");
    p.register_agent("4");

    p.push_agent_commits("4", "main", &[("a.txt", "A\n")]);
    assert!(p.shared_has_ref("refs/heads/agent-4/main"));

    // Land the same patch on main as a distinct commit.
    git(&["checkout", "main"], &p.dir);
    fs::write(p.dir.join("a.txt"), "A\n").unwrap();
    git(&["add", "a.txt"], &p.dir);
    git(&["commit", "-m", "land a.txt on main"], &p.dir);

    let out = p.cav(&["push"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "push failed: {}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        !stderr.contains("Rebased agent-4/main"),
        "should not print rebase note for delivered branch: {stderr}"
    );
    assert!(
        !p.shared_has_ref("refs/heads/agent-4/main"),
        "patch-equivalent dev ref should be swept by push"
    );
}

/// Collecting an agent must reclaim its Claude state but never the permanent
/// session record or the project's shared memory. Those outlive every agent.
#[test]
fn gc_keeps_session_records_and_memory() {
    let p = Project::new("gc-keeps-records");
    let slug = p.dir.file_name().unwrap().to_str().unwrap().to_string();
    let sid = "11111111-2222-3333-4444-555555555555";
    let uuid = uuid::Uuid::new_v4().to_string();

    let conn = rusqlite::Connection::open(p.data_dir.join("coding-cave.db")).unwrap();
    conn.execute(
        "INSERT INTO agents (agent_id, uuid, project, session_id, host_pid, started_at, exited_at)
         VALUES ('3', ?1, ?2, ?3, NULL, 0, 1)",
        rusqlite::params![uuid, slug, sid],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO sessions (session_id, project, agent_id)
         VALUES (?1, ?2, '3')",
        rusqlite::params![sid, slug],
    )
    .unwrap();

    // Claude state is uuid-keyed, like the worktree and nix-store dirs.
    let claude = p.data_dir.join("claude/agents").join(&uuid);
    fs::create_dir_all(claude.join("projects")).unwrap();
    fs::write(claude.join("history.jsonl"), "x").unwrap();

    let record = p.data_dir.join("sessions").join(format!("{sid}.jsonl"));
    fs::create_dir_all(record.parent().unwrap()).unwrap();
    fs::write(&record, "{}\n").unwrap();

    let memory = p.data_dir.join("memory").join(&slug);
    fs::create_dir_all(&memory).unwrap();
    fs::write(memory.join("a.md"), "fact").unwrap();

    let out = p.cav(&["gc", "--older-than", "0s", "-y"]);
    assert!(
        out.status.success(),
        "gc failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert!(!claude.exists(), "agent Claude state survived gc");
    assert!(record.is_file(), "permanent session record was collected");
    assert!(memory.join("a.md").is_file(), "shared memory was collected");
    let still_there: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?1",
            rusqlite::params![sid],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(still_there, 1, "session row was deleted");
}
