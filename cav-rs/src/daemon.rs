use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Result};

use crate::constants::{self, DAEMON_BASE_PORT};

pub struct DaemonHandle {
    pub proc: Child,
    pub base_dir: PathBuf,
}

pub fn agent_port(agent_id: &str) -> u16 {
    let n: u16 = agent_id.parse().unwrap_or(0);
    DAEMON_BASE_PORT as u16 + n
}

pub fn remote_url(agent_id: &str, slug: &str, host: &str) -> String {
    let port = agent_port(agent_id);
    format!("git://{host}:{port}/{slug}")
}

pub fn start(agent_id: &str, shared_repos: &[(PathBuf, String)]) -> Result<DaemonHandle> {
    let port = agent_port(agent_id);
    kill_stale_listener(port);

    let base_dir = {
        let prefix = format!("cav-daemon-{agent_id}-");
        let dir = std::env::temp_dir().join(&prefix);
        let dir = tempdir_with_prefix(&prefix)?;
        dir
    };

    for (repo_path, slug) in shared_repos {
        let link = base_dir.join(slug);
        std::os::unix::fs::symlink(repo_path, &link)?;
    }

    let socat_bin = which("socat")?;
    let handler_bin = {
        let p = PathBuf::from(&constants::build().cav_bin).join("cav-handler");
        if !p.is_file() {
            bail!("cav-handler not found at {}", p.display());
        }
        p.to_string_lossy().to_string()
    };
    let git_bin = which("git")?;
    let git_exec_path = std::process::Command::new(&git_bin)
        .arg("--exec-path")
        .output()?
        .stdout;
    let git_exec_path = String::from_utf8_lossy(&git_exec_path).trim().to_string();

    let proc = Command::new(&socat_bin)
        .arg(format!("TCP-LISTEN:{port},fork,reuseaddr,bind=127.0.0.1"))
        .arg(format!("EXEC:{handler_bin}"))
        .env_clear()
        .env("PATH", &git_exec_path)
        .env("CODING_CAVE_HANDLER_AGENT_ID", agent_id)
        .env("CODING_CAVE_HANDLER_BASE_PATH", base_dir.to_str().unwrap_or(""))
        .spawn()?;

    if !wait_for_port(port, 5.0, None) {
        bail!("Git daemon failed to start on port {port}");
    }

    Ok(DaemonHandle { proc, base_dir })
}

pub fn stop(mut handle: DaemonHandle) {
    let _ = handle.proc.kill();
    let _ = handle.proc.wait();
    let _ = std::fs::remove_dir_all(&handle.base_dir);
}

fn tempdir_with_prefix(prefix: &str) -> Result<PathBuf> {
    let tmp = std::env::temp_dir();
    for i in 0..1000 {
        let dir = tmp.join(format!("{prefix}{i}"));
        if std::fs::create_dir(&dir).is_ok() {
            return Ok(dir);
        }
    }
    bail!("Could not create temp dir with prefix {prefix}")
}

fn which(name: &str) -> Result<String> {
    // Check PATH explicitly
    for dir in std::env::var("PATH").unwrap_or_default().split(':') {
        let p = Path::new(dir).join(name);
        if p.is_file() {
            return Ok(p.to_string_lossy().to_string());
        }
    }
    bail!("{name} not found on PATH")
}

fn kill_stale_listener(port: u16) {
    // Read /proc/net/tcp to find socket inode for the port
    let tcp = match std::fs::read_to_string("/proc/net/tcp") {
        Ok(s) => s,
        Err(_) => return,
    };
    for line in tcp.lines().skip(1) {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 10 {
            continue;
        }
        let local = parts[1];
        let state = parts[3];
        if state != "0A" {
            continue;
        }
        let lport = match local.split(':').nth(1).and_then(|p| u16::from_str_radix(p, 16).ok()) {
            Some(p) => p,
            None => continue,
        };
        if lport != port {
            continue;
        }
        let inode: u64 = match parts[9].parse() {
            Ok(n) => n,
            Err(_) => continue,
        };
        kill_by_inode(inode);
        return;
    }
}

fn kill_by_inode(inode: u64) {
    let proc_dir = match std::fs::read_dir("/proc") {
        Ok(d) => d,
        Err(_) => return,
    };
    let target = format!("socket:[{inode}]");
    for entry in proc_dir.flatten() {
        if !entry.file_name().to_string_lossy().chars().all(|c| c.is_ascii_digit()) {
            continue;
        }
        let pid: i32 = match entry.file_name().to_string_lossy().parse() {
            Ok(n) => n,
            Err(_) => continue,
        };
        let fd_dir = entry.path().join("fd");
        let fds = match std::fs::read_dir(&fd_dir) {
            Ok(d) => d,
            Err(_) => continue,
        };
        for fd_entry in fds.flatten() {
            if let Ok(link) = std::fs::read_link(fd_entry.path()) {
                if link.to_string_lossy() == target {
                    let Some(rpid) = rustix::process::Pid::from_raw(pid) else {
                        return;
                    };
                    let _ = rustix::process::kill_process(rpid, rustix::process::Signal::Term);
                    for _ in 0..10 {
                        if rustix::process::test_kill_process(rpid).is_err() {
                            return;
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                    let _ = rustix::process::kill_process(rpid, rustix::process::Signal::Kill);
                    return;
                }
            }
        }
    }
}

fn wait_for_port(port: u16, timeout_secs: f64, _proc: Option<&Child>) -> bool {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_secs);
    while Instant::now() < deadline {
        if TcpStream::connect(format!("127.0.0.1:{port}")).is_ok() {
            return true;
        }
        thread::sleep(Duration::from_millis(100));
    }
    false
}
