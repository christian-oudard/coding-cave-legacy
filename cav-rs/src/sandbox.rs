use std::collections::HashMap;
use std::io::Write;
use std::os::fd::{AsRawFd, IntoRawFd, RawFd};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

use anyhow::{bail, Result};
use rustix::process::Signal;
use serde::Deserialize;

use crate::config::{self, inside_cave, parse_dev_branch, project_slug, shared_repo_path};
use crate::constants::{self, CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE};
use crate::{daemon, git, registry, style, user_env, workspace};

const SLIRP_DNS: &str = "10.0.2.3";
const SLIRP_GATEWAY: &str = "10.0.2.2";
const CRINJ_PORT: u16 = 10255;

#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
struct SandboxConfig {
    bwrap: String,
    static_args: Vec<String>,
    forward_env: Vec<String>,
    proc_mask_files: Vec<String>,
    proc_mask_dirs: Vec<String>,
    proc_mask: String,
    proc_mask_dir: String,
    proc_mask_meminfo: String,
    seccomp_filter: String,
    dot_claude_json_default: String,
    ca_cert_bundle: String,
    nix_config: String,
}

fn load_sandbox_config() -> Result<SandboxConfig> {
    let path = &constants::build().sandbox_config;
    if path.is_empty() {
        bail!("CODING_CAVE_BUILD_CONFIG not set. Run via 'nix build' or 'nix develop'.");
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| anyhow::anyhow!("Cannot read sandbox config {path}: {e}"))?;
    serde_json::from_str(&text).map_err(|e| anyhow::anyhow!("Bad sandbox config: {e}"))
}

fn write_cpuinfo() -> Result<PathBuf> {
    let ncpu = num_cpus();
    let mut content = String::new();
    for i in 0..ncpu {
        content.push_str(&format!(
            "processor\t: {i}\nvendor_id\t: GenericCPU\n\
             cpu family\t: 0\nmodel\t\t: 0\nmodel name\t: Generic CPU\n\
             stepping\t: 0\ncpu MHz\t\t: 0.000\ncache size\t: 0 KB\n\
             siblings\t: {ncpu}\ncore id\t\t: {i}\ncpu cores\t: {ncpu}\n\
             flags\t\t:\nbogomips\t: 0.00\n\n"
        ));
    }
    let path = temp_file("cave-cpuinfo-")?;
    std::fs::write(&path, content)?;
    Ok(path)
}

fn write_resolv_conf(dns_server: &str) -> Result<PathBuf> {
    let path = temp_file("cave-resolv-")?;
    std::fs::write(&path, format!("nameserver {dns_server}\n"))?;
    Ok(path)
}

/// Managed-settings file that puts the locally-generated cave-messages plugin
/// on Claude Code's approved-channels allowlist. Without it, --channels rejects
/// the plugin (not on the allowlist) and the development-channels flag prompts
/// an interactive confirmation that would wedge the headless launch.
fn write_managed_settings() -> Result<PathBuf> {
    let path = temp_file("cave-managed-settings-")?;
    let content = serde_json::json!({
        "channelsEnabled": true,
        "allowedChannelPlugins": [
            {"marketplace": "cave", "plugin": "cave-messages"}
        ],
    });
    std::fs::write(&path, serde_json::to_string(&content)?)?;
    Ok(path)
}

/// Validate user-supplied --dev paths. `--dev-bind` would mount any host path
/// onto any cave path, so we restrict to char/block device nodes under /dev.
/// `metadata` follows symlinks, so stable names like
/// /dev/serial/by-id/usb-...-if00 are accepted when they resolve to a real
/// device node.
fn validate_host_devices(paths: Vec<String>) -> Result<Vec<String>> {
    use std::os::unix::fs::FileTypeExt;
    let mut out = Vec::with_capacity(paths.len());
    for p in paths {
        let path = Path::new(&p);
        if !path.is_absolute() || !path.starts_with("/dev/") {
            bail!("--dev path must be absolute and under /dev/: {p}");
        }
        let md = std::fs::metadata(path)
            .map_err(|e| anyhow::anyhow!("--dev path {p}: {e}"))?;
        let ft = md.file_type();
        if !ft.is_char_device() && !ft.is_block_device() {
            bail!("--dev path is not a char or block device: {p}");
        }
        out.push(p);
    }
    Ok(out)
}

/// Resolve the GID we should adopt as primary so the cave's user-namespace
/// mapping passes through device group ownership.
///
/// bwrap creates a user_ns whose gid_map has one entry: inner-gid ->
/// caller's primary gid. Any other host gid renders as `nogroup` (65534)
/// inside, so the cave-coder user can't satisfy mode-660 group checks on
/// bind-mounted devices unless our primary gid IS the device's owning gid.
/// We engineer that by `setresgid`'ing to the device's group before exec'ing
/// bwrap. setresgid is gated on the target being one of {real, effective,
/// saved} gid for unprivileged callers (supplementary group membership
/// alone is not enough), so cav re-execs itself under `sg` up front (see
/// `ensure_egid_for_devices`) when the target gid isn't already settable.
///
/// First, though: if the host user can already open the device (mode bits,
/// POSIX ACL, whatever), `--dev-bind` preserves the inode and the kernel
/// access check inside the cave uses the host uid via uid_map, so the
/// existing grant carries over. Skip the gid dance entirely in that case.
///
/// Returns `Some(gid)` when devices need a shared owning group adopted as
/// primary; `None` when no adjustment is required.
fn device_passthrough_gid(host_devices: &[String]) -> Result<Option<u32>> {
    use std::os::unix::fs::MetadataExt;
    let Some(first) = host_devices.first() else {
        return Ok(None);
    };
    if host_devices.iter().all(|d| host_can_open(d)) {
        return Ok(None);
    }
    let target = std::fs::metadata(first)?.gid();
    for d in &host_devices[1..] {
        let g = std::fs::metadata(d)?.gid();
        if g != target {
            bail!(
                "--dev paths must share an owning group; got gid {target} for {first} but gid {g} for {d}"
            );
        }
    }
    if !host_user_in_group(target)? {
        let name = lookup_group_name(target)
            .unwrap_or_else(|| format!("gid {target}"));
        bail!(
            "host user is not in group {name} (owner of {first}); \
             add yourself with `sudo usermod -aG {name} $USER` and log back in"
        );
    }
    Ok(Some(target))
}

/// Try to open the device read+write. Success means the host user has
/// access via *some* mechanism (owner, group, or ACL) and the cave will
/// inherit it through the bind-mounted inode + uid_map.
fn host_can_open(path: &str) -> bool {
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .is_ok()
}

fn host_user_in_group(target: u32) -> Result<bool> {
    let cur = unsafe { libc::getgid() };
    if cur == target {
        return Ok(true);
    }
    let n = unsafe { libc::getgroups(0, std::ptr::null_mut()) };
    if n < 0 {
        bail!("getgroups: {}", std::io::Error::last_os_error());
    }
    let mut buf: Vec<libc::gid_t> = vec![0; n as usize];
    let n = unsafe { libc::getgroups(buf.len() as i32, buf.as_mut_ptr()) };
    if n < 0 {
        bail!("getgroups: {}", std::io::Error::last_os_error());
    }
    buf.truncate(n as usize);
    Ok(buf.iter().any(|&g| g == target))
}

/// If --dev devices require an egid we can't reach via `setresgid`, re-exec
/// cav under `sg <group> -c <cmd>` so the new process has that gid as its
/// primary. setresgid only accepts a target in {real, effective, saved}
/// gid for unprivileged callers; supplementary group membership is not
/// enough. `sg` (set-uid root, from shadow-utils) is the standard
/// system-provided way to adopt a supplementary group as primary.
///
/// On the re-exec branch this function does not return.
pub fn ensure_egid_for_devices(host_devices: &[String]) -> Result<()> {
    if host_devices.is_empty() {
        return Ok(());
    }
    let validated = validate_host_devices(host_devices.to_vec())?;
    let Some(target) = device_passthrough_gid(&validated)? else {
        return Ok(());
    };

    let (rgid, egid, sgid) = current_resgids();
    if target == rgid || target == egid || target == sgid {
        return Ok(());
    }

    let group_name = lookup_group_name(target)
        .ok_or_else(|| anyhow::anyhow!("no /etc/group entry for gid {target}"))?;
    let sg_path = which_in_path("sg").ok_or_else(|| {
        anyhow::anyhow!(
            "'sg' not found in PATH; install shadow-utils so cav can adopt \
             group '{group_name}' (gid {target}) as its primary gid"
        )
    })?;

    let exe = std::env::current_exe()
        .map_err(|e| anyhow::anyhow!("current_exe: {e}"))?;
    let mut shell_cmd = sh_quote(&exe.to_string_lossy());
    for arg in std::env::args().skip(1) {
        shell_cmd.push(' ');
        shell_cmd.push_str(&sh_quote(&arg));
    }

    use std::os::unix::process::CommandExt;
    let err = Command::new(&sg_path)
        .arg(&group_name)
        .arg("-c")
        .arg(&shell_cmd)
        .exec();
    Err(anyhow::anyhow!("exec {}: {err}", sg_path.display()))
}

fn current_resgids() -> (libc::gid_t, libc::gid_t, libc::gid_t) {
    let mut r: libc::gid_t = 0;
    let mut e: libc::gid_t = 0;
    let mut s: libc::gid_t = 0;
    unsafe { libc::getresgid(&mut r, &mut e, &mut s) };
    (r, e, s)
}

fn lookup_group_name(gid: libc::gid_t) -> Option<String> {
    use std::ffi::CStr;
    let g = unsafe { libc::getgrgid(gid) };
    if g.is_null() {
        return None;
    }
    let name = unsafe { CStr::from_ptr((*g).gr_name) };
    Some(name.to_string_lossy().into_owned())
}

fn which_in_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

fn sh_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('\'');
    for c in s.chars() {
        if c == '\'' {
            out.push_str("'\\''");
        } else {
            out.push(c);
        }
    }
    out.push('\'');
    out
}

#[cfg(test)]
mod validate_host_devices_tests {
    use super::validate_host_devices;

    #[test]
    fn accepts_char_device() {
        let v = validate_host_devices(vec!["/dev/null".to_string()]).unwrap();
        assert_eq!(v, vec!["/dev/null".to_string()]);
    }

    #[test]
    fn accepts_multiple_devices() {
        let v = validate_host_devices(vec![
            "/dev/null".to_string(),
            "/dev/zero".to_string(),
        ])
        .unwrap();
        assert_eq!(v.len(), 2);
    }

    #[test]
    fn rejects_relative_path() {
        let e = validate_host_devices(vec!["dev/null".to_string()]).unwrap_err();
        assert!(e.to_string().contains("absolute and under /dev/"));
    }

    #[test]
    fn rejects_path_outside_dev() {
        let e = validate_host_devices(vec!["/etc/passwd".to_string()]).unwrap_err();
        assert!(e.to_string().contains("absolute and under /dev/"));
    }

    #[test]
    fn rejects_devil_path() {
        // Component-wise prefix check: /devil must not match /dev/.
        let e = validate_host_devices(vec!["/devil/foo".to_string()]).unwrap_err();
        assert!(e.to_string().contains("absolute and under /dev/"));
    }

    #[test]
    fn rejects_directory_under_dev() {
        // /dev itself is a directory, not a device node. Prevents users from
        // binding the entire host devtmpfs into the cave.
        let e = validate_host_devices(vec!["/dev".to_string()]).unwrap_err();
        assert!(e.to_string().contains("not a char or block device"));
    }

    #[test]
    fn rejects_missing_path() {
        let e = validate_host_devices(vec![
            "/dev/__cav_nonexistent_xyz123".to_string(),
        ])
        .unwrap_err();
        assert!(e.to_string().contains("--dev path"));
    }
}

#[cfg(test)]
mod device_passthrough_gid_tests {
    use super::device_passthrough_gid;
    use std::os::unix::fs::MetadataExt;

    #[test]
    fn empty_returns_none() {
        assert_eq!(device_passthrough_gid(&[]).unwrap(), None);
    }

    #[test]
    fn mismatched_gids_bail() {
        // Different host environments map /dev gids differently (notably,
        // inside an unprivileged user_ns they all collapse to 65534). Skip
        // when /dev/null and /dev/tty are indistinguishable so the test is
        // meaningful only where the kernel actually exposes their gids.
        let g0 = std::fs::metadata("/dev/null").unwrap().gid();
        let g1 = std::fs::metadata("/dev/tty").unwrap().gid();
        if g0 == g1 {
            eprintln!("skip: /dev/null and /dev/tty share gid {g0} in this env");
            return;
        }
        let e = device_passthrough_gid(&[
            "/dev/null".to_string(),
            "/dev/tty".to_string(),
        ])
        .unwrap_err();
        assert!(e.to_string().contains("must share an owning group"));
    }
}

#[cfg(test)]
mod sh_quote_tests {
    use super::sh_quote;

    #[test]
    fn plain_word() {
        assert_eq!(sh_quote("foo"), "'foo'");
    }

    #[test]
    fn embedded_space() {
        assert_eq!(sh_quote("a b"), "'a b'");
    }

    #[test]
    fn embedded_single_quote() {
        // POSIX: end the quote, escape the literal ', re-open the quote.
        assert_eq!(sh_quote("it's"), "'it'\\''s'");
    }

    #[test]
    fn shell_metacharacters_are_inert() {
        let s = "$(rm -rf /); `whoami` && echo *";
        let q = sh_quote(s);
        // Every metacharacter is inside a single-quoted segment, so the
        // shell sees them as literal.
        assert!(q.starts_with('\''));
        assert!(q.ends_with('\''));
        assert!(!q.contains("''"));
    }
}

fn temp_file(prefix: &str) -> Result<PathBuf> {
    let tmp = std::env::temp_dir();
    for i in 0..10000 {
        let p = tmp.join(format!("{prefix}{i}"));
        if std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&p)
            .is_ok()
        {
            return Ok(p);
        }
    }
    bail!("Could not create temp file with prefix {prefix}")
}

fn num_cpus() -> usize {
    let n = rustix::process::sched_getaffinity(None)
        .map(|s| s.count() as usize)
        .unwrap_or(0);
    n.max(1)
}

fn build_bwrap_argv(
    cfg: &SandboxConfig,
    env: &HashMap<String, String>,
    claude_cmd: &[String],
    seccomp_fd: RawFd,
) -> Vec<String> {
    let mut args: Vec<String> = vec![cfg.bwrap.clone()];
    args.extend(cfg.static_args.iter().cloned());

    for var in &cfg.forward_env {
        if let Some(val) = env.get(var) {
            args.extend(["--setenv".to_string(), var.clone(), val.clone()]);
        }
    }

    // Top-level cave: bind its own chroot store over /nix/store and /nix/var
    // (read-write), replacing the static_args ro-bind of the host /nix/store.
    // The cave's nix uses its own store and DB and builds into it as itself.
    // A nested cave leaves CODING_CAVE_STORE_ROOT unset and keeps the
    // inherited read-only /nix/store from the host cave (and the base
    // NIX_CONFIG, set just below for every cave).
    let nix_config = cfg.nix_config.clone();
    if let Some(root) = env.get("CODING_CAVE_STORE_ROOT") {
        let store = format!("{root}/nix/store");
        let var = format!("{root}/nix/var");
        args.extend(["--bind".to_string(), store, "/nix/store".to_string()]);
        args.extend(["--bind".to_string(), var, "/nix/var".to_string()]);
    }
    args.extend(["--setenv".to_string(), "NIX_CONFIG".to_string(), nix_config]);

    if Path::new("/dev/net/tun").exists() {
        args.extend([
            "--dev-bind".to_string(),
            "/dev/net/tun".to_string(),
            "/dev/net/tun".to_string(),
        ]);
    }

    if let Some(list) = env.get("CODING_CAVE_HOST_DEVICES") {
        for dev in list.split(':').filter(|s| !s.is_empty()) {
            args.extend([
                "--dev-bind".to_string(),
                dev.to_string(),
                dev.to_string(),
            ]);
        }
    }

    let cave_home = env.get("CODING_CAVE_HOME").cloned().unwrap_or_default();
    args.extend([
        "--bind".to_string(), cave_home.clone(), "/home/cave-coder".to_string(),
    ]);

    let claude_json = PathBuf::from(&cave_home).join(".claude.json");
    if !claude_json.exists() && !cfg.dot_claude_json_default.is_empty() {
        let _ = std::fs::copy(&cfg.dot_claude_json_default, &claude_json);
    }

    args.extend(["--dir".to_string(), "/projects".to_string()]);
    let workspaces: Vec<&str> = env
        .get("CODING_CAVE_WORKSPACES")
        .map(|s| s.as_str())
        .unwrap_or("")
        .split(':')
        .collect();
    let slugs: Vec<&str> = env
        .get("CODING_CAVE_PROJECT_SLUGS")
        .map(|s| s.as_str())
        .unwrap_or("")
        .split(':')
        .collect();
    for (ws, slug) in workspaces.iter().zip(slugs.iter()) {
        if !ws.is_empty() && !slug.is_empty() {
            args.extend([
                "--bind".to_string(), ws.to_string(), format!("/projects/{slug}"),
            ]);
        }
    }

    let shared: Vec<&str> = env
        .get("CODING_CAVE_SHARED_REPOS")
        .map(|s| s.as_str())
        .unwrap_or("")
        .split(':')
        .collect();
    for (repo, slug) in shared.iter().zip(slugs.iter()) {
        if !repo.is_empty() && !slug.is_empty() {
            args.extend([
                "--ro-bind".to_string(), repo.to_string(),
                format!("/projects/{slug}/.coding-cave/host-cave-shared.git"),
            ]);
        }
    }

    if let Some(sock) = env.get("CODING_CAVE_GPG_SOCKET") {
        if Path::new(sock).exists() {
            args.extend([
                "--bind".to_string(), sock.clone(),
                "/home/cave-coder/.gnupg/S.gpg-agent".to_string(),
                "--dir".to_string(), "/run/user/1000/gnupg".to_string(),
                "--bind".to_string(), sock.clone(),
                "/run/user/1000/gnupg/S.gpg-agent".to_string(),
            ]);
        }
    }
    if let Some(pub_) = env.get("CODING_CAVE_GPG_PUBRING") {
        if Path::new(pub_).is_file() {
            let name = Path::new(pub_)
                .file_name()
                .unwrap_or_default()
                .to_string_lossy();
            args.extend([
                "--ro-bind".to_string(), pub_.clone(),
                format!("/home/cave-coder/.gnupg/{name}"),
            ]);
        }
    }

    if let Some(ca) = env.get("CODING_CAVE_PROXY_CA") {
        if Path::new(ca).is_file() {
            args.extend([
                "--ro-bind".to_string(), ca.clone(), "/etc/crinj-ca.pem".to_string(),
            ]);
        }
    }

    // Channel messaging: bind broker socket and expose env to the MCP server.
    if let Some(sock) = env.get("CODING_CAVE_MSG_SOCK") {
        if Path::new(sock).exists() {
            args.extend([
                "--bind".to_string(), sock.clone(), "/run/cave-channel.sock".to_string(),
                "--setenv".to_string(), "CAVE_MSG_SOCK".to_string(),
                "/run/cave-channel.sock".to_string(),
            ]);
            if let Some(aid) = env.get("CODING_CAVE_AGENT_ID") {
                args.extend([
                    "--setenv".to_string(), "CAVE_AGENT_ID".to_string(), aid.clone(),
                ]);
            }
            if let Ok(settings) = write_managed_settings() {
                args.extend([
                    "--ro-bind".to_string(),
                    settings.to_string_lossy().to_string(),
                    "/etc/claude-code/managed-settings.json".to_string(),
                ]);
            }
        }
    }

    if let Some(dns) = env.get("CODING_CAVE_DNS_SERVER") {
        if let Ok(resolv) = write_resolv_conf(dns) {
            args.extend([
                "--ro-bind".to_string(),
                resolv.to_string_lossy().to_string(),
                "/etc/resolv.conf".to_string(),
            ]);
        }
    }

    for p in &cfg.proc_mask_files {
        args.extend([
            "--ro-bind-try".to_string(), cfg.proc_mask.clone(), p.clone(),
        ]);
    }
    for d in &cfg.proc_mask_dirs {
        args.extend([
            "--ro-bind-try".to_string(), cfg.proc_mask_dir.clone(), d.clone(),
        ]);
    }
    args.extend([
        "--ro-bind".to_string(),
        cfg.proc_mask_meminfo.clone(),
        "/proc/meminfo".to_string(),
    ]);

    if let Ok(cpuinfo) = write_cpuinfo() {
        args.extend([
            "--ro-bind".to_string(),
            cpuinfo.to_string_lossy().to_string(),
            "/proc/cpuinfo".to_string(),
        ]);
    }

    args.extend(["--seccomp".to_string(), seccomp_fd.to_string()]);
    extend_runtime_env(&mut args, env);
    if let Some(slug) = env.get("CODING_CAVE_PROJECT_SLUG") {
        args.extend(["--chdir".to_string(), format!("/projects/{slug}")]);
    }

    args.extend_from_slice(claude_cmd);
    args
}

/// Build the per-launch environment that bwrap should inject via --setenv:
/// PATH, the home-manager session vars (env.json), HTTP proxy and TLS-CA
/// vars when a crinj proxy is wired up. Replaces what the old bash entry
/// script exported on every launch.
fn extend_runtime_env(args: &mut Vec<String>, env: &HashMap<String, String>) {
    let mut path = vec!["/home/cave-coder/bin".to_string()];
    if let Some(uenv) = env.get("CODING_CAVE_USER_ENV") {
        path.push(format!("{uenv}/home-path/bin"));
    }
    if let Some(cb) = env.get("CODING_CAVE_BIN") {
        path.push(cb.clone());
    }
    args.extend(["--setenv".to_string(), "PATH".to_string(), path.join(":")]);

    if let Some(uenv) = env.get("CODING_CAVE_USER_ENV") {
        let env_json = Path::new(uenv).join("env.json");
        if let Ok(text) = std::fs::read_to_string(&env_json) {
            if let Ok(map) = serde_json::from_str::<HashMap<String, String>>(&text) {
                for (k, v) in map {
                    args.extend(["--setenv".to_string(), k, v]);
                }
            }
        }
    }

    if let Some(addr) = env.get("CODING_CAVE_PROXY_ADDR") {
        let url = format!("http://{addr}");
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"] {
            args.extend(["--setenv".to_string(), k.to_string(), url.clone()]);
        }
        for k in ["NO_PROXY", "no_proxy"] {
            args.extend([
                "--setenv".to_string(),
                k.to_string(),
                "localhost,127.0.0.1,::1".to_string(),
            ]);
        }
    }

    if env.contains_key("CODING_CAVE_PROXY_CA") {
        let combined = "/home/cave-coder/.cave-ca-combined.pem".to_string();
        for k in ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"] {
            args.extend(["--setenv".to_string(), k.to_string(), combined.clone()]);
        }
        args.extend([
            "--setenv".to_string(),
            "NODE_EXTRA_CA_CERTS".to_string(),
            "/etc/crinj-ca.pem".to_string(),
        ]);
    }
}

fn read_git_global(key: &str) -> Option<String> {
    Command::new("git")
        .args(["config", "--global", key])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
}

fn host_gitconfig() -> Option<PathBuf> {
    let home = host_home();
    let xdg = std::env::var("XDG_CONFIG_HOME")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));
    [home.join(".gitconfig"), xdg.join("git").join("config")]
        .into_iter()
        .find(|p| p.exists())
}

fn gpg_homedir() -> Option<PathBuf> {
    let out = Command::new("gpgconf")
        .args(["--list-dirs", "homedir"])
        .output()
        .ok()?;
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() { None } else { Some(PathBuf::from(s)) }
}

fn gpg_socket() -> Option<String> {
    let out = Command::new("gpgconf")
        .args(["--list-dirs", "agent-extra-socket"])
        .output()
        .ok();
    let sock = out
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();

    if !sock.is_empty() && Path::new(&sock).exists() {
        return Some(sock);
    }
    if !inside_cave() {
        let _ = Command::new("gpgconf").args(["--launch", "gpg-agent"]).output();
        if !sock.is_empty() && Path::new(&sock).exists() {
            return Some(sock);
        }
        return None;
    }
    if let Some(hd) = gpg_homedir() {
        let bound = hd.join("S.gpg-agent");
        if bound.exists() {
            return Some(bound.to_string_lossy().to_string());
        }
    }
    None
}

fn host_home() -> PathBuf {
    std::env::var("CODING_CAVE_HOST_HOME")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var("HOME")
                .ok()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/root"))
        })
}

fn crinj_ca_path() -> Result<PathBuf> {
    if inside_cave() {
        let p = PathBuf::from("/etc/crinj-ca.pem");
        if p.exists() {
            return Ok(p);
        }
    }
    let hh = host_home();
    let home = PathBuf::from(std::env::var("HOME").unwrap_or_default());
    let xdg = std::env::var("XDG_DATA_HOME")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| hh.join(".local").join("share"));
    for p in [
        home.join(".crinj").join("gateway").join("ca.pem"),
        hh.join(".crinj").join("gateway").join("ca.pem"),
        xdg.join("crinj").join("gateway").join("ca.pem"),
    ] {
        if p.exists() {
            return Ok(p);
        }
    }
    bail!("crinj CA certificate not found. Expected at ~/.crinj/gateway/ca.pem")
}

fn crinj_listening() -> bool {
    std::net::TcpStream::connect(format!("127.0.0.1:{CRINJ_PORT}")).is_ok()
}

/// Directory holding all crinj runtime state: the pid file, the session
/// refcount dir, and per-instance logs.
fn crinj_dir() -> PathBuf {
    config::data_dir().join("crinj")
}

fn crinj_sessions_dir() -> PathBuf {
    crinj_dir().join("sessions")
}

fn crinj_logs_dir() -> PathBuf {
    crinj_dir().join("logs")
}

fn crinj_pid_file() -> PathBuf {
    crinj_dir().join("pid")
}

fn crinj_signal(sig: Signal) {
    if let Ok(text) = std::fs::read_to_string(crinj_pid_file()) {
        if let Ok(raw) = text.trim().parse::<i32>() {
            if let Some(pid) = rustix::process::Pid::from_raw(raw) {
                let _ = rustix::process::kill_process(pid, sig);
            }
        }
    }
}

/// Remove dead-pid marker files from a service's session dir. Shared by the
/// crinj and broker refcounts, pruning is run both at registration and at
/// unregistration so a SIGKILL'd cave never leaves a stale marker that keeps
/// the genuinely-last cave from seeing the dir as empty.
fn prune_dead_sessions(dir: &Path) {
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            if let Ok(raw) = entry.file_name().to_string_lossy().parse::<i32>() {
                if !pid_alive(raw) {
                    let _ = std::fs::remove_file(entry.path());
                }
            }
        }
    }
}

fn pid_alive(raw: i32) -> bool {
    let Some(pid) = rustix::process::Pid::from_raw(raw) else {
        return false;
    };
    match rustix::process::test_kill_process(pid) {
        Ok(()) => true,
        Err(e) => e == rustix::io::Errno::PERM,
    }
}

/// Register this process in a service's session dir, returning the marker path.
fn register_session(dir: &Path) -> PathBuf {
    let _ = std::fs::create_dir_all(dir);
    prune_dead_sessions(dir);
    let f = dir.join(std::process::id().to_string());
    let _ = std::fs::File::create(&f);
    f
}

/// Drop this process's marker and report whether no live session remains, the
/// caller's cue to stop the shared service. Prunes dead markers first so a
/// crashed cave's leftover marker doesn't mask the last live cave's exit.
fn unregister_session(session: &Path, dir: &Path) -> bool {
    let _ = std::fs::remove_file(session);
    prune_dead_sessions(dir);
    dir.read_dir().map(|mut e| e.next().is_none()).unwrap_or(true)
}

#[cfg(test)]
mod session_refcount_tests {
    use super::*;

    fn tmp_dir(label: &str) -> PathBuf {
        static SEQ: AtomicI32 = AtomicI32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let p = std::env::temp_dir()
            .join(format!("cav-session-{}-{label}-{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn marker(dir: &Path, pid: i32) {
        std::fs::File::create(dir.join(pid.to_string())).unwrap();
    }

    /// A pid that has been spawned and reaped, so it is reliably not alive.
    fn dead_pid() -> i32 {
        let mut child = Command::new("true").spawn().unwrap();
        let pid = child.id() as i32;
        let _ = child.wait();
        pid
    }

    #[test]
    fn register_prunes_dead_marker() {
        let dir = tmp_dir("reg-prune");
        marker(&dir, dead_pid());
        register_session(&dir);
        let names: Vec<_> = dir
            .read_dir()
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert_eq!(names, vec![std::process::id().to_string()]);
    }

    #[test]
    fn unregister_not_empty_while_another_live_session_remains() {
        let dir = tmp_dir("unreg-live");
        let mine = register_session(&dir);
        let mut other = Command::new("sleep").arg("30").spawn().unwrap();
        marker(&dir, other.id() as i32);
        assert!(!unregister_session(&mine, &dir));
        let _ = other.kill();
        let _ = other.wait();
    }

    #[test]
    fn unregister_empty_despite_stale_dead_marker() {
        // The leak fix: a SIGKILL'd cave's leftover marker must not stop the
        // last live cave from seeing the dir as empty and stopping the service.
        let dir = tmp_dir("unreg-stale");
        let mine = register_session(&dir);
        marker(&dir, dead_pid());
        assert!(unregister_session(&mine, &dir));
    }
}

/// Severity of a crinj log event. Ordering enables `>= Warn` etc.
#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum CrinjLevel {
    Other,
    Info,
    Warn,
    Error,
}

impl CrinjLevel {
    fn parse(s: &str) -> Self {
        match s {
            "ERROR" => Self::Error,
            "WARN" => Self::Warn,
            "INFO" => Self::Info,
            _ => Self::Other,
        }
    }
}

/// Handle to the crinj log for the instance this cav spawned. Each instance
/// logs to its own timestamp-named file, so the file holds exactly the
/// current instance's output; events are read from it on demand rather than
/// streamed.
struct CrinjState {
    log_path: PathBuf,
}

/// Name for a new crinj instance's log file, e.g. `crinj-20260705-093042.log`.
/// Local time, zero-padded so names sort chronologically.
fn crinj_log_name() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0) as libc::time_t;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe { libc::localtime_r(&secs, &mut tm) };
    format!(
        "crinj-{:04}{:02}{:02}-{:02}{:02}{:02}.log",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
    )
}

/// Delete all but the newest `keep` crinj logs in `dir`. The timestamp
/// names sort chronologically, so lexicographic order is age order.
fn prune_crinj_logs(dir: &Path, keep: usize) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut logs: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            let name = p.file_name().unwrap_or_default().to_string_lossy();
            name.starts_with("crinj-") && name.ends_with(".log")
        })
        .collect();
    logs.sort();
    for old in logs.iter().rev().skip(keep) {
        let _ = std::fs::remove_file(old);
    }
}

/// Render every line crinj has logged this session.
fn read_crinj_events(state: &CrinjState) -> Vec<(CrinjLevel, String)> {
    let Ok(text) = std::fs::read_to_string(&state.log_path) else {
        return Vec::new();
    };
    text.lines().map(render_crinj_event).collect()
}

/// Right before claude takes over the terminal: surface what crinj logged
/// during boot (WARN/ERROR, or everything with -v). Anything crinj logs
/// after this lands only in the log file and gets replayed at session exit
/// if it's a warning/error.
fn print_crinj_boot_events(state: &CrinjState) {
    let threshold = if config::verbose() >= 1 {
        CrinjLevel::Info
    } else {
        CrinjLevel::Warn
    };
    for (level, pretty) in read_crinj_events(state) {
        if level >= threshold {
            eprintln!("{pretty}");
        }
    }
}

#[cfg(test)]
mod crinj_log_tests {
    use super::*;

    #[test]
    fn prune_keeps_newest_logs() {
        let dir = std::env::temp_dir().join(format!("prune-test-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        for name in [
            "crinj-20260101-000000.log",
            "crinj-20260102-000000.log",
            "crinj-20260103-000000.log",
            "unrelated.txt",
        ] {
            std::fs::write(dir.join(name), "x").unwrap();
        }
        prune_crinj_logs(&dir, 2);
        assert!(!dir.join("crinj-20260101-000000.log").exists());
        assert!(dir.join("crinj-20260102-000000.log").exists());
        assert!(dir.join("crinj-20260103-000000.log").exists());
        assert!(dir.join("unrelated.txt").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn log_name_is_timestamped() {
        let name = crinj_log_name();
        assert!(name.starts_with("crinj-20"));
        assert!(name.ends_with(".log"));
        assert_eq!(name.len(), "crinj-20260705-093042.log".len());
    }
}

/// Render one JSON-formatted crinj log line into a human-readable form.
/// Falls back to the raw line if it doesn't parse as the expected shape.
/// crinj uses Go's `log/slog` JSON handler: top-level `time`, `level`, `msg`,
/// with all structured attributes flat alongside them.
fn render_crinj_event(line: &str) -> (CrinjLevel, String) {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
        return (CrinjLevel::Other, line.to_string());
    };
    let Some(map) = value.as_object() else {
        return (CrinjLevel::Other, line.to_string());
    };
    let level_str = map.get("level").and_then(|v| v.as_str()).unwrap_or("");
    let level = CrinjLevel::parse(level_str);
    let message = map.get("msg").and_then(|v| v.as_str()).unwrap_or("");
    let mut extras: Vec<String> = Vec::new();
    for (k, v) in map {
        if k == "time" || k == "level" || k == "msg" {
            continue;
        }
        let rendered = match v {
            serde_json::Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        extras.push(format!("{k}={rendered}"));
    }
    let pretty = if extras.is_empty() {
        format!("{level_str} {message}")
    } else {
        format!("{level_str} {message} ({})", extras.join(", "))
    };
    (level, pretty)
}

#[cfg(test)]
mod render_crinj_event_tests {
    use super::*;

    #[test]
    fn parses_slog_info_with_attrs() {
        let line = r#"{"time":"2026-05-14T21:22:01Z","level":"INFO","msg":"starting crinj","data_dir":"/home/foo"}"#;
        let (level, pretty) = render_crinj_event(line);
        assert_eq!(level, CrinjLevel::Info);
        assert_eq!(pretty, "INFO starting crinj (data_dir=/home/foo)");
    }

    #[test]
    fn parses_slog_error_with_multiple_attrs() {
        let line = r#"{"time":"2026-05-14T21:22:01Z","level":"ERROR","msg":"loading config","error":"boom","config":"/x/y.toml"}"#;
        let (level, pretty) = render_crinj_event(line);
        assert_eq!(level, CrinjLevel::Error);
        assert!(pretty.starts_with("ERROR loading config ("));
        assert!(pretty.contains("error=boom"));
        assert!(pretty.contains("config=/x/y.toml"));
    }

    #[test]
    fn message_only_when_no_extras() {
        let line = r#"{"time":"t","level":"INFO","msg":"ready"}"#;
        let (level, pretty) = render_crinj_event(line);
        assert_eq!(level, CrinjLevel::Info);
        assert_eq!(pretty, "INFO ready");
    }

    #[test]
    fn falls_back_on_non_json() {
        let (level, pretty) = render_crinj_event("plain text");
        assert_eq!(level, CrinjLevel::Other);
        assert_eq!(pretty, "plain text");
    }
}

fn start_crinj() -> Result<(Option<std::process::Child>, Option<CrinjState>)> {
    if !which_exists("crinj") {
        bail!("crinj not found on PATH");
    }
    if crinj_listening() {
        crate::log_debug!("crinj already listening, sending SIGHUP to reload config");
        crinj_signal(Signal::Hup);
        return Ok((None, None));
    }
    crate::log_debug!("starting crinj...");
    let xdg = std::env::var("XDG_CONFIG_HOME")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| host_home().join(".config"));
    let rules = xdg.join("crinj").join("rules.toml");
    if !rules.exists() {
        let _ = std::fs::create_dir_all(rules.parent().unwrap());
        let _ = std::fs::File::create(&rules);
    }
    let logs_dir = crinj_logs_dir();
    let _ = std::fs::create_dir_all(&logs_dir);
    // crinj outlives this cav process (session refcount), so it must own its
    // log destination: --log-file makes crinj write the log itself, with
    // nothing routed through cav. A stderr pipe's reader would die with cav
    // and crinj's next log write would hit EPIPE. Each instance gets a
    // timestamp-named file, so a crashed instance's log survives its
    // replacement starting up.
    prune_crinj_logs(&logs_dir, 9);
    let log_path = logs_dir.join(crinj_log_name());
    use std::os::unix::process::CommandExt;
    let mut cmd = Command::new("crinj");
    cmd.args([
        "--port",
        &CRINJ_PORT.to_string(),
        "--allow-empty-rules",
        "--log-format=json",
        "--log-file",
    ])
    .arg(&log_path)
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::null());
    // Detach crinj into its own session so terminal signals to the spawning
    // cave's terminal (Ctrl-C SIGINT, close-time SIGHUP) can't reach it. The
    // cave itself is shielded by bwrap --new-session, but crinj sits in cav's
    // session; without setsid a Ctrl-C would kill the shared proxy and strand
    // every other cave. Lifetime is governed solely by the session refcount.
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let mut proc = cmd.spawn()?;
    let state = CrinjState { log_path };

    let start = Instant::now();
    let deadline = start + Duration::from_secs(5);
    loop {
        if crinj_listening() {
            crate::log_debug!("crinj ready after {:.1}s", start.elapsed().as_secs_f64());
            break;
        }
        if let Some(status) = proc.try_wait()? {
            let events = read_crinj_events(&state);
            let dump = if events.is_empty() {
                "(no log output)".to_string()
            } else {
                events
                    .iter()
                    .map(|(_, p)| format!("  {p}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            };
            bail!("crinj exited with {status} before becoming ready:\n{dump}");
        }
        if Instant::now() > deadline {
            bail!("crinj failed to start within 5s");
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    let _ = std::fs::write(crinj_pid_file(), proc.id().to_string());
    Ok((Some(proc), Some(state)))
}

/// At session shutdown: if crinj logged any warnings or errors during this
/// run, print a one-time replay so the user notices issues that scrolled
/// past during boot or happened silently while claude held the terminal.
fn print_crinj_session_warnings(state: &CrinjState) {
    let events = read_crinj_events(state);
    let alerts: Vec<&String> = events
        .iter()
        .filter(|(l, _)| *l >= CrinjLevel::Warn)
        .map(|(_, p)| p)
        .collect();
    if alerts.is_empty() {
        return;
    }
    let plural = if alerts.len() == 1 { "" } else { "s" };
    eprintln!(
        "crinj logged {} alert{plural} this session:",
        style::bold(&alerts.len().to_string()),
    );
    for a in alerts {
        eprintln!("  {a}");
    }
}

fn which_exists(name: &str) -> bool {
    for dir in std::env::var("PATH").unwrap_or_default().split(':') {
        if Path::new(dir).join(name).is_file() {
            return true;
        }
    }
    false
}

// -- Channel broker (inter-agent messaging) --

fn broker_dir() -> PathBuf {
    config::data_dir().join("channel")
}

fn broker_socket_path() -> PathBuf {
    broker_dir().join("broker.sock")
}

fn broker_pid_file() -> PathBuf {
    broker_dir().join("broker.pid")
}

fn broker_sessions_dir() -> PathBuf {
    broker_dir().join("sessions")
}

fn broker_alive() -> bool {
    let pid_file = broker_pid_file();
    let sock = broker_socket_path();
    if !pid_file.exists() || !sock.exists() {
        return false;
    }
    let Ok(text) = std::fs::read_to_string(&pid_file) else { return false };
    let Ok(raw) = text.trim().parse::<i32>() else { return false };
    pid_alive(raw)
}

fn start_broker() -> Result<Option<std::process::Child>> {
    if broker_alive() {
        return Ok(None);
    }
    let broker_bin = PathBuf::from(&constants::build().cav_bin).join("cav-comms-broker");
    if !broker_bin.exists() {
        bail!("cav-comms-broker not found at {}", broker_bin.display());
    }
    let sock = broker_socket_path();
    let _ = std::fs::create_dir_all(sock.parent().unwrap());
    let _ = std::fs::remove_file(&sock);

    // Redirect stderr to a log file. Piping without a reader would wedge
    // the broker once the 64 KB pipe buffer fills with log lines.
    let log_path = broker_dir().join("broker.log");
    let log_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)?;
    let proc = Command::new(&broker_bin)
        .arg(&sock)
        .stdout(Stdio::null())
        .stderr(Stdio::from(log_file))
        .spawn()?;

    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if sock.exists() {
            break;
        }
        if Instant::now() > deadline {
            bail!("broker failed to start within 5s");
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    let _ = std::fs::write(broker_pid_file(), proc.id().to_string());
    Ok(Some(proc))
}

fn stop_broker() {
    if let Ok(text) = std::fs::read_to_string(broker_pid_file()) {
        if let Ok(raw) = text.trim().parse::<i32>() {
            if let Some(pid) = rustix::process::Pid::from_raw(raw) {
                let _ = rustix::process::kill_process(pid, Signal::Term);
            }
        }
    }
    let _ = std::fs::remove_file(broker_pid_file());
}

/// Write the cave-messages plugin marketplace into the persistent home.
/// Claude Code discovers it via extraKnownMarketplaces in settings.json.
fn setup_channel_marketplace(home: &Path) -> Result<()> {
    let mp_dir = home.join(".cave-marketplace");
    let mp_meta = mp_dir.join(".claude-plugin");
    let pl_meta = mp_dir.join("cave-messages").join(".claude-plugin");
    std::fs::create_dir_all(&pl_meta)?;
    std::fs::create_dir_all(&mp_meta)?;

    let marketplace = serde_json::json!({
        "name": "cave",
        "owner": {"name": "coding-cave"},
        "plugins": [{
            "name": "cave-messages",
            "source": "./cave-messages",
            "version": "0.1.0",
            "description": "Inter-agent messaging via Unix socket broker",
        }],
    });
    std::fs::write(
        mp_meta.join("marketplace.json"),
        serde_json::to_string_pretty(&marketplace)?,
    )?;

    let server_bin = PathBuf::from(&constants::build().cav_bin).join("cav-comms-channel-server");
    let plugin = serde_json::json!({
        "name": "cave-messages",
        "version": "0.1.0",
        "description": "Inter-agent messaging channel",
        "mcpServers": {
            "cave-messages": {
                "command": server_bin.to_string_lossy(),
                "args": [],
            },
        },
        "channels": [{"server": "cave-messages"}],
    });
    std::fs::write(
        pl_meta.join("plugin.json"),
        serde_json::to_string_pretty(&plugin)?,
    )?;

    Ok(())
}

fn apply_firewall(pid: u32, allowed_ports: &[u16]) -> Result<()> {
    let nsenter = find_bin("nsenter")?;
    let iptables_restore = find_bin("iptables-restore")?;
    let mut rules = "*filter\n".to_string();
    for port in allowed_ports {
        rules.push_str(&format!(
            "-A OUTPUT -d {SLIRP_GATEWAY} -p tcp --dport {port} -j ACCEPT\n"
        ));
    }
    rules.push_str(&format!("-A OUTPUT -d {SLIRP_GATEWAY} -j DROP\nCOMMIT\n"));

    let r = Command::new(&nsenter)
        .args([
            "--preserve-credentials",
            &format!("--user=/proc/{pid}/ns/user"),
            &format!("--net=/proc/{pid}/ns/net"),
            &iptables_restore,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .and_then(|mut child| {
            child.stdin.as_mut().unwrap().write_all(rules.as_bytes())?;
            drop(child.stdin.take());
            child.wait_with_output()
        });
    match r {
        Ok(out) if out.status.success() => Ok(()),
        Ok(out) => bail!(
            "iptables-restore failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ),
        Err(e) => bail!("iptables-restore exec failed: {e}"),
    }
}

fn start_slirp4netns(pid: u32) -> Result<std::process::Child> {
    use std::os::unix::process::CommandExt;
    let slirp = find_bin("slirp4netns")?;
    let (ready_r, ready_w) = rustix::pipe::pipe()?;
    let ready_w_fd = ready_w.as_raw_fd();
    let mut cmd = Command::new(&slirp);
    cmd.args([
            "--configure",
            &format!("--ready-fd={ready_w_fd}"),
            &pid.to_string(),
            "tap0",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0);
    // slirp4netns reads ready-fd from the child, keep it open across exec.
    unsafe {
        cmd.pre_exec(move || {
            let flags = libc::fcntl(ready_w_fd, libc::F_GETFD);
            if flags >= 0 {
                libc::fcntl(ready_w_fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC);
            }
            Ok(())
        });
    }
    let mut proc = cmd.spawn()?;
    drop(ready_w);
    let mut buf = [0u8; 1];
    let n = rustix::io::read(&ready_r, &mut buf).unwrap_or(0);
    if n == 0 {
        let status = proc.wait().ok().and_then(|s| s.code()).unwrap_or(-1);
        bail!("slirp4netns exited with code {status} before signaling ready");
    }
    if let Ok(Some(status)) = proc.try_wait() {
        bail!("slirp4netns exited with code {}", status.code().unwrap_or(-1));
    }
    Ok(proc)
}

fn find_bin(name: &str) -> Result<String> {
    for dir in std::env::var("PATH").unwrap_or_default().split(':') {
        let p = Path::new(dir).join(name);
        if p.is_file() {
            return Ok(p.to_string_lossy().to_string());
        }
    }
    bail!("{name} not found on PATH")
}

fn signal_descendants(pid: u32, sig: Signal) {
    let mut visited = std::collections::HashSet::new();
    fn walk(p: u32, visited: &mut std::collections::HashSet<u32>, sig: Signal) {
        if !visited.insert(p) {
            return;
        }
        let children_path = format!("/proc/{p}/task/{p}/children");
        if let Ok(s) = std::fs::read_to_string(&children_path) {
            for child in s.split_whitespace() {
                if let Ok(cpid) = child.parse::<u32>() {
                    walk(cpid, visited, sig);
                    if let Some(target) = rustix::process::Pid::from_raw(cpid as i32) {
                        let _ = rustix::process::kill_process(target, sig);
                    }
                }
            }
        }
    }
    walk(pid, &mut visited, sig);
}

// bwrap --new-session puts the sandboxed process in a fresh session, so the
// kernel delivers terminal signals (SIGINT/SIGTERM/SIGWINCH) only to cav, not
// to descendants. Forward them via the self-pipe trick: a minimal AS-safe
// handler writes the signal number to a pipe, and a reader thread fans it out
// to descendants.
static SIG_PIPE_WRITE: AtomicI32 = AtomicI32::new(-1);

extern "C" fn self_pipe_handler(sig: libc::c_int) {
    let fd = SIG_PIPE_WRITE.load(Ordering::Relaxed);
    if fd >= 0 {
        let byte = sig as u8;
        unsafe { libc::write(fd, &byte as *const u8 as *const libc::c_void, 1) };
    }
}

fn forward_cave_signals(cave_pid: u32) -> Result<()> {
    let (read_fd, write_fd) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC)?;
    SIG_PIPE_WRITE.store(write_fd.into_raw_fd(), Ordering::SeqCst);

    let handler = self_pipe_handler as *const () as usize;
    unsafe {
        libc::signal(libc::SIGINT, handler);
        libc::signal(libc::SIGTERM, handler);
        libc::signal(libc::SIGWINCH, handler);
    }

    std::thread::spawn(move || {
        let mut buf = [0u8; 64];
        loop {
            match rustix::io::read(&read_fd, &mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    for &b in &buf[..n] {
                        if let Some(sig) = Signal::from_raw(b as i32) {
                            signal_descendants(cave_pid, sig);
                        }
                    }
                }
                Err(e) if e == rustix::io::Errno::INTR => continue,
                Err(_) => break,
            }
        }
    });
    Ok(())
}

fn build_claude_cmd(
    session_id: Option<&str>,
    resuming: bool,
    prompt: Option<&str>,
    channels: bool,
    system_prompt: &str,
) -> Vec<String> {
    let mut cmd = vec![
        "claude".to_string(),
        "--dangerously-skip-permissions".to_string(),
        "--append-system-prompt".to_string(),
        system_prompt.to_string(),
    ];
    // Channels only work in interactive mode. --print short-circuits
    // MCP server startup, so skip the flag there.
    if channels && prompt.is_none() {
        // cave-messages@cave is allowlisted via the managed-settings.json the
        // sandbox binds in, so the plain --channels path loads it with no
        // confirmation prompt.
        cmd.extend(["--channels".to_string(), "plugin:cave-messages@cave".to_string()]);
    }
    if let Some(sid) = session_id {
        if resuming {
            cmd.extend(["--resume".to_string(), sid.to_string()]);
        } else {
            cmd.extend(["--session-id".to_string(), sid.to_string()]);
        }
    }
    if let Some(p) = prompt {
        cmd.extend(["--print".to_string(), p.to_string()]);
    }
    cmd
}

fn seed_claude_config(home: &Path, project_slug_str: &str, comms: bool) {
    let claude_json = home.join(".claude.json");
    if !claude_json.exists() {
        let host = host_home().join(".claude.json");
        if host.exists() {
            let _ = std::fs::copy(&host, &claude_json);
        }
    }
    let project_path = format!("/projects/{project_slug_str}");
    let cfg_text = std::fs::read_to_string(&claude_json).unwrap_or_default();
    let mut cfg: serde_json::Value =
        serde_json::from_str(&cfg_text).unwrap_or_else(|_| serde_json::json!({}));

    let mut changed = false;

    if let Some(proj) = cfg
        .as_object_mut()
        .and_then(|o| {
            o.entry("projects")
                .or_insert(serde_json::json!({}))
                .as_object_mut()
        })
        .and_then(|o| {
            o.entry(&project_path)
                .or_insert(serde_json::json!({}))
                .as_object_mut()
        })
    {
        let already = proj
            .get("hasTrustDialogAccepted")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if !already {
            proj.insert("hasTrustDialogAccepted".to_string(), serde_json::json!(true));
            proj.insert(
                "hasCompletedProjectOnboarding".to_string(),
                serde_json::json!(true),
            );
            changed = true;
        }
    }

    // Channel feature gates: tengu_harbor enables the channel system;
    // tengu_harbor_ledger is the per-plan allowlist. Only seed when comms
    // is enabled so the feature stays dormant by default.
    if comms {
        if let Some(gates) = cfg.as_object_mut().and_then(|o| {
            o.entry("cachedGrowthBookFeatures")
                .or_insert(serde_json::json!({}))
                .as_object_mut()
        }) {
            if gates.get("tengu_harbor").and_then(|v| v.as_bool()) != Some(true) {
                gates.insert("tengu_harbor".to_string(), serde_json::json!(true));
                changed = true;
            }
            let ledger = serde_json::json!([{"plugin": "cave-messages", "marketplace": "cave"}]);
            if gates.get("tengu_harbor_ledger") != Some(&ledger) {
                gates.insert("tengu_harbor_ledger".to_string(), ledger);
                changed = true;
            }
        }
    }

    if changed {
        if let Ok(json) = serde_json::to_string(&cfg) {
            let _ = std::fs::write(&claude_json, json);
        }
    }
}

/// Populate a cave's own nix store with the closures it needs before launch.
///
/// Each cave runs ordinary nix against its private chroot store at
/// `<cave_root>` (bound to /nix/store + /nix/var inside the sandbox); nix
/// manages the store's database itself. Two closures are seeded from the host
/// store and GC-rooted so the cave's own GC never collects them:
///   - cave infrastructure: the cav package itself (cav-host,
///     cav-comms-channel-server, cav-git, ...), referenced by absolute store
///     path via CODING_CAVE_BIN and the comms MCP server. Every cave needs it,
///     independent of cave.nix.
///   - the user environment: the user-env / claude-code closure built from
///     cave.nix (absent on a first run that produced nothing).
/// `nix copy` is incremental, so this is cheap on resume. Returns the cave
/// store root for the env/bwrap wiring.
fn populate_cave_store(agent_id: &str, user_env: Option<&Path>) -> Result<PathBuf> {
    let cave_root = workspace::cave_store_root_for(agent_id);
    std::fs::create_dir_all(cave_root.join("nix").join("store"))?;
    std::fs::create_dir_all(cave_root.join("nix").join("var").join("nix"))?;
    let cave_uri = format!("local?root={}", cave_root.display());

    // Cave infrastructure: the cav package's own closure (parent of its bin/).
    if let Some(cav_store) = Path::new(&constants::build().cav_bin)
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .filter(|p| p.starts_with("/nix/store/"))
    {
        seed_cave_closure(&cave_uri, &cave_root, "cav", &cav_store)?;
    }

    // User environment from cave.nix.
    if let Some(env_path) = user_env {
        seed_cave_closure(&cave_uri, &cave_root, "user-env", &env_path.to_string_lossy())?;
    }

    Ok(cave_root)
}

/// `nix copy` a closure from the host store into the cave's chroot store and
/// register a GC root (`gcroots/cav/<name>`) so the cave's own GC keeps it.
/// Incremental: paths already present in the cave store are skipped.
fn seed_cave_closure(cave_uri: &str, cave_root: &Path, name: &str, closure: &str) -> Result<()> {
    // Copy host store -> cave store. No --from: the host's default store
    // (daemon or local) is the source.
    let out = Command::new("nix")
        .args([
            "--extra-experimental-features",
            "nix-command",
            "copy",
            "--no-check-sigs",
            "--to",
            cave_uri,
            closure,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| anyhow::anyhow!("spawn nix copy into cave store: {e}"))?;
    if !out.status.success() {
        bail!(
            "nix copy of {name} closure into {} failed: {}",
            cave_root.display(),
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }

    // A symlink under the store's gcroots dir is the documented persistent-root
    // mechanism.
    let gcroots = cave_root
        .join("nix")
        .join("var")
        .join("nix")
        .join("gcroots")
        .join("cav");
    std::fs::create_dir_all(&gcroots)?;
    let out = Command::new("nix-store")
        .args([
            "--store",
            cave_uri,
            "--add-root",
            &gcroots.join(name).to_string_lossy(),
            "-r",
            closure,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| anyhow::anyhow!("spawn nix-store --add-root in cave store: {e}"))?;
    if !out.status.success() {
        bail!(
            "rooting {name} closure in {} failed: {}",
            cave_root.display(),
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }
    Ok(())
}

fn user_env_path() -> Option<PathBuf> {
    let result = crate::user_env::user_env_result_path();
    // Resolve the symlink to the actual nix store path so it's accessible inside bwrap
    // (bwrap binds /home/cave-coder to agent home, shadowing the symlink)
    result.canonicalize().ok().filter(|p| p.exists())
}

/// Mirror the user-env's `files/` tree into the persistent cave home.
/// Replaces what the old shell entry script did with find | while-read |
/// per-file forks. fs::copy uses sendfile on Linux so this stays cheap
/// even for thousands of small files.
fn merge_user_files(src: &Path, dst: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    if !src.is_dir() {
        return Ok(());
    }
    std::fs::create_dir_all(dst)?;
    if let Ok(meta) = std::fs::metadata(dst) {
        let mut p = meta.permissions();
        p.set_mode(p.mode() | 0o200);
        let _ = std::fs::set_permissions(dst, p);
    }
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let src_p = entry.path();
        let dst_p = dst.join(entry.file_name());
        let ft = entry.file_type()?;
        if ft.is_dir() {
            merge_user_files(&src_p, &dst_p)?;
        } else if ft.is_symlink() {
            let _ = std::fs::remove_file(&dst_p);
            let target = std::fs::read_link(&src_p)?;
            std::os::unix::fs::symlink(target, &dst_p)?;
        } else {
            let _ = std::fs::remove_file(&dst_p);
            std::fs::copy(&src_p, &dst_p)?;
            let m = std::fs::metadata(&dst_p)?;
            let mut p = m.permissions();
            p.set_mode(p.mode() | 0o200);
            std::fs::set_permissions(&dst_p, p)?;
        }
    }
    Ok(())
}

fn build_system_prompt(
    workspaces: &[(PathBuf, PathBuf, String)],
    agent_id: &str,
    display_name: &str,
    comms: bool,
) -> String {
    let mut out = String::new();
    out.push_str(include_str!("../../prompts/system.md"));
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.push('\n');
    out.push_str(if comms {
        include_str!("../../prompts/comms-on.md")
    } else {
        include_str!("../../prompts/comms-off.md")
    });
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.push_str(&format!(
        "\nYou are agent {agent_id}, named `{display_name}`.\n"
    ));
    out.push_str("\n## Projects\n\n");
    for (i, (ws, _, slug)) in workspaces.iter().enumerate() {
        let primary = if i == 0 { " (primary)" } else { "" };
        let branch = git::current_branch(Some(ws)).unwrap_or_default();
        let branch_part = if branch.is_empty() {
            String::new()
        } else {
            format!(" — branch `{branch}`")
        };
        out.push_str(&format!("- `/projects/{slug}/`{primary}{branch_part}\n"));
    }
    out
}

fn write_combined_ca_bundle(home: &Path, os_bundle: &Path, crinj_ca: &Path) -> Result<()> {
    let dest = home.join(".cave-ca-combined.pem");
    let mut content = std::fs::read(os_bundle)?;
    if content.last() != Some(&b'\n') {
        content.push(b'\n');
    }
    content.extend(std::fs::read(crinj_ca)?);
    std::fs::write(&dest, content)?;
    Ok(())
}

/// Write a minimal ~/.gitconfig in the cave from the host's
/// `git config --global` values. host_gitconfig() copying the host file
/// already covers the more common case.
fn ensure_gitconfig(home: &Path) -> Result<()> {
    let dest = home.join(".gitconfig");
    if dest.exists() {
        return Ok(());
    }
    let mut user = String::new();
    if let Some(v) = read_git_global("user.name") {
        user.push_str(&format!("\tname = {v}\n"));
    }
    if let Some(v) = read_git_global("user.email") {
        user.push_str(&format!("\temail = {v}\n"));
    }
    if let Some(v) = read_git_global("user.signingkey") {
        user.push_str(&format!("\tsigningkey = {v}\n"));
    }
    if user.is_empty() {
        return Ok(());
    }
    let mut content = format!("[user]\n{user}");
    if read_git_global("commit.gpgsign").as_deref() == Some("true") {
        content.push_str("[commit]\n\tgpgsign = true\n");
    }
    std::fs::write(&dest, content)?;
    Ok(())
}

fn install_git_wrapper(home: &Path) -> Result<()> {
    // Copy cav-git binary as ~/bin/git inside the cave
    let cav_bin_dir = &constants::build().cav_bin;
    let cav_git = PathBuf::from(cav_bin_dir).join("cav-git");
    if !cav_git.exists() {
        // Fall back: try adjacent to the current executable
        let exe = std::env::current_exe().unwrap_or_default();
        let adj = exe.parent().unwrap_or(Path::new("")).join("cav-git");
        if adj.exists() {
            let agent_bin = home.join("bin");
            std::fs::create_dir_all(&agent_bin)?;
            let dest = agent_bin.join("git");
            // Compare before copying to avoid unnecessary writes
            if !dest.exists() || std::fs::read(&dest).ok() != std::fs::read(&adj).ok() {
                std::fs::copy(&adj, &dest)?;
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&dest, std::fs::Permissions::from_mode(0o755))?;
            }
            return Ok(());
        }
        // Neither location has cav-git; skip (git wrapper unavailable)
        return Ok(());
    }
    let agent_bin = home.join("bin");
    std::fs::create_dir_all(&agent_bin)?;
    let dest = agent_bin.join("git");
    if !dest.exists() || std::fs::read(&dest).ok() != std::fs::read(&cav_git).ok() {
        std::fs::copy(&cav_git, &dest)?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&dest, std::fs::Permissions::from_mode(0o755))?;
    }
    Ok(())
}

struct DaemonGuard(Option<daemon::DaemonHandle>);
impl Drop for DaemonGuard {
    fn drop(&mut self) {
        if let Some(h) = self.0.take() {
            daemon::stop(h);
        }
    }
}

/// Print the cav resume commands. Claude already prints its own
/// "Resume this session with: claude --resume <uuid>" line, so we just
/// append the cav-flavored alternatives. Session uuid first (the
/// canonical, archive-backed form), then the agent-id shortcut. The
/// flag suffix echoes the session-affecting options the user invoked
/// with, so a copy-pasted resume command reproduces the same mounts.
fn format_resume_flags(
    extra_slugs: &[String],
    host_ports: &[u16],
    host_devices: &[String],
    gpu: bool,
    comms: bool,
) -> String {
    let mut s = String::new();
    for slug in extra_slugs {
        s.push_str(" -p ");
        s.push_str(&crate::shell::shell_quote(slug));
    }
    for port in host_ports {
        s.push_str(&format!(" --port {port}"));
    }
    if gpu {
        s.push_str(" --gpu");
    }
    for dev in host_devices {
        s.push_str(" -d ");
        s.push_str(&crate::shell::shell_quote(dev));
    }
    if comms {
        s.push_str(" --comms");
    }
    s
}

fn print_resume_hints(
    agent_id: &str,
    session_id: Option<&str>,
    agent_id_gone: bool,
    extra_slugs: &[String],
    host_ports: &[u16],
    host_devices: &[String],
    gpu: bool,
    comms: bool,
) {
    let suffix = format_resume_flags(extra_slugs, host_ports, host_devices, gpu, comms);
    if let Some(sid) = session_id {
        eprintln!("{}", style::bold_cyan(&format!("cav --resume {sid}{suffix}")));
    }
    if !agent_id_gone {
        eprintln!("{}", style::bold_cyan(&format!("cav -r {agent_id}{suffix}")));
    }
}

/// Rebase the worktree's dev branch onto host-cave/<base>. Called at
/// agent resume to catch up on base movement that happened while the
/// agent was paused (push of base no longer rebases paused worktrees).
/// Stashes uncommitted work (including untracked) before rebasing, then
/// pops. On conflict, leaves the rebase state in place and surfaces the
/// stash to the agent so they don't lose track of their changes.
fn rebase_dev_onto_base(ws: &Path, current: &str) {
    if current.is_empty() || current == "HEAD" {
        return;
    }
    let remote_ref = format!("{HOST_SHARED_REMOTE}/{current}");
    if git::git(
        &["rev-parse", "--verify", &remote_ref],
        Some(ws),
        false,
        true,
        false,
    )
    .is_err()
    {
        return;
    }
    // Fast path: dev branch already includes base. Skip the rebase
    // invocation entirely on quiet resumes — saves a fork+exec per
    // workspace when base hasn't moved.
    if git::is_ancestor(&remote_ref, "HEAD", Some(ws)) {
        return;
    }
    // Stash if (and only if) there's real local state to preserve. `-u`
    // captures untracked files too, since `git rebase` will refuse to
    // overwrite them and an unguarded rebase would fail mid-flight.
    let has_changes = git::git_output(&["status", "--porcelain"], Some(ws))
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false);
    let stashed = has_changes
        && git::git(
            &["stash", "push", "-u", "-m", "cav-resume-autorebase"],
            Some(ws),
            false,
            true,
            false,
        )
        .is_ok();
    let rebase_ok = git::git(
        &["rebase", &remote_ref],
        Some(ws),
        false,
        true,
        false,
    )
    .is_ok();
    if !rebase_ok {
        let stash_hint = if stashed {
            " Uncommitted changes were stashed (`cav-resume-autorebase`); \
              run `git stash pop` after `git rebase --continue`."
        } else {
            ""
        };
        eprintln!(
            "{} dev branch could not be rebased onto {}. Resolve conflicts \
             in the worktree and run `git rebase --continue`.{}",
            style::bold_yellow("Warning:"),
            style::yellow(&remote_ref),
            stash_hint,
        );
        // Leave the rebase state (and stash, if any) in place; don't
        // abort. The agent resolves on startup.
        return;
    }
    if stashed {
        let pop_ok = git::git(&["stash", "pop"], Some(ws), false, true, false).is_ok();
        if !pop_ok {
            eprintln!(
                "{} rebase succeeded but stashed changes did not re-apply \
                 cleanly. Resolve conflicts in the worktree; the stash \
                 (`cav-resume-autorebase`) remains until you drop it.",
                style::bold_yellow("Warning:"),
            );
        }
    }
}

/// Drop shared dev branches whose work is already on base. "Already on
/// base" means either the dev tip is an ancestor of base (covers tip ==
/// base after approve and tip behind base when the agent lagged), or
/// every commit on the dev branch has a patch-equivalent on base (covers
/// branches that were rebased into different SHAs but the same content).
/// Safe to call from any command that fetches the shared remote first.
pub fn reconcile_merged_dev_branches(base: &Path, verbose: bool) {
    let Ok(remotes) = git::remote_branches(CAVE_SHARED_REMOTE, Some(base)) else {
        return;
    };
    let shared = shared_repo_path(base);
    let mut deleted_any = false;
    for rb in remotes {
        let Some((base_name, _)) = parse_dev_branch(&rb) else { continue };
        let remote_dev = format!("{CAVE_SHARED_REMOTE}/{rb}");
        let remote_base = format!("{CAVE_SHARED_REMOTE}/{base_name}");
        if git::git_output(&["rev-parse", &remote_dev], Some(base)).is_err()
            || git::git_output(&["rev-parse", &remote_base], Some(base)).is_err()
        {
            continue;
        }
        let delivered = git::is_ancestor(&remote_dev, &remote_base, Some(base))
            || git::cherry_undelivered(&remote_base, &remote_dev, Some(base))
                .map(|v| v.is_empty())
                .unwrap_or(false);
        if !delivered {
            continue;
        }
        git::git(&["branch", "-D", &rb], Some(&shared), false, true, false).ok();
        git::git(&["branch", "-D", &rb], Some(base), false, true, false).ok();
        deleted_any = true;
    }
    if deleted_any {
        git::git(
            &["fetch", "--prune", CAVE_SHARED_REMOTE],
            Some(base),
            false,
            verbose,
            verbose,
        )
        .ok();
    }
}

pub struct RunAgentOpts {
    pub base: PathBuf,
    pub continue_agent: bool,
    pub resume_agent: Option<String>,
    pub extra_projects: Vec<String>,
    pub host_ports: Vec<u16>,
    /// The effective bind-mount list (user `-d` entries plus any
    /// `--gpu` expansion). Used for sandbox setup and env wiring.
    pub host_devices: Vec<String>,
    /// What the user actually typed for `-d`. The resume hint echoes
    /// these alongside `--gpu` so a copy-pasted command re-runs
    /// detection rather than freezing today's render-node enumeration.
    pub host_devices_user: Vec<String>,
    pub gpu: bool,
    pub comms: bool,
    pub prompt: Option<String>,
}

pub fn run_agent(opts: RunAgentOpts) -> Result<()> {
    let RunAgentOpts {
        base,
        continue_agent,
        resume_agent,
        extra_projects,
        host_ports,
        host_devices,
        host_devices_user,
        gpu,
        comms,
        prompt,
    } = opts;
    let slug = project_slug(&base);

    // Resolve the agent session BEFORE loading sandbox config so error paths
    // (missing agent, empty -c, etc.) don't trip on missing build config.
    let (agent_id, session_id, resuming) = if continue_agent {
        let rc = registry::resolve_continue(&base, None)?;
        (rc.agent_id, Some(rc.session_id), rc.resuming)
    } else if let Some(ref spec) = resume_agent {
        // Spec accepts agent id ("3", "@3", "project@3") or a Claude session
        // uuid. If neither matches a known agent, mint a fresh agent ID and
        // pass the uuid straight through to claude --resume. Claude validates.
        let parsed = spec
            .rfind('@')
            .map(|i| spec[i + 1..].to_string())
            .unwrap_or_else(|| spec.trim_start_matches('@').to_string());
        if config::is_agent_id(&parsed) {
            let rc = registry::resolve_continue(&base, Some(&parsed))?;
            (rc.agent_id, Some(rc.session_id), rc.resuming)
        } else if let Some(aid) = registry::find_agent_by_session(&parsed)? {
            let rc = registry::resolve_continue(&base, Some(&aid))?;
            (rc.agent_id, Some(rc.session_id), rc.resuming)
        } else {
            // Unknown session id -- mint a fresh agent and let claude resolve.
            let aid = registry::acquire_agent_id(&base)?;
            registry::write_session_id(&aid, &parsed)?;
            (aid, Some(parsed), true)
        }
    } else {
        let aid = registry::acquire_agent_id(&base)?;
        let sid = registry::read_session_id(&aid)?.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        (aid, Some(sid), false)
    };

    let cfg = load_sandbox_config()?;

    let agent_name = registry::display_name(&base, &agent_id);

    let host_devices = validate_host_devices(host_devices)?;
    let device_gid = device_passthrough_gid(&host_devices)?;

    let extra_slugs: Vec<String> = extra_projects
        .iter()
        .map(|p| project_slug(&PathBuf::from(p)))
        .collect();
    registry::save_agent_extras(&agent_id, &extra_slugs)?;

    // Create isolated workspace clones for the agent
    crate::log_info!("preparing workspaces...");
    let (ws, shared) = workspace::create(&base, &agent_id)?;
    let mut workspaces: Vec<(PathBuf, PathBuf, String)> =
        vec![(ws, shared, slug.clone())];
    for extra in &extra_projects {
        let ep = PathBuf::from(extra);
        let es = project_slug(&ep);
        let (ep_ws, ep_shared) = workspace::create(&ep, &agent_id)?;
        workspaces.push((ep_ws, ep_shared, es));
    }

    let home = config::persistent_home();
    std::fs::create_dir_all(&home)?;
    for (_, _, s) in &workspaces {
        seed_claude_config(&home, s, comms);
    }

    install_git_wrapper(&home)?;

    // Migrate old remote name
    for (ws, _, _) in &workspaces {
        if git::git(
            &["remote", "get-url", HOST_SHARED_REMOTE],
            Some(ws),
            false,
            true,
            false,
        )
        .is_err()
        {
            git::git(
                &["remote", "rename", CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE],
                Some(ws),
                false,
                true,
                false,
            )
            .ok();
        }
    }

    // Verify workspace layout
    for (ws, _, _) in &workspaces {
        workspace::verify_layout(ws, &agent_id)?;
    }
    crate::log_info!("workspaces ready");

    // Start git daemon
    crate::log_info!("starting git daemon...");
    let shared_repos: Vec<(PathBuf, String)> = workspaces
        .iter()
        .map(|(_, sr, s)| (sr.clone(), s.clone()))
        .collect();
    let daemon_handle = daemon::start(&agent_id, &shared_repos)?;
    let guard = DaemonGuard(Some(daemon_handle));
    crate::log_info!("git daemon ready");

    // Set remote URLs (localhost for sync)
    for (ws, _, s) in &workspaces {
        let url = daemon::remote_url(&agent_id, s, "127.0.0.1");
        git::git(
            &["remote", "set-url", HOST_SHARED_REMOTE, &url],
            Some(ws),
            false,
            true,
            false,
        )?;
    }

    // Sync workspace branches
    crate::log_info!("syncing workspace branches...");
    for (ws, _, _) in &workspaces {
        git::git(&["fetch", "--prune", HOST_SHARED_REMOTE], Some(ws), false, true, false).ok();
        let current = git::current_branch(Some(ws)).unwrap_or_default();
        for branch in git::remote_branches(HOST_SHARED_REMOTE, Some(ws)).unwrap_or_default() {
            if parse_dev_branch(&branch).is_some() || branch == current {
                continue;
            }
            let remote_ref = format!("{HOST_SHARED_REMOTE}/{branch}");
            git::git(&["branch", "-f", &branch, &remote_ref], Some(ws), false, true, false).ok();
        }
        // Rebase the dev branch onto current base. Paused agents skip
        // the host-side auto-rebase during push, so this is where they
        // catch up. The worktree's current branch is the dev branch
        // (named after the base via the agent push refspec).
        rebase_dev_onto_base(ws, &current);
    }
    crate::log_info!("branches synced");

    // Set slirp-reachable URL
    for (ws, _, s) in &workspaces {
        let url = daemon::remote_url(&agent_id, s, SLIRP_GATEWAY);
        git::git(
            &["remote", "set-url", HOST_SHARED_REMOTE, &url],
            Some(ws),
            false,
            true,
            false,
        )?;
    }

    // Build user env
    crate::log_info!("checking user env...");
    let _ = user_env::build(false);
    let uenv = user_env_path();
    crate::log_info!("user env ready");

    // Give the cave its own ordinary nix store. Top-level caves get a private
    // chroot store, seeded with the env closure and bound at /nix/store +
    // /nix/var inside the sandbox; the cave builds into it as itself, no idmap.
    // A nested cave (subcave) keeps using the inherited /nix/store from its
    // host cave (bwrap static_args ro-bind) — building a separate per-cave
    // store one level down is not supported.
    let cave_store_root = if inside_cave() {
        None
    } else {
        crate::log_info!("populating cave nix store...");
        let root = populate_cave_store(&agent_id, uenv.as_deref())?;
        crate::log_info!("cave nix store ready");
        Some(root)
    };

    // Build env map
    let ws_paths = workspaces
        .iter()
        .map(|(ws, _, _)| ws.to_string_lossy().to_string())
        .collect::<Vec<_>>()
        .join(":");
    let shared_repos_str = workspaces
        .iter()
        .map(|(_, sr, _)| sr.to_string_lossy().to_string())
        .collect::<Vec<_>>()
        .join(":");
    let slugs_str = workspaces
        .iter()
        .map(|(_, _, s)| s.clone())
        .collect::<Vec<_>>()
        .join(":");

    let mut env: HashMap<String, String> = HashMap::new();
    for var in &[
        "TERM",
        "COLORTERM",
        "TZ",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ] {
        if let Ok(v) = std::env::var(var) {
            env.insert(var.to_string(), v);
        }
    }
    env.insert("CODING_CAVE_VERSION".to_string(), constants::version().to_string());
    #[cfg(debug_assertions)]
    if config::verbose() >= 1 {
        env.insert(
            "CODING_CAVE_VERBOSE".to_string(),
            config::verbose().to_string(),
        );
        env.insert(
            "CODING_CAVE_TS_START".to_string(),
            config::log_anchor().to_string(),
        );
    }
    if let Some(root) = &cave_store_root {
        env.insert(
            "CODING_CAVE_STORE_ROOT".to_string(),
            root.to_string_lossy().to_string(),
        );
    }
    env.insert("CODING_CAVE_HOME".to_string(), home.to_string_lossy().to_string());
    env.insert("CODING_CAVE_WORKSPACES".to_string(), ws_paths);
    env.insert("CODING_CAVE_SHARED_REPOS".to_string(), shared_repos_str);
    env.insert("CODING_CAVE_AGENT_NAME".to_string(), agent_name.clone());
    env.insert("CODING_CAVE_PROJECT_SLUGS".to_string(), slugs_str);
    env.insert("CODING_CAVE_PROJECT_SLUG".to_string(), slug.clone());
    env.insert(
        "CODING_CAVE_HOST_HOME".to_string(),
        std::env::var("CODING_CAVE_HOST_HOME")
            .unwrap_or_else(|_| host_home().to_string_lossy().to_string()),
    );
    env.insert("CODING_CAVE_DNS_SERVER".to_string(), SLIRP_DNS.to_string());
    if !host_devices.is_empty() {
        env.insert(
            "CODING_CAVE_HOST_DEVICES".to_string(),
            host_devices.join(":"),
        );
    }

    if let Some(gc) = host_gitconfig() {
        env.insert(
            "CODING_CAVE_GITCONFIG".to_string(),
            gc.to_string_lossy().to_string(),
        );
    }
    if let Some(sock) = gpg_socket() {
        env.insert("CODING_CAVE_GPG_SOCKET".to_string(), sock);
        if let Some(gpg_home) = gpg_homedir() {
            for name in &["pubring.kbx", "pubring.gpg"] {
                let p = gpg_home.join(name);
                if p.exists() {
                    env.insert(
                        "CODING_CAVE_GPG_PUBRING".to_string(),
                        p.to_string_lossy().to_string(),
                    );
                    break;
                }
            }
        }
    }
    env.insert(
        "CODING_CAVE_BIN".to_string(),
        constants::build().cav_bin.clone(),
    );
    if let Some(uep) = &uenv {
        env.insert(
            "CODING_CAVE_USER_ENV".to_string(),
            uep.to_string_lossy().to_string(),
        );
    }
    let crinj_session = register_session(&crinj_sessions_dir());
    crate::log_info!("starting crinj proxy...");
    let (crinj_proc, crinj_state) = start_crinj()?;
    crate::log_info!("crinj proxy ready");
    env.insert("CODING_CAVE_GATEWAY".to_string(), SLIRP_GATEWAY.to_string());
    env.insert(
        "CODING_CAVE_PROXY_ADDR".to_string(),
        format!("{SLIRP_GATEWAY}:{CRINJ_PORT}"),
    );
    let ca_path = crinj_ca_path()?;
    env.insert(
        "CODING_CAVE_PROXY_CA".to_string(),
        ca_path.to_string_lossy().to_string(),
    );

    // Channel broker (inter-agent messaging). Gated by --comms.
    let (broker_session, broker_proc, channels_enabled) = if comms {
        let session = register_session(&broker_sessions_dir());
        let proc = match start_broker() {
            Ok(p) => p,
            Err(e) => {
                eprintln!("{} channel broker failed to start: {e}", style::bold_yellow("Warning:"));
                None
            }
        };
        let enabled = broker_socket_path().exists();
        if enabled {
            env.insert(
                "CODING_CAVE_MSG_SOCK".to_string(),
                broker_socket_path().to_string_lossy().to_string(),
            );
            env.insert("CODING_CAVE_AGENT_ID".to_string(), agent_id.clone());
            if let Err(e) = setup_channel_marketplace(&home) {
                eprintln!("{} channel marketplace setup failed: {e}", style::bold_yellow("Warning:"));
            }
        }
        (Some(session), proc, enabled)
    } else {
        (None, None, false)
    };

    // Materialize cave-home contents the in-cave entry script used to do
    // each launch: HM-files merge, per-session CLAUDE.md append, gitconfig
    // copy/fallback, combined TLS-CA bundle. All endpoints are host paths
    // (cave_home is bind-mounted at /home/cave-coder), so doing this here
    // means the cave can exec claude directly without a shell wrapper.
    crate::log_info!("seeding cave home...");
    if let Some(uep) = &uenv {
        let src = uep.join("files");
        if src.is_dir() {
            merge_user_files(&src, &home)?;
        }
    }
    if let Some(gc) = host_gitconfig() {
        let _ = std::fs::copy(&gc, home.join(".gitconfig"));
    }
    ensure_gitconfig(&home)?;
    if !cfg.ca_cert_bundle.is_empty() {
        write_combined_ca_bundle(&home, Path::new(&cfg.ca_cert_bundle), &ca_path)?;
    }
    crate::log_info!("cave home ready");

    let system_prompt = build_system_prompt(&workspaces, &agent_id, &agent_name, comms);
    let claude_cmd = build_claude_cmd(
        session_id.as_deref(),
        resuming,
        prompt.as_deref(),
        channels_enabled,
        &system_prompt,
    );
    let daemon_port = daemon::agent_port(&agent_id);

    let (sync_r, sync_w) = rustix::pipe::pipe()?;

    env.insert(
        "CODING_CAVE_NS_SYNC_FD".to_string(),
        sync_w.as_raw_fd().to_string(),
    );

    let seccomp_fd = rustix::fs::open(
        cfg.seccomp_filter.as_str(),
        rustix::fs::OFlags::RDONLY,
        rustix::fs::Mode::empty(),
    )
    .map_err(|e| anyhow::anyhow!("Cannot open seccomp filter {}: {e}", cfg.seccomp_filter))?;
    rustix::io::fcntl_setfd(&seccomp_fd, rustix::io::FdFlags::empty())?;

    let bwrap_argv = build_bwrap_argv(&cfg, &env, &claude_cmd, seccomp_fd.as_raw_fd());
    let supervisor_bin = constants::supervisor_path();
    let unshare_bin = find_bin("unshare")?;

    // --pid isolates the supervisor and its helpers (slirp4netns, nsenter) from
    // the host pid-ns as defence-in-depth; bwrap handles the agent's pid-ns
    // separately via --unshare-pid. --fork is required so the exec'd target
    // becomes PID 1 of the new namespace.
    //
    // --mount-proc remounts /proc inside the new mount+pid ns. Without it,
    // /proc inside supervisor is the parent's procfs (cave's pid view), so
    // bwrap's namespace_ids_read(child_pid) opens /proc/<bwrap-pid-ns-pid>
    // against the wrong /proc. In a nested cave that usually means ENOENT and
    // bwrap dies before applying the seccomp/firewall.
    let mut ns_cmd: Vec<String> = vec![
        unshare_bin,
        "--user".to_string(),
        "--net".to_string(),
        "--mount".to_string(),
        "--pid".to_string(),
        "--fork".to_string(),
        "--mount-proc".to_string(),
        "--map-user=0".to_string(),
        "--map-group=0".to_string(),
        "--".to_string(),
        supervisor_bin.to_string_lossy().to_string(),
    ];
    ns_cmd.extend(bwrap_argv);

    let mut proc_cmd = Command::new(&ns_cmd[0]);
    proc_cmd.args(&ns_cmd[1..]);
    proc_cmd.env_clear();
    for (k, v) in &env {
        proc_cmd.env(k, v);
    }
    if let Ok(bc) = std::env::var("CODING_CAVE_BUILD_CONFIG") {
        proc_cmd.env("CODING_CAVE_BUILD_CONFIG", bc);
    }

    if let Some(gid) = device_gid {
        // setresgid before spawn so the unshare child (and bwrap inside it)
        // inherits this gid. unshare's --map-group=0 then maps inner gid 0
        // → host gid <target>, and bwrap's nested gid_map carries that
        // through, so mode-660 group bits on the bind-mounted device grant
        // access inside the cave.
        let r = unsafe { libc::setresgid(gid, gid, gid) };
        if r != 0 {
            bail!("setresgid({gid}): {}", std::io::Error::last_os_error());
        }
    }

    if let Some(ref s) = crinj_state {
        print_crinj_boot_events(s);
    }
    crate::log_info!("launching sandbox...");
    let mut proc = proc_cmd.spawn()?;
    drop(sync_w);
    drop(seccomp_fd);

    registry::set_cave_pid(&agent_id, proc.id())?;

    crate::log_info!("waiting for sandbox namespace setup (pid {})", proc.id());
    let mut sync_buf = [0u8; 1];
    let n = rustix::io::read(&sync_r, &mut sync_buf).unwrap_or(0);
    if n == 0 {
        let _ = proc.kill();
        bail!("sandbox child died during namespace setup");
    }
    crate::log_info!("namespace ready");
    drop(sync_r);

    crate::log_info!("applying firewall");
    let mut allowed = vec![daemon_port, CRINJ_PORT];
    allowed.extend(host_ports.iter().copied());
    apply_firewall(proc.id(), &allowed)?;

    crate::log_info!("starting slirp4netns");
    let mut slirp = start_slirp4netns(proc.id())?;
    crate::log_info!("slirp4netns ready, agent running");

    forward_cave_signals(proc.id())?;

    let rc = proc.wait().map(|s| s.code().unwrap_or(1)).unwrap_or(1);

    // claude --session-id is binding: the .jsonl lands at the uuid we asked
    // for, or claude wrote nothing (early exit, no messages sent). No
    // post-hoc inference needed -- check the path we requested and that's
    // it. The previous "fall back to latest .jsonl" path silently stole
    // sessions from other agents in the shared archive.
    let actual_sid = session_id
        .as_deref()
        .filter(|sid| config::session_file_exists(&slug, sid, Some(&home)))
        .map(str::to_string);

    // Exit always transitions to Paused. The session log, worktree, and
    // agent branches stay in place until an explicit `cav gc` (or eventual
    // archive bump). Previous "trivially clean = teardown" heuristic was
    // wrong: substantive session-log work and uncommitted-but-untracked
    // files weren't in its signal set, so it routinely discarded results
    // the user still cared about.
    if let Some(ref sid) = actual_sid {
        if Some(sid.as_str()) != session_id.as_deref() {
            registry::write_session_id(&agent_id, sid).ok();
        }
    }
    registry::mark_agent_exited(&agent_id).ok();
    if actual_sid.is_some() {
        print_resume_hints(
            &agent_id,
            actual_sid.as_deref(),
            false,
            &extra_slugs,
            &host_ports,
            &host_devices_user,
            gpu,
            comms,
        );
    }

    let _ = slirp.kill();
    let _ = slirp.wait();
    // Read warnings before signaling crinj to shut down: crinj itself logs
    // a WARN when it receives our SIGTERM, and that expected, self-inflicted
    // shutdown message would otherwise be reported back as a session alert.
    if let Some(ref s) = crinj_state {
        print_crinj_session_warnings(s);
    }
    if unregister_session(&crinj_session, &crinj_sessions_dir()) {
        crinj_signal(Signal::Term);
        if let Some(mut cp) = crinj_proc {
            let _ = cp.wait();
        }
    }
    if let Some(session) = broker_session {
        if unregister_session(&session, &broker_sessions_dir()) {
            stop_broker();
            if let Some(mut bp) = broker_proc {
                let _ = bp.wait();
            }
        }
    }
    drop(guard);

    std::process::exit(rc);
}

#[cfg(test)]
mod tests {
    use super::format_resume_flags;

    #[test]
    fn resume_flags_empty() {
        assert_eq!(format_resume_flags(&[], &[], &[], false, false), "");
    }

    #[test]
    fn resume_flags_all_options() {
        let extras = vec!["coding-cave".to_string(), "other-proj".to_string()];
        let ports = vec![3000u16, 8080];
        let devs = vec!["/dev/dri".to_string()];
        let s = format_resume_flags(&extras, &ports, &devs, false, true);
        assert_eq!(
            s,
            " -p coding-cave -p other-proj --port 3000 --port 8080 -d /dev/dri --comms"
        );
    }

    #[test]
    fn resume_flags_quotes_spaces() {
        let extras = vec!["my project".to_string()];
        let s = format_resume_flags(&extras, &[], &[], false, false);
        assert_eq!(s, " -p 'my project'");
    }

    #[test]
    fn resume_flags_gpu_preserves_flag_not_paths() {
        // --gpu expands at run time, but the resume hint keeps `--gpu`
        // so a copy-pasted command re-runs detection.
        let s = format_resume_flags(&[], &[], &[], true, false);
        assert_eq!(s, " --gpu");
    }

    #[test]
    fn resume_flags_gpu_with_explicit_dev() {
        // Mixed --gpu and explicit -d: emit both, since -d entries are
        // independent of GPU detection.
        let devs = vec!["/dev/snd".to_string()];
        let s = format_resume_flags(&[], &[], &devs, true, false);
        assert_eq!(s, " --gpu -d /dev/snd");
    }
}

#[cfg(test)]
mod rebase_dev_tests {
    use super::rebase_dev_onto_base;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::sync::atomic::{AtomicU32, Ordering};

    /// Drop-cleaned scratch dir so test failures don't leak tmp trees.
    struct TmpGuard {
        path: PathBuf,
        shared: PathBuf,
        ws: PathBuf,
    }

    impl Drop for TmpGuard {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }

    fn git_run(args: &[&str], dir: &Path) {
        let s = Command::new("git")
            .args(args)
            .current_dir(dir)
            .output()
            .unwrap();
        assert!(
            s.status.success(),
            "git {args:?} in {dir:?} failed: {}",
            String::from_utf8_lossy(&s.stderr)
        );
    }

    fn git_out(args: &[&str], dir: &Path) -> String {
        let o = Command::new("git").args(args).current_dir(dir).output().unwrap();
        assert!(o.status.success(), "git {args:?} failed");
        String::from_utf8_lossy(&o.stdout).trim().to_string()
    }

    fn setup(label: &str) -> TmpGuard {
        // Monotonic counter scopes parallel-test runs of the same label
        // to distinct tmp dirs without paying for a uuid.
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "cav-rebase-dev-{}-{label}-{seq}",
            std::process::id(),
        ));
        let _ = std::fs::remove_dir_all(&path);
        let shared = path.join("shared.git");
        let ws = path.join("ws");
        std::fs::create_dir_all(&shared).unwrap();
        std::fs::create_dir_all(&ws).unwrap();
        git_run(&["init", "--bare", "-b", "main"], &shared);
        git_run(&["init", "-b", "main"], &ws);
        git_run(&["config", "user.email", "t@t"], &ws);
        git_run(&["config", "user.name", "t"], &ws);
        std::fs::write(ws.join("seed.txt"), "0\n").unwrap();
        git_run(&["add", "seed.txt"], &ws);
        git_run(&["commit", "-m", "seed"], &ws);
        git_run(&["remote", "add", "host-cave", shared.to_str().unwrap()], &ws);
        git_run(&["push", "host-cave", "main"], &ws);
        TmpGuard { path, shared, ws }
    }

    /// Make a sibling clone of `shared` at `<g.path>/user`, advance its
    /// `main` with a single commit, and push it back. Returns the user
    /// dir path so the caller can chain further mutations if needed.
    fn advance_base(g: &TmpGuard, file: &str, content: &str, msg: &str) -> PathBuf {
        let user = g.path.join("user");
        git_run(
            &["clone", g.shared.to_str().unwrap(), user.to_str().unwrap()],
            &g.path,
        );
        git_run(&["config", "user.email", "t@t"], &user);
        git_run(&["config", "user.name", "t"], &user);
        std::fs::write(user.join(file), content).unwrap();
        git_run(&["add", file], &user);
        git_run(&["commit", "-m", msg], &user);
        git_run(&["push", "origin", "main"], &user);
        user
    }

    #[test]
    fn rebases_onto_advanced_base() {
        let g = setup("advance");
        // Worktree adds a non-conflicting commit on main.
        std::fs::write(g.ws.join("dev.txt"), "D\n").unwrap();
        git_run(&["add", "dev.txt"], &g.ws);
        git_run(&["commit", "-m", "dev commit"], &g.ws);

        advance_base(&g, "base.txt", "B\n", "base advance");
        git_run(&["fetch", "host-cave"], &g.ws);
        rebase_dev_onto_base(&g.ws, "main");

        let history = git_out(&["log", "--format=%s", "HEAD"], &g.ws);
        assert!(
            history.contains("dev commit") && history.contains("base advance"),
            "history should include both commits, got:\n{history}",
        );
        assert_eq!(
            git_out(&["log", "-1", "--format=%s", "HEAD^"], &g.ws),
            "base advance",
        );
    }

    #[test]
    fn does_not_stash_when_worktree_is_clean() {
        let g = setup("clean-no-stash");
        rebase_dev_onto_base(&g.ws, "main");
        let stashes = git_out(&["stash", "list"], &g.ws);
        assert!(
            stashes.is_empty(),
            "no stash should be created when worktree is clean, got: {stashes}"
        );
    }

    #[test]
    fn preserves_stash_on_conflict_and_hints_at_it() {
        let g = setup("dirty-conflict");
        // Worktree commits a change to seed.txt, AND has unstaged edits
        // (plus an untracked file) that need stashing.
        std::fs::write(g.ws.join("seed.txt"), "WORKTREE\n").unwrap();
        git_run(&["add", "seed.txt"], &g.ws);
        git_run(&["commit", "-m", "ws change"], &g.ws);
        std::fs::write(g.ws.join("seed.txt"), "DIRTY\n").unwrap();
        std::fs::write(g.ws.join("untracked.txt"), "u\n").unwrap();

        advance_base(&g, "seed.txt", "BASE\n", "base change");
        git_run(&["fetch", "host-cave"], &g.ws);
        rebase_dev_onto_base(&g.ws, "main");

        let git_dir = g.ws.join(".git");
        assert!(
            git_dir.join("rebase-merge").exists() || git_dir.join("rebase-apply").exists(),
            "rebase state should remain after conflict"
        );
        let stashes = git_out(&["stash", "list"], &g.ws);
        assert!(
            stashes.contains("cav-resume-autorebase"),
            "stash should remain after conflict, got: {stashes}"
        );
    }

    #[test]
    fn leaves_rebase_state_on_conflict() {
        let g = setup("conflict");
        std::fs::write(g.ws.join("seed.txt"), "WORKTREE\n").unwrap();
        git_run(&["add", "seed.txt"], &g.ws);
        git_run(&["commit", "-m", "ws change"], &g.ws);

        advance_base(&g, "seed.txt", "BASE\n", "base change");
        git_run(&["fetch", "host-cave"], &g.ws);
        rebase_dev_onto_base(&g.ws, "main");

        let git_dir = g.ws.join(".git");
        assert!(
            git_dir.join("rebase-merge").exists() || git_dir.join("rebase-apply").exists(),
            "expected rebase state to be left in place after conflict"
        );
    }

    #[test]
    fn no_op_when_remote_ref_missing() {
        let g = setup("noremote");
        git_run(&["checkout", "-b", "lonely"], &g.ws);
        std::fs::write(g.ws.join("x.txt"), "x\n").unwrap();
        git_run(&["add", "x.txt"], &g.ws);
        git_run(&["commit", "-m", "lonely commit"], &g.ws);
        let before = git_out(&["rev-parse", "HEAD"], &g.ws);

        rebase_dev_onto_base(&g.ws, "lonely");
        let after = git_out(&["rev-parse", "HEAD"], &g.ws);
        assert_eq!(before, after, "no rebase should happen without remote ref");
    }
}
