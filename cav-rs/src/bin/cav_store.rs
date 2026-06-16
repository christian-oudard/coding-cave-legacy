// cav-store: the host-side root daemon behind coding-cave-host.service.
// Verbs: `cav-store setup | teardown | watch`. A standalone binary rather
// than a `cav` subcommand, so the root systemd process links only this
// daemon and not the user-facing CLI. The unit definitions stay thin shims
// calling these verbs; logic lives in Rust and updates ride the binary
// instead of unit-file rewrites.

use anyhow::{bail, Context, Result};
use rusqlite::Connection;

#[path = "../nix_db.rs"]
mod nix_db;
use rustix::fd::{AsFd, OwnedFd};
use rustix::fs::inotify::{self, CreateFlags, WatchFlags};
use rustix::io::Errno;
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
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
const HOST_DB: &str = "/nix/var/nix/db/db.sqlite";

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

    // Down-sync runs on its own inotify fd in a side thread so it never
    // interleaves with the up-sync (promote) loop below. Skip it in test
    // mode unless the test points us at a host DB of its own, so the
    // up-sync integration tests don't reconcile against the real host store.
    if !test_mode() || std::env::var_os("CODING_CAVE_HOST_WATCH_DB").is_some() {
        let base = w.overlay_base.clone();
        std::thread::spawn(move || {
            if let Err(e) = run_down_sync(base) {
                eprintln!("cav-store down-sync thread exited: {e:#}");
            }
        });
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

    if test_mode() {
        // Exercise the diff/rename pipeline without nix-store or chown.
        for (_, name) in &to_promote {
            let src = state.upper_dir.join(name);
            let dst = store_target.join(name);
            std::fs::rename(&src, &dst)
                .with_context(|| format!("rename {src:?} -> {dst:?}"))?;
        }
    } else if !to_promote.is_empty() {
        promote_to_host(state, &store_target, &to_promote)?;
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

fn host_db() -> PathBuf {
    // Override only for tests; production unconditionally uses the host DB.
    std::env::var("CODING_CAVE_HOST_WATCH_DB")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(HOST_DB))
}

/// Watch the parent (host) nix DB and propagate its changes into every
/// running agent's cave DB. The host store keeps changing under live caves
/// (the host's own builds, GC, and other agents' promotions); without this
/// the cave DB drifts and nix trips over on-disk paths it considers
/// unregistered. Debounced so a build's burst of writes collapses into one
/// reconcile pass.
fn run_down_sync(overlay_base: PathBuf) -> Result<()> {
    let host_db = host_db();
    let watch_dir = host_db
        .parent()
        .context("host db path has no parent dir")?
        .to_path_buf();

    let ifd = inotify::init(CreateFlags::empty()).context("down-sync inotify init")?;
    inotify::add_watch(
        &ifd,
        &watch_dir,
        WatchFlags::MODIFY | WatchFlags::CLOSE_WRITE | WatchFlags::MOVED_TO,
    )
    .with_context(|| format!("watch host db dir {}", watch_dir.display()))?;

    // Agents launched before us still hold a launch-time snapshot; bring
    // them current immediately.
    reconcile_all_agents(&overlay_base, &host_db);

    let mut buf = [MaybeUninit::<u8>::uninit(); 4096];
    loop {
        let relevant = {
            let mut reader = inotify::Reader::new(ifd.as_fd(), &mut buf);
            match reader.next() {
                Ok(ev) => ev
                    .file_name()
                    .and_then(|n| n.to_str().ok())
                    .map(is_host_db_write)
                    .unwrap_or(false),
                Err(Errno::INTR) => continue,
                Err(e) => bail!("down-sync inotify read: {e}"),
            }
        };
        // Ignore -shm churn (WAL readers, including our own dump-db, touch it)
        // so reconcile never retriggers itself.
        if !relevant {
            continue;
        }
        // Let the writer's burst settle; queued events coalesce into the
        // next blocking read.
        std::thread::sleep(std::time::Duration::from_millis(200));
        reconcile_all_agents(&overlay_base, &host_db);
    }
}

/// A change to the DB proper (a checkpoint) or its write-ahead log (every
/// commit). The shared-memory index (`-shm`) is touched by readers too, so
/// it is not a write signal.
fn is_host_db_write(name: &str) -> bool {
    name == "db.sqlite" || name == "db.sqlite-wal"
}

fn reconcile_all_agents(overlay_base: &Path, host_db: &Path) {
    let entries = match std::fs::read_dir(overlay_base) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let Some(id) = agent_id_from_name(&entry.file_name()) else {
            continue;
        };
        let Some(state) = AgentState::new(overlay_base, &id) else {
            continue;
        };
        let cave_state_dir = state.db_dir.parent().unwrap_or(&state.db_dir);
        match nix_db::reconcile_cave_db(
            cave_state_dir,
            &state.db_path,
            host_db,
            &state.upper_dir,
        ) {
            Ok(plan) if !plan.adds.is_empty() || !plan.removes.is_empty() => {
                eprintln!(
                    "cav-store down-sync: agent-{id}: +{} -{}",
                    plan.adds.len(),
                    plan.removes.len()
                );
            }
            Ok(_) => {}
            Err(e) => eprintln!("cav-store down-sync: agent-{id}: {e:#}"),
        }
    }
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

/// What to do with one candidate promotion after authenticity checking.
#[derive(Debug, PartialEq, Eq)]
enum PromoteDecision {
    /// Content-addressed or trusted-signed: forge-proof, promote.
    Promote,
    /// Not cryptographically verifiable, but the trust-local policy allows
    /// promoting the agent's own builds anyway. Its agent-claimed signatures
    /// are stripped before registration.
    PromoteUntrusted,
    /// Not verifiable and trust-local is off: refuse.
    Reject,
}

fn classify_promote(authentic: bool, trust_local: bool) -> PromoteDecision {
    if authentic {
        PromoteDecision::Promote
    } else if trust_local {
        PromoteDecision::PromoteUntrusted
    } else {
        PromoteDecision::Reject
    }
}

/// Whether to promote the agent's own unsigned, input-addressed builds.
/// Off by default: only content-addressed or trusted-signed paths cross into
/// a store other agents trust, so a malicious agent cannot poison a sibling
/// by pre-registering forged content under a predictable path name. Set
/// CODING_CAVE_PROMOTE_TRUST_LOCAL=1 in a single-user fleet to also share
/// locally-built packages, accepting that this trusts the producing agent.
fn promote_trust_local() -> bool {
    std::env::var_os("CODING_CAVE_PROMOTE_TRUST_LOCAL").is_some()
}

/// Promote an agent's installs into the host store. For each path: refuse
/// symlinks, rename it from the agent's upper into `/nix/store`, gate it on
/// cryptographic authenticity (content-address or trusted signature) plus the
/// trust-local policy, chown to root, and register the survivors' closure in
/// the host DB. A rejected or failed path is unstaged so the store never
/// keeps an unregistered orphan.
fn promote_to_host(
    state: &AgentState,
    store_target: &Path,
    to_promote: &[(String, std::ffi::OsString)],
) -> Result<()> {
    let snapshot = mkstate_for(&state.db_path)?;
    let trust_local = promote_trust_local();
    let result = (|| -> Result<()> {
        let mut promoted = Vec::new();
        let mut untrusted = Vec::new();
        for (path, name) in to_promote {
            let src = state.upper_dir.join(name);
            let dst = store_target.join(name);

            // A symlink is not a legitimate store path and would point
            // wherever the agent chose; never promote one.
            match src.symlink_metadata() {
                Ok(m) if m.file_type().is_symlink() => {
                    eprintln!(
                        "cav-store: refusing to promote {}: symlink",
                        name.to_string_lossy()
                    );
                    continue;
                }
                Ok(_) => {}
                Err(_) => continue,
            }

            std::fs::rename(&src, &dst)
                .with_context(|| format!("rename {src:?} -> {dst:?}"))?;

            let authentic = verify_authentic(&snapshot, path).unwrap_or(false);
            match classify_promote(authentic, trust_local) {
                PromoteDecision::Reject => {
                    unstage(&dst, &src);
                    eprintln!(
                        "cav-store: refusing to promote {}: not content-addressed \
                         or trusted-signed (set CODING_CAVE_PROMOTE_TRUST_LOCAL=1 \
                         to share local builds)",
                        name.to_string_lossy()
                    );
                    continue;
                }
                PromoteDecision::PromoteUntrusted => untrusted.push(path.clone()),
                PromoteDecision::Promote => {}
            }

            if let Err(e) = chown_tree_to_root(&dst) {
                unstage(&dst, &src);
                eprintln!(
                    "cav-store: rolled back {} after chown failure: {e:#}",
                    name.to_string_lossy()
                );
                continue;
            }
            promoted.push(path.clone());
        }
        // Trust-local promotions are unverified, so don't carry the agent's
        // claimed signatures (which it could fabricate) into the host DB.
        if !untrusted.is_empty() {
            strip_sigs(&snapshot, &untrusted)?;
        }
        nix_db::register_paths(None, Some(&snapshot), &promoted)
    })();
    let _ = std::fs::remove_dir_all(&snapshot);
    result
}

/// Return a staged path to the agent's upper, or remove it if that fails, so
/// a rejected promotion never leaves an unregistered path in the host store.
fn unstage(dst: &Path, src: &Path) {
    if std::fs::rename(dst, src).is_ok() {
        return;
    }
    let _ = std::fs::remove_file(dst);
    let _ = std::fs::remove_dir_all(dst);
}

/// Verify a just-renamed store path is authentic: its contents hash to its
/// recorded value AND it is either content-addressed (the name binds the
/// content) or carries a signature from a trusted key. An agent cannot forge
/// either, so this is the anti-poisoning gate. Reads metadata from the agent
/// DB snapshot and contents from the canonical `/nix/store` location.
fn verify_authentic(snapshot_state_dir: &Path, store_path: &str) -> Result<bool> {
    let status = Command::new("nix")
        .args([
            "--extra-experimental-features",
            "nix-command",
            "store",
            "verify",
            "--sigs-needed",
            "1",
            store_path,
        ])
        .env("NIX_REMOTE", "")
        .env("NIX_STATE_DIR", snapshot_state_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .context("spawn nix store verify")?;
    Ok(status.success())
}

/// Clear the signature column for `paths` in the snapshot DB so unverified,
/// trust-local promotions cannot inject fabricated trusted-signature metadata
/// into the host DB.
fn strip_sigs(snapshot_state_dir: &Path, paths: &[String]) -> Result<()> {
    let db = snapshot_state_dir.join("db").join("db.sqlite");
    let conn = Connection::open(&db)?;
    let mut stmt = conn.prepare("UPDATE ValidPaths SET sigs = NULL WHERE path = ?1")?;
    for p in paths {
        stmt.execute([p])?;
    }
    Ok(())
}

/// Build a NIX_STATE_DIR tree whose db.sqlite is a snapshot of the
/// agent's DB. A copy (not a symlink) so concurrent writes from the
/// cave can't race the dump. nix resolves the DB at `$NIX_STATE_DIR/db`,
/// so the returned path is the state dir itself.
fn mkstate_for(agent_db: &Path) -> Result<PathBuf> {
    let base = Path::new(STATE_DIR).join("promote-tmp");
    std::fs::create_dir_all(&base).context("create promote-tmp")?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let dir = base.join(format!("p-{}-{}", std::process::id(), nanos));
    let db_target_dir = dir.join("db");
    std::fs::create_dir_all(&db_target_dir).context("create promote tmp dirs")?;
    std::fs::copy(agent_db, db_target_dir.join("db.sqlite")).context("snapshot agent db")?;
    Ok(dir)
}

#[cfg(test)]
mod tests {
    use super::{classify_promote, PromoteDecision};

    #[test]
    fn authentic_paths_always_promote() {
        assert_eq!(classify_promote(true, false), PromoteDecision::Promote);
        assert_eq!(classify_promote(true, true), PromoteDecision::Promote);
    }

    #[test]
    fn unverifiable_paths_rejected_unless_trust_local() {
        assert_eq!(classify_promote(false, false), PromoteDecision::Reject);
        assert_eq!(
            classify_promote(false, true),
            PromoteDecision::PromoteUntrusted
        );
    }
}
