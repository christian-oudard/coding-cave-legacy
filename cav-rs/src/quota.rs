//! Per-agent (and shared-home) disk quotas via ext4 project quotas.
//!
//! The host daemon reads a JSON config; a quota is *inactive* unless its size
//! is set. When a quota is active but the filesystem can't enforce project
//! quotas, the daemon crashes rather than run unprotected (see
//! `should_crash`). Each agent's writable trees get a stable project ID with
//! a block limit; the shared cave home gets one reserved project ID.
//!
//! Pure logic (config parse, size parse, project-ID derivation, the
//! crash decision) is unit-tested here. The `chattr`/`setquota`/`quotaon`
//! shell-outs are the effectful boundary, exercised by the host's own
//! verification steps (see README) rather than in-tree tests.

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Project ID reserved for the shared cave home. Per-agent IDs are derived to
/// avoid it (and 0, which means "no project"). Currently reserved but not
/// applied: the shared-home cap is unenforceable since the nix store moved to
/// /var/lib and the home stayed on the user's filesystem (see cav-store's
/// setup_quota). Kept so the reserved ID stays carved out if it returns.
#[allow(dead_code)]
pub const SHARED_HOME_PROJID: u32 = 1;

const DEFAULT_CONFIG_PATH: &str = "/etc/coding-cave/host.json";

/// Host-side configuration. Absent fields mean the corresponding quota is
/// inactive. Field names match the NixOS module options that render the file.
#[derive(Debug, Default, Deserialize)]
pub struct HostConfig {
    #[serde(rename = "perAgentQuota")]
    pub per_agent_quota: Option<String>,
    #[serde(rename = "sharedHomeQuota")]
    pub shared_home_quota: Option<String>,
}

impl HostConfig {
    /// Read the config from `CODING_CAVE_HOST_CONFIG` (or the default path).
    /// A missing file means all-inactive defaults; a malformed file is a hard
    /// error (a quota you meant to set must not be silently dropped).
    pub fn load() -> Result<Self> {
        let path = std::env::var("CODING_CAVE_HOST_CONFIG")
            .unwrap_or_else(|_| DEFAULT_CONFIG_PATH.to_string());
        match std::fs::read_to_string(&path) {
            Ok(s) => serde_json::from_str(&s)
                .with_context(|| format!("parse host config {path}")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e).with_context(|| format!("read host config {path}")),
        }
    }

    /// Whether any quota is active.
    pub fn active(&self) -> bool {
        self.per_agent_quota.is_some() || self.shared_home_quota.is_some()
    }

    pub fn per_agent_bytes(&self) -> Result<Option<u64>> {
        self.per_agent_quota.as_deref().map(parse_size).transpose()
    }

    pub fn shared_home_bytes(&self) -> Result<Option<u64>> {
        self.shared_home_quota.as_deref().map(parse_size).transpose()
    }
}

/// Parse a size like `10G`, `512M`, `1024K`, or a bare byte count into bytes.
/// Suffixes are binary (1G = 1024^3).
pub fn parse_size(s: &str) -> Result<u64> {
    let s = s.trim();
    let last = s.chars().last().context("empty size")?;
    let (digits, mult) = match last {
        'K' | 'k' => (&s[..s.len() - 1], 1u64 << 10),
        'M' | 'm' => (&s[..s.len() - 1], 1u64 << 20),
        'G' | 'g' => (&s[..s.len() - 1], 1u64 << 30),
        'T' | 't' => (&s[..s.len() - 1], 1u64 << 40),
        c if c.is_ascii_digit() => (s, 1u64),
        c => bail!("bad size suffix {c:?} in {s:?}"),
    };
    let n: u64 = digits
        .trim()
        .parse()
        .with_context(|| format!("bad size number in {s:?}"))?;
    n.checked_mul(mult).context("size overflow")
}

/// A stable, nonzero project ID for an agent key, never colliding with the
/// reserved shared-home ID. FNV-1a over the key; collisions across the handful
/// of live agents are astronomically unlikely, and a collision only means two
/// agents share one cap.
pub fn projid_for(agent_key: &str) -> u32 {
    let mut h: u32 = 0x811c_9dc5;
    for b in agent_key.bytes() {
        h ^= b as u32;
        h = h.wrapping_mul(0x0100_0193);
    }
    if h < 2 {
        h + 2
    } else {
        h
    }
}

/// Crash exactly when a quota is configured but the filesystem can't enforce
/// it — never run silently unprotected, never crash when no quota was asked
/// for.
pub fn should_crash(active: bool, supported: bool) -> bool {
    active && !supported
}

/// The mount point of the filesystem containing `path` (longest mount-point
/// prefix in /proc/self/mountinfo). Quota tools operate per filesystem.
pub fn mountpoint_for(path: &Path) -> Result<PathBuf> {
    let data = std::fs::read_to_string("/proc/self/mountinfo")
        .context("read /proc/self/mountinfo")?;
    let mut best: Option<PathBuf> = None;
    for line in data.lines() {
        // Field 5 (0-indexed 4) is the mount point.
        if let Some(mp) = line.split(' ').nth(4) {
            let mp = PathBuf::from(mp);
            if path.starts_with(&mp) {
                if best.as_ref().map(|b| mp.as_os_str().len() > b.as_os_str().len()).unwrap_or(true) {
                    best = Some(mp);
                }
            }
        }
    }
    best.with_context(|| format!("no mount point found for {}", path.display()))
}

/// Whether project quota is on (enforcing) for the filesystem at `mountpoint`.
pub fn project_quota_supported(mountpoint: &Path) -> bool {
    let out = Command::new("quotaon")
        .arg("-Pp")
        .arg(mountpoint)
        .output();
    match out {
        Ok(o) => o.status.success() && String::from_utf8_lossy(&o.stdout).contains("is on"),
        Err(_) => false,
    }
}

/// Apply `projid` (with inheritance) to `dirs` and cap that project at
/// `bytes` on the filesystem at `mountpoint`. Idempotent.
pub fn apply(dirs: &[PathBuf], projid: u32, bytes: u64, mountpoint: &Path) -> Result<()> {
    for dir in dirs {
        if !dir.exists() {
            continue;
        }
        let status = Command::new("chattr")
            .arg("-R")
            .arg("-p")
            .arg(projid.to_string())
            .arg("+P")
            .arg(dir)
            .status()
            .context("spawn chattr")?;
        if !status.success() {
            bail!("chattr -p {projid} +P {} failed", dir.display());
        }
    }
    // setquota block limits are in 1 KiB blocks; round up.
    let hard_kib = bytes.div_ceil(1024);
    let status = Command::new("setquota")
        .arg("-P")
        .arg(projid.to_string())
        .arg("0")
        .arg(hard_kib.to_string())
        .arg("0")
        .arg("0")
        .arg(mountpoint)
        .status()
        .context("spawn setquota")?;
    if !status.success() {
        bail!("setquota -P {projid} on {} failed", mountpoint.display());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_size_units() {
        assert_eq!(parse_size("10G").unwrap(), 10 * (1 << 30));
        assert_eq!(parse_size("512M").unwrap(), 512 * (1 << 20));
        assert_eq!(parse_size("1024K").unwrap(), 1 << 20);
        assert_eq!(parse_size("4096").unwrap(), 4096);
        assert!(parse_size("10X").is_err());
        assert!(parse_size("").is_err());
    }

    #[test]
    fn projid_is_stable_nonzero_and_dodges_reserved() {
        let a = projid_for("ebd762f3-7aeb-4df9-bade-ead9f628ff47");
        assert_eq!(a, projid_for("ebd762f3-7aeb-4df9-bade-ead9f628ff47"));
        assert!(a >= 2);
        assert_ne!(a, SHARED_HOME_PROJID);
        assert_ne!(a, projid_for("d36fa73d-cd43-4773-a157-5ab658a49d10"));
    }

    #[test]
    fn crash_only_when_active_and_unsupported() {
        assert!(should_crash(true, false));
        assert!(!should_crash(true, true));
        assert!(!should_crash(false, false));
        assert!(!should_crash(false, true));
    }

    #[test]
    fn config_inactive_when_empty_or_absent() {
        let c = HostConfig::default();
        assert!(!c.active());
        let c: HostConfig = serde_json::from_str(r#"{"perAgentQuota":"10G"}"#).unwrap();
        assert!(c.active());
        assert_eq!(c.per_agent_bytes().unwrap(), Some(10 * (1 << 30)));
        assert_eq!(c.shared_home_bytes().unwrap(), None);
    }
}
