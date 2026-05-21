/// Integration tests for the pre-receive hook binary.
///
/// Verifies that the hook correctly accepts/rejects pushes by running the
/// `cav-git-pre-receive` binary with stdin input and the CODING_CAVE_AGENT_ID
/// env var set, mimicking git's hook invocation.
use std::env;
use std::io::Write;
use std::process::{Command, Stdio};

const PRE_RECEIVE: &str = env!("CARGO_BIN_EXE_cav-git-pre-receive");

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
