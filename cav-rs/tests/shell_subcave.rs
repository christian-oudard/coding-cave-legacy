/// Subcave integration test for `cav shell`.
///
/// Spawns a real `unshare --pid --fork -- bwrap` topology that mirrors the
/// shape of a cave, registers it in the registry, then drives `cav shell`
/// against it and verifies the entered shell can run a command and see the
/// project directory.
///
/// Regression: `cav shell` previously read a recorded `inner_pid` from the
/// registry and handed it to host `nsenter`. That value was bwrap's pid as
/// seen from inside the supervisor's pid namespace (typically 2 or 3),
/// not a host pid, so `setns` failed with EPERM. The fix walks the proc
/// tree from cav's pid namespace and finds the descendant whose mount
/// namespace differs from the outer unshare process — that's the
/// sandboxed target we want to enter.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

const CAV: &str = env!("CARGO_BIN_EXE_cav");

struct Subcave {
    project_dir: PathBuf,
    data_dir: PathBuf,
    slug: String,
    agent_id: String,
    child: std::process::Child,
}

impl Subcave {
    fn new() -> Self {
        let pid = std::process::id();
        let slug = format!("cav-shell-test-{pid}");
        // Keep the project at /projects/<slug> so cav shell's inner
        // `cd /projects/<slug>` works without bind-mount setup. We rely on
        // bwrap's `--bind / /` to expose this path inside the cave.
        let project_dir = PathBuf::from(format!("/projects/{slug}"));
        let data_dir = std::env::temp_dir().join(format!("cav-shell-data-{pid}"));

        let _ = fs::remove_dir_all(&project_dir);
        let _ = fs::remove_dir_all(&data_dir);
        fs::create_dir_all(&project_dir).unwrap();
        fs::create_dir_all(&data_dir).unwrap();

        git(&["init", "-b", "main"], &project_dir);
        git(&["config", "user.email", "test@test.com"], &project_dir);
        git(&["config", "user.name", "Test"], &project_dir);
        fs::write(project_dir.join("README.md"), "# test\n").unwrap();
        git(&["add", "README.md"], &project_dir);
        git(&["commit", "-m", "init"], &project_dir);

        let out = Command::new(CAV)
            .args(["init", project_dir.to_str().unwrap()])
            .env("CODING_CAVE_DATA_DIR", &data_dir)
            .env_remove("CODING_CAVE_VERSION")
            .env_remove("CODING_CAVE_AGENT_ID")
            .output()
            .expect("cav init");
        assert!(
            out.status.success(),
            "cav init: {}",
            String::from_utf8_lossy(&out.stderr)
        );

        // Mirror the real cave's namespace topology: outer unshare creates
        // user/net/mount/pid namespaces and supervisor (here just bwrap)
        // becomes PID 1 inside the new pid ns. bwrap's --unshare-pid creates
        // a further inner pid ns for the sandbox. cav shell's --pid setns
        // requires CAP_SYS_ADMIN in the user ns that owns the pid ns, which
        // is the cave's user ns; nsenter is fine here because we entered
        // --user first. Cleanup: kill bwrap (unshare's forked child), and
        // bwrap's --die-with-parent cascades to the sandboxed sleep, then
        // unshare exits naturally — no orphaned zombies.
        let child = Command::new("unshare")
            .args([
                "--user",
                "--net",
                "--mount",
                "--pid",
                "--fork",
                "--map-user=0",
                "--map-group=0",
                "--",
                "bwrap",
                "--die-with-parent",
                "--bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--unshare-pid",
                "/bin/bash",
                "-c",
                "exec sleep 30",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn unshare+bwrap");

        // Wait for bwrap to set up its inner mount/pid ns and exec sleep,
        // otherwise the proc-tree walk may not yet see the inner child's
        // /proc/<pid>/ns/mnt.
        wait_for_inner_ns(child.id(), std::time::Duration::from_secs(5));

        let agent_id = "1".to_string();
        register_running_agent(&data_dir, &slug, &agent_id, child.id());

        Subcave {
            project_dir,
            data_dir,
            slug,
            agent_id,
            child,
        }
    }

    fn cav(&self, args: &[&str]) -> Output {
        Command::new(CAV)
            .args(args)
            .current_dir(&self.project_dir)
            .env("CODING_CAVE_DATA_DIR", &self.data_dir)
            .env_remove("CODING_CAVE_VERSION")
            .env_remove("CODING_CAVE_AGENT_ID")
            .output()
            .expect("cav binary not found")
    }
}

impl Drop for Subcave {
    fn drop(&mut self) {
        // Cleanup needs care: unshare --fork runs as a thin wait()-ing
        // wrapper around bwrap. Killing unshare directly orphans bwrap (and
        // its sandboxed sleep) to PID 1, which doesn't reap, leaving zombies.
        // Instead: SIGKILL bwrap, let unshare reap it and exit naturally,
        // then wait() reaps unshare. bwrap's --die-with-parent cascades
        // SIGKILL to the sandboxed sleep on the way down.
        let children = read_children(self.child.id());
        for c in &children {
            unsafe { libc::kill(*c as i32, libc::SIGKILL) };
        }
        if children.is_empty() {
            // Fallback: nothing visible under unshare yet, kill it directly.
            let _ = self.child.kill();
        }
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.project_dir);
        let _ = fs::remove_dir_all(&self.data_dir);
    }
}

fn git(args: &[&str], dir: &Path) {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

fn register_running_agent(data_dir: &Path, slug: &str, agent_id: &str, pid: u32) {
    let db_path = data_dir.join("coding-cave.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute(
        "INSERT OR REPLACE INTO agents \
         (agent_id, uuid, project, host_pid, cave_pid, started_at, exited_at) \
         VALUES (?1, ?2, ?3, ?4, ?4, ?5, NULL)",
        rusqlite::params![agent_id, uuid::Uuid::new_v4().to_string(), slug, pid, 0.0],
    )
    .unwrap();
}

fn mnt_inode(pid: u32) -> Option<u64> {
    use std::os::unix::fs::MetadataExt;
    fs::metadata(format!("/proc/{pid}/ns/mnt"))
        .ok()
        .map(|m| m.ino())
}

fn read_children(pid: u32) -> Vec<u32> {
    fs::read_to_string(format!("/proc/{pid}/task/{pid}/children"))
        .unwrap_or_default()
        .split_whitespace()
        .filter_map(|s| s.parse().ok())
        .collect()
}

// Independent oracle for "is a descendant in a different mount ns visible
// yet?". Deliberately not imported from `shell::find_inner_pid` (cav has no
// lib target, but more importantly: an oracle that mirrors the SUT can't
// catch a bug where both return None for the same wrong reason).
fn find_descendant_in_other_mnt_ns(outer: u32) -> Option<u32> {
    let outer_ns = mnt_inode(outer)?;
    let mut queue = std::collections::VecDeque::from([outer]);
    while let Some(pid) = queue.pop_front() {
        for child in read_children(pid) {
            if let Some(ns) = mnt_inode(child) {
                if ns != outer_ns {
                    return Some(child);
                }
            }
            queue.push_back(child);
        }
    }
    None
}

fn wait_for_inner_ns(outer: u32, timeout: std::time::Duration) {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if find_descendant_in_other_mnt_ns(outer).is_some() {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
}

#[test]
fn cav_shell_enters_real_subcave() {
    let cave = Subcave::new();

    // Verify the proc-tree walk locates a descendant in a different mount
    // namespace; this is the property cav shell relies on.
    let inner = find_descendant_in_other_mnt_ns(cave.child.id())
        .expect("no descendant of unshare landed in a different mount ns");
    assert_ne!(
        mnt_inode(inner),
        mnt_inode(cave.child.id()),
        "inner pid should be in a different mount ns from cave_pid"
    );

    // Drive cav shell against this subcave. Output goes through nsenter
    // into bwrap's mount ns; pwd should be the project dir bound into the
    // cave (it lives at /projects/<slug> on the host filesystem we expose).
    let out = cave.cav(&[
        "shell",
        &cave.agent_id,
        "--",
        "/bin/bash",
        "-c",
        "pwd; echo HELLO_FROM_CAVE",
    ]);
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success(),
        "cav shell failed.\nstdout: {stdout}\nstderr: {stderr}"
    );
    assert!(
        stdout.contains("HELLO_FROM_CAVE"),
        "missing greeting.\nstdout: {stdout}\nstderr: {stderr}"
    );
    assert!(
        stdout.contains(&format!("/projects/{}", cave.slug)),
        "wrong cwd.\nstdout: {stdout}\nstderr: {stderr}"
    );
}
