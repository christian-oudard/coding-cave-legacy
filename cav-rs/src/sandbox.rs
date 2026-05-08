use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::os::fd::{AsRawFd, IntoRawFd, RawFd};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{bail, Result};
use rustix::process::Signal;
use serde::Deserialize;

use crate::config::{self, inside_cave, launch_home, parse_dev_branch, project_slug, shared_repo_path};
use crate::constants::{self, CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE};
use crate::{daemon, git, registry, user_env, workspace};

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
/// bwrap creates a user_ns whose gid_map has one entry: inner-gid →
/// caller's primary gid. Any other host gid renders as `nogroup` (65534)
/// inside, so the cave-coder user can't satisfy mode-660 group checks on
/// bind-mounted devices unless our primary gid IS the device's owning gid.
/// We engineer that by `setresgid`'ing to the device's group before exec'ing
/// bwrap. setresgid is gated on the target being one of {real, effective,
/// saved} gid for unprivileged callers — supplementary group membership
/// alone is not enough — so cav re-execs itself under `sg` up front (see
/// `ensure_egid_for_devices`) when the target gid isn't already settable.
///
/// Returns `Some(gid)` when one or more devices share an owning group we
/// can adopt; `None` when no devices were requested.
fn device_passthrough_gid(host_devices: &[String]) -> Result<Option<u32>> {
    use std::os::unix::fs::MetadataExt;
    let Some(first) = host_devices.first() else {
        return Ok(None);
    };
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
        bail!(
            "host user is not in group with gid {target} (owner of {first}); \
             add yourself with `sudo usermod -aG <group> $USER` and log back in"
        );
    }
    Ok(Some(target))
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

    if let Some(merged) = env.get("CODING_CAVE_NIX_MERGED") {
        args.extend(["--bind".to_string(), merged.clone(), "/nix/store".to_string()]);
        args.extend([
            "--tmpfs".to_string(), "/nix/var".to_string(),
            "--dir".to_string(), "/nix/var/nix".to_string(),
        ]);
        if let Some(db_dir) = env.get("CODING_CAVE_NIX_DB_DIR") {
            if Path::new(db_dir).is_dir() {
                args.extend([
                    "--bind".to_string(), db_dir.clone(), "/nix/var/nix/db".to_string(),
                ]);
            }
        }
    }

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

fn host_git_identity() -> HashMap<String, String> {
    let mut env = HashMap::new();
    let keys = [
        ("user.name", "CODING_CAVE_GIT_NAME"),
        ("user.email", "CODING_CAVE_GIT_EMAIL"),
        ("user.signingkey", "CODING_CAVE_GIT_SIGNINGKEY"),
        ("commit.gpgsign", "CODING_CAVE_GIT_GPGSIGN"),
    ];
    for (key, envvar) in &keys {
        if let Ok(out) = Command::new("git").args(["config", "--global", key]).output() {
            let val = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !val.is_empty() {
                env.insert(envvar.to_string(), val);
            }
        }
    }
    env
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

fn crinj_sessions_dir() -> PathBuf {
    config::data_dir().join("crinj-sessions")
}

fn crinj_pid_file() -> PathBuf {
    config::data_dir().join("crinj.pid")
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

fn crinj_register_session() -> PathBuf {
    let d = crinj_sessions_dir();
    let _ = std::fs::create_dir_all(&d);
    if let Ok(entries) = std::fs::read_dir(&d) {
        for entry in entries.flatten() {
            if let Ok(raw) = entry.file_name().to_string_lossy().parse::<i32>() {
                if !pid_alive(raw) {
                    let _ = std::fs::remove_file(entry.path());
                }
            }
        }
    }
    let f = d.join(std::process::id().to_string());
    let _ = std::fs::File::create(&f);
    f
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

fn crinj_unregister_session(session: &Path) -> bool {
    let _ = std::fs::remove_file(session);
    let d = crinj_sessions_dir();
    d.read_dir().map(|mut e| e.next().is_none()).unwrap_or(true)
}

/// Severity of a crinj log event. Ordering enables `>= Warn` etc.
#[derive(Copy, Clone, PartialEq, Eq, PartialOrd, Ord)]
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

/// Shared state between cav's main thread and the crinj-stderr drain thread.
struct CrinjState {
    /// Live-mirror gate. While true, the drain thread prints WARN/ERROR (and
    /// INFO when -v) to cav's stderr. Flipped false right before claude is
    /// exec'd: from then on, crinj output goes only to the log file.
    mirror: AtomicBool,
    /// Every event seen, pretty-rendered. Used for the startup-failure dump
    /// (entire log) and for the at-exit replay (warnings only).
    events: Mutex<Vec<(CrinjLevel, String)>>,
}

/// Render one JSON-formatted crinj log line into a human-readable form.
/// Falls back to the raw line if it doesn't parse as the expected shape.
fn render_crinj_event(line: &str) -> (CrinjLevel, String) {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
        return (CrinjLevel::Other, line.to_string());
    };
    let level_str = value.get("level").and_then(|v| v.as_str()).unwrap_or("");
    let level = CrinjLevel::parse(level_str);
    let fields = value.get("fields").and_then(|v| v.as_object());
    let message = fields
        .and_then(|f| f.get("message"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let mut extras: Vec<String> = Vec::new();
    if let Some(map) = fields {
        for (k, v) in map {
            if k == "message" {
                continue;
            }
            let rendered = match v {
                serde_json::Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            extras.push(format!("{k}={rendered}"));
        }
    }
    let pretty = if extras.is_empty() {
        format!("{level_str} {message}")
    } else {
        format!("{level_str} {message} ({})", extras.join(", "))
    };
    (level, pretty)
}

fn start_crinj() -> Result<(Option<std::process::Child>, Option<Arc<CrinjState>>)> {
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
    let _ = std::fs::create_dir_all(config::data_dir());
    let log_path = config::data_dir().join("crinj.log");
    let mut proc = Command::new("crinj")
        .args([
            "--port",
            &CRINJ_PORT.to_string(),
            "--allow-empty-rules",
            "--log-format=json",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()?;

    let stderr = proc.stderr.take().expect("piped stderr");
    let state = Arc::new(CrinjState {
        mirror: AtomicBool::new(true),
        events: Mutex::new(Vec::new()),
    });
    let state_thread = Arc::clone(&state);
    let drain = std::thread::spawn(move || {
        let mut log = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&log_path)
            .ok();
        let reader = BufReader::new(stderr);
        for line in reader.lines() {
            let Ok(line) = line else { break };
            if let Some(f) = log.as_mut() {
                let _ = writeln!(f, "{line}");
            }
            let (level, pretty) = render_crinj_event(&line);
            if let Ok(mut v) = state_thread.events.lock() {
                v.push((level, pretty.clone()));
            }
            if state_thread.mirror.load(Ordering::Relaxed) {
                let threshold = if config::verbose() >= 1 {
                    CrinjLevel::Info
                } else {
                    CrinjLevel::Warn
                };
                if level >= threshold {
                    eprintln!("{pretty}");
                }
            }
        }
    });

    let start = Instant::now();
    let deadline = start + Duration::from_secs(5);
    let mut drain = Some(drain);
    loop {
        if crinj_listening() {
            crate::log_debug!("crinj ready after {:.1}s", start.elapsed().as_secs_f64());
            break;
        }
        if let Some(status) = proc.try_wait()? {
            // Pipe closes when crinj exits; the drain thread finishes promptly.
            if let Some(h) = drain.take() {
                let _ = h.join();
            }
            let events = state.events.lock().unwrap();
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
/// past during boot or happened silently after the live mirror was muted.
fn print_crinj_session_warnings(state: &CrinjState) {
    let events = match state.events.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
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
        alerts.len()
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

fn broker_register_session() -> PathBuf {
    let d = broker_sessions_dir();
    let _ = std::fs::create_dir_all(&d);
    if let Ok(entries) = std::fs::read_dir(&d) {
        for entry in entries.flatten() {
            if let Ok(raw) = entry.file_name().to_string_lossy().parse::<i32>() {
                if !pid_alive(raw) {
                    let _ = std::fs::remove_file(entry.path());
                }
            }
        }
    }
    let f = d.join(std::process::id().to_string());
    let _ = std::fs::File::create(&f);
    f
}

fn broker_unregister_session(session: &Path) -> bool {
    let _ = std::fs::remove_file(session);
    let d = broker_sessions_dir();
    d.read_dir().map(|mut e| e.next().is_none()).unwrap_or(true)
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
    let broker_bin = PathBuf::from(&constants::build().cav_bin).join("cav-broker");
    if !broker_bin.exists() {
        bail!("cav-broker not found at {}", broker_bin.display());
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

    let server_bin = PathBuf::from(&constants::build().cav_bin).join("cav-channel-server");
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
) -> Vec<String> {
    let mut cmd = vec![
        "claude".to_string(),
        "--dangerously-skip-permissions".to_string(),
    ];
    // Channels only work in interactive mode. --print short-circuits
    // MCP server startup, so skip the flag there.
    if channels && prompt.is_none() {
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

fn nix_overlay_dirs(home: &Path, agent_id: &str) -> (PathBuf, PathBuf, PathBuf) {
    let base = home
        .join(".local")
        .join("share")
        .join("coding-cave")
        .join("nix-overlay")
        .join(format!("agent-{agent_id}"));
    let (upper, work, merged) = (base.join("upper"), base.join("work"), base.join("merged"));
    for d in [&upper, &work, &merged] {
        let _ = std::fs::create_dir_all(d);
    }
    (upper, work, merged)
}

fn migrate_shared_overlay(home: &Path, agent_id: &str) {
    let root = home
        .join(".local")
        .join("share")
        .join("coding-cave")
        .join("nix-overlay");
    let legacy = root.join("shared");
    let target = root.join(format!("agent-{agent_id}"));
    if legacy.is_dir() && !target.exists() {
        let _ = std::fs::rename(&legacy, &target);
    }
}

fn rm_store_path(path: &Path) {
    if path.is_symlink() || !path.is_dir() {
        if let Some(parent) = path.parent() {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700));
        }
        let _ = std::fs::remove_file(path);
        return;
    }
    let _ = chmod_tree(path);
    let _ = std::fs::remove_dir_all(path);
}

fn chmod_tree(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
    if let Ok(entries) = std::fs::read_dir(path) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() && !p.is_symlink() {
                chmod_tree(&p)?;
            }
        }
    }
    Ok(())
}

fn setup_nix_overlay(home: &Path, agent_id: &str) -> String {
    migrate_shared_overlay(home, agent_id);
    let (upper, work, merged) = nix_overlay_dirs(home, agent_id);
    // Clean stale upper entries
    let host_names: std::collections::HashSet<String> = std::fs::read_dir("/nix/store")
        .map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect())
        .unwrap_or_default();
    if let Ok(entries) = std::fs::read_dir(&upper) {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with('.') {
                continue;
            }
            if name.ends_with(".lock") {
                if entry.path().metadata().map(|m| m.len() == 0).unwrap_or(false) {
                    let _ = std::fs::remove_file(entry.path());
                }
                continue;
            }
            if !host_names.contains(&name) {
                rm_store_path(&entry.path());
            }
        }
    }
    // Reset work dir
    let work_inner = work.join("work");
    if work_inner.is_dir() {
        let _ = std::fs::remove_dir_all(&work_inner);
        let _ = std::fs::create_dir(&work_inner);
    }
    merged.to_string_lossy().to_string()
}

fn seed_nix_db(home: &Path, agent_id: &str, merged_path: &str) -> Option<PathBuf> {
    use rusqlite::{Connection, params};

    let parent_db = Path::new("/nix/var/nix/db/db.sqlite");
    if !parent_db.exists() {
        return None;
    }

    let db_dir = home
        .join(".local")
        .join("share")
        .join("coding-cave")
        .join("nix-overlay")
        .join(format!("agent-{agent_id}"))
        .join("db");
    let _ = std::fs::create_dir_all(&db_dir);
    let cave_db = db_dir.join("db.sqlite");
    let stamp = db_dir.join("db.stamp");

    // Skip reseed if host DB unchanged
    let state = parent_db.metadata().ok().map(|m| {
        use std::os::unix::fs::MetadataExt;
        format!("{}:{}", m.mtime_nsec(), m.size())
    });
    if let Some(ref s) = state {
        if cave_db.exists()
            && stamp.exists()
            && std::fs::read_to_string(&stamp).unwrap_or_default().trim() == s
        {
            crate::log_debug!("nix DB: host unchanged, skipping reseed");
            return Some(db_dir);
        }
    }

    // Determine store root for existence checks
    let store_root = if !merged_path.is_empty() {
        let candidate = Path::new(merged_path);
        if candidate.read_dir().map(|mut d| d.next().is_some()).unwrap_or(false) {
            candidate.to_path_buf()
        } else {
            PathBuf::from("/nix/store")
        }
    } else {
        PathBuf::from("/nix/store")
    };

    let store_names: std::collections::HashSet<String> = std::fs::read_dir(&store_root)
        .map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect())
        .unwrap_or_default();

    let result = (|| -> Result<()> {
        // Collect cave-only paths
        let mut cave_only_rows: Vec<(String, String, f64, String, i64, i64, Option<String>, Option<String>)> = vec![];
        if cave_db.exists() {
            let old = Connection::open_with_flags(
                &cave_db,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )?;
            let host = Connection::open_with_flags(
                parent_db,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )?;
            let host_paths: std::collections::HashSet<String> = {
                let mut stmt = host.prepare("SELECT path FROM ValidPaths")?;
                let rows = stmt.query_map([], |r| r.get(0))?;
                rows.filter_map(|r| r.ok()).collect()
            };
            let mut stmt = old.prepare(
                "SELECT path, hash, registrationTime, deriver, narSize, ultimate, sigs, ca \
                 FROM ValidPaths",
            )?;
            let rows = stmt.query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, f64>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, i64>(4)?,
                    r.get::<_, i64>(5)?,
                    r.get::<_, Option<String>>(6)?,
                    r.get::<_, Option<String>>(7)?,
                ))
            })?;
            for row in rows.flatten() {
                if !host_paths.contains(&row.0) {
                    let name = PathBuf::from(&row.0)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .to_string();
                    if store_names.contains(&name) {
                        cave_only_rows.push(row);
                    }
                }
            }
        }

        // Fresh backup from parent (copy file)
        for wal in std::fs::read_dir(&db_dir)
            .into_iter()
            .flatten()
            .flatten()
        {
            let name = wal.file_name().to_string_lossy().to_string();
            if name.starts_with("db.sqlite-") {
                let _ = std::fs::remove_file(wal.path());
            }
        }
        std::fs::copy(parent_db, &cave_db)?;
        let dst = Connection::open(&cave_db)?;

        // Merge cave-only paths
        if !cave_only_rows.is_empty() {
            dst.execute_batch("BEGIN")?;
            for (path, hash, reg_time, deriver, nar_size, ult, sigs, ca) in &cave_only_rows {
                dst.execute(
                    "INSERT OR IGNORE INTO ValidPaths \
                     (path, hash, registrationTime, deriver, narSize, ultimate, sigs, ca) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                    params![path, hash, reg_time, deriver, nar_size, ult, sigs, ca],
                )?;
            }
            dst.execute_batch("COMMIT")?;
        }

        // Remove stale entries
        let stale: Vec<i64> = {
            let mut stmt = dst.prepare("SELECT id, path FROM ValidPaths")?;
            let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
            rows.flatten()
                .filter(|(_, path)| {
                    let name = PathBuf::from(path)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .to_string();
                    !store_names.contains(&name)
                })
                .map(|(id, _)| id)
                .collect()
        };
        if !stale.is_empty() {
            dst.execute_batch("BEGIN")?;
            for vid in &stale {
                dst.execute("DELETE FROM Refs WHERE referrer = ?1 OR reference = ?1", params![vid])?;
                dst.execute("DELETE FROM ValidPaths WHERE id = ?1", params![vid])?;
            }
            dst.execute_batch("COMMIT")?;
        }
        Ok(())
    })();

    if let Err(e) = result {
        eprintln!("Warning: nix DB seed failed: {e}");
    }

    if let Some(s) = state {
        let _ = std::fs::write(&stamp, s);
    }

    Some(db_dir)
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

fn append_user_claude_md(home: &Path, host_md: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let dest = home.join(".claude").join("CLAUDE.md");
    if !dest.exists() || !host_md.exists() {
        return Ok(());
    }
    let m = std::fs::metadata(&dest)?;
    let mut p = m.permissions();
    p.set_mode(p.mode() | 0o200);
    std::fs::set_permissions(&dest, p)?;
    let mut text = std::fs::read_to_string(&dest)?;
    if !text.ends_with('\n') {
        text.push('\n');
    }
    text.push('\n');
    text.push_str(&std::fs::read_to_string(host_md)?);
    std::fs::write(&dest, text)?;
    Ok(())
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

/// Write a minimal ~/.gitconfig in the cave when the host has no
/// gitconfig but exposed git identity via env vars (CODING_CAVE_GIT_*).
/// host_gitconfig() copy already covers the more common case.
fn ensure_gitconfig(home: &Path) -> Result<()> {
    let dest = home.join(".gitconfig");
    if dest.exists() {
        return Ok(());
    }
    let mut user = String::new();
    if let Ok(v) = std::env::var("CODING_CAVE_GIT_NAME") {
        user.push_str(&format!("\tname = {v}\n"));
    }
    if let Ok(v) = std::env::var("CODING_CAVE_GIT_EMAIL") {
        user.push_str(&format!("\temail = {v}\n"));
    }
    if let Ok(v) = std::env::var("CODING_CAVE_GIT_SIGNINGKEY") {
        user.push_str(&format!("\tsigningkey = {v}\n"));
    }
    if user.is_empty() {
        return Ok(());
    }
    let mut content = format!("[user]\n{user}");
    if std::env::var("CODING_CAVE_GIT_GPGSIGN").as_deref() == Ok("true") {
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

/// Trivially clean = no uncommitted worktree changes anywhere, no unpushed
/// local commits or stash entries in the worktree clone, AND every agent-N/*
/// branch on every shared repo is at the same sha as its base. Absent agent
/// branches count as clean (the agent never pushed anything). An absent
/// worktree dir counts as clean for that slug (nothing to check); any
/// salvageable work still lives on the shared agent branch.
pub fn is_trivially_clean(
    workspaces: &[(PathBuf, PathBuf, String)],
    agent_id: &str,
) -> bool {
    for (ws, shared, _slug) in workspaces {
        if ws.exists() {
            if let Ok(out) = git::git_output(&["status", "--porcelain"], Some(ws)) {
                if !out.is_empty() {
                    return false;
                }
            } else {
                // Worktree dir exists but git status failed: weird state, preserve.
                return false;
            }

            // Stash entries are always salvageable work.
            if git::git(
                &["rev-parse", "--verify", "--quiet", "refs/stash"],
                Some(ws),
                false,
                true,
                false,
            )
            .is_ok()
            {
                return false;
            }

            // Every local branch must have all its commits reachable from a
            // remote-tracking ref. Unpushed local commits = preserve.
            let local_branches = git::git_output(
                &["for-each-ref", "refs/heads/", "--format=%(refname:short)"],
                Some(ws),
            )
            .unwrap_or_default();
            for br in local_branches.lines() {
                let count = git::git_output(
                    &[
                        "rev-list",
                        "--count",
                        &format!("refs/heads/{br}"),
                        "--not",
                        "--remotes",
                    ],
                    Some(ws),
                )
                .unwrap_or_else(|_| "1".to_string());
                if count.trim() != "0" {
                    return false;
                }
            }
        }

        let refs = git::git_output(
            &[
                "for-each-ref",
                &format!("refs/heads/agent-{agent_id}/"),
                "--format=%(refname:short)",
            ],
            Some(shared),
        )
        .unwrap_or_default();
        for ab in refs.lines() {
            let base_name = config::resolve_base(ab.to_string());
            let ab_sha = git::git_output(
                &["rev-parse", &format!("refs/heads/{ab}")],
                Some(shared),
            );
            let base_sha = git::git_output(
                &["rev-parse", &format!("refs/heads/{base_name}")],
                Some(shared),
            );
            match (ab_sha, base_sha) {
                (Ok(a), Ok(b)) if a == b => continue,
                _ => return false,
            }
        }
    }
    true
}

/// Remove every trace of the agent: shared-repo refs, local host branches,
/// worktree dirs, registry row. Call only after `is_trivially_clean`.
fn teardown_agent(
    base: &Path,
    workspaces: &[(PathBuf, PathBuf, String)],
    agent_id: &str,
) {
    let primary_slug = project_slug(base);
    for (ws, shared, slug) in workspaces {
        let refs = git::git_output(
            &[
                "for-each-ref",
                &format!("refs/heads/agent-{agent_id}/"),
                "--format=%(refname)",
            ],
            Some(shared),
        )
        .unwrap_or_default();
        for rf in refs.lines() {
            git::git(&["update-ref", "-d", rf], Some(shared), false, true, false).ok();
        }

        let _ = std::fs::remove_dir_all(ws);

        let host_base: Option<PathBuf> = if slug == &primary_slug {
            Some(base.to_path_buf())
        } else {
            registry::project_path(slug)
                .ok()
                .flatten()
                .map(PathBuf::from)
        };
        if let Some(hb) = host_base {
            let local_refs = git::git_output(
                &[
                    "for-each-ref",
                    &format!("refs/heads/agent-{agent_id}/"),
                    "--format=%(refname:short)",
                ],
                Some(&hb),
            )
            .unwrap_or_default();
            for ab in local_refs.lines() {
                git::git(&["branch", "-D", ab], Some(&hb), false, true, false).ok();
            }
            git::git(
                &["fetch", "--prune", CAVE_SHARED_REMOTE],
                Some(&hb),
                false,
                true,
                false,
            )
            .ok();
        }
    }
    registry::remove_agent(agent_id).ok();
}

/// Print the cav resume commands. Claude already prints its own
/// "Resume this session with: claude --resume <uuid>" line, so we just
/// append the cav-flavored alternatives. Session uuid first (the
/// canonical, archive-backed form), then the agent-id shortcut.
fn print_resume_hints(agent_id: &str, session_id: Option<&str>, agent_id_gone: bool) {
    if let Some(sid) = session_id {
        eprintln!("cav --resume {sid}");
    }
    if !agent_id_gone {
        eprintln!("cav -r {agent_id}");
    }
}

/// Reconstruct an agent's workspace tuples from registry state. Used at
/// command boundaries when reaping agents whose run_agent handler never got
/// the chance to do teardown (SIGKILL, OOM, etc).
fn resolve_workspaces_for(agent_id: &str) -> Vec<(PathBuf, PathBuf, String)> {
    let slugs = registry::agent_all_slugs(agent_id).unwrap_or_default();
    let mut out = Vec::new();
    for slug in slugs {
        let Ok(Some(hb_str)) = registry::project_path(&slug) else { continue };
        let hb = PathBuf::from(hb_str);
        let shared = config::shared_repo_path(&hb);
        let wt = config::worktree_dir(&slug, agent_id);
        out.push((wt, shared, slug));
    }
    out
}

/// Walk the registry, detect agents whose PID is dead (reaped by
/// `is_agent_running`) and whose state is trivially clean, and tear them
/// down. Exists so crashes / SIGKILLs eventually get the teardown that a
/// clean exit would have done, without a background sweeper.
pub fn reap_dead_agents() {
    let Ok(ids) = registry::all_agent_ids(None) else { return };
    let current = std::env::var("CODING_CAVE_AGENT_ID").ok();
    for aid in ids {
        if current.as_deref() == Some(&aid) {
            continue;
        }
        if registry::is_agent_running(&aid).unwrap_or(false) {
            continue;
        }
        // An agent with approvals is still a valid reject target: keep the
        // registry row alive so reject can reassign work back to it.
        if registry::agent_has_approvals(&aid).unwrap_or(false) {
            continue;
        }
        let workspaces = resolve_workspaces_for(&aid);
        if workspaces.is_empty() {
            continue;
        }
        if !is_trivially_clean(&workspaces, &aid) {
            continue;
        }
        let primary_slug = workspaces[0].2.clone();
        let Ok(Some(base_str)) = registry::project_path(&primary_slug) else { continue };
        teardown_agent(&PathBuf::from(base_str), &workspaces, &aid);
    }
}

/// Drop shared dev branches whose tip is already reachable from the base.
/// Provably nothing to lose: the dev tip is an ancestor of base, so every
/// commit on it is already in base. Covers tip == base (the post-approve
/// case) and tip behind base (agent lagging while base advanced). Safe to
/// call from any command that fetches the shared remote first.
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
        if !git::is_ancestor(&remote_dev, &remote_base, Some(base)) {
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
    pub host_devices: Vec<String>,
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

    let home = launch_home(&agent_name);
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

    // Inside a nested cave, overlayfs is not supported. Use the inherited
    // /nix/store (from bwrap static_args ro-bind) without a writable overlay.
    let (nix_merged, nix_db_dir) = if inside_cave() {
        (String::new(), None)
    } else {
        crate::log_info!("setting up nix overlay...");
        let merged = setup_nix_overlay(&home, &agent_id);
        let db_dir = seed_nix_db(&home, &agent_id, &merged);
        crate::log_info!("nix overlay ready");
        (merged, db_dir)
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
    if !nix_merged.is_empty() {
        env.insert("CODING_CAVE_NIX_MERGED".to_string(), nix_merged.clone());
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
    if let Some(db) = &nix_db_dir {
        env.insert(
            "CODING_CAVE_NIX_DB_DIR".to_string(),
            db.to_string_lossy().to_string(),
        );
    }
    env.insert("CODING_CAVE_DNS_SERVER".to_string(), SLIRP_DNS.to_string());
    if !host_devices.is_empty() {
        env.insert(
            "CODING_CAVE_HOST_DEVICES".to_string(),
            host_devices.join(":"),
        );
    }

    let parent_lineage = std::env::var("CODING_CAVE_LINEAGE").unwrap_or_default();
    let parent_agent = std::env::var("CODING_CAVE_AGENT_NAME").unwrap_or_default();
    if !parent_agent.is_empty() {
        let lineage = if parent_lineage.is_empty() {
            parent_agent.clone()
        } else {
            format!("{parent_lineage}.{parent_agent}")
        };
        env.insert("CODING_CAVE_LINEAGE".to_string(), lineage);
    }

    env.extend(host_git_identity());
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
    let user_claude_md = host_home()
        .join(".config")
        .join("coding-cave")
        .join("CLAUDE.md");

    let crinj_session = crinj_register_session();
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
    env.insert(
        "CLAUDE_CODE_OAUTH_TOKEN".to_string(),
        "sk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\
         AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-AAAAAAAA"
            .to_string(),
    );

    // Channel broker (inter-agent messaging). Gated by --comms.
    let (broker_session, broker_proc, channels_enabled) = if comms {
        let session = broker_register_session();
        let proc = match start_broker() {
            Ok(p) => p,
            Err(e) => {
                eprintln!("Warning: channel broker failed to start: {e}");
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
                eprintln!("Warning: channel marketplace setup failed: {e}");
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
    if user_claude_md.is_file() {
        append_user_claude_md(&home, &user_claude_md)?;
    }
    if let Some(gc) = host_gitconfig() {
        let _ = std::fs::copy(&gc, home.join(".gitconfig"));
    }
    ensure_gitconfig(&home)?;
    if !cfg.ca_cert_bundle.is_empty() {
        write_combined_ca_bundle(&home, Path::new(&cfg.ca_cert_bundle), &ca_path)?;
    }
    crate::log_info!("cave home ready");

    let claude_cmd = build_claude_cmd(
        session_id.as_deref(),
        resuming,
        prompt.as_deref(),
        channels_enabled,
    );
    let daemon_port = daemon::agent_port(&agent_id);

    let (sync_r, sync_w) = rustix::pipe::pipe()?;

    env.insert(
        "CODING_CAVE_NS_SYNC_FD".to_string(),
        sync_w.as_raw_fd().to_string(),
    );
    if !inside_cave() {
        let (upper, work, merged_pb) = nix_overlay_dirs(&home, &agent_id);
        env.insert(
            "CODING_CAVE_NS_OVERLAY_MERGED".to_string(),
            merged_pb.to_string_lossy().to_string(),
        );
        env.insert(
            "CODING_CAVE_NS_OVERLAY_UPPER".to_string(),
            upper.to_string_lossy().to_string(),
        );
        env.insert(
            "CODING_CAVE_NS_OVERLAY_WORK".to_string(),
            work.to_string_lossy().to_string(),
        );
    }

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

    // Stop streaming crinj logs to the terminal once we hand off to claude.
    // Anything crinj logs from here on lands in crinj.log and gets replayed
    // at session exit if it's a warning/error.
    if let Some(ref s) = crinj_state {
        s.mirror.store(false, Ordering::Relaxed);
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

    // Keep the registry row alive whenever there's anything worth coming
    // back to: salvageable git work OR an archived conversation. Pure
    // teardown only when both are absent.
    let trivially_clean = is_trivially_clean(&workspaces, &agent_id);
    let agent_id_survives = !trivially_clean || actual_sid.is_some();
    if agent_id_survives {
        if let Some(ref sid) = actual_sid {
            if Some(sid.as_str()) != session_id.as_deref() {
                registry::write_session_id(&agent_id, sid).ok();
            }
        }
        registry::mark_agent_exited(&agent_id).ok();
    } else {
        teardown_agent(&base, &workspaces, &agent_id);
    }
    if actual_sid.is_some() {
        print_resume_hints(&agent_id, actual_sid.as_deref(), !agent_id_survives);
    }

    let _ = slirp.kill();
    let _ = slirp.wait();
    if crinj_unregister_session(&crinj_session) {
        crinj_signal(Signal::Term);
        if let Some(mut cp) = crinj_proc {
            let _ = cp.wait();
        }
    }
    if let Some(ref s) = crinj_state {
        print_crinj_session_warnings(s);
    }
    if let Some(session) = broker_session {
        if broker_unregister_session(&session) {
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
    use super::is_trivially_clean;
    use std::fs;
    use std::path::PathBuf;
    use std::process::Command;

    fn git(args: &[&str], dir: &std::path::Path) {
        let out = Command::new("git")
            .args(args)
            .current_dir(dir)
            .output()
            .unwrap_or_else(|_| panic!("git {args:?} failed"));
        assert!(
            out.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }

    struct Fixture {
        _tmp: PathBuf,
        ws: PathBuf,
        shared: PathBuf,
        slug: String,
    }

    impl Fixture {
        fn new(name: &str) -> Self {
            let tmp = std::env::temp_dir()
                .join(format!("cav-tc-{}-{name}", std::process::id()));
            let _ = fs::remove_dir_all(&tmp);
            fs::create_dir_all(&tmp).unwrap();

            let shared = tmp.join("cave-shared.git");
            Command::new("git")
                .args(["init", "--bare", "-b", "main"])
                .arg(&shared)
                .output()
                .unwrap();

            let ws = tmp.join("worktree");
            Command::new("git")
                .args(["clone"])
                .arg(&shared)
                .arg(&ws)
                .output()
                .unwrap();
            git(&["config", "user.email", "t@t"], &ws);
            git(&["config", "user.name", "T"], &ws);
            fs::write(ws.join("file"), "x\n").unwrap();
            git(&["add", "file"], &ws);
            git(&["commit", "-m", "base"], &ws);
            git(&["push", "origin", "main"], &ws);

            Self {
                _tmp: tmp,
                ws,
                shared,
                slug: "proj".to_string(),
            }
        }

        fn workspaces(&self) -> Vec<(PathBuf, PathBuf, String)> {
            vec![(self.ws.clone(), self.shared.clone(), self.slug.clone())]
        }

        fn push_agent_branch(&self, agent_id: &str, base: &str, add_commit: bool) {
            let dev = format!("agent-{agent_id}/{base}");
            git(&["checkout", "-B", &dev, base], &self.ws);
            if add_commit {
                fs::write(self.ws.join("agent-file"), "a\n").unwrap();
                git(&["add", "agent-file"], &self.ws);
                git(&["commit", "-m", "agent work"], &self.ws);
            }
            git(
                &["push", "origin", &format!("{dev}:refs/heads/{dev}")],
                &self.ws,
            );
            git(&["checkout", base], &self.ws);
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self._tmp);
        }
    }

    #[test]
    fn clean_with_no_agent_branch_is_trivially_clean() {
        let f = Fixture::new("no-branch");
        assert!(is_trivially_clean(&f.workspaces(), "1"));
    }

    #[test]
    fn agent_branch_matching_base_is_trivially_clean() {
        let f = Fixture::new("match-base");
        f.push_agent_branch("1", "main", false);
        assert!(is_trivially_clean(&f.workspaces(), "1"));
    }

    #[test]
    fn agent_branch_with_commits_is_not_clean() {
        let f = Fixture::new("with-commits");
        f.push_agent_branch("1", "main", true);
        assert!(!is_trivially_clean(&f.workspaces(), "1"));
    }

    #[test]
    fn uncommitted_changes_in_worktree_are_not_clean() {
        let f = Fixture::new("dirty-wt");
        fs::write(f.ws.join("dirty"), "junk").unwrap();
        assert!(!is_trivially_clean(&f.workspaces(), "1"));
    }

    #[test]
    fn other_agents_branch_does_not_count() {
        // Agent 2 has commits, but we're asking about agent 1.
        let f = Fixture::new("other-agent");
        f.push_agent_branch("2", "main", true);
        assert!(is_trivially_clean(&f.workspaces(), "1"));
    }

    #[test]
    fn unpushed_local_commit_is_not_clean() {
        // Worktree clone has a local agent branch with a commit that was
        // never pushed to the shared bare repo: must preserve.
        let f = Fixture::new("unpushed");
        let dev = "agent-1/main";
        git(&["checkout", "-B", dev, "main"], &f.ws);
        fs::write(f.ws.join("local-only"), "x\n").unwrap();
        git(&["add", "local-only"], &f.ws);
        git(&["commit", "-m", "local only, never pushed"], &f.ws);
        git(&["checkout", "main"], &f.ws);
        assert!(!is_trivially_clean(&f.workspaces(), "1"));
    }

    #[test]
    fn stash_in_worktree_is_not_clean() {
        // Stash entries are salvageable work even with a clean worktree:
        // must preserve.
        let f = Fixture::new("stash");
        fs::write(f.ws.join("file"), "modified\n").unwrap();
        git(&["stash", "push", "-m", "wip"], &f.ws);
        assert!(!is_trivially_clean(&f.workspaces(), "1"));
    }
}
