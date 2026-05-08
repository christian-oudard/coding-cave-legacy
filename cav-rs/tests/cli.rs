/// Integration tests for CLI usability: friendly errors, init, status.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const CAV: &str = env!("CARGO_BIN_EXE_cav");

struct TestRepo {
    dir: PathBuf,
    data_dir: PathBuf,
}

impl TestRepo {
    fn new(name: &str) -> Self {
        // Each test gets a unique data dir so concurrent `cargo test` runs
        // don't share the same registry.db. The pid-only suffix in the old
        // path collided across tests within one binary because the harness
        // runs them in parallel under one process.
        let uniq = format!("{}-{}", std::process::id(), name);
        let dir = std::env::temp_dir().join(format!("cav-cli-test-{uniq}"));
        let data_dir = std::env::temp_dir().join(format!("cav-cli-data-{uniq}"));
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
        Self { dir, data_dir }
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
}

impl Drop for TestRepo {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
        let _ = fs::remove_dir_all(&self.data_dir);
    }
}

fn git(args: &[&str], dir: &Path) {
    Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap_or_else(|_| panic!("git {:?} failed", args));
}

fn git_out(args: &[&str], dir: &Path) -> String {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap_or_else(|_| panic!("git {:?} failed", args));
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn agent_push(args: &[&str], dir: &Path, agent_id: &str) {
    Command::new("git")
        .args(args)
        .current_dir(dir)
        .env("CODING_CAVE_AGENT_ID", agent_id)
        .env("CODING_CAVE_AGENT_NAME", format!("coding-cave@{agent_id}"))
        .output()
        .unwrap_or_else(|_| panic!("agent git {:?} failed", args));
}

fn stderr(o: &Output) -> String {
    String::from_utf8_lossy(&o.stderr).to_string()
}

fn stdout(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).to_string()
}

// --- Require init errors ---

#[test]
fn agent_without_init_gives_friendly_error() {
    let repo = TestRepo::new("no-init-agent");
    let out = repo.cav(&[]);
    assert_ne!(out.status.code(), Some(0));
    let err = stderr(&out);
    assert!(
        err.contains("not initialized") || err.contains("cav init"),
        "expected friendly error, got: {err}"
    );
    assert!(!err.contains("thread 'main' panicked"), "got panic: {err}");
}

#[test]
fn status_without_init_gives_friendly_error() {
    let repo = TestRepo::new("no-init-status");
    let out = repo.cav(&["status"]);
    assert_ne!(out.status.code(), Some(0));
    assert!(
        stderr(&out).contains("not initialized"),
        "got: {}",
        stderr(&out)
    );
}

#[test]
fn pull_without_init_gives_friendly_error() {
    let repo = TestRepo::new("no-init-pull");
    let out = repo.cav(&["pull"]);
    assert_ne!(out.status.code(), Some(0));
    assert!(
        stderr(&out).contains("not initialized"),
        "got: {}",
        stderr(&out)
    );
}

#[test]
fn push_without_init_gives_friendly_error() {
    let repo = TestRepo::new("no-init-push");
    let out = repo.cav(&["push"]);
    assert_ne!(out.status.code(), Some(0));
    assert!(
        stderr(&out).contains("not initialized"),
        "got: {}",
        stderr(&out)
    );
}

// Bare `cav <agent-flag>` should route to the `agent` subcommand rather
// than failing with an "unexpected argument" parse error.
#[test]
fn bare_agent_flag_routes_to_agent() {
    let repo = TestRepo::new("bare-agent-flag");
    let out = repo.cav(&["-p", "/nonexistent/extra-project"]);
    assert_ne!(out.status.code(), Some(0));
    let err = stderr(&out);
    assert!(
        !err.contains("unexpected argument"),
        "should not bail at top-level parse: {err}"
    );
}

// `cav agent -p <missing-path>` should surface a clear error identifying
// the bad extra, not a cryptic git failure from deep inside workspace setup.
#[test]
fn agent_extra_missing_path_is_friendly() {
    let repo = TestRepo::new("extra-missing");
    repo.cav(&["init", repo.dir.to_str().unwrap()]);
    let out = repo.cav(&["agent", "-p", "/definitely/does/not/exist"]);
    assert_ne!(out.status.code(), Some(0));
    let err = stderr(&out);
    assert!(
        err.contains("/definitely/does/not/exist")
            && !err.contains("git checkout")
            && !err.contains("git clone"),
        "expected clear error naming the bad path, got: {err}"
    );
}

// `cav agent -p <uninitialized-project>` should say "not initialized" and
// point at `cav init`, not proceed into clone/checkout.
#[test]
fn agent_extra_uninitialized_is_friendly() {
    let primary = TestRepo::new("extra-primary");
    primary.cav(&["init", primary.dir.to_str().unwrap()]);

    // A second git repo that has NOT been cav init'd.
    let uninit = TestRepo::new("extra-uninit");

    let out = primary.cav(&["agent", "-p", uninit.dir.to_str().unwrap()]);
    assert_ne!(out.status.code(), Some(0));
    let err = stderr(&out);
    assert!(
        (err.contains("not initialized") || err.contains("cav init"))
            && !err.contains("git checkout")
            && !err.contains("git clone"),
        "expected friendly init error for extra, got: {err}"
    );
}

// --- cav init ---

#[test]
fn init_creates_shared_repo_and_hook() {
    let repo = TestRepo::new("init");
    let out = repo.cav(&["init", repo.dir.to_str().unwrap()]);
    assert_eq!(out.status.code(), Some(0), "stderr: {}", stderr(&out));
    assert!(
        stdout(&out).contains("initialized"),
        "got: {}",
        stdout(&out)
    );

    let shared = repo.dir.join(".coding-cave").join("cave-shared.git");
    assert!(shared.exists(), ".coding-cave/cave-shared.git must exist");
    assert!(
        shared.join("hooks").join("pre-receive").exists(),
        "pre-receive hook must exist"
    );
}

fn run_empty_init_agent_flow(default_branch: &str, name_suffix: &str) {
    // Empty repo with `default_branch` as the unborn HEAD. cav init must
    // succeed without fabricating a commit. The agent then makes the very
    // first commit on its dev branch and pushes it to the shared remote.
    // The host fetches and verifies the dev branch is reachable.
    let uniq = format!("{}-{}", std::process::id(), name_suffix);
    let dir = std::env::temp_dir().join(format!("cav-cli-test-{uniq}"));
    let data_dir = std::env::temp_dir().join(format!("cav-cli-data-{uniq}"));
    let _ = fs::remove_dir_all(&dir);
    let _ = fs::remove_dir_all(&data_dir);
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&data_dir).unwrap();
    git(&["init", "-b", default_branch], &dir);
    git(&["config", "user.email", "test@test.com"], &dir);
    git(&["config", "user.name", "Test"], &dir);

    let init_out = Command::new(CAV)
        .args(["init", dir.to_str().unwrap()])
        .env("CODING_CAVE_DATA_DIR", &data_dir)
        .env_remove("CODING_CAVE_VERSION")
        .env_remove("CODING_CAVE_AGENT_ID")
        .output()
        .expect("cav binary not found");
    assert_eq!(init_out.status.code(), Some(0), "stderr: {}", stderr(&init_out));

    let head_check = Command::new("git")
        .args(["rev-parse", "--verify", "HEAD"])
        .current_dir(&dir)
        .output()
        .unwrap();
    assert!(
        !head_check.status.success(),
        "cav init must not fabricate a commit; HEAD should remain unborn"
    );

    // Simulate the agent workspace: clone shared, checkout orphan default
    // branch, make the first commit, push as agent-1/<default_branch>.
    let shared = dir.join(".coding-cave").join("cave-shared.git");
    let agent_ws = std::env::temp_dir().join(format!("cav-cli-agent-{uniq}"));
    let _ = fs::remove_dir_all(&agent_ws);
    git(
        &[
            "clone",
            "--shared",
            shared.to_str().unwrap(),
            agent_ws.to_str().unwrap(),
        ],
        &dir,
    );
    git(&["config", "user.email", "agent@test.com"], &agent_ws);
    git(&["config", "user.name", "Agent"], &agent_ws);
    git(&["checkout", "--orphan", default_branch], &agent_ws);
    fs::write(agent_ws.join("first.txt"), "hello\n").unwrap();
    git(&["add", "first.txt"], &agent_ws);
    git(&["commit", "-m", "first commit"], &agent_ws);
    let agent_sha = git_out(&["rev-parse", "HEAD"], &agent_ws);

    let dev_branch = format!("agent-1/{default_branch}");
    agent_push(
        &[
            "push",
            shared.to_str().unwrap(),
            &format!("{default_branch}:refs/heads/{dev_branch}"),
        ],
        &agent_ws,
        "1",
    );

    // Shared repo must now have the agent's first commit on the dev branch.
    let shared_sha = git_out(&["rev-parse", &dev_branch], &shared);
    assert_eq!(
        shared_sha, agent_sha,
        "shared repo must have the agent's first commit on {dev_branch}"
    );

    // Host can fetch the dev branch from the cave remote (this is the first
    // ref that ever existed in the system).
    git(&["fetch", "cave"], &dir);
    let host_sha = git_out(&["rev-parse", &format!("cave/{dev_branch}")], &dir);
    assert_eq!(
        host_sha, agent_sha,
        "host repo must see the agent's first commit via the cave remote"
    );

    let _ = fs::remove_dir_all(&dir);
    let _ = fs::remove_dir_all(&agent_ws);
    let _ = fs::remove_dir_all(&data_dir);
}

#[test]
fn empty_init_then_agent_first_commit_main() {
    run_empty_init_agent_flow("main", "empty-init-main");
}

#[test]
fn empty_init_then_agent_first_commit_master() {
    run_empty_init_agent_flow("master", "empty-init-master");
}

#[test]
fn init_idempotent() {
    let repo = TestRepo::new("init-idempotent");
    repo.cav(&["init", repo.dir.to_str().unwrap()]);
    let out = repo.cav(&["init", repo.dir.to_str().unwrap()]);
    assert_eq!(out.status.code(), Some(0), "stderr: {}", stderr(&out));
    assert!(
        stdout(&out).contains("already initialized"),
        "got: {}",
        stdout(&out)
    );
}

#[test]
fn init_reinitialize_updates_hooks() {
    let repo = TestRepo::new("reinit");
    repo.cav(&["init", repo.dir.to_str().unwrap()]);
    let shared = repo.dir.join(".coding-cave").join("cave-shared.git");
    let post_recv = shared.join("hooks").join("post-receive");
    fs::remove_file(&post_recv).unwrap();
    assert!(!post_recv.exists());

    let out = repo.cav(&["init", "--reinitialize", repo.dir.to_str().unwrap()]);
    assert_eq!(out.status.code(), Some(0), "stderr: {}", stderr(&out));
    assert!(post_recv.exists(), "post-receive hook must be restored");
}

#[test]
fn init_in_subdirectory_errors_helpfully() {
    let repo = TestRepo::new("subdir-init");
    let subdir = repo.dir.join("src");
    fs::create_dir_all(&subdir).unwrap();
    let out = Command::new(CAV)
        .arg("init")
        .current_dir(&subdir)
        .output()
        .expect("cav binary not found");
    assert_ne!(out.status.code(), Some(0), "should fail in subdirectory");
    let err = stderr(&out);
    assert!(
        err.contains("existing git repo") || err.contains("repo root"),
        "error should mention git repo root: {err}"
    );
}

// --- version ---

#[test]
fn version_prints_something() {
    let repo = TestRepo::new("version");
    let out = repo.cav(&["version"]);
    assert_eq!(out.status.code(), Some(0), "stderr: {}", stderr(&out));
    // Version output either goes to stdout or stderr; just check it ran
    let combined = format!("{}{}", stdout(&out), stderr(&out));
    assert!(!combined.is_empty(), "version produced no output");
}

// --- approve: partial-then-full branch cleanup ---

#[test]
fn partial_approve_on_last_commit_deletes_dev_branch() {
    // Regression: a partial approve whose commit target equals dev_tip must
    // be treated as a full approve (partial=false) and delete the dev branch.
    let repo = TestRepo::new("partial-full-approve");
    repo.cav(&["init", repo.dir.to_str().unwrap()]);

    let shared = repo.dir.join(".coding-cave").join("cave-shared.git");

    // Push the base branch to the shared remote (simulates normal user push).
    git(&["push", "cave", "main"], &repo.dir);

    // Make two agent commits and push them as agent-1/main.
    fs::write(repo.dir.join("a.txt"), "a\n").unwrap();
    git(&["add", "a.txt"], &repo.dir);
    git(&["commit", "-m", "commit-A"], &repo.dir);
    let sha_a = git_out(&["rev-parse", "HEAD"], &repo.dir);

    fs::write(repo.dir.join("b.txt"), "b\n").unwrap();
    git(&["add", "b.txt"], &repo.dir);
    git(&["commit", "-m", "commit-B"], &repo.dir);
    let sha_b = git_out(&["rev-parse", "HEAD"], &repo.dir);

    // Reset user's main back to before the agent commits so they're pending.
    git(&["reset", "--hard", "HEAD~2"], &repo.dir);
    git(&["push", "cave", "main"], &repo.dir);

    // Push the two commits as an agent branch.
    agent_push(
        &["push", "cave", &format!("{sha_b}:refs/heads/agent-1/main")],
        &repo.dir,
        "1",
    );

    // Partial approve: approve only commit A.
    let out = repo.cav(&["approve", "1", "--yes", "-c", &sha_a]);
    assert_eq!(out.status.code(), Some(0), "partial approve failed: {}", stderr(&out));

    // Dev branch should still exist (B is still pending).
    let branches = git_out(&["branch", "--list", "agent-1/main"], &shared);
    assert!(!branches.is_empty(), "dev branch should still exist after partial approve");

    // Approve the last commit (B = dev tip). This should be a full approve.
    let out = repo.cav(&["approve", "1", "--yes", "-c", &sha_b]);
    assert_eq!(out.status.code(), Some(0), "final approve failed: {}", stderr(&out));

    // Dev branch must be gone from the shared repo.
    let branches = git_out(&["branch", "--list", "agent-1/main"], &shared);
    assert!(
        branches.is_empty(),
        "dev branch should be deleted after approving the last commit, got: {branches}"
    );
}

// --- review: auto-pick agent ID ---

#[test]
fn review_no_args_picks_from_current_dev_branch() {
    // When the user is already on an agent dev branch, `cav review` (no args)
    // should pick that agent without requiring an explicit ID.
    let repo = TestRepo::new("review-current-branch");
    repo.cav(&["init", repo.dir.to_str().unwrap()]);

    git(&["push", "cave", "main"], &repo.dir);

    fs::write(repo.dir.join("a.txt"), "a\n").unwrap();
    git(&["add", "a.txt"], &repo.dir);
    git(&["commit", "-m", "commit-A"], &repo.dir);
    let sha_a = git_out(&["rev-parse", "HEAD"], &repo.dir);

    git(&["reset", "--hard", "HEAD~1"], &repo.dir);
    git(&["push", "cave", "main"], &repo.dir);

    agent_push(
        &["push", "cave", &format!("{sha_a}:refs/heads/agent-2/main")],
        &repo.dir,
        "2",
    );

    // Place the host on the dev branch so the inference picks agent 2.
    git(&["checkout", "-B", "agent-2/main", &sha_a], &repo.dir);

    let out = repo.cav(&["review"]);
    assert_eq!(out.status.code(), Some(0), "stderr: {}", stderr(&out));
    assert!(
        stdout(&out).contains("Checked out agent-2/main"),
        "expected auto-pick of agent 2, got: {}",
        stdout(&out)
    );
}

#[test]
fn review_no_args_with_no_agents_errors_clearly() {
    let repo = TestRepo::new("review-no-agents");
    repo.cav(&["init", repo.dir.to_str().unwrap()]);

    let out = repo.cav(&["review"]);
    assert_ne!(out.status.code(), Some(0));
    let err = stderr(&out);
    assert!(
        err.contains("No agents"),
        "expected friendly empty-registry error, got: {err}"
    );
}

#[test]
fn remove_agent_no_args_with_no_paused_agents_errors_clearly() {
    let repo = TestRepo::new("remove-agent-no-agents");
    repo.cav(&["init", repo.dir.to_str().unwrap()]);

    let out = repo.cav(&["remove-agent"]);
    assert_ne!(out.status.code(), Some(0));
    let err = stderr(&out);
    assert!(
        err.contains("No paused agents"),
        "expected friendly empty-registry error, got: {err}"
    );
}
