/// Regression test for the bwrap-namespace bug: cav agent invoked
/// repeatedly inside the same parent-cave session must succeed every
/// time, not just the first.
///
/// We don't need claude to actually talk to anthropic for this. The bug
/// fires during bwrap's namespace setup, before claude is even exec'd.
/// The test checks cav's verbose log for the post-namespace milestone
/// `[cav] slirp4netns ready, agent running` — if cav reaches that line,
/// the bug we care about isn't firing. Two consecutive invocations are
/// the minimum to catch the bug, since pre-fix the first run worked
/// coincidentally and only the second onward broke.
///
/// Requires being inside a coding-cave (CODING_CAVE_VERSION set) and
/// having the wrapped binary at ./result/bin/cav (`nix build` first).
/// Outside those preconditions the test panics rather than silently
/// pretending to pass.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// Cargo's CARGO_BIN_EXE_cav points at target/debug/cav, the raw rustc
/// output. The nix wrapper at result/bin/cav prepends crinj/util-linux/
/// iptables/slirp4netns/socat onto PATH, which cav agent shells out to.
/// Use the wrapper exclusively; the bare cargo binary doesn't have what
/// it needs to actually launch a sandbox.
fn cav_binary() -> PathBuf {
    let workspace_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let wrapped = workspace_root.join("result/bin/cav");
    assert!(
        wrapped.exists(),
        "{} not found. Run `nix build` first; this test depends on the \
         wrapped binary that prepends crinj/slirp4netns/iptables to PATH.",
        wrapped.display()
    );
    wrapped
}

const READY_MARKER: &str = "slirp4netns ready, agent running";

struct Project {
    dir: PathBuf,
    data_dir: PathBuf,
}

impl Project {
    /// `name` keeps fixtures distinct: tests in a suite share a process, so
    /// keying the scratch dirs on the pid alone makes them collide.
    fn new(name: &str) -> Self {
        let uniq = format!("{}-{name}", std::process::id());
        let dir = std::env::temp_dir().join(format!("cav-subcave-{uniq}"));
        let data_dir = std::env::temp_dir().join(format!("cav-subcave-data-{uniq}"));
        let _ = fs::remove_dir_all(&dir);
        let _ = fs::remove_dir_all(&data_dir);
        fs::create_dir_all(&dir).unwrap();
        fs::create_dir_all(&data_dir).unwrap();

        git(&["init", "-b", "main"], &dir);
        git(&["config", "user.email", "test@test.com"], &dir);
        git(&["config", "user.name", "Test"], &dir);
        fs::write(dir.join("README.md"), "# subcave test\n").unwrap();
        git(&["add", "README.md"], &dir);
        git(&["commit", "-m", "init"], &dir);

        // Inside a cave, `cav init` requires .coding-cave/host-cave-shared.git
        // to already exist (production: a bind mount from the parent cave).
        // The test stages it as an empty bare repo to satisfy the check;
        // real cav init then creates cave-shared.git alongside as it would
        // in production.
        let host_shared = dir.join(".coding-cave/host-cave-shared.git");
        fs::create_dir_all(host_shared.parent().unwrap()).unwrap();
        git(&["init", "--bare", &host_shared.to_string_lossy()], &dir);

        let p = Self { dir, data_dir };
        let out = p.cav(&["init"]);
        assert!(
            out.status.success(),
            "cav init failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        p
    }

    fn cav(&self, args: &[&str]) -> Output {
        // This suite runs inside a parent cave (the test asserts
        // CODING_CAVE_VERSION is set) and exercises `cav agent` to spawn a
        // subcave, so the nesting gate would otherwise refuse.
        Command::new(cav_binary())
            .args(args)
            .current_dir(&self.dir)
            .env("CODING_CAVE_DATA_DIR", &self.data_dir)
            .env("CODING_CAVE_NIX_STORES_DIR", self.data_dir.join("nix-stores"))
            .env("CODING_CAVE_NESTED", "1")
            .output()
            .expect("cav binary not found")
    }
}

impl Drop for Project {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
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

#[test]
fn cav_agent_launches_inside_a_cave_repeatedly() {
    assert!(
        std::env::var("CODING_CAVE_VERSION").is_ok(),
        "this test requires being inside a coding-cave (CODING_CAVE_VERSION \
         must be set). Run via `cav agent` on the host first."
    );

    let project = Project::new("relaunch");

    // Two consecutive invocations. With the bwrap /proc bug present,
    // run #1 used to succeed by accident (some short-lived process
    // happened to occupy cave-pid 3 at the moment bwrap read /proc/3/ns)
    // and run #2 always died with `bwrap: open /proc/3/ns failed: No
    // such file or directory`. After the --mount-proc fix, /proc inside
    // supervisor reflects supervisor's pid namespace and bwrap reads
    // the correct ns IDs; both runs should reach the agent-running
    // milestone.
    //
    // We pass --prompt with a placeholder. Whatever claude does after
    // launch (likely fails, since we don't expose any anthropic
    // endpoint) doesn't matter for this test — the regression is in
    // the pre-claude namespace setup, and READY_MARKER is logged just
    // before claude is exec'd.
    for run in 1..=2 {
        let out = project.cav(&["-v", "agent", "--prompt", "ping"]);
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(
            stderr.contains(READY_MARKER),
            "run {run} did not reach `{READY_MARKER}` — namespace setup \
             likely failed.\nstderr:\n{stderr}"
        );
    }
}

/// A real cave launch must materialize the per-agent Claude directory, the
/// permanent session store, and the project's shared memory pool, and bwrap
/// must accept every bind. A malformed or missing bind source kills bwrap
/// before READY_MARKER, so reaching it is itself an assertion that the mount
/// layout is sound.
#[test]
fn launch_materializes_the_session_and_memory_layout() {
    assert!(
        std::env::var("CODING_CAVE_VERSION").is_ok(),
        "this test requires being inside a coding-cave"
    );
    let project = Project::new("layout");
    let out = project.cav(&["-v", "agent", "--prompt", "ping"]);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains(READY_MARKER),
        "launch did not reach `{READY_MARKER}`; a bind source is likely \
         missing.\nstderr:\n{stderr}"
    );

    let data = &project.data_dir;
    assert!(
        data.join("sessions").is_dir(),
        "permanent session store was not created"
    );
    let slug = project.dir.file_name().unwrap().to_str().unwrap();
    assert!(
        data.join("memory").join(slug).is_dir(),
        "shared memory pool for the project was not created"
    );
    let agents = data.join("claude/agents");
    let any_agent = fs::read_dir(&agents)
        .map(|d| d.filter_map(|e| e.ok()).any(|e| e.path().is_dir()))
        .unwrap_or(false);
    assert!(any_agent, "no per-agent Claude directory under {agents:?}");
}
