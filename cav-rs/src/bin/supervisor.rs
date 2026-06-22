/// Namespace supervisor: signals the parent, disables tty suspend, runs bwrap,
/// and waits. Runs as PID 1 inside a user+mount+pid+net namespace created by
/// unshare(1). Each cave now uses its own nix store (bound by bwrap), so the
/// supervisor no longer mounts an overlay or shadows host lock files.
use std::os::fd::{BorrowedFd, FromRawFd, OwnedFd};
use std::process::{exit, Command};
#[cfg(debug_assertions)]
use std::time::{SystemTime, UNIX_EPOCH};
use std::env;

#[cfg(debug_assertions)]
fn log_phase(msg: &str) {
    if env::var("CODING_CAVE_VERBOSE").is_err() {
        return;
    }
    let start_ns: u128 = env::var("CODING_CAVE_TS_START")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let now_ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let elapsed = now_ns.saturating_sub(start_ns) as f64 / 1e9;
    eprintln!("[cav +{elapsed:6.3}s] supervisor: {msg}");
}

#[cfg(not(debug_assertions))]
fn log_phase(_msg: &str) {}

use rustix::io::write;
use rustix::termios::{isatty, tcgetattr, tcsetattr, OptionalActions, SpecialCodeIndex};

fn signal_parent(fd: &OwnedFd) {
    let _ = write(fd, b"1");
}

// CAUTION: this binary runs as PID 1 inside `unshare --pid --fork`'s new pid
// namespace. Any pid returned by `Command::id()` (or any libc call here) is
// in *that* namespace, not the host's. Don't pipe such pids back to cav for
// host-side use (e.g. nsenter), or they'll resolve to unrelated host
// processes (kernel threads at low pids), and setns will fail with EPERM.
// cav resolves the sandboxed target by walking /proc from its own pid ns.

fn disable_tty_suspend() -> Option<Box<dyn Fn()>> {
    let fd: BorrowedFd<'static> = rustix::stdio::stdin();
    if !isatty(fd) {
        return None;
    }
    let orig = tcgetattr(fd).ok()?;
    let mut new = orig.clone();
    new.special_codes[SpecialCodeIndex::VSUSP] = 0;
    tcsetattr(fd, OptionalActions::Now, &new).ok()?;
    Some(Box::new(move || {
        let _ = tcsetattr(fd, OptionalActions::Now, &orig);
    }))
}

fn main() {
    let env_var = |name: &str| env::var(name).unwrap_or_default();

    let sync_fd_raw: i32 = env_var("CODING_CAVE_NS_SYNC_FD").parse().unwrap_or(-1);

    log_phase("started");

    // Signal parent: namespace exists.
    if sync_fd_raw >= 0 {
        let fd = unsafe { OwnedFd::from_raw_fd(sync_fd_raw) };
        signal_parent(&fd);
        drop(fd);
    }

    let restore_tty = disable_tty_suspend();

    // Run bwrap as child, stay alive to clean up the tty on exit.
    let argv: Vec<String> = env::args().skip(1).collect();
    log_phase("exec bwrap");
    let rc = if argv.is_empty() {
        eprintln!("supervisor: no command given");
        1
    } else {
        match Command::new(&argv[0]).args(&argv[1..]).spawn() {
            Ok(mut child) => match child.wait() {
                Ok(s) => s.code().unwrap_or(1),
                Err(e) => {
                    eprintln!("supervisor: wait failed: {e}");
                    1
                }
            },
            Err(e) => {
                eprintln!("supervisor: exec failed: {e}");
                1
            }
        }
    };

    if let Some(restore) = restore_tty {
        restore();
    }

    exit(rc);
}
