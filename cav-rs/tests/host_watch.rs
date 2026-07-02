/// Integration test for the store manager's converge loop.
///
/// Exercises `cav-store watch` against fake per-cave stores and a shared
/// store. The timer-driven convergence pass is what we cover; the real
/// `nix copy` / verify / promote need a real nix store, so we short-circuit
/// those via CODING_CAVE_HOST_WATCH_TEST_MODE, which stubs them with a tree
/// copy. The hardlink collapse still runs for real, so the dedup the loop
/// exists to perform is under test. Production never sets that env var.
use rusqlite::Connection;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::thread;
use std::time::{Duration, Instant};

const CAV_STORE: &str = env!("CARGO_BIN_EXE_cav-store");

struct Watcher {
    child: Child,
}

impl Drop for Watcher {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct Fixture {
    root: PathBuf,
    /// Per-cave nix-store base the manager watches for `agent-*` chroot stores.
    nix_stores: PathBuf,
    /// Shared store root (a chroot store: paths live under `<shared>/nix/store`).
    shared: PathBuf,
}

impl Fixture {
    fn new(name: &str) -> Self {
        let uniq = format!("{}-{}", std::process::id(), name);
        let root = std::env::temp_dir().join(format!("cav-host-watch-{uniq}"));
        let _ = fs::remove_dir_all(&root);
        let nix_stores = root.join("nix-stores");
        let shared = root.join("shared");
        fs::create_dir_all(&nix_stores).unwrap();
        fs::create_dir_all(&shared).unwrap();
        Self { root, nix_stores, shared }
    }

    /// Where a promoted path lands in the shared store.
    fn shared_path(&self, basename: &str) -> PathBuf {
        self.shared.join("nix").join("store").join(basename)
    }

    fn agent(&self, id: &str) -> AgentFiles {
        let base = self.nix_stores.join(format!("agent-{id}"));
        let store_dir = base.join("nix/store");
        let db_dir = base.join("nix/var/nix/db");
        fs::create_dir_all(&store_dir).unwrap();
        fs::create_dir_all(&db_dir).unwrap();
        let db = db_dir.join("db.sqlite");
        init_nix_db(&db);
        AgentFiles { store_dir, db }
    }

    fn watcher(&self) -> Watcher {
        let mut cmd = Command::new(CAV_STORE);
        cmd.args(["watch"])
            .env("CODING_CAVE_NIX_STORES_DIR", &self.nix_stores)
            .env("CODING_CAVE_SHARED_STORE", &self.shared)
            // Point the host store at paths that don't exist, so convergence is
            // hermetic (no host_has ever true) and never reads the real host DB.
            .env("CODING_CAVE_HOST_NIX_STORE", self.root.join("no-host-store"))
            .env("CODING_CAVE_HOST_NIX_DB", self.root.join("no-host-db.sqlite"))
            .env("CODING_CAVE_HOST_WATCH_TEST_MODE", "1")
            .env_remove("CODING_CAVE_VERSION")
            .env_remove("CODING_CAVE_AGENT_ID");
        let child = cmd.spawn().expect("spawn cav-store watch");
        // Let the first converge pass run before the test mutates the stores.
        thread::sleep(Duration::from_millis(300));
        Watcher { child }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

struct AgentFiles {
    /// The cave's `<root>/nix/store` where its own builds land.
    store_dir: PathBuf,
    db: PathBuf,
}

impl AgentFiles {
    fn fake_install(&self, basename: &str) {
        let dir = self.store_dir.join(basename);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("content"), b"hi").unwrap();
        let conn = Connection::open(&self.db).unwrap();
        conn.execute(
            "INSERT INTO ValidPaths (path, hash, registrationTime, deriver, narSize, ultimate) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                format!("/nix/store/{basename}"),
                "sha256:0000000000000000000000000000000000000000000000000000000000000000",
                0_i64,
                "",
                7_i64,
                1_i64,
            ],
        )
        .unwrap();
    }
}

fn init_nix_db(path: &Path) {
    let conn = Connection::open(path).unwrap();
    // Schema slice the watcher actually reads (SELECT path FROM ValidPaths).
    // The full nix schema has Refs/DerivationOutputs/etc; not needed here.
    conn.execute_batch(
        "CREATE TABLE ValidPaths (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            hash TEXT NOT NULL,
            registrationTime INTEGER NOT NULL,
            deriver TEXT,
            narSize INTEGER,
            ultimate INTEGER DEFAULT 0,
            sigs TEXT,
            ca TEXT
        );
        CREATE TABLE Refs (
            referrer INTEGER NOT NULL,
            reference INTEGER NOT NULL
        );",
    )
    .unwrap();
}

fn ino(p: &Path) -> Option<u64> {
    use std::os::unix::fs::MetadataExt;
    fs::metadata(p).ok().map(|m| m.ino())
}

fn wait_for<F: Fn() -> bool>(timeout: Duration, predicate: F) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if predicate() {
            return true;
        }
        thread::sleep(Duration::from_millis(50));
    }
    predicate()
}

#[test]
fn promotes_new_install_into_shared_store() {
    let fx = Fixture::new("simple");
    let agent = fx.agent("1");
    let _watcher = fx.watcher();

    let pkg = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-fake-pkg";
    agent.fake_install(pkg);

    let promoted = fx.shared_path(pkg);
    let cave_file = agent.store_dir.join(pkg).join("content");
    let shared_file = promoted.join("content");
    assert!(
        wait_for(Duration::from_secs(5), || promoted.exists()),
        "watcher did not promote `{pkg}` into the shared store within 5s",
    );
    // The cave keeps the path, but convergence collapses its copy onto the
    // shared inode: one copy on disk, shared by cave and shared store.
    assert!(
        wait_for(Duration::from_secs(5), || ino(&cave_file).is_some()
            && ino(&cave_file) == ino(&shared_file)),
        "cave copy of `{pkg}` was not collapsed onto the shared inode",
    );
}

#[test]
fn promotes_agent_created_after_watcher_started() {
    let fx = Fixture::new("late-agent");
    let _watcher = fx.watcher();

    // The watcher discovers this agent via the CREATE event on the
    // store-base watch, then attaches a DB watch in time for the first
    // install on it.
    let agent = fx.agent("2");
    thread::sleep(Duration::from_millis(150));
    let pkg = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-late-pkg";
    agent.fake_install(pkg);

    assert!(
        wait_for(Duration::from_secs(5), || fx.shared_path(pkg).exists()),
        "watcher did not promote late-agent install within 5s"
    );
}

#[test]
fn promotes_preexisting_install_on_watcher_startup() {
    // Models a host reboot or systemd restart: the cave's DB already has
    // rows and the cave store still has the files when the watcher first
    // starts. Hits scan_and_promote with last_seen_id = 0 against a
    // pre-populated DB, which event-driven tests don't cover.
    let fx = Fixture::new("preexisting");
    let agent = fx.agent("4");
    let pkg = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-preexisting-pkg";
    agent.fake_install(pkg);

    let _watcher = fx.watcher();

    assert!(
        wait_for(Duration::from_secs(5), || fx.shared_path(pkg).exists()),
        "watcher did not promote pre-existing install on startup"
    );
}

#[test]
fn second_install_after_first_promotion_also_promotes() {
    let fx = Fixture::new("sequence");
    let agent = fx.agent("3");
    let _watcher = fx.watcher();

    let first = "cccccccccccccccccccccccccccccccc-first";
    let second = "dddddddddddddddddddddddddddddddd-second";
    agent.fake_install(first);
    assert!(
        wait_for(Duration::from_secs(5), || fx.shared_path(first).exists()),
        "first install not promoted"
    );

    agent.fake_install(second);
    assert!(
        wait_for(Duration::from_secs(5), || fx.shared_path(second).exists()),
        "second install not promoted (state tracking broken?)"
    );
    // First install must still be in the shared store.
    assert!(fx.shared_path(first).exists());
}
