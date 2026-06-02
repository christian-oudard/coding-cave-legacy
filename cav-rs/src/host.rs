// `cav host setup` / `cav host teardown` — the imperative body of
// modules/host.nix's systemd unit. Lives in the cav binary so the unit
// definition is a thin shim that calls back into cav; logic stays in one
// place (Rust) and updates ride the cav binary instead of a unit-file rewrite.

use anyhow::{bail, Context, Result};
use std::path::Path;
use std::process::Command;

const STATE_DIR: &str = "/var/lib/coding-cave";
const IDMAP_TARGET: &str = "/var/lib/coding-cave/nix-store";
const IDMAP_OPTS: &str = "X-mount.idmap=u:0:1000:1 g:0:1000:1";

pub fn setup() -> Result<()> {
    if !rustix::process::geteuid().is_root() {
        bail!("cav host setup must be run as root");
    }
    std::fs::create_dir_all(STATE_DIR).context("create /var/lib/coding-cave")?;
    if !Path::new(IDMAP_TARGET).exists() {
        std::fs::create_dir(IDMAP_TARGET).context("create idmap target")?;
    }
    let status = Command::new("mount")
        .args(["--bind", "--read-only", "-o", IDMAP_OPTS, "/nix/store", IDMAP_TARGET])
        .status()
        .context("invoke mount")?;
    if !status.success() {
        bail!("mount failed: {status}");
    }
    Ok(())
}

pub fn teardown() -> Result<()> {
    if !rustix::process::geteuid().is_root() {
        bail!("cav host teardown must be run as root");
    }
    let status = Command::new("umount")
        .args(["--lazy", IDMAP_TARGET])
        .status()
        .context("invoke umount")?;
    if !status.success() {
        bail!("umount failed: {status}");
    }
    Ok(())
}
