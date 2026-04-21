/// Integration tests for CLI usability: friendly errors, init, status.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const CAV: &str = env!("CARGO_BIN_EXE_cav");

struct TestRepo {
    dir: PathBuf,
}

impl TestRepo {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir()
            .join(format!("cav-cli-test-{}-{}", std::process::id(), name));
        fs::create_dir_all(&dir).unwrap();
        git(&["init", "-b", "main"], &dir);
        git(&["config", "user.email", "test@test.com"], &dir);
        git(&["config", "user.name", "Test"], &dir);
        fs::write(dir.join("README.md"), "# test\n").unwrap();
        git(&["add", "README.md"], &dir);
        git(&["commit", "-m", "init"], &dir);
        Self { dir }
    }

    fn cav(&self, args: &[&str]) -> Output {
        Command::new(CAV)
            .args(args)
            .current_dir(&self.dir)
            .env_remove("CODING_CAVE_VERSION")
            .env_remove("CODING_CAVE_AGENT_ID")
            .output()
            .expect("cav binary not found")
    }
}

impl Drop for TestRepo {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn git(args: &[&str], dir: &Path) {
    Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap_or_else(|_| panic!("git {:?} failed", args));
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
