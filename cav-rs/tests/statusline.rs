/// Integration tests for defaults/statusline.sh rendering.
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()
}

fn statusline() -> PathBuf {
    repo_root().join("defaults").join("statusline.sh")
}

fn render(persist_json: Option<&str>, agent: &str) -> String {
    let td = tempdir();
    if let Some(json) = persist_json {
        fs::create_dir_all(td.join(".claude")).unwrap();
        fs::write(td.join(".claude/persist.json"), json).unwrap();
    }
    let out = Command::new("bash")
        .arg(statusline())
        .current_dir(&td)
        .env_clear()
        .env("PATH", std::env::var("PATH").unwrap_or_default())
        .env("HOME", "/does-not-exist")
        .env("CODING_CAVE_AGENT_NAME", agent)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("bash not found")
        .wait_with_output()
        .unwrap();
    let _ = fs::remove_dir_all(&td);
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn tempdir() -> PathBuf {
    let d = std::env::temp_dir()
        .join(format!("cav-statusline-{}-{}", std::process::id(), rand_u32()));
    fs::create_dir_all(&d).unwrap();
    d
}

fn rand_u32() -> u32 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .subsec_nanos()
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

const AGENT: &str = "test@1";
const LABEL: &str = "Coding Cave - test@1";

#[test]
fn no_persist_session() {
    assert_eq!(render(None, AGENT), LABEL);
}

#[test]
fn total_bound() {
    let json = r#"{"iteration":2,"total":5,"deadline":null,"started":0,"prompt":"x"}"#;
    assert_eq!(render(Some(json), AGENT), format!("{LABEL} [persist 2/5]"));
}

#[test]
fn forever() {
    let json = r#"{"iteration":7,"total":null,"deadline":null,"started":0,"prompt":"x"}"#;
    assert_eq!(render(Some(json), AGENT), format!("{LABEL} [persist forever]"));
}

#[test]
fn locked_total() {
    let json = r#"{"iteration":2,"total":10,"deadline":null,"started":0,"prompt":"x","lock":true}"#;
    assert_eq!(render(Some(json), AGENT), format!("{LABEL} [persist --lock 2/10]"));
}

#[test]
fn locked_forever() {
    let json = r#"{"iteration":4,"total":null,"deadline":null,"started":0,"prompt":"x","lock":true}"#;
    assert_eq!(render(Some(json), AGENT), format!("{LABEL} [persist --lock forever]"));
}

#[test]
fn deadline_bound() {
    let n = now();
    let json = format!(
        r#"{{"iteration":3,"total":null,"deadline":{deadline},"started":{started},"prompt":"x"}}"#,
        deadline = n + 1800,
        started = n - 1800,
    );
    let result = render(Some(&json), AGENT);
    assert!(
        result.starts_with(&format!("{LABEL} [persist ")),
        "expected persist prefix, got: {result}"
    );
    assert!(result.contains('/'), "expected elapsed/total, got: {result}");
}

#[test]
fn locked_deadline() {
    let n = now();
    let json = format!(
        r#"{{"iteration":3,"total":null,"deadline":{deadline},"started":{started},"prompt":"x","lock":true}}"#,
        deadline = n + 1800,
        started = n - 1800,
    );
    let result = render(Some(&json), AGENT);
    assert!(
        result.contains("persist --lock"),
        "expected persist --lock, got: {result}"
    );
}
