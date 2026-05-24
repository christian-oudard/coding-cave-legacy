/// Namespace supervisor: mounts kernel overlayfs, runs bwrap, unmounts on exit.
/// Runs inside a user+mount+net namespace created by unshare(1).
use std::mem::MaybeUninit;
use std::os::fd::{BorrowedFd, FromRawFd, OwnedFd};
use std::process::{exit, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
#[cfg(debug_assertions)]
use std::time::{SystemTime, UNIX_EPOCH};
use std::{env, fs};

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

use rustix::event::{poll, PollFd, PollFlags};
use rustix::fs::inotify::{self, CreateFlags, WatchFlags};
use rustix::io::{write, Errno};
use rustix::mount::{mount, unmount, MountFlags, UnmountFlags};
use rustix::pipe::pipe;
use rustix::termios::{isatty, tcgetattr, tcsetattr, OptionalActions, SpecialCodeIndex};

// Provided by modules/host.nix on the host: an idmapped read-only mount
// of /nix/store that relabels host-uid-0 to cave-coder. cav's pre-launch
// check (sandbox.rs::require_host_module) refuses to start without it,
// so the supervisor can assume it's present.
const NIX_STORE_IDMAPPED: &str = "/var/lib/coding-cave/nix-store";

fn signal_parent(fd: &OwnedFd) {
    let _ = write(fd, b"1");
}

// CAUTION: this binary runs as PID 1 inside `unshare --pid --fork`'s new pid
// namespace. Any pid returned by `Command::id()` (or any libc call here) is
// in *that* namespace, not the host's. Don't pipe such pids back to cav for
// host-side use (e.g. nsenter), or they'll resolve to unrelated host
// processes (kernel threads at low pids), and setns will fail with EPERM.
// cav resolves the sandboxed target by walking /proc from its own pid ns.

fn is_nix_lock_file(path: &str) -> bool {
    match fs::metadata(path) {
        Ok(m) => m.len() == 0 && m.file_type().is_file(),
        Err(_) => false,
    }
}

fn shadow_host_lock_files(upper: &str) {
    let dir = match fs::read_dir(NIX_STORE_IDMAPPED) {
        Ok(d) => d,
        Err(_) => return,
    };
    for entry in dir.flatten() {
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        if !name_str.ends_with(".lock") {
            continue;
        }
        let full_path = format!("{NIX_STORE_IDMAPPED}/{name_str}");
        if !is_nix_lock_file(&full_path) {
            continue;
        }
        let shadow = format!("{upper}/{name_str}");
        if fs::symlink_metadata(&shadow).is_ok() {
            continue;
        }
        let _ = fs::File::create(&shadow);
    }
}

fn try_overlay_mount(merged: &str, upper: &str, work: &str) -> rustix::io::Result<()> {
    let variants = [
        format!("lowerdir={NIX_STORE_IDMAPPED},upperdir={upper},workdir={work},userxattr,uuid=off"),
        format!("lowerdir={NIX_STORE_IDMAPPED},upperdir={upper},workdir={work},userxattr"),
        format!("lowerdir={NIX_STORE_IDMAPPED},upperdir={upper},workdir={work}"),
    ];
    let mut last = Err(Errno::INVAL);
    for opts in &variants {
        match mount("overlay", merged, "overlay", MountFlags::empty(), opts.as_str()) {
            Ok(()) => return Ok(()),
            Err(e) => last = Err(e),
        }
    }
    last
}

fn mount_kernel_overlay(merged: &str, upper: &str, work: &str) {
    if let Err(e) = try_overlay_mount(merged, upper, work) {
        eprintln!("supervisor: kernel overlayfs mount failed: {e}");
        exit(1);
    }
}

fn umount_kernel(path: &str) {
    if let Err(e) = unmount(path, UnmountFlags::DETACH) {
        eprintln!("supervisor: umount2 {path}: {e}");
    }
}

fn create_shadow(upper: &str, name: &str) {
    let shadow = format!("{upper}/{name}");
    let _ = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&shadow);
}

fn start_lock_watcher(upper: String) -> Option<Box<dyn Fn() + Send>> {
    let ifd = match inotify::init(CreateFlags::NONBLOCK) {
        Ok(fd) => fd,
        Err(_) => {
            eprintln!("supervisor: inotify_init failed, lock file watcher disabled");
            return None;
        }
    };
    if inotify::add_watch(&ifd, NIX_STORE_IDMAPPED, WatchFlags::CREATE | WatchFlags::MOVED_TO).is_err() {
        eprintln!("supervisor: inotify_add_watch failed, lock file watcher disabled");
        return None;
    }

    let (stop_r, stop_w) = pipe().ok()?;

    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_flag2 = stop_flag.clone();

    thread::spawn(move || {
        let mut event_buf = [MaybeUninit::<u8>::uninit(); 8192];
        loop {
            if stop_flag2.load(Ordering::Relaxed) {
                break;
            }
            let mut pollfds = [
                PollFd::new(&ifd, PollFlags::IN),
                PollFd::new(&stop_r, PollFlags::IN),
            ];
            let ret = poll(&mut pollfds, 1000).unwrap_or(0);
            if ret == 0 {
                continue;
            }
            if pollfds[1].revents().contains(PollFlags::IN) {
                break;
            }
            if !pollfds[0].revents().contains(PollFlags::IN) {
                continue;
            }
            let mut reader = inotify::Reader::new(&ifd, &mut event_buf);
            loop {
                match reader.next() {
                    Ok(ev) => {
                        if let Some(name) = ev.file_name() {
                            let name = name.to_string_lossy();
                            if name.ends_with(".lock") {
                                create_shadow(&upper, &name);
                            }
                        }
                    }
                    Err(Errno::WOULDBLOCK) => break,
                    Err(_) => break,
                }
            }
        }
    });

    Some(Box::new(move || {
        stop_flag.store(true, Ordering::Relaxed);
        let _ = write(&stop_w, b"x");
        thread::sleep(Duration::from_millis(100));
    }))
}

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
    let merged = env_var("CODING_CAVE_NS_OVERLAY_MERGED");
    let upper = env_var("CODING_CAVE_NS_OVERLAY_UPPER");
    let work = env_var("CODING_CAVE_NS_OVERLAY_WORK");

    log_phase("started");

    // Signal parent: namespace exists.
    if sync_fd_raw >= 0 {
        let fd = unsafe { OwnedFd::from_raw_fd(sync_fd_raw) };
        signal_parent(&fd);
        drop(fd);
    }

    // Shadow host lock files before overlay mount
    if !upper.is_empty() {
        shadow_host_lock_files(&upper);
        log_phase("lock files shadowed");
    }

    // Top-level cave: cav guarantees the host module is mounted before getting
    // here, so a mount failure is a real error. Nested caves skip overlay
    // entirely (cav leaves the env vars empty) and use the inherited /nix/store.
    let needs_overlay = !merged.is_empty() && !upper.is_empty();
    if needs_overlay {
        mount_kernel_overlay(&merged, &upper, &work);
        log_phase("overlay mounted");
    }

    // Start lock file watcher
    let stop_watcher = if !upper.is_empty() {
        start_lock_watcher(upper.clone())
    } else {
        None
    };

    let restore_tty = disable_tty_suspend();

    // Run bwrap as child, stay alive to clean up
    let argv: Vec<String> = env::args().skip(1).collect();
    log_phase("exec bwrap");
    let rc = if argv.is_empty() {
        eprintln!("supervisor: no command given");
        1
    } else {
        match Command::new(&argv[0]).args(&argv[1..]).spawn() {
            Ok(mut child) => {
                match child.wait() {
                    Ok(s) => s.code().unwrap_or(1),
                    Err(e) => {
                        eprintln!("supervisor: wait failed: {e}");
                        1
                    }
                }
            }
            Err(e) => {
                eprintln!("supervisor: exec failed: {e}");
                1
            }
        }
    };

    // Cleanup
    if let Some(restore) = restore_tty {
        restore();
    }
    if let Some(stop) = stop_watcher {
        stop();
    }
    if needs_overlay {
        umount_kernel(&merged);
    }

    exit(rc);
}
