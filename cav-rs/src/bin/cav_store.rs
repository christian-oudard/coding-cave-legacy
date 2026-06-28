// cav-store: the host-side root daemon behind coding-cave-host.service.
// Verbs: `cav-store setup | teardown | watch`. A standalone binary rather
// than a `cav` subcommand, so the root systemd process links only this
// daemon and not the user-facing CLI. The unit definitions stay thin shims
// calling these verbs; logic lives in Rust and updates ride the binary
// instead of unit-file rewrites.
//
// Model (see docs/nix-store-redesign.md): each cave owns a private chroot
// store under a fixed system base (store_paths::CAVE_STORES_BASE); the cave
// does its own builds as itself, no idmap. This daemon owns the single shared
// store (root-owned, chroot store at store_paths::SHARED_STORE_ROOT). It
// watches each cave store's nix DB; for every new valid path it gates the path
// through the trust logic and, if it crosses, `nix copy`s the path (and
// closure) from the cave store into the shared store and hardlinks the cave's
// file to the shared inode. Caves do NOT substitute from the shared store —
// they fetch packages normally (binary cache or build) and the hardlink dedup
// collapses the duplicate onto one inode. Shared paths are root-owned
// (mode 555), which is what stops a cave rewriting them; no immutable bit (it
// only broke GC). The daemon also garbage-collects the shared store, rooting
// the closures every live cave references so a path in use is never removed.

use anyhow::{bail, Context, Result};
use rusqlite::Connection;

#[path = "../quota.rs"]
mod quota;
#[path = "../nix_db.rs"]
mod nix_db;
#[path = "../store_paths.rs"]
mod store_paths;
use rustix::fd::{AsFd, OwnedFd};
use rustix::fs::inotify::{self, CreateFlags, WatchFlags};
use rustix::fs::{flock, open, FlockOperation, Mode, OFlags};
use rustix::io::Errno;
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::mem::MaybeUninit;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const STATE_DIR: &str = "/var/lib/coding-cave";

// How often the background thread garbage-collects the shared store.
const GC_INTERVAL: std::time::Duration = std::time::Duration::from_secs(3600);

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
        Some("gc") => gc_shared_store(),
        other => bail!("usage: cav-store <setup|teardown|watch|gc> (got {other:?})"),
    }
}

fn setup() -> Result<()> {
    if !rustix::process::geteuid().is_root() {
        bail!("cav-store setup must be run as root");
    }
    std::fs::create_dir_all(STATE_DIR).context("create /var/lib/coding-cave")?;
    init_shared_store(&store_paths::shared_store_root())?;
    init_cave_stores_base(&store_paths::cave_stores_base())
}

/// Create the base that holds every per-cave nix store, sticky and
/// world-writable (mode 1777, like `/tmp`). The launcher runs unprivileged as
/// whatever user starts a cave, so it can't create a dir under root-owned
/// `/var/lib`; this root-side step makes the base writable to every user while
/// the sticky bit stops one user removing another's store. Idempotent: the
/// mode is reasserted each run in case a prior umask left it too tight.
fn init_cave_stores_base(base: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(base)
        .with_context(|| format!("create cave store base {}", base.display()))?;
    std::fs::set_permissions(base, std::fs::Permissions::from_mode(0o1777))
        .with_context(|| format!("chmod 1777 {}", base.display()))?;
    Ok(())
}

/// Create the shared store's `nix/store` and `nix/var` subdirectories.
///
/// Nix initializes its database (nix/var/nix/db) lazily on the first
/// `nix copy --to`, but the cave sandbox bind-mounts nix/store and nix/var at
/// launch — which happens before any promotion can populate them. Without
/// these directories the first cave on a fresh host fails to launch with
/// "bwrap: Can't find source path .../store/nix/var". Idempotent.
fn init_shared_store(root: &Path) -> Result<()> {
    std::fs::create_dir_all(root.join("nix/store")).context("create shared store nix/store")?;
    std::fs::create_dir_all(root.join("nix/var")).context("create shared store nix/var")?;
    Ok(())
}

fn teardown() -> Result<()> {
    // Nothing to tear down: the shared store is a plain directory that persists
    // across restarts. Kept as a verb so the unit's ExecStopPost shim is
    // stable.
    Ok(())
}

/// Watch each cave's nix DB; promote newly-installed paths into the shared
/// store and hardlink the cave's copy to the shared inode so the package lives
/// once on disk. Long-running; the ExecStart of coding-cave-host.service.
fn watch() -> Result<()> {
    if !test_mode() && !rustix::process::geteuid().is_root() {
        bail!("cav-store watch must be run as root");
    }
    let store_base = store_paths::cave_stores_base();
    std::fs::create_dir_all(&store_base).context("create cave-store base")?;

    let ifd = inotify::init(CreateFlags::empty()).context("inotify init")?;
    let base_wd = inotify::add_watch(&ifd, &store_base, WatchFlags::CREATE)
        .context("watch cave-store base")?;

    // Crashes here if a quota is configured but the filesystem can't enforce
    // it; applies the shared-home cap. Skipped in test mode so a dev host's
    // /etc/coding-cave/host.json can't perturb the integration tests.
    let quota = if test_mode() {
        None
    } else {
        setup_quota(&store_base)?
    };

    let mut w = Watcher {
        ifd,
        store_base,
        base_wd,
        active: HashMap::new(),
        pending: HashMap::new(),
        tracked: HashSet::new(),
        quota,
    };

    // Adopt any caves that already exist. Picks up installs that happened
    // while the watcher was down by way of advance_tracking's initial scan.
    if let Ok(entries) = std::fs::read_dir(&w.store_base) {
        for entry in entries.flatten() {
            if let Some(id) = agent_id_from_name(&entry.file_name()) {
                w.advance_tracking(&id, None);
            }
        }
    }

    // Periodically garbage-collect the shared store, rooting the closures
    // every live cave references so a path in use is never removed. Runs on
    // its own thread so it never blocks the inotify promote loop. Skipped in
    // test mode (no real nix store to collect).
    if !test_mode() {
        let base = w.store_base.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(GC_INTERVAL);
            if let Err(e) = gc_with_base(&base) {
                eprintln!("cav-store gc: {e:#}");
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
    store_base: PathBuf,
    base_wd: i32,
    /// wd -> AgentState; the watch is on `agent-<id>/nix/var/nix/db/` and
    /// fires MODIFY/CLOSE_WRITE for every nix DB write.
    active: HashMap<i32, AgentState>,
    /// wd -> agent_id; the watch is on a parent of the cave DB waiting for
    /// db.sqlite to materialize. Promoted to `active` on the next event that
    /// lets us attach the real watch.
    pending: HashMap<i32, String>,
    /// All agent_ids currently in `active` or `pending`. Lets the base-wd
    /// CREATE handler skip caves we're already tracking.
    tracked: HashSet<String>,
    /// Per-agent disk-quota state, when a per-agent cap is configured.
    quota: Option<QuotaState>,
}

/// Resolved per-agent quota: the cap and the filesystem to set it on.
struct QuotaState {
    per_agent_bytes: u64,
    mountpoint: PathBuf,
}

impl Watcher {
    /// Drive a cave toward the active state. If a pending watch was already in
    /// place for this cave, `current_wd` is its wd; that watch gets retired
    /// before we attach the next one (no inotify watch leaks).
    ///
    /// The loop closes the small race where db.sqlite is created between our
    /// "does it exist?" check and the add_watch on its parent dir — after
    /// attaching, we re-check; if the deeper level is now ready, we drop this
    /// pending watch and try again.
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
            if let Some(mut state) = AgentState::new(&self.store_base, agent_id) {
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
                        self.apply_agent_quota(agent_id);
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

            // Not ready: attach a watch on the deepest existing ancestor of
            // db.sqlite so we wake up when the next piece materializes.
            let target = deepest_existing(&cave_db_dir(&self.store_base, agent_id));
            let Some(target) = target else {
                // The cave dir vanished (created then deleted). Drop tracking.
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
                    if AgentState::new(&self.store_base, agent_id).is_some() {
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

    /// Apply the per-agent cap to a cave's writable trees. Best-effort: a
    /// failure is logged, never fatal, so a quota hiccup can't wedge a cave.
    fn apply_agent_quota(&self, agent_id: &str) {
        let Some(q) = &self.quota else {
            return;
        };
        let dirs = agent_quota_dirs(&self.store_base, agent_id);
        if let Err(e) = quota::apply(
            &dirs,
            quota::projid_for(agent_id),
            q.per_agent_bytes,
            &q.mountpoint,
        ) {
            eprintln!("cav-store quota: agent-{agent_id} cap not applied: {e:#}");
        }
    }
}

/// The cave-store dir for one agent: `<store_base>/agent-<id>`.
fn cave_store_root(store_base: &Path, agent_id: &str) -> PathBuf {
    store_base.join(format!("agent-{agent_id}"))
}

/// The nix store URI the manager uses to *read* a cave store (verify, copy
/// --from). Always read-only: the cave store is owned by the unprivileged
/// user that launched the cave, and nix opening a writable local store as
/// root rewrites its dir to root:nixbld 1775, locking the owner out of its
/// own store. read-only=true reads without taking ownership.
fn cave_read_uri(cave_root: &Path) -> String {
    format!("local?root={}&read-only=true", cave_root.display())
}

/// The cave's nix DB directory: `<cave-store>/nix/var/nix/db`.
fn cave_db_dir(store_base: &Path, agent_id: &str) -> PathBuf {
    cave_store_root(store_base, agent_id)
        .join("nix")
        .join("var")
        .join("nix")
        .join("db")
}

/// The deepest ancestor of `target` (inclusive) that currently exists, or
/// None if not even the cave-store root exists. Lets inotify watch the
/// closest live dir while the nix DB tree is still being created.
fn deepest_existing(target: &Path) -> Option<PathBuf> {
    let mut cur = target;
    loop {
        if cur.exists() {
            return Some(cur.to_path_buf());
        }
        cur = cur.parent()?;
        // Stop climbing above the agent's own store root; the base-dir watch
        // covers creation of the agent dir itself.
        if cur.file_name().and_then(|n| n.to_str()).map(|n| n.starts_with("agent-")) == Some(true) {
            return if cur.exists() { Some(cur.to_path_buf()) } else { None };
        }
    }
}

/// The cave's writable tree this daemon caps: its per-cave nix store. Worktrees
/// used to be capped too, but they live on the user's home filesystem while the
/// nix store now lives under /var/lib — a different filesystem the daemon
/// reaches, and project quotas are per-filesystem. So the per-agent cap now
/// bounds only the nix store (the shared-disk risk); worktree disk is the
/// user's home filesystem's concern.
fn agent_quota_dirs(store_base: &Path, agent_key: &str) -> Vec<PathBuf> {
    vec![cave_store_root(store_base, agent_key)]
}

/// Resolve quota config at startup. Crashes if a quota is configured but the
/// filesystem can't enforce it. Returns the per-agent state (if a per-agent cap
/// is set) for use as caves are adopted.
fn setup_quota(store_base: &Path) -> Result<Option<QuotaState>> {
    let cfg = quota::HostConfig::load()?;
    if !cfg.active() {
        return Ok(None);
    }
    let mountpoint = quota::mountpoint_for(store_base)?;
    let supported = quota::project_quota_supported(&mountpoint);
    if quota::should_crash(cfg.active(), supported) {
        bail!(
            "disk quota configured but project quota is not active on {}; \
             enable it (see README) or unset the quota",
            mountpoint.display()
        );
    }
    // The shared-home cap bounded the cave home, which lives on the user's
    // filesystem. The per-cave nix store now lives under /var/lib, so this
    // root daemon no longer tracks the home and can't enforce that cap. Fail
    // loudly rather than silently leave a requested cap unapplied (a quota you
    // asked for that can't be enforced is a misconfiguration, not something to
    // paper over). Cap the home on its own filesystem instead.
    if cfg.shared_home_bytes()?.is_some() {
        bail!(
            "sharedHomeQuota is no longer enforceable by the host store \
             manager: the cave home stays on the user's filesystem while the \
             nix store moved to {}. Remove sharedHomeQuota and cap the cave \
             home on its own filesystem.",
            store_base.display()
        );
    }
    Ok(cfg
        .per_agent_bytes()?
        .map(|per_agent_bytes| QuotaState {
            per_agent_bytes,
            mountpoint,
        }))
}

fn agent_id_from_name(name: &OsStr) -> Option<String> {
    name.to_str()?.strip_prefix("agent-").map(|s| s.to_string())
}

struct AgentState {
    agent_id: String,
    /// The cave's chroot-store root (`<store_base>/agent-<id>`), passed to nix
    /// as `local?root=<this>`.
    cave_root: PathBuf,
    /// Physical store dir (`<cave_root>/nix/store`), where the cave's files
    /// live and where we hardlink the shared inode.
    store_dir: PathBuf,
    db_path: PathBuf,
    db_dir: PathBuf,
    /// Highest `ValidPaths.id` we've processed for this cave. The next scan
    /// reads `WHERE id > last_seen_id`. `ValidPaths.id` is
    /// `INTEGER PRIMARY KEY AUTOINCREMENT` so values are monotonic and never
    /// reused.
    last_seen_id: i64,
}

impl AgentState {
    fn new(store_base: &Path, agent_id: &str) -> Option<Self> {
        let cave_root = cave_store_root(store_base, agent_id);
        let db_dir = cave_db_dir(store_base, agent_id);
        let db_path = db_dir.join("db.sqlite");
        if !db_path.exists() {
            return None;
        }
        Some(Self {
            agent_id: agent_id.to_string(),
            store_dir: cave_root.join("nix").join("store"),
            cave_root,
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
    .context("open cave db")?;

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
        // (cave setup mid-flight). The next DB write will retry.
        Err(rusqlite::Error::SqliteFailure(_, Some(ref msg))) if msg.contains("no such table") => {
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };
    drop(conn);

    if rows.is_empty() {
        return Ok(());
    }

    let shared = store_paths::shared_store_root();
    let shared_store_dir = shared.join("nix").join("store");
    // Every newly-valid path whose files are actually in this cave's store. A
    // path already in the shared store is NOT skipped: the cave's own copy
    // still has to be collapsed onto the shared inode (dedup), which is the
    // whole point — a path absent from the cave store is nothing to do.
    let mut cave_paths = Vec::new();
    for (_id, path) in &rows {
        let Some(name) = Path::new(path).file_name() else {
            continue;
        };
        if !state.store_dir.join(name).exists() {
            continue;
        }
        cave_paths.push((path.clone(), name.to_os_string()));
    }

    if test_mode() {
        // Exercise the diff pipeline without nix/flock: "promote" not-yet-
        // shared paths by copying them into the shared store dir.
        std::fs::create_dir_all(&shared_store_dir).ok();
        for (_, name) in &cave_paths {
            let dst = shared_store_dir.join(name);
            if dst.exists() {
                continue;
            }
            copy_tree(&state.store_dir.join(name), &dst)
                .with_context(|| format!("copy -> {dst:?}"))?;
        }
    } else if !cave_paths.is_empty() {
        promote_and_dedup(state, &shared, &shared_store_dir, &cave_paths)?;
    }

    // Advance the cursor only after everything in this batch succeeded.
    // On error we return early without updating last_seen_id; the next
    // event re-reads the same range and the exists() filters cheaply
    // skip whatever already made it across.
    state.last_seen_id = rows.last().map(|(id, _)| *id).unwrap_or(state.last_seen_id);
    Ok(())
}

/// Recursively copy a tree (test-mode promotion stand-in for `nix copy`).
fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    let meta = src.symlink_metadata()?;
    if meta.file_type().is_symlink() {
        let target = std::fs::read_link(src)?;
        std::os::unix::fs::symlink(target, dst)?;
    } else if meta.is_dir() {
        std::fs::create_dir_all(dst)?;
        for entry in std::fs::read_dir(src)? {
            let entry = entry?;
            copy_tree(&entry.path(), &dst.join(entry.file_name()))?;
        }
    } else {
        std::fs::copy(src, dst)?;
    }
    Ok(())
}

/// What to do with one candidate promotion after authenticity checking.
#[derive(Debug, PartialEq, Eq)]
enum PromoteDecision {
    /// Content-addressed or trusted-signed: forge-proof, promote.
    Promote,
    /// Not cryptographically verifiable, but the trust-local policy allows
    /// promoting the cave's own builds anyway.
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

/// Whether to promote the cave's own unsigned, input-addressed builds.
/// Off by default: only content-addressed or trusted-signed paths cross into
/// the shared store other caves trust, so a malicious agent cannot poison a
/// sibling by pre-registering forged content under a predictable path name.
/// Set CODING_CAVE_PROMOTE_TRUST_LOCAL=1 in a single-user fleet to also share
/// locally-built packages, accepting that this trusts the producing agent.
fn promote_trust_local() -> bool {
    std::env::var_os("CODING_CAVE_PROMOTE_TRUST_LOCAL").is_some()
}

/// Promote a cave's installs into the shared store. For each path: refuse
/// symlinks at the top level, gate it on cryptographic authenticity
/// (content-address or trusted signature) plus the trust-local policy, then
/// `nix copy` the path and its closure cave-store -> shared store. After the
/// copy, hardlink the cave's file to the shared inode (one copy on disk). The
/// shared path is root-owned (mode 555), which is what stops a cave rewriting
/// it; no immutable bit (it only broke GC).
fn promote_and_dedup(
    state: &AgentState,
    shared_root: &Path,
    shared_store_dir: &Path,
    cave_paths: &[(String, std::ffi::OsString)],
) -> Result<()> {
    // The one point where the manager *writes* a live cave's store is the
    // hardlink below. Hold a shared lock on the cave's gc.lock for the whole
    // pass: nix's GC takes it exclusively, so this blocks the cave from
    // collecting a path out from under a hardlink (and the cave's GC blocks
    // us). Released when the fd drops at end of scope.
    let _gc_lock = lock_cave_gc(&state.cave_root)?;
    let trust_local = promote_trust_local();
    // Read the cave store read-only so reading it as root doesn't reset its
    // ownership (see cave_read_uri). The verify/copy calls below enable the
    // read-only-local-store experimental feature this URI needs.
    let cave_uri = cave_read_uri(&state.cave_root);
    let shared_uri = format!("local?root={}", shared_root.display());

    for (path, name) in cave_paths {
        let src = state.store_dir.join(name);
        let shared_path = shared_store_dir.join(name);

        if !shared_path.exists() {
            // Not yet shared: gate it, then copy cave -> shared.
            //
            // A symlink at the store-path root is not a legitimate store path
            // and would point wherever the agent chose; never promote one.
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

            // Fail loudly: a verify that couldn't even run (missing nix, etc.)
            // is not "unauthentic" — skip the path rather than silently treat
            // a broken check as a rejection or, worse, a trust-local pass.
            let authentic = match verify_authentic(&cave_uri, path) {
                Ok(a) => a,
                Err(e) => {
                    eprintln!(
                        "cav-store: verify of {} failed: {e:#}",
                        name.to_string_lossy()
                    );
                    continue;
                }
            };
            let decision = classify_promote(authentic, trust_local);
            if decision == PromoteDecision::Reject {
                eprintln!(
                    "cav-store: refusing to promote {}: not content-addressed \
                     or trusted-signed (set CODING_CAVE_PROMOTE_TRUST_LOCAL=1 \
                     to share local builds)",
                    name.to_string_lossy()
                );
                continue;
            }

            // Trust-local promotions are unverified, so don't carry the cave's
            // claimed signatures into the shared store; verified ones copy with
            // signature checking on.
            let trusted = decision == PromoteDecision::Promote;
            if let Err(e) = nix_copy(&cave_uri, &shared_uri, path, trusted) {
                eprintln!(
                    "cav-store: nix copy of {} failed: {e:#}",
                    name.to_string_lossy()
                );
                continue;
            }
        }

        // Now in the shared store (just copied, or already there because a
        // sibling built it first): collapse the cave's own copy onto the
        // shared inode so the package lives once on disk. Always done, even
        // when the path was already shared — that is what dedups a path two
        // caves built independently. The shared store is root-owned, so the
        // cave can't rewrite shared content through this hardlink.
        if let Err(e) = hardlink_into_shared(&src, &shared_path) {
            eprintln!(
                "cav-store: hardlink dedup of {} failed: {e:#}",
                name.to_string_lossy()
            );
        }
    }
    Ok(())
}

/// Open the cave store's gc.lock and take a shared advisory lock. nix's GC
/// holds it exclusively, so a shared lock lets the cave's normal operations
/// proceed but blocks GC while the manager hardlinks. Opened read-only (root
/// can open the cave-owned lock regardless of mode); released when the
/// returned fd drops.
fn lock_cave_gc(cave_root: &Path) -> Result<OwnedFd> {
    let path = cave_root
        .join("nix")
        .join("var")
        .join("nix")
        .join("gc.lock");
    let fd = open(&path, OFlags::RDONLY, Mode::empty())
        .with_context(|| format!("open {}", path.display()))?;
    flock(&fd, FlockOperation::LockShared).context("flock cave gc.lock")?;
    Ok(fd)
}

/// Verify a cave store path is authentic: it is either content-addressed (the
/// name binds the content) or carries a signature from a trusted key, and its
/// contents hash to the recorded value. An agent cannot forge either, so this
/// is the anti-poisoning gate. Reads from the cave's chroot store.
fn verify_authentic(cave_uri: &str, store_path: &str) -> Result<bool> {
    let status = Command::new("nix")
        .args([
            // read-only-local-store: cave_uri carries read-only=true so this
            // root process reads the user-owned cave store without nix seizing
            // ownership of it.
            "--extra-experimental-features",
            "nix-command read-only-local-store",
            "store",
            "verify",
            "--store",
            cave_uri,
            "--sigs-needed",
            "1",
            store_path,
        ])
        .env("NIX_REMOTE", "")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .context("spawn nix store verify")?;
    Ok(status.success())
}

/// `nix copy` a path and its closure from one chroot store to another. When
/// `check_sigs` is false (trust-local promotions), signature checking is
/// skipped and nix recomputes integrity from the content on the way in.
fn nix_copy(from_uri: &str, to_uri: &str, store_path: &str, check_sigs: bool) -> Result<()> {
    let mut cmd = Command::new("nix");
    cmd.args([
        // read-only-local-store: the from URI (the cave store) carries
        // read-only=true, so reading it as root doesn't reset its ownership.
        "--extra-experimental-features",
        "nix-command read-only-local-store",
        "copy",
        "--from",
        from_uri,
        "--to",
        to_uri,
    ]);
    if !check_sigs {
        cmd.arg("--no-check-sigs");
    }
    cmd.arg(store_path);
    let out = cmd
        .env("NIX_REMOTE", "")
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .context("spawn nix copy")?;
    if !out.status.success() {
        bail!("nix copy: {}", String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(())
}

/// Hardlink the cave's store path to the shared inode so the two share one
/// copy on disk. Both stores sit on the same filesystem (under the cave
/// home / state dir), so a cross-store hardlink of the top-level entry (and
/// every file beneath it) collapses the duplicate. The shared file is the
/// link target; the cave's file is replaced by a link to it.
fn hardlink_into_shared(cave_path: &Path, shared_path: &Path) -> Result<()> {
    fn link_tree(cave: &Path, shared: &Path) -> std::io::Result<()> {
        let meta = shared.symlink_metadata()?;
        if meta.is_dir() && !meta.file_type().is_symlink() {
            for entry in std::fs::read_dir(shared)? {
                let entry = entry?;
                link_tree(&cave.join(entry.file_name()), &entry.path())?;
            }
            return Ok(());
        }
        // File or symlink: replace the cave's copy with a hardlink to the
        // shared inode. Symlinks can't be hardlinked portably, so leave the
        // cave's symlink in place (negligible disk).
        if meta.file_type().is_symlink() {
            return Ok(());
        }
        let _ = std::fs::remove_file(cave);
        std::fs::hard_link(shared, cave)
    }
    link_tree(cave_path, shared_path)
        .with_context(|| format!("hardlink {cave_path:?} <- {shared_path:?}"))
}

/// `cav-store gc`: one-shot garbage collection of the shared store. Roots
/// every path any cave still references, then collects the rest.
fn gc_shared_store() -> Result<()> {
    if !test_mode() && !rustix::process::geteuid().is_root() {
        bail!("cav-store gc must be run as root");
    }
    gc_with_base(&store_paths::cave_stores_base())
}

/// Garbage-collect the shared store, first rooting every store path any cave
/// still references so a path in use is never removed. Conservative: roots all
/// caves' paths, not just running ones, so a shared path is collected only once
/// no cave references it at all.
///
/// Roots are plain symlinks under the shared store's gcroots dir (the
/// documented "any symlink in gcroots is a root" mechanism). The set is rebuilt
/// from scratch each pass, so a removed cave stops pinning its paths. Only the
/// shared store's own paths are rooted; a cave's private builds live in the
/// cave store and are not the shared store's concern.
fn gc_with_base(store_base: &Path) -> Result<()> {
    let shared = store_paths::shared_store_root();
    let shared_store_dir = shared.join("nix").join("store");
    let gcroots = shared
        .join("nix")
        .join("var")
        .join("nix")
        .join("gcroots")
        .join("cav");
    let _ = std::fs::remove_dir_all(&gcroots);
    std::fs::create_dir_all(&gcroots).context("create shared gcroots")?;

    let mut rooted = 0u64;
    if let Ok(entries) = std::fs::read_dir(store_base) {
        for entry in entries.flatten() {
            let Some(id) = agent_id_from_name(&entry.file_name()) else {
                continue;
            };
            let Some(state) = AgentState::new(store_base, &id) else {
                continue;
            };
            let Ok(paths) = nix_db::valid_paths(&state.db_path) else {
                continue;
            };
            for p in paths {
                let name = nix_db::store_name(&p);
                let target = shared_store_dir.join(&name);
                if !target.exists() {
                    continue;
                }
                // Duplicate names across caves just fail the second symlink.
                let _ = std::os::unix::fs::symlink(&target, gcroots.join(&name));
                rooted += 1;
            }
        }
    }

    if test_mode() {
        return Ok(());
    }
    let status = Command::new("nix-store")
        .args(["--store", &format!("local?root={}", shared.display()), "--gc"])
        .status()
        .context("nix-store --gc on shared store")?;
    if !status.success() {
        bail!("shared store gc failed");
    }
    eprintln!("cav-store gc: rooted {rooted} shared paths, collected the rest");
    Ok(())
}

/// Test-only escape hatch: skips the root check and the nix shell-outs.
/// Lets the integration test exercise the inotify/diff pipeline without root,
/// nix, or a real nix DB. Production never sets this.
fn test_mode() -> bool {
    std::env::var_os("CODING_CAVE_HOST_WATCH_TEST_MODE").is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

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

    #[test]
    fn init_shared_store_creates_sandbox_bind_sources() {
        let root = std::env::temp_dir().join(format!("cav-store-init-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        // The sandbox bind-mounts these two paths at cave launch; both must
        // exist after setup or the first cave on a fresh host can't start.
        init_shared_store(&root).unwrap();
        assert!(root.join("nix/store").is_dir());
        assert!(root.join("nix/var").is_dir());
        // Idempotent across service restarts.
        init_shared_store(&root).unwrap();
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn cave_read_uri_is_read_only() {
        // Regression: the manager must read a cave store read-only. Opening it
        // writable as root rewrites the store dir to root:nixbld 1775 and locks
        // the cave's unprivileged owner out of its own store, breaking the next
        // launch's `nix copy` with EACCES on a store lock file.
        let uri = cave_read_uri(Path::new("/x/store/agent-7"));
        assert_eq!(uri, "local?root=/x/store/agent-7&read-only=true");
    }

    #[test]
    fn cave_db_dir_is_chroot_store_db() {
        let base = Path::new("/x/store");
        assert_eq!(
            cave_db_dir(base, "7"),
            Path::new("/x/store/agent-7/nix/var/nix/db"),
        );
    }

    #[test]
    fn cave_store_root_layout() {
        let base = Path::new("/x/store");
        let s = AgentState {
            agent_id: "7".into(),
            cave_root: cave_store_root(base, "7"),
            store_dir: cave_store_root(base, "7").join("nix").join("store"),
            db_path: cave_db_dir(base, "7").join("db.sqlite"),
            db_dir: cave_db_dir(base, "7"),
            last_seen_id: 0,
        };
        assert_eq!(s.cave_root, Path::new("/x/store/agent-7"));
        assert_eq!(s.store_dir, Path::new("/x/store/agent-7/nix/store"));
    }

    #[test]
    fn deepest_existing_finds_live_ancestor() {
        let tmp = std::env::temp_dir().join(format!("cav-deepest-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let base = tmp.join("store");
        // Create only agent-1/nix; the db dir below it does not exist yet.
        std::fs::create_dir_all(base.join("agent-1").join("nix")).unwrap();
        let db_dir = cave_db_dir(&base, "1");
        let found = deepest_existing(&db_dir).unwrap();
        assert_eq!(found, base.join("agent-1").join("nix"));
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
