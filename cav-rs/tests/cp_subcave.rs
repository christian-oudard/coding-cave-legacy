/// Subcave integration test for `cav cp` against cave-internal absolute paths.
///
/// Spawns a real `unshare --pid --fork -- bwrap --tmpfs /tmp ...` topology so
/// the cave's /tmp is a fresh tmpfs invisible from the host. Drops a marker
/// file inside that tmpfs, registers the cave, and drives `cav cp
/// cave@1:/tmp/<file> <host_dest>` against it.
///
/// Regression: `cp::cave_root_for_slug` previously built `/proc/<cave_pid>/root`
/// directly from the registry's `cave_pid`, which is the outer unshare pid.
/// unshare lives in a clone of the host's mount namespace, so paths under
/// `/proc/<unshare_pid>/root/tmp/...` resolve in unshare's tree — not the
/// bwrap-bind-mounted tmpfs the cave actually sees. The fix walks descendants
/// for one whose mount ns differs (mirrors `cav shell`), which lands in the
/// bwrap'd target and exposes the cave's tmpfs /tmp.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

const CAV: &str = env!("CARGO_BIN_EXE_cav");

const FILE_NAME: &str = "cavfile.txt";
const FILE_CONTENTS: &str = "CONTENTS_FROM_CAVE";

struct Subcave {
    project_dir: PathBuf,
    data_dir: PathBuf,
    agent_id: String,
    child: std::process::Child,
}

impl Subcave {
    fn new() -> Self {
        let pid = std::process::id();
        let slug = format!("cav-cp-test-{pid}");
        let project_dir = PathBuf::from(format!("/projects/{slug}"));
        let data_dir = std::env::temp_dir().join(format!("cav-cp-data-{pid}"));

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

        // bwrap mounts a fresh tmpfs at /tmp inside its mount ns; the host's
        // /tmp keeps its own contents and never sees this file. The bash
        // command writes the marker before exec-ing sleep so the tmpfs has
        // observable content the moment the inner ns is reachable.
        // The contents/filename are static and shell-safe, so no quoting
        // gymnastics needed.
        let bash_script = format!(
            "printf '%s' '{FILE_CONTENTS}' > /tmp/{FILE_NAME}; exec sleep 30",
        );
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
                "--tmpfs",
                "/tmp",
                "--unshare-pid",
                "/bin/bash",
                "-c",
                &bash_script,
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn unshare+bwrap");

        // Wait for both: a descendant with a different mnt ns AND the marker
        // file visible through that descendant's /proc/<pid>/root. This is
        // an oracle independent of the SUT (cav cp's own resolution) so the
        // test fails for the right reason if cp uses the wrong pid.
        let agent_id = "1".to_string();
        register_running_agent(&data_dir, &slug, &agent_id, child.id());

        wait_for_cave_file(child.id(), FILE_NAME, std::time::Duration::from_secs(5));

        Subcave {
            project_dir,
            data_dir,
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
        // SIGKILL bwrap so unshare reaps it cleanly; --die-with-parent
        // cascades into the sandboxed sleep. Killing unshare directly would
        // orphan bwrap to PID 1 and leave zombies.
        let children = read_children(self.child.id());
        for c in &children {
            unsafe { libc::kill(*c as i32, libc::SIGKILL) };
        }
        if children.is_empty() {
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

// Independent of `proc_ns::find_inner_pid`: an oracle that mirrors the SUT
// can't catch a bug where both return the same wrong pid.
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

fn wait_for_cave_file(outer: u32, file_name: &str, timeout: std::time::Duration) {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if let Some(inner) = find_descendant_in_other_mnt_ns(outer) {
            let path = format!("/proc/{inner}/root/tmp/{file_name}");
            if std::path::Path::new(&path).exists() {
                return;
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    panic!("cave file /tmp/{file_name} did not appear within {timeout:?}");
}

#[test]
fn cav_cp_reads_cave_internal_tmp_file() {
    let cave = Subcave::new();

    // Sanity-check the oracle: outer pid sees host /tmp; the inner descendant
    // sees the cave's tmpfs. The marker exists only via the inner pid, so the
    // bug (using outer pid) cannot accidentally pass.
    let inner = find_descendant_in_other_mnt_ns(cave.child.id())
        .expect("no descendant of unshare landed in a different mount ns");
    assert!(
        std::path::Path::new(&format!("/proc/{inner}/root/tmp/{FILE_NAME}")).exists(),
        "marker file must be visible through the inner pid's mount ns",
    );
    assert!(
        !std::path::Path::new(&format!("/proc/{}/root/tmp/{FILE_NAME}", cave.child.id())).exists(),
        "marker file must NOT be visible through the outer (unshare) pid's mount ns; \
         if it is, the test isn't exercising the bug",
    );

    let host_dest = cave.data_dir.join("copied.txt");
    let dest_str = host_dest.to_str().unwrap();
    let src_spec = format!("cave@{}:/tmp/{FILE_NAME}", cave.agent_id);
    let out = cave.cav(&["cp", &src_spec, dest_str]);
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success(),
        "cav cp failed.\nstdout: {stdout}\nstderr: {stderr}"
    );
    let copied = fs::read_to_string(&host_dest).unwrap_or_else(|e| {
        panic!("cav cp claimed success but {dest_str} is unreadable: {e}\nstderr: {stderr}")
    });
    assert_eq!(copied, FILE_CONTENTS, "destination contents mismatch");
}
