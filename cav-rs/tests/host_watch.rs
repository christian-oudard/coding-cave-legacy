/// Integration test for the auto-promote watcher.
///
/// Exercises `cav-store watch` against a fake nix-overlay tree and store.
/// The watcher's inotify-driven scan/rename loop is what we want covered;
/// the chown and `nix-store --load-db` invocations would need CAP_CHOWN
/// and a real nix DB, so we short-circuit them via
/// CODING_CAVE_HOST_WATCH_TEST_MODE and verify the rename behavior.
///
/// Production never sets that env var. A separate manual smoke test
/// (see docs/nix-store.md) covers the full chown + register path.
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
    home: PathBuf,
    store: PathBuf,
}

impl Fixture {
    fn new(name: &str) -> Self {
        let uniq = format!("{}-{}", std::process::id(), name);
        let root = std::env::temp_dir().join(format!("cav-host-watch-{uniq}"));
        let _ = fs::remove_dir_all(&root);
        let home = root.join("home");
        let store = root.join("store");
        fs::create_dir_all(&home).unwrap();
        fs::create_dir_all(&store).unwrap();
        Self { root, home, store }
    }

    fn agent(&self, id: &str) -> AgentFiles {
        let base = self
            .home
            .join(".local/share/coding-cave/nix-overlay")
            .join(format!("agent-{id}"));
        let upper = base.join("upper");
        let db_dir = base.join("db");
        fs::create_dir_all(&upper).unwrap();
        fs::create_dir_all(&db_dir).unwrap();
        let db = db_dir.join("db.sqlite");
        init_nix_db(&db);
        AgentFiles { upper, db }
    }

    fn watcher(&self) -> Watcher {
        let child = Command::new(CAV_STORE)
            .args(["watch"])
            .env("CODING_CAVE_HOST_WATCH_HOME", &self.home)
            .env("CODING_CAVE_HOST_WATCH_STORE", &self.store)
            .env("CODING_CAVE_HOST_WATCH_TEST_MODE", "1")
            .env_remove("CODING_CAVE_VERSION")
            .env_remove("CODING_CAVE_AGENT_ID")
            .spawn()
            .expect("spawn cav-store watch");
        // Give the watcher a moment to set up inotify before the first
        // simulated install. Without this the install can land between
        // discovery and add_watch and slip through until the next event.
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
    upper: PathBuf,
    db: PathBuf,
}

impl AgentFiles {
    fn fake_install(&self, basename: &str) {
        let dir = self.upper.join(basename);
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
        );",
    )
    .unwrap();
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
fn promotes_new_install_into_host_store() {
    let fx = Fixture::new("simple");
    let agent = fx.agent("1");
    let _watcher = fx.watcher();

    let pkg = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-fake-pkg";
    agent.fake_install(pkg);

    let promoted = fx.store.join(pkg);
    let stayed_in_upper = agent.upper.join(pkg);
    assert!(
        wait_for(Duration::from_secs(5), || promoted.exists() && !stayed_in_upper.exists()),
        "watcher did not promote `{pkg}` within 5s. promoted={} upper={}",
        promoted.exists(),
        stayed_in_upper.exists()
    );
}

#[test]
fn promotes_agent_created_after_watcher_started() {
    let fx = Fixture::new("late-agent");
    let _watcher = fx.watcher();

    // The watcher discovers this agent via the CREATE event on the
    // overlay-base watch, then attaches a DB watch in time for the
    // first install on it.
    let agent = fx.agent("2");
    // Inotify event ordering: the CREATE for agent-2/ fires before the
    // DB-file write that fake_install does. The watcher must have
    // already added the db-dir watch by then. The base-dir CREATE
    // event is for `agent-2`, and we create the db file underneath
    // it on a separate filesystem operation, so the ordering is fine
    // in practice — but allow a brief settle for the watcher to
    // process the CREATE.
    thread::sleep(Duration::from_millis(150));
    let pkg = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-late-pkg";
    agent.fake_install(pkg);

    let promoted = fx.store.join(pkg);
    assert!(
        wait_for(Duration::from_secs(5), || promoted.exists()),
        "watcher did not promote late-agent install within 5s"
    );
}

#[test]
fn promotes_preexisting_install_on_watcher_startup() {
    // Models a host reboot or systemd restart: the agent's DB already
    // has rows + the upper still has the files when the watcher first
    // starts. Hits scan_and_promote with last_seen_id = 0 against a
    // pre-populated DB, which event-driven tests don't cover.
    let fx = Fixture::new("preexisting");
    let agent = fx.agent("4");
    let pkg = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-preexisting-pkg";
    agent.fake_install(pkg);

    let _watcher = fx.watcher();

    let promoted = fx.store.join(pkg);
    assert!(
        wait_for(Duration::from_secs(5), || promoted.exists()),
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
        wait_for(Duration::from_secs(5), || fx.store.join(first).exists()),
        "first install not promoted"
    );

    agent.fake_install(second);
    assert!(
        wait_for(Duration::from_secs(5), || fx.store.join(second).exists()),
        "second install not promoted (state tracking broken?)"
    );
    // First install must still be in the store, not back in upper.
    assert!(fx.store.join(first).exists());
    assert!(!agent.upper.join(first).exists());
}
