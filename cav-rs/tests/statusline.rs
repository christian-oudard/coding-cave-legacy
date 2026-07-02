/// Tests for `cav-host agent-tag`, the byline segment the default
/// statusline (and dotfiles-composed statuslines) build on.
use std::process::Command;

const CAV_HOST: &str = env!("CARGO_BIN_EXE_cav-host");

fn agent_tag(agent: Option<&str>) -> String {
    let mut cmd = Command::new(CAV_HOST);
    cmd.arg("agent-tag").env_remove("CODING_CAVE_AGENT_NAME");
    if let Some(a) = agent {
        cmd.env("CODING_CAVE_AGENT_NAME", a);
    }
    let out = cmd.output().expect("cav-host not found");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

#[test]
fn byline_from_agent_name() {
    assert_eq!(agent_tag(Some("dotfiles@10")), "Coding Cave - dotfiles@10");
}

#[test]
fn byline_fallback_when_unset() {
    assert_eq!(agent_tag(None), "Coding Cave - ?");
}
