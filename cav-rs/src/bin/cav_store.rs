// cav-store: the host-side root daemon behind coding-cave-host.service.
// Verbs: `cav-store setup | teardown | watch`. A standalone binary rather
// than a `cav` subcommand, so the root systemd process links only this
// daemon and not the user-facing CLI. The unit definitions stay thin shims
// calling these verbs; logic lives in Rust and updates ride the binary
// instead of unit-file rewrites.

use anyhow::{bail, Context, Result};
use rusqlite::Connection;
use rustix::fd::{AsFd, OwnedFd};
use rustix::fs::inotify::{self, CreateFlags, WatchFlags};
use rustix::io::Errno;
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::io::Read;
use std::mem::MaybeUninit;
use std::os::unix::fs::lchown;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const STATE_DIR: &str = "/var/lib/coding-cave";
const IDMAP_TARGET: &str = "/var/lib/coding-cave/nix-store";
const IDMAP_OPTS: &str = "X-mount.idmap=u:0:1000:1 g:0:1000:1";

// The cave user's home. uid 1000 by convention (see IDMAP_OPTS above).
const CAVE_USER_HOME: &str = "/home/cave-coder";
const HOST_STORE: &str = "/nix/store";

fn main() {
    if let Err(e) = run() {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    match std::env::args().nth(1).as_deref() {
        Some("setup") => setup(),
        Some("teardown") => teardown(),
        Some("watch") => watch(),
        other => bail!("usage: cav-store <setup|teardown|watch> (got {other:?})"),
    }
}

fn setup() -> Result<()> {
    if !rustix::process::geteuid().is_root() {
        bail!("cav-store setup must be run as root");
    }
    std::fs::create_dir_all(STATE_DIR).context("create /var/lib/coding-cave")?;
    if !Path::new(IDMAP_TARGET).exists() {
        std::fs::create_dir(IDMAP_TARGET).context("create idmap target")?;
    }
    // Idempotent: the watch ExecStart can crash and trigger a restart, which
    // re-runs ExecStartPre. If the mount is still active from the previous
    // invocation, leave it in place — re-mounting would fail and cascade
    // into a restart loop.
    if is_mount_point(IDMAP_TARGET) {
        return Ok(());
    }
    let status = Command::new("mount")
        .args(["--bind", "--read-only", "-o", IDMAP_OPTS, "/nix/store", IDMAP_TARGET])
        .status()
        .context("invoke mount")?;
    if !status.success() {
        bail!("mount failed: {status}");
    }
    Ok(())
}

fn is_mount_point(path: &str) -> bool {
    // Field 5 of each /proc/self/mountinfo line is the mount point.
    let Ok(data) = std::fs::read_to_string("/proc/self/mountinfo") else {
        return false;
    };
    data.lines().any(|l| l.split(' ').nth(4) == Some(path))
}

fn teardown() -> Result<()> {
    if !rustix::process::geteuid().is_root() {
        bail!("cav-store teardown must be run as root");
    }
    let status = Command::new("umount")
        .args(["--lazy", IDMAP_TARGET])
        .status()
        .context("invoke umount")?;
    if !status.success() {
        bail!("umount failed: {status}");
    }
    Ok(())
}

/// Watch each agent's nix DB; promote newly-installed paths into the
/// host /nix/store so siblings inherit them via the overlay lower.
/// Long-running; the ExecStart of coding-cave-host.service.
fn watch() -> Result<()> {
    if !test_mode() && !rustix::process::geteuid().is_root() {
        bail!("cav-store watch must be run as root");
    }
    let overlay_base = overlay_base_from_env();
    std::fs::create_dir_all(&overlay_base).context("create nix-overlay base")?;

    let ifd = inotify::init(CreateFlags::empty()).context("inotify init")?;
    let base_wd = inotify::add_watch(&ifd, &overlay_base, WatchFlags::CREATE)
        .context("watch overlay base")?;

    let mut w = Watcher {
        ifd,
        overlay_base,
        base_wd,
        active: HashMap::new(),
        pending: HashMap::new(),
        tracked: HashSet::new(),
    };

    // Adopt any agents that already exist. Picks up installs that happened
    // while the watcher was down by way of advance_tracking's initial scan.
    if let Ok(entries) = std::fs::read_dir(&w.overlay_base) {
        for entry in entries.flatten() {
            if let Some(id) = agent_id_from_name(&entry.file_name()) {
                w.advance_tracking(&id, None);
            }
        }
    }

    let mut buf = [MaybeUninit::<u8>::uninit(); 8192];
    loop {
        let mut reader = inotify::Reader::new(w.ifd.as_fd(), &mut buf);
        let ev = match reader.next() {
            Ok(ev) => ev,
            Err(Errno::INTR) => continue,
            Err(e) => bail!("inotify read: {e}"),
        };
        let wd = ev.wd();
        if wd == w.base_wd {
            if let Some(name) = ev.file_name() {
                if let Ok(s) = std::str::from_utf8(name.to_bytes()) {
                    if let Some(id) = agent_id_from_name(OsStr::new(s)) {
                        w.advance_tracking(&id, None);
                    }
                }
            }
        } else if let Some(id) = w.pending.get(&wd).cloned() {
            // Something appeared in a parent dir we were waiting on. Either
            // db.sqlite is now in place (promote to active), or we move the
            // pending watch one level deeper.
            w.advance_tracking(&id, Some(wd));
        } else if w.active.contains_key(&wd) {
            if let Some(state) = w.active.get_mut(&wd) {
                let id = format!("agent-{}", state.agent_id);
                if let Err(e) = scan_and_promote(state) {
                    eprintln!("cav-store watch: {id} promotion: {e:#}");
                }
            }
        }
    }
}

struct Watcher {
    ifd: OwnedFd,
    overlay_base: PathBuf,
    base_wd: i32,
    /// wd -> AgentState; the watch is on `agent-<id>/db/` and fires
    /// MODIFY/CLOSE_WRITE for every nix DB write.
    active: HashMap<i32, AgentState>,
    /// wd -> agent_id; the watch is on `agent-<id>/` or `agent-<id>/db/`
    /// waiting for db.sqlite to materialize. Promoted to `active` on the
    /// next event that lets us attach the real watch.
    pending: HashMap<i32, String>,
    /// All agent_ids currently in `active` or `pending`. Lets the base-wd
    /// CREATE handler skip agents we're already tracking.
    tracked: HashSet<String>,
}

impl Watcher {
    /// Drive an agent toward the active state. If a pending watch was
    /// already in place for this agent, `current_wd` is its wd; that watch
    /// gets retired before we attach the next one (no inotify watch leaks).
    ///
    /// The loop closes the small race where db.sqlite is created between
    /// our "does it exist?" check and the add_watch on its parent dir —
    /// after attaching, we re-check; if the deeper level is now ready, we
    /// drop this pending watch and try again.
    fn advance_tracking(&mut self, agent_id: &str, current_wd: Option<i32>) {
        if let Some(wd) = current_wd {
            self.pending.remove(&wd);
            let _ = inotify::remove_watch(&self.ifd, wd);
        } else if self.tracked.contains(agent_id) {
            // Already tracked from a prior CREATE event.
            return;
        }
        self.tracked.insert(agent_id.to_string());

        loop {
            // Ready: attach the db-dir watch and run the initial scan.
            if let Some(mut state) = AgentState::new(&self.overlay_base, agent_id) {
                match inotify::add_watch(
                    &self.ifd,
                    &state.db_dir,
                    WatchFlags::MODIFY | WatchFlags::CLOSE_WRITE,
                ) {
                    Ok(wd) => {
                        if let Err(e) = scan_and_promote(&mut state) {
                            eprintln!(
                                "cav-store watch: agent-{agent_id} initial scan: {e:#}"
                            );
                        }
                        self.active.insert(wd, state);
                    }
                    Err(e) => {
                        eprintln!(
                            "cav-store watch: add_watch {}: {e}",
                            state.db_dir.display()
                        );
                        self.tracked.remove(agent_id);
                    }
                }
                return;
            }

            // Not ready: attach a watch on the deepest existing parent of
            // db.sqlite so we wake up when the next piece materializes.
            let agent_dir = self.overlay_base.join(format!("agent-{agent_id}"));
            let db_dir = agent_dir.join("db");
            let target = if db_dir.exists() {
                db_dir
            } else if agent_dir.exists() {
                agent_dir
            } else {
                // The agent dir vanished (created then deleted). Drop tracking.
                self.tracked.remove(agent_id);
                return;
            };
            match inotify::add_watch(
                &self.ifd,
                &target,
                WatchFlags::CREATE | WatchFlags::MOVED_TO,
            ) {
                Ok(wd) => {
                    // Close the check-vs-attach race: if db.sqlite materialized
                    // between our exists() and add_watch, retry with the new
                    // watch retired.
                    if AgentState::new(&self.overlay_base, agent_id).is_some() {
                        let _ = inotify::remove_watch(&self.ifd, wd);
                        continue;
                    }
                    self.pending.insert(wd, agent_id.to_string());
                    return;
                }
                Err(e) => {
                    eprintln!(
                        "cav-store watch: add_watch {}: {e}",
                        target.display()
                    );
                    self.tracked.remove(agent_id);
                    return;
                }
            }
        }
    }
}

fn overlay_base_from_env() -> PathBuf {
    let home = std::env::var("CODING_CAVE_HOST_WATCH_HOME")
        .unwrap_or_else(|_| CAVE_USER_HOME.to_string());
    PathBuf::from(home)
        .join(".local")
        .join("share")
        .join("coding-cave")
        .join("nix-overlay")
}

fn agent_id_from_name(name: &OsStr) -> Option<String> {
    name.to_str()?.strip_prefix("agent-").map(|s| s.to_string())
}

struct AgentState {
    agent_id: String,
    upper_dir: PathBuf,
    db_path: PathBuf,
    db_dir: PathBuf,
    /// Highest `ValidPaths.id` we've processed for this agent. The next
    /// scan reads `WHERE id > last_seen_id`. `ValidPaths.id` is
    /// `INTEGER PRIMARY KEY AUTOINCREMENT` so values are monotonic and
    /// never reused.
    last_seen_id: i64,
}

impl AgentState {
    fn new(overlay_base: &Path, agent_id: &str) -> Option<Self> {
        let agent_dir = overlay_base.join(format!("agent-{agent_id}"));
        let db_dir = agent_dir.join("db");
        let db_path = db_dir.join("db.sqlite");
        if !db_path.exists() {
            return None;
        }
        Some(Self {
            agent_id: agent_id.to_string(),
            upper_dir: agent_dir.join("upper"),
            db_path,
            db_dir,
            last_seen_id: 0,
        })
    }
}

fn scan_and_promote(state: &mut AgentState) -> Result<()> {
    let conn = Connection::open_with_flags(
        &state.db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .context("open agent db")?;

    let rows: Vec<(i64, String)> = match conn
        .prepare("SELECT id, path FROM ValidPaths WHERE id > ?1 ORDER BY id")
    {
        Ok(mut stmt) => {
            let rows = stmt.query_map([state.last_seen_id], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
            })?;
            rows.filter_map(|r| r.ok()).collect()
        }
        // Race: db.sqlite exists but the schema hasn't been written yet
        // (agent setup mid-flight). The next DB write will retry.
        Err(rusqlite::Error::SqliteFailure(_, Some(ref msg))) if msg.contains("no such table") => {
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };
    drop(conn);

    if rows.is_empty() {
        return Ok(());
    }

    let store_target = host_store_dir();
    let mut to_promote = Vec::new();
    for (_id, path) in &rows {
        let Some(name) = Path::new(path).file_name() else {
            continue;
        };
        // Already on host (prior promotion, or originally from host store).
        if store_target.join(name).exists() {
            continue;
        }
        // In the agent's DB via the seed-from-parent, not installed on
        // this layer. Nothing to promote.
        if !state.upper_dir.join(name).exists() {
            continue;
        }
        to_promote.push((path.clone(), name.to_os_string()));
    }

    let testing = test_mode();
    // Rename upper -> host store, then register the closure in one nix-store
    // round-trip so dependencies resolve. Rename first because nix-store
    // --dump-db verifies path presence under NIX_STORE_DIR.
    for (_, name) in &to_promote {
        let src = state.upper_dir.join(name);
        let dst = store_target.join(name);
        std::fs::rename(&src, &dst)
            .with_context(|| format!("rename {src:?} -> {dst:?}"))?;
        if !testing {
            chown_tree_to_root(&dst)?;
        }
    }
    if !to_promote.is_empty() && !testing {
        let paths: Vec<String> = to_promote.iter().map(|(p, _)| p.clone()).collect();
        register_paths_in_host_db(&state.db_path, &paths)?;
    }

    // Advance the cursor only after everything in this batch succeeded.
    // On error we return early without updating last_seen_id; the next
    // event re-reads the same range and the exists() filters cheaply
    // skip whatever already made it across.
    state.last_seen_id = rows.last().map(|(id, _)| *id).unwrap_or(state.last_seen_id);
    Ok(())
}

fn host_store_dir() -> PathBuf {
    // Override only for tests; production unconditionally uses /nix/store.
    std::env::var("CODING_CAVE_HOST_WATCH_STORE")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(HOST_STORE))
}

/// Test-only escape hatch: skips the root check, the recursive chown,
/// and the nix-store --load-db invocation. Lets the integration test
/// exercise the inotify/diff/rename pipeline without CAP_CHOWN or a
/// real nix DB. Production never sets this.
fn test_mode() -> bool {
    std::env::var_os("CODING_CAVE_HOST_WATCH_TEST_MODE").is_some()
}

fn chown_tree_to_root(path: &Path) -> Result<()> {
    fn walk(p: &Path) -> std::io::Result<()> {
        lchown(p, Some(0), Some(0))?;
        let meta = p.symlink_metadata()?;
        if meta.is_dir() && !meta.file_type().is_symlink() {
            for entry in std::fs::read_dir(p)? {
                walk(&entry?.path())?;
            }
        }
        Ok(())
    }
    walk(path).with_context(|| format!("chown {path:?}"))?;
    Ok(())
}

/// Ship the closure metadata from the agent's DB into the host DB by
/// piping nix-store --dump-db (pointed at the agent's DB) into
/// nix-store --load-db (default host DB). This keeps schema knowledge
/// inside nix, not our INSERTs.
fn register_paths_in_host_db(agent_db: &Path, paths: &[String]) -> Result<()> {
    let tmp = mkstate_for(agent_db)?;
    let dump_result = (|| -> Result<()> {
        let mut dump = Command::new("nix-store")
            .arg("--dump-db")
            .args(paths)
            .env("NIX_REMOTE", "")
            .env("NIX_STATE_DIR", &tmp)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("spawn nix-store --dump-db")?;
        let dump_stdout = dump.stdout.take().expect("piped stdout");

        let mut load = Command::new("nix-store")
            .arg("--load-db")
            .stdin(Stdio::from(dump_stdout))
            .stderr(Stdio::piped())
            .spawn()
            .context("spawn nix-store --load-db")?;

        let load_status = load.wait().context("wait load-db")?;
        let dump_status = dump.wait().context("wait dump-db")?;

        if !dump_status.success() {
            let mut err = String::new();
            if let Some(mut e) = dump.stderr {
                let _ = e.read_to_string(&mut err);
            }
            bail!("nix-store --dump-db failed: {err}");
        }
        if !load_status.success() {
            let mut err = String::new();
            if let Some(mut e) = load.stderr {
                let _ = e.read_to_string(&mut err);
            }
            bail!("nix-store --load-db failed: {err}");
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&tmp);
    dump_result
}

/// Build a NIX_STATE_DIR tree whose db.sqlite is a snapshot of the
/// agent's DB. A copy (not a symlink) so concurrent writes from the
/// cave can't race the dump.
fn mkstate_for(agent_db: &Path) -> Result<PathBuf> {
    let base = Path::new(STATE_DIR).join("promote-tmp");
    std::fs::create_dir_all(&base).context("create promote-tmp")?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let dir = base.join(format!("p-{}-{}", std::process::id(), nanos));
    let db_target_dir = dir.join("nix").join("db");
    std::fs::create_dir_all(&db_target_dir).context("create promote tmp dirs")?;
    std::fs::copy(agent_db, db_target_dir.join("db.sqlite")).context("snapshot agent db")?;
    Ok(dir)
}
