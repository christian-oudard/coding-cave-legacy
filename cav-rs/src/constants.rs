use std::env;
use std::path::PathBuf;

use serde::Deserialize;

#[derive(Deserialize, Default)]
#[serde(default)]
pub struct BuildConfig {
    pub version: String,
    #[serde(rename = "versionDate")]
    pub version_date: String,
    #[serde(rename = "cavBin")]
    pub cav_bin: String,
    #[serde(rename = "sandboxConfig")]
    pub sandbox_config: String,
    #[serde(rename = "nixpkgsUrl")]
    pub nixpkgs_url: String,
    #[serde(rename = "claudeCodeNixUrl")]
    pub claude_code_nix_url: String,
    #[serde(rename = "homeManagerUrl")]
    pub home_manager_url: String,
    #[serde(rename = "codingCaveUrl")]
    pub coding_cave_url: String,
    #[serde(rename = "caveSourceHash")]
    pub cave_source_hash: String,
}

impl BuildConfig {
    fn load() -> Self {
        let path = match env::var("CODING_CAVE_BUILD_CONFIG") {
            Ok(p) => p,
            Err(_) => return Self::default(),
        };
        let text = match std::fs::read_to_string(&path) {
            Ok(t) => t,
            Err(_) => return Self::default(),
        };
        serde_json::from_str(&text).unwrap_or_default()
    }
}

pub static BUILD: std::sync::OnceLock<BuildConfig> = std::sync::OnceLock::new();

pub fn build() -> &'static BuildConfig {
    BUILD.get_or_init(BuildConfig::load)
}

pub fn version() -> &'static str {
    let v = &build().version;
    if v.is_empty() { "dev" } else { v.as_str() }
}

pub fn version_date() -> &'static str {
    let v = &build().version_date;
    if v.is_empty() { "dev" } else { v.as_str() }
}

pub fn supervisor_path() -> PathBuf {
    PathBuf::from(&build().cav_bin).join("cav-supervisor")
}

// Git remote names
pub const CAVE_SHARED_REMOTE: &str = "cave";
pub const HOST_SHARED_REMOTE: &str = "host-cave";

// Bare repo directory names under .coding-cave/
pub const SHARED_REPO_DIR: &str = "cave-shared.git";
pub const HOST_SHARED_REPO_DIR: &str = "host-cave-shared.git";

// Networking
pub const DAEMON_BASE_PORT: u16 = 61000;
