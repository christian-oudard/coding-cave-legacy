/// Integration tests for the receive hook binaries.
///
/// Verifies that pre-receive accepts/rejects pushes, and that post-receive
/// records where a dev branch forked, by running the binaries with stdin
/// input and the CODING_CAVE_AGENT_ID env var set, mimicking git's hook
/// invocation.
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const PRE_RECEIVE: &str = env!("CARGO_BIN_EXE_cav-git-pre-receive");
const POST_RECEIVE: &str = env!("CARGO_BIN_EXE_cav-git-post-receive");

const ZERO: &str = "0000000000000000000000000000000000000000";
const SHA_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const SHA_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn run_hook(lines: &[&str], agent_id: Option<&str>) -> std::process::Output {
    let mut cmd = Command::new(PRE_RECEIVE);
    if let Some(id) = agent_id {
        cmd.env("CODING_CAVE_AGENT_ID", id);
    } else {
        cmd.env_remove("CODING_CAVE_AGENT_ID");
    }
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().expect("cav-git-pre-receive not found");
    let stdin = child.stdin.as_mut().unwrap();
    for line in lines {
        writeln!(stdin, "{line}").unwrap();
    }
    child.wait_with_output().unwrap()
}

fn allowed(lines: &[&str], agent_id: Option<&str>) -> bool {
    run_hook(lines, agent_id).status.success()
}

fn rejected(lines: &[&str], agent_id: Option<&str>) -> bool {
    !allowed(lines, agent_id)
}

// --- User pushes (no agent id) ---

#[test]
fn user_can_push_base_branch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/main");
    assert!(allowed(&[&line], None));
}

#[test]
fn user_can_delete_base_branch() {
    let line = format!("{SHA_A} {ZERO} refs/heads/main");
    assert!(allowed(&[&line], None));
}

#[test]
fn user_can_push_agent_branch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/agent-1/main");
    assert!(allowed(&[&line], None));
}

#[test]
fn user_can_create_branch() {
    let line = format!("{ZERO} {SHA_A} refs/heads/develop");
    assert!(allowed(&[&line], None));
}

#[test]
fn user_can_push_tags() {
    let line = format!("{ZERO} {SHA_A} refs/tags/v1.0");
    assert!(allowed(&[&line], None));
}

// --- Agent: own branch ---

#[test]
fn agent_can_push_own_branch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/agent-1/main");
    assert!(allowed(&[&line], Some("1")));
}

#[test]
fn agent_can_create_own_branch() {
    let line = format!("{ZERO} {SHA_A} refs/heads/agent-1/develop");
    assert!(allowed(&[&line], Some("1")));
}

// --- Agent: other agent's branch ---

#[test]
fn agent_cannot_push_other_agents_branch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/agent-2/main");
    assert!(rejected(&[&line], Some("1")));
}

#[test]
fn agent_cannot_create_other_agents_branch() {
    let line = format!("{ZERO} {SHA_A} refs/heads/agent-2/main");
    assert!(rejected(&[&line], Some("1")));
}

// --- Agent: base branch ---

#[test]
fn agent_cannot_push_base_branch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/main");
    assert!(rejected(&[&line], Some("1")));
}

#[test]
fn agent_cannot_delete_base_branch() {
    let line = format!("{SHA_A} {ZERO} refs/heads/main");
    assert!(rejected(&[&line], Some("1")));
}

#[test]
fn agent_cannot_create_base_branch() {
    let line = format!("{ZERO} {SHA_A} refs/heads/develop");
    assert!(rejected(&[&line], Some("1")));
}

// --- HEAD branch trap ---

#[test]
fn nobody_can_push_head_branch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/agent-1/HEAD");
    assert!(rejected(&[&line], None));
    assert!(rejected(&[&line], Some("1")));
}

// --- Non-branch refs ---

#[test]
fn agent_cannot_push_tags() {
    let line = format!("{ZERO} {SHA_A} refs/tags/v1.0");
    assert!(rejected(&[&line], Some("1")));
}

#[test]
fn agent_cannot_push_notes() {
    let line = format!("{SHA_A} {SHA_B} refs/notes/commits");
    assert!(rejected(&[&line], Some("1")));
}

#[test]
fn agent_cannot_push_replace_refs() {
    let line = format!("{SHA_A} {SHA_B} refs/replace/{SHA_A}");
    assert!(rejected(&[&line], Some("1")));
}

// --- Multiple refs in one push: any rejection fails the whole push ---

#[test]
fn mixed_push_fails_on_rejected_ref() {
    let ok_line = format!("{SHA_A} {SHA_B} refs/heads/agent-1/main");
    let bad_line = format!("{SHA_A} {SHA_B} refs/heads/main");
    assert!(rejected(&[&ok_line, &bad_line], Some("1")));
}

// --- High agent ID ---

#[test]
fn agent_high_id_own_branch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/agent-42/main");
    assert!(allowed(&[&line], Some("42")));
}

#[test]
fn agent_id_mismatch() {
    let line = format!("{SHA_A} {SHA_B} refs/heads/agent-10/main");
    assert!(rejected(&[&line], Some("1")));
}

// --- post-receive: record where a dev branch forked ---

/// A bare repo (what the cave shares) plus a work repo pushing into it.
struct HookRepo {
    dir: PathBuf,
    bare: PathBuf,
    work: PathBuf,
}

impl Drop for HookRepo {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

impl HookRepo {
    fn new(name: &str) -> Self {
        let dir = env::temp_dir().join(format!("cav-post-recv-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let (bare, work) = (dir.join("shared.git"), dir.join("work"));
        fs::create_dir_all(&work).unwrap();
        run(&dir, &["init", "--bare", "-b", "main", "shared.git"]);
        run(&work, &["init", "-b", "main"]);
        run(&work, &["config", "user.email", "test@test.com"]);
        run(&work, &["config", "user.name", "Test"]);
        let repo = Self { dir, bare, work };
        repo.commit("README.md", "init\n");
        run(&repo.work, &["remote", "add", "cave", &repo.bare.display().to_string()]);
        run(&repo.work, &["push", "cave", "main"]);
        repo
    }

    fn commit(&self, file: &str, body: &str) -> String {
        fs::write(self.work.join(file), body).unwrap();
        run(&self.work, &["add", file]);
        run(&self.work, &["commit", "-m", &format!("add {file}")]);
        self.rev("HEAD")
    }

    fn rev(&self, spec: &str) -> String {
        out(&self.work, &["rev-parse", spec])
    }

    /// Push `branch` into the bare repo as `dev`, then run the hook the way
    /// git would, with the ref's old value and cwd inside the bare repo.
    fn push_dev(&self, branch: &str, dev: &str) {
        let old = out(&self.bare, &["rev-parse", "--verify", "--quiet", dev]);
        let old = if old.is_empty() { ZERO.to_string() } else { old };
        run(&self.work, &["push", "-f", "cave", &format!("{branch}:refs/heads/{dev}")]);
        let new = out(&self.bare, &["rev-parse", dev]);
        post_receive(&self.bare, &format!("{old} {new} refs/heads/{dev}"));
    }

    fn bare_rev(&self, spec: &str) -> String {
        out(&self.bare, &["rev-parse", "--verify", "--quiet", spec])
    }
}

fn run(dir: &Path, args: &[&str]) {
    let out = Command::new("git").args(args).current_dir(dir).output().unwrap();
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

fn out(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git").args(args).current_dir(dir).output().unwrap();
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn post_receive(dir: &Path, line: &str) {
    let mut child = Command::new(POST_RECEIVE)
        .current_dir(dir)
        .env("CODING_CAVE_AGENT_ID", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("cav-git-post-receive not found");
    writeln!(child.stdin.as_mut().unwrap(), "{line}").unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success(), "post-receive failed: {}", String::from_utf8_lossy(&out.stderr));
}

#[test]
fn post_receive_creates_missing_target_at_the_fork_point() {
    // The agent targets a branch the shared repo has never seen. Its base
    // has to be recorded now: later on, the refs that define the fork point
    // have moved and can only be guessed at.
    let r = HookRepo::new("creates-target");
    let fork = r.rev("main");
    run(&r.work, &["checkout", "-b", "agent-1/writing"]);
    r.commit("a.txt", "A\n");
    r.push_dev("agent-1/writing", "agent-1/writing");

    assert_eq!(r.bare_rev("refs/heads/writing"), fork, "target created at the fork");
    assert_eq!(
        out(&r.bare, &["rev-list", "--count", "writing..agent-1/writing"]),
        "1",
        "approve's range covers exactly the agent's commit"
    );
}

#[test]
fn post_receive_leaves_an_existing_target_alone() {
    // The user's own branch is not the hook's to move.
    let r = HookRepo::new("existing-target");
    run(&r.work, &["checkout", "-b", "writing"]);
    let writing = r.commit("w.txt", "W\n");
    run(&r.work, &["push", "cave", "writing"]);
    r.commit("a.txt", "A\n");
    r.push_dev("writing", "agent-1/writing");

    assert_eq!(r.bare_rev("refs/heads/writing"), writing, "target untouched");
}

#[test]
fn post_receive_ignores_non_dev_branches() {
    let r = HookRepo::new("non-dev");
    run(&r.work, &["checkout", "-b", "feature"]);
    r.commit("a.txt", "A\n");
    r.push_dev("feature", "feature");

    assert_eq!(r.bare_rev("refs/heads/agent-1/feature"), "", "no dev branch invented");
}

#[test]
fn post_receive_records_nothing_for_an_unrelated_history() {
    // An orphan line shares no commit with any base branch, so there is no
    // fork point to name. Every commit on it is the agent's, which is what
    // approve reports when the target is absent.
    let r = HookRepo::new("unrelated");
    run(&r.work, &["checkout", "--orphan", "agent-1/fresh"]);
    run(&r.work, &["rm", "-rf", "--cached", "."]);
    r.commit("f.txt", "F\n");
    r.push_dev("agent-1/fresh", "agent-1/fresh");

    assert_eq!(r.bare_rev("refs/heads/fresh"), "", "no fork point to record");
}

#[test]
fn post_receive_records_the_nearest_fork_of_a_merge() {
    // Work that merges two base lines has two boundary commits. The nearer
    // one is recorded, so approve lists the fewest commits the agent did
    // not write.
    let r = HookRepo::new("merge-fork");
    let main_tip = r.rev("main");
    run(&r.work, &["checkout", "-b", "develop"]);
    let develop_tip = r.commit("d.txt", "D\n");
    run(&r.work, &["push", "cave", "develop"]);

    run(&r.work, &["checkout", "-b", "agent-1/writing", &main_tip]);
    r.commit("a.txt", "A\n");
    run(&r.work, &["merge", "--no-edit", "develop"]);
    r.push_dev("agent-1/writing", "agent-1/writing");

    assert_eq!(
        r.bare_rev("refs/heads/writing"),
        develop_tip,
        "nearest boundary wins over the older fork from main"
    );
}
