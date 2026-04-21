/// Namespace supervisor: mounts kernel overlayfs, runs bwrap, unmounts on exit.
/// Runs inside a user+mount+net namespace created by unshare(1).
use std::ffi::CString;
use std::os::unix::process::CommandExt;
use std::process::{exit, Command};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::Duration;
use std::{env, fs, io, os::fd::RawFd};

// libc bindings
extern "C" {
    fn mount(
        source: *const libc::c_char,
        target: *const libc::c_char,
        fstype: *const libc::c_char,
        flags: libc::c_ulong,
        data: *const libc::c_void,
    ) -> libc::c_int;
    fn umount2(target: *const libc::c_char, flags: libc::c_int) -> libc::c_int;
    fn inotify_init1(flags: libc::c_int) -> libc::c_int;
    fn inotify_add_watch(
        fd: libc::c_int,
        pathname: *const libc::c_char,
        mask: u32,
    ) -> libc::c_int;
}

const MNT_DETACH: libc::c_int = 2;
const IN_CREATE: u32 = 0x0000_0100;
const IN_MOVED_TO: u32 = 0x0000_0080;
const IN_NONBLOCK: libc::c_int = 0x0000_0800;

fn signal_parent(fd: RawFd) {
    let buf = b"1";
    unsafe { libc::write(fd, buf.as_ptr() as *const libc::c_void, 1) };
    unsafe { libc::close(fd) };
}

fn is_nix_lock_file(path: &str) -> bool {
    match fs::metadata(path) {
        Ok(m) => m.len() == 0 && m.file_type().is_file(),
        Err(_) => false,
    }
}

fn shadow_host_lock_files(upper: &str) {
    let dir = match fs::read_dir("/nix/store") {
        Ok(d) => d,
        Err(_) => return,
    };
    for entry in dir.flatten() {
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        if !name_str.ends_with(".lock") {
            continue;
        }
        let full_path = format!("/nix/store/{name_str}");
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

fn try_overlay_mount(merged: &str, upper: &str, work: &str) -> bool {
    let source = CString::new("overlay").unwrap();
    let target = CString::new(merged).unwrap();
    let fstype = CString::new("overlay").unwrap();
    let variants = [
        format!("lowerdir=/nix/store,upperdir={upper},workdir={work},userxattr,uuid=off"),
        format!("lowerdir=/nix/store,upperdir={upper},workdir={work},userxattr"),
        format!("lowerdir=/nix/store,upperdir={upper},workdir={work}"),
    ];
    for opts in &variants {
        let opts_c = CString::new(opts.as_str()).unwrap();
        let ret = unsafe {
            mount(
                source.as_ptr(),
                target.as_ptr(),
                fstype.as_ptr(),
                0,
                opts_c.as_ptr() as *const libc::c_void,
            )
        };
        if ret == 0 {
            return true;
        }
    }
    false
}

fn mount_kernel_overlay(merged: &str, upper: &str, work: &str) -> bool {
    if try_overlay_mount(merged, upper, work) {
        return true;
    }
    let errno = unsafe { *libc::__errno_location() };
    let msg = unsafe { libc::strerror(errno) };
    let msg_str = if msg.is_null() {
        "unknown".to_string()
    } else {
        unsafe { std::ffi::CStr::from_ptr(msg) }
            .to_string_lossy()
            .to_string()
    };
    eprintln!("supervisor: kernel overlayfs mount failed: {msg_str} (continuing without overlay)");
    false
}

fn umount_kernel(path: &str) {
    let path_c = CString::new(path).unwrap();
    let ret = unsafe { umount2(path_c.as_ptr(), MNT_DETACH) };
    if ret < 0 {
        let errno = unsafe { *libc::__errno_location() };
        eprintln!("supervisor: umount2 {path}: errno {errno}");
    }
}

fn create_shadow(upper: &str, name: &str) {
    let shadow = format!("{upper}/{name}");
    let _ = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&shadow);
}

fn inotify_read_events(fd: RawFd) -> Vec<String> {
    let mut buf = [0u8; 8192];
    let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
    if n <= 0 {
        return vec![];
    }
    let buf = &buf[..n as usize];
    let hdr_size = 16usize; // wd(4) + mask(4) + cookie(4) + len(4)
    let mut names = vec![];
    let mut offset = 0;
    while offset + hdr_size <= buf.len() {
        let name_len = u32::from_ne_bytes(buf[offset + 12..offset + 16].try_into().unwrap()) as usize;
        offset += hdr_size;
        if name_len > 0 && offset + name_len <= buf.len() {
            let raw = &buf[offset..offset + name_len];
            let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
            if let Ok(s) = std::str::from_utf8(&raw[..end]) {
                names.push(s.to_string());
            }
        }
        offset += name_len;
    }
    names
}

fn start_lock_watcher(upper: String) -> Option<Box<dyn Fn() + Send>> {
    let ifd = unsafe { inotify_init1(IN_NONBLOCK) };
    if ifd < 0 {
        eprintln!("supervisor: inotify_init failed, lock file watcher disabled");
        return None;
    }
    let store_c = CString::new("/nix/store").unwrap();
    let wd = unsafe { inotify_add_watch(ifd, store_c.as_ptr(), IN_CREATE | IN_MOVED_TO) };
    if wd < 0 {
        unsafe { libc::close(ifd) };
        eprintln!("supervisor: inotify_add_watch failed, lock file watcher disabled");
        return None;
    }

    let (stop_r, stop_w) = {
        let mut fds = [0i32; 2];
        unsafe { libc::pipe(fds.as_mut_ptr()) };
        (fds[0], fds[1])
    };

    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_flag2 = stop_flag.clone();

    thread::spawn(move || {
        let mut pollfds = [
            libc::pollfd { fd: ifd, events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: stop_r, events: libc::POLLIN, revents: 0 },
        ];
        while !stop_flag2.load(Ordering::Relaxed) {
            let ret = unsafe {
                libc::poll(pollfds.as_mut_ptr(), pollfds.len() as libc::nfds_t, 1000)
            };
            if ret <= 0 {
                continue;
            }
            if pollfds[1].revents & libc::POLLIN != 0 {
                break;
            }
            if pollfds[0].revents & libc::POLLIN != 0 {
                for name in inotify_read_events(ifd) {
                    if name.ends_with(".lock") {
                        create_shadow(&upper, &name);
                    }
                }
            }
        }
        unsafe {
            libc::close(ifd);
            libc::close(stop_r);
        }
    });

    Some(Box::new(move || {
        stop_flag.store(true, Ordering::Relaxed);
        unsafe {
            libc::write(stop_w, b"x".as_ptr() as *const libc::c_void, 1);
            libc::close(stop_w);
        }
        thread::sleep(Duration::from_millis(100));
    }))
}

fn disable_tty_suspend() -> Option<Box<dyn Fn()>> {
    // VSUSP = 10 on Linux
    const VSUSP: usize = 10;
    let fd = 0i32; // stdin
    let mut orig = unsafe { std::mem::zeroed::<libc::termios>() };
    if unsafe { libc::tcgetattr(fd, &mut orig) } != 0 {
        return None;
    }
    let mut new = orig;
    new.c_cc[VSUSP] = 0;
    unsafe { libc::tcsetattr(fd, libc::TCSANOW, &new) };
    Some(Box::new(move || {
        unsafe { libc::tcsetattr(fd, libc::TCSANOW, &orig) };
    }))
}

fn main() {
    let env_var = |name: &str| env::var(name).unwrap_or_default();

    let sync_fd: RawFd = env_var("CODING_CAVE_NS_SYNC_FD")
        .parse()
        .unwrap_or(-1);
    let merged = env_var("CODING_CAVE_NS_OVERLAY_MERGED");
    let upper = env_var("CODING_CAVE_NS_OVERLAY_UPPER");
    let work = env_var("CODING_CAVE_NS_OVERLAY_WORK");

    // Signal parent: namespace exists
    if sync_fd >= 0 {
        signal_parent(sync_fd);
    }

    // Shadow host lock files before overlay mount
    if !upper.is_empty() {
        shadow_host_lock_files(&upper);
    }

    // Mount kernel overlayfs (non-fatal: nested caves may not support it)
    let overlay_mounted = if !merged.is_empty() && !upper.is_empty() {
        mount_kernel_overlay(&merged, &upper, &work)
    } else {
        false
    };

    // Start lock file watcher
    let stop_watcher = if !upper.is_empty() {
        start_lock_watcher(upper.clone())
    } else {
        None
    };

    let restore_tty = disable_tty_suspend();

    // Run bwrap as child, stay alive to clean up
    let argv: Vec<String> = env::args().skip(1).collect();
    let rc = if argv.is_empty() {
        eprintln!("supervisor: no command given");
        1
    } else {
        match Command::new(&argv[0]).args(&argv[1..]).status() {
            Ok(s) => s.code().unwrap_or(1),
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
    if overlay_mounted {
        umount_kernel(&merged);
    }

    exit(rc);
}
