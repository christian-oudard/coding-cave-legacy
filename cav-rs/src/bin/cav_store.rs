// cav-store: the host-side root daemon behind coding-cave-host.service.
// Verbs: `cav-store setup | teardown | watch`. A standalone binary rather
// than a `cav` subcommand, so the root systemd process links only this
// daemon and not the user-facing CLI. The unit definitions stay thin shims
// calling these verbs; logic lives in Rust and updates ride the binary
// instead of unit-file rewrites.
//
// Model (see docs/nix-store.md): a three-level tree, deduplicated so a package
// lives once on disk across all of it:
//
//   level 0  host /nix/store          nix-managed, trusted, never written by us
//   level 1  shared store             daemon-owned quarantine for cave content
//   level 2  per-cave chroot stores   each cave builds/fetches as itself
//
// The canonical inode for a path is the host's when the host has it, else the
// shared store's; everything hardlinks down onto it. Cave-originated content
// never rises above the shared store: the daemon only ever *reads* the host
// store, and only content-addressed / trusted-signed paths cross from a cave
// into the shared store. Shared paths are root-owned (mode 555), which is what
// stops a cave rewriting them; no immutable bit (it only broke GC).
//
// The daemon runs one idempotent `converge` pass per cave path on a timer: it
// materializes the shared copy (importing from the host when the host has it,
// else promoting from the cave through the trust gate), relinks the shared copy
// onto the host inode when the host has it, and collapses the cave's copy onto
// the shared inode. Every step is "hardlink onto canonical, skip if already
// there", so a pass repairs any drift — a moved inode, a package the host
// acquired later — instead of depending on an invariant that inodes never move.
// It also garbage-collects the shared store, rooting the closures every cave
// references so a path in use is never removed.

use anyhow::{bail, Context, Result};

#[path = "../quota.rs"]
mod quota;
#[path = "../nix_db.rs"]
mod nix_db;
#[path = "../store_paths.rs"]
mod store_paths;
use rustix::fd::OwnedFd;
use rustix::fs::{flock, open, FlockOperation, Mode, OFlags};
use std::collections::HashSet;
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

const STATE_DIR: &str = "/var/lib/coding-cave";

// How often a full converge pass runs over every cave store. Each pass is
// readdir + sqlite reads + stat-guarded hardlinks, so it is cheap; the first
// pass after a deploy does the real import/link work, later passes are nearly
// all same-inode skips.
const CONVERGE_INTERVAL: Duration = Duration::from_secs(300);

// How often the converge loop garbage-collects the shared store.
const GC_INTERVAL: Duration = Duration::from_secs(3600);

// Error policy: fail loudly and fast. The first serious error — an import or
// hardlink that fails, an unreadable DB, a GC failure — exits the daemon so
// the systemd unit lands in "failed" and points at exactly what to fix. No
// skip lists, no best-effort passes: a store problem that is tolerated is a
// scar that compounds (tolerating one is how the disk filled last time). The
// only non-errors are normal lifecycle: a cave or path vanishing mid-pass, an
// unsigned build staying private, a .drv staying cave-local.

// Set once if hardlinking the shared store onto host inodes ever fails with
// EXDEV (host /nix/store and the shared store on different filesystems). From
// then on the host<->shared relink is skipped for the daemon's lifetime; the
// shared store stays canonical and cross-cave dedup still works.
static HOST_RELINK_DISABLED: AtomicBool = AtomicBool::new(false);

// Bounded retry for reading a nix DB that is momentarily unreadable (hot WAL)
// while another process writes it, before giving up. Used for the host DB and
// cave DBs, in both the converge pass and the GC rooting pass.
const RVP_RETRIES: u32 = 5;
const RVP_RETRY_DELAY: Duration = Duration::from_millis(100);

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

/// Converge every cave store onto the deduplicated three-level tree, forever.
/// Long-running; the ExecStart of coding-cave-host.service. Each pass walks
/// every cave, converges each of its valid paths (idempotent), and periodically
/// garbage-collects the shared store. A timer, not inotify: a full pass is cheap
/// (stat-guarded hardlinks skip already-deduped files) and, being stateless,
/// self-heals any drift a purely event-driven scan would miss forever.
fn watch() -> Result<()> {
    if !test_mode() && !rustix::process::geteuid().is_root() {
        bail!("cav-store watch must be run as root");
    }
    let store_base = store_paths::cave_stores_base();
    std::fs::create_dir_all(&store_base).context("create cave-store base")?;

    // Crashes here if a quota is configured but the filesystem can't enforce
    // it. Skipped in test mode so a dev host's /etc/coding-cave/host.json can't
    // perturb the integration tests.
    let quota = if test_mode() {
        None
    } else {
        setup_quota(&store_base)?
    };

    // Caves whose per-agent quota has been applied this process lifetime, so we
    // reapply only to newly-appearing caves (applying is best-effort and off the
    // convergence path).
    let mut quota_applied: HashSet<String> = HashSet::new();
    // Force a GC on the first pass, then hourly.
    let mut last_gc = Instant::now()
        .checked_sub(GC_INTERVAL)
        .unwrap_or_else(Instant::now);

    loop {
        converge_pass(&store_base, quota.as_ref(), &mut quota_applied)?;

        if !test_mode() && last_gc.elapsed() >= GC_INTERVAL {
            gc_with_base(&store_base)?;
            last_gc = Instant::now();
        }
        std::thread::sleep(converge_interval());
    }
}

/// The pause between converge passes. Short in test mode so the integration
/// test's post-startup installs are picked up within its timeout.
fn converge_interval() -> Duration {
    if test_mode() {
        Duration::from_millis(100)
    } else {
        CONVERGE_INTERVAL
    }
}

/// One convergence pass over every cave store under `store_base`. Reads the
/// host's valid paths once (cached for the pass), then for each cave applies its
/// quota (once per lifetime) and converges each valid path. Fail-fast: the
/// first serious error aborts the pass and exits the daemon (see the error
/// policy above). A cave that vanishes mid-pass is normal lifecycle (`cav gc`
/// removing it), not an error.
fn converge_pass(
    store_base: &Path,
    quota: Option<&QuotaState>,
    quota_applied: &mut HashSet<String>,
) -> Result<()> {
    let shared = store_paths::shared_store_root();
    let shared_store_dir = shared.join("nix").join("store");
    let host_store_dir = store_paths::host_nix_store();

    // Which paths the host already has. Read once per pass. A missing DB is a
    // legitimate "host has nothing" (empty set: hermetic tests, non-nix hosts);
    // an unreadable one, after the bounded hot-WAL retry, is a real failure.
    let host_db = store_paths::host_nix_db();
    let host_valid = if !host_db.exists() {
        HashSet::new()
    } else {
        read_valid_paths_retrying(&host_db).context("read host nix DB")?
    };

    let entries = std::fs::read_dir(store_base)
        .with_context(|| format!("read cave store base {}", store_base.display()))?;
    for entry in entries.flatten() {
        let Some(id) = agent_id_from_name(&entry.file_name()) else {
            continue;
        };
        let Some(state) = AgentState::new(store_base, &id) else {
            continue; // no DB yet: cave mid-setup, nothing valid to converge.
        };

        let mut cave = || -> Result<()> {
            if quota_applied.insert(id.clone()) {
                apply_agent_quota(store_base, &id, quota)?;
            }
            converge_cave(&state, &shared, &shared_store_dir, &host_store_dir, &host_valid)
        };
        if let Err(e) = cave() {
            if !state.cave_root.exists() {
                continue; // cave removed mid-pass: normal lifecycle.
            }
            return Err(e.context(format!("converge agent-{id}")));
        }
    }
    Ok(())
}

/// Converge one cave: hold its gc.lock (so its own GC can't delete a path out
/// from under a hardlink) and converge each valid path. The lock is the single
/// point where we write a live cave's store; nix's GC takes it exclusively, our
/// shared lock coexists with the cave's normal operations. Fail-fast: the
/// first path that errors aborts (the caller distinguishes a vanished cave
/// from a real failure).
fn converge_cave(
    state: &AgentState,
    shared_root: &Path,
    shared_store_dir: &Path,
    host_store_dir: &Path,
    host_valid: &HashSet<String>,
) -> Result<()> {
    let paths = read_valid_paths_retrying(&state.db_path)
        .with_context(|| format!("read cave db for agent-{}", state.agent_id))?;

    // In production hold the cave's gc.lock across the pass; in test mode there
    // is no real nix (and no gc.lock), so skip it.
    let _gc_lock = if test_mode() {
        None
    } else {
        Some(lock_cave_gc(&state.cave_root)?)
    };

    for path in &paths {
        let Some(name) = Path::new(path).file_name() else {
            continue;
        };
        // Derivations stay cave-private. Importing a .drv makes nix check that
        // the drv's whole input-drv closure is valid in the destination store,
        // a guarantee a cave that has GC'd some inputs can't meet (a dangling
        // .drv is legal in a store; only a transfer checks it). They are
        // kilobyte recipes, so sharing them saves nothing anyway.
        if path.ends_with(".drv") {
            continue;
        }
        if !state.store_dir.join(name).exists() {
            continue; // valid in the DB but not physically here: nothing to do.
        }
        converge(state, shared_root, shared_store_dir, host_store_dir, host_valid, path, name)
            .with_context(|| format!("converge {}", name.to_string_lossy()))?;
    }
    Ok(())
}

/// Resolved per-agent quota: the cap and the filesystem to set it on.
struct QuotaState {
    per_agent_bytes: u64,
    mountpoint: PathBuf,
}

/// Apply the per-agent cap to a cave's writable trees. A configured quota that
/// can't be applied is a violated invariant, so the error propagates and exits
/// the daemon (error policy above). No-op when no per-agent quota is
/// configured.
fn apply_agent_quota(store_base: &Path, agent_id: &str, quota: Option<&QuotaState>) -> Result<()> {
    let Some(q) = quota else {
        return Ok(());
    };
    let dirs = agent_quota_dirs(store_base, agent_id);
    quota::apply(
        &dirs,
        quota::projid_for(agent_id),
        q.per_agent_bytes,
        &q.mountpoint,
    )
    .with_context(|| format!("apply quota cap to agent-{agent_id}"))
}

/// The cave-store dir for one agent: `<store_base>/agent-<id>`.
fn cave_store_root(store_base: &Path, agent_id: &str) -> PathBuf {
    store_base.join(format!("agent-{agent_id}"))
}

/// The cave's nix DB directory: `<cave-store>/nix/var/nix/db`.
fn cave_db_dir(store_base: &Path, agent_id: &str) -> PathBuf {
    cave_store_root(store_base, agent_id)
        .join("nix")
        .join("var")
        .join("nix")
        .join("db")
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
}

impl AgentState {
    fn new(store_base: &Path, agent_id: &str) -> Option<Self> {
        let cave_root = cave_store_root(store_base, agent_id);
        let db_path = cave_db_dir(store_base, agent_id).join("db.sqlite");
        if !db_path.exists() {
            return None;
        }
        Some(Self {
            agent_id: agent_id.to_string(),
            store_dir: cave_root.join("nix").join("store"),
            cave_root,
            db_path,
        })
    }
}

/// Converge one cave path onto the deduplicated tree. Idempotent: safe to run
/// every pass; already-linked files are stat-skipped. Returns Ok for both
/// success and a legitimate no-cross (a symlink output, or an unauthentic cave
/// build that stays private); returns Err only for a genuine failure so the
/// caller logs it and the next pass retries.
///
/// The canonical inode is the host's when the host has the path, else the
/// shared store's. Steps:
///   1. ensure the shared copy exists — import from the host (trusted) when the
///      host has it, else gate the cave copy and promote it;
///   2. when the host has it, relink the shared copy onto the host inode;
///   3. collapse the cave copy onto the shared inode.
fn converge(
    state: &AgentState,
    shared_root: &Path,
    shared_store_dir: &Path,
    host_store_dir: &Path,
    host_valid: &HashSet<String>,
    path: &str,
    name: &OsStr,
) -> Result<()> {
    let src = state.store_dir.join(name);
    let shared_path = shared_store_dir.join(name);
    let host_path = host_store_dir.join(name);

    // A symlink at the store-path root is not a legitimate store path and would
    // point wherever the agent chose; never treat one as a package. A symlink
    // output (e.g. a `-man` alias) or a path that vanished since the DB read is
    // a normal no-cross, not an error.
    match src.symlink_metadata() {
        Ok(m) if !m.file_type().is_symlink() => {}
        _ => return Ok(()),
    }

    // The host "has" the path only if it is both DB-valid and physically
    // present, so we never relink onto a phantom inode.
    let host_has = host_valid.contains(path) && host_path.exists();

    // 1. Materialize the shared copy if absent.
    if !shared_path.exists() {
        if host_has {
            // Trusted: the host store is root-managed level 0. Import it (and
            // its closure) straight from the host; no signature gate needed.
            import_from_host(shared_root, host_store_dir, path, name)?;
        } else if test_mode() {
            // Stub the gate + nix promotion with a tree copy; the hardlink
            // collapse below still runs for real.
            copy_tree(&src, &shared_path).with_context(|| format!("test-mode promote of {path}"))?;
        } else {
            // Cave content crossing the quarantine boundary: only content-
            // addressed or trusted-signed closures cross. An unsigned local
            // build stays private to the cave (a normal no-cross). A verify
            // that couldn't even *run* is a real failure, propagated.
            if !verify_authentic(&state.cave_root, path)? {
                return Ok(());
            }
            promote_via_export(&state.cave_root, shared_root, path)?;
        }
    }

    // 1b. Repair the shared copy if the store's DB claims it but the file is
    //     gone (see repair_shared_if_missing). Source: the host copy when the
    //     host has it, else the (already gate-verified) cave copy.
    let source = if host_has { host_path.as_path() } else { src.as_path() };
    repair_shared_if_missing(name, &shared_path, source)?;

    // 2. Relink the shared copy onto the host inode so host and shared share one
    //    copy. Skipped once EXDEV proves they're on different filesystems.
    if host_has && !HOST_RELINK_DISABLED.load(Ordering::Relaxed) {
        if let Err(e) = collapse_onto(&shared_path, &host_path) {
            if is_exdev(&e) {
                HOST_RELINK_DISABLED.store(true, Ordering::Relaxed);
                eprintln!(
                    "cav-store: host /nix/store and the shared store are on different \
                     filesystems; host<->shared dedup disabled (cross-cave dedup unaffected)"
                );
            } else {
                return Err(e);
            }
        }
    }

    // 3. Collapse the cave's own copy onto the shared inode (which is the host
    //    inode after step 2). Always done, even when the path was already
    //    shared — that is what dedups a path two caves obtained independently.
    //    The canonical inode is root-owned, so the cave can't rewrite it.
    collapse_onto(&src, &shared_path)
}

/// Import a path and its closure from the host store into the shared store.
/// The host store is trusted (root-managed level 0), so no signature gate. Root
/// runs this: the shared store is root-owned, which is the ownership `nix copy`
/// into a local store wants. Test mode stubs `nix` with a tree copy.
fn import_from_host(
    shared_root: &Path,
    host_store_dir: &Path,
    path: &str,
    name: &OsStr,
) -> Result<()> {
    if test_mode() {
        let shared_store_dir = shared_root.join("nix").join("store");
        std::fs::create_dir_all(&shared_store_dir).ok();
        return copy_tree(&host_store_dir.join(name), &shared_store_dir.join(name))
            .with_context(|| format!("test-mode import of {path}"));
    }
    let shared_uri = format!("local?root={}", shared_root.display());
    let out = Command::new("nix")
        .args([
            "--extra-experimental-features",
            "nix-command",
            "copy",
            "--no-check-sigs",
            "--to",
            &shared_uri,
            path,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .env("NIX_REMOTE", "")
        .output()
        .context("spawn nix copy host -> shared")?;
    if !out.status.success() {
        bail!("nix copy of {path} host -> shared: {}", String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(())
}

fn is_exdev(e: &anyhow::Error) -> bool {
    e.chain().any(|c| {
        c.downcast_ref::<std::io::Error>()
            .and_then(|io| io.raw_os_error())
            == Some(libc_exdev())
    })
}

/// EXDEV without a libc dep: it is 18 on Linux.
fn libc_exdev() -> i32 {
    18
}

/// Recursively copy a tree (test-mode stand-in for `nix copy`).
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

/// Repair a shared copy that the shared store's SQLite DB claims is valid but
/// whose file is gone — crash residue from a store manager that died mid-work,
/// leaving the DB row while the file was deleted. nix's `copy`/`--import` trust
/// the DB and no-op on such a path, so the file never returns on its own; copy
/// the canonical bytes into place instead (the DB row is already right, only
/// disk lagged). The shared store is our own level-1 cache, so this is a repair
/// we own, not a fatal external error. No-op when the copy is already present.
fn repair_shared_if_missing(name: &OsStr, shared_path: &Path, source: &Path) -> Result<()> {
    if shared_path.exists() {
        return Ok(());
    }
    eprintln!(
        "cav-store: repairing shared copy of {} (DB-valid but file missing)",
        name.to_string_lossy()
    );
    copy_tree(source, shared_path)
        .with_context(|| format!("repair missing shared copy {shared_path:?} from {source:?}"))
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

/// A `Command` for `bin` that runs as the cave store's owning user, with a
/// HOME nix can write. The cave store belongs to the unprivileged user that
/// launched the cave; the manager runs as root. nix opening a writable local
/// store as root rewrites its dir to root:nixbld 1775 (its `/nix/store`
/// ownership contract), seizing the store from its owner. Running as the owner
/// (non-root) gives nix no root to chown to, so the store is left alone.
fn nix_as_owner(bin: &str, cave_root: &Path) -> Result<Command> {
    use std::os::unix::fs::MetadataExt;
    use std::os::unix::process::CommandExt;
    let md = cave_root
        .metadata()
        .with_context(|| format!("stat {}", cave_root.display()))?;
    let mut cmd = Command::new(bin);
    cmd.uid(md.uid())
        .gid(md.gid())
        // root's HOME (/root) is unreadable to the dropped uid; point nix at
        // the owner-owned cave dir so any cache/config write it attempts lands
        // somewhere writable.
        .env("HOME", cave_root)
        .env("NIX_REMOTE", "");
    Ok(cmd)
}

/// Verify a cave store path AND its whole closure are authentic: every path is
/// content-addressed (the name binds the content) or carries a trusted-key
/// signature, and hashes to its recorded value. An agent can forge neither, so
/// this is the anti-poisoning gate. Recursive (`-r`) because promotion exports
/// the closure unsigned-checked; a signed leaf with an unsigned dependency
/// must not pass. Run as the cave owner so the read doesn't seize the store.
fn verify_authentic(cave_root: &Path, store_path: &str) -> Result<bool> {
    let cave_uri = format!("local?root={}", cave_root.display());
    let status = nix_as_owner("nix", cave_root)?
        .args([
            "--extra-experimental-features",
            "nix-command",
            "store",
            "verify",
            "--store",
            &cave_uri,
            "--sigs-needed",
            "1",
            "-r",
            store_path,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .context("spawn nix store verify")?;
    Ok(status.success())
}

/// Promote a path's closure from a cave store into the shared store without
/// the manager ever opening the cave store as root (which would seize it).
/// Export the closure as the cave's owner (non-root: no canonicalize) and pipe
/// it to `nix-store --import` into the root-owned shared store as root. Import
/// does not re-check signatures, which is safe because `verify_authentic` has
/// already verified the whole closure is authentic.
fn promote_via_export(cave_root: &Path, shared_root: &Path, store_path: &str) -> Result<()> {
    let cave_uri = format!("local?root={}", cave_root.display());
    let shared_uri = format!("local?root={}", shared_root.display());

    // The closure, topologically ordered (deps before dependents) so import
    // never sees a dependent before its dependency. Read as the owner.
    let qr = nix_as_owner("nix-store", cave_root)?
        .args(["--store", &cave_uri, "-qR", store_path])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .context("spawn nix-store -qR")?;
    if !qr.status.success() {
        bail!("nix-store -qR: {}", String::from_utf8_lossy(&qr.stderr).trim());
    }
    let closure: Vec<String> = String::from_utf8_lossy(&qr.stdout)
        .lines()
        .map(|s| s.to_string())
        .collect();
    if closure.is_empty() {
        bail!("empty closure for {store_path}");
    }

    // export (owner) | import (root). Export stderr inherits to the journal;
    // its stdout is the NAR stream piped straight into import's stdin.
    let mut export = nix_as_owner("nix-store", cave_root)?
        .args(["--store", &cave_uri, "--export"])
        .args(&closure)
        .stdout(Stdio::piped())
        .spawn()
        .context("spawn nix-store --export")?;
    let export_out = export.stdout.take().expect("export stdout piped");

    let import = Command::new("nix-store")
        .args(["--store", &shared_uri, "--import"])
        .stdin(export_out)
        .stdout(Stdio::null())
        .env("NIX_REMOTE", "")
        .status()
        .context("spawn nix-store --import")?;
    let export_status = export.wait().context("wait nix-store --export")?;

    if !export_status.success() {
        bail!("nix-store --export failed (see log)");
    }
    if !import.success() {
        bail!("nix-store --import into shared store failed (see log)");
    }
    Ok(())
}

/// Collapse the `dest` store path onto the `canonical` copy so the two share
/// one inode on disk: every regular file under `dest` is replaced by a hardlink
/// to its `canonical` counterpart. Used for both cave->shared and shared->host
/// collapse. Idempotent: a file already on the canonical inode is left alone
/// (the same-inode skip), so re-running a pass is cheap. `canonical` must be on
/// the same filesystem as `dest`; a cross-device link surfaces as EXDEV, which
/// the host-relink caller catches. Symlinks aren't hardlinkable portably, so
/// they're left in place (negligible disk).
fn collapse_onto(dest: &Path, canonical: &Path) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    fn link_tree(dest: &Path, canonical: &Path) -> std::io::Result<()> {
        let meta = canonical.symlink_metadata()?;
        if meta.is_dir() && !meta.file_type().is_symlink() {
            for entry in std::fs::read_dir(canonical)? {
                let entry = entry?;
                link_tree(&dest.join(entry.file_name()), &entry.path())?;
            }
            return Ok(());
        }
        if meta.file_type().is_symlink() {
            return Ok(());
        }
        // Already the same inode? Nothing to do — this is what makes re-walks
        // cheap and the operation idempotent.
        if let Ok(dm) = dest.symlink_metadata() {
            if !dm.file_type().is_symlink() && dm.ino() == meta.ino() && dm.dev() == meta.dev() {
                return Ok(());
            }
        }
        let _ = std::fs::remove_file(dest);
        std::fs::hard_link(canonical, dest)
    }
    link_tree(dest, canonical)
        .with_context(|| format!("hardlink {dest:?} <- {canonical:?}"))
}

/// `cav-store gc`: one-shot garbage collection of the shared store. Roots
/// every path any cave still references, then collects the rest.
fn gc_shared_store() -> Result<()> {
    if !test_mode() && !rustix::process::geteuid().is_root() {
        bail!("cav-store gc must be run as root");
    }
    gc_with_base(&store_paths::cave_stores_base())
}

/// Read a cave's valid paths, retrying the transient sqlite "unable to open"
/// (SQLITE_CANTOPEN) that happens when the cave is mid-write and its DB has a
/// hot WAL. A read that never succeeds is a hard error the caller must respect:
/// GC cannot safely collect without this cave's references. Bounded so a truly
/// broken DB surfaces loudly instead of looping forever.
fn read_valid_paths_retrying(db: &Path) -> Result<std::collections::HashSet<String>> {
    let mut last: Option<anyhow::Error> = None;
    for attempt in 0..RVP_RETRIES {
        match nix_db::valid_paths(db) {
            Ok(paths) => return Ok(paths),
            Err(e) => {
                last = Some(e);
                if attempt + 1 < RVP_RETRIES {
                    std::thread::sleep(RVP_RETRY_DELAY);
                }
            }
        }
    }
    Err(last.expect("at least one attempt ran"))
}

/// Root every shared-store path that any cave references, returning the count.
/// Fails closed: an unreadable cave DB (after retries) or an unreadable store
/// base returns Err rather than a partial set, so the caller never collects
/// with incomplete knowledge of what is in use. Roots are plain symlinks under
/// the shared store's gcroots dir (the "any symlink in gcroots is a root"
/// mechanism); duplicate names across caves just fail the second symlink.
fn root_referenced_paths(
    store_base: &Path,
    shared_store_dir: &Path,
    gcroots: &Path,
) -> Result<u64> {
    let mut rooted = 0u64;
    let entries = std::fs::read_dir(store_base)
        .with_context(|| format!("read cave store base {}", store_base.display()))?;
    for entry in entries.flatten() {
        let Some(id) = agent_id_from_name(&entry.file_name()) else {
            continue;
        };
        // No DB yet (cave mid-setup): nothing valid to reference, safe to skip.
        let Some(state) = AgentState::new(store_base, &id) else {
            continue;
        };
        let paths = read_valid_paths_retrying(&state.db_path).with_context(|| {
            format!("read cave db for agent-{id}; refusing to GC with incomplete roots")
        })?;
        for p in paths {
            let name = nix_db::store_name(&p);
            let target = shared_store_dir.join(&name);
            if !target.exists() {
                continue;
            }
            let _ = std::os::unix::fs::symlink(&target, gcroots.join(&name));
            rooted += 1;
        }
    }
    Ok(rooted)
}

/// Garbage-collect the shared store, first rooting every store path any cave
/// still references so a path in use is never removed. Conservative: roots all
/// caves' paths, not just running ones, so a shared path is collected only once
/// no cave references it at all, and refuses to collect at all if any cave's
/// references can't be read (see root_referenced_paths).
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

    // Root every path any cave references BEFORE collecting. This fails closed:
    // if a cave DB can't be read we have incomplete knowledge of what is in
    // use, so we must not run nix-store --gc — doing so would delete a path a
    // cave still references, and the later re-import lands under a new inode
    // that strands every cave already collapsed onto the old one. Bail (retried
    // next interval) instead.
    let rooted = root_referenced_paths(store_base, &shared_store_dir, &gcroots)?;

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

/// Test-only escape hatch: skips the root check and stubs the nix shell-outs
/// (verify, import, promote, GC) with tree copies. The hardlink collapse still
/// runs for real, so tests cover the dedup path that production depends on.
/// Production never sets this.
fn test_mode() -> bool {
    std::env::var_os("CODING_CAVE_HOST_WATCH_TEST_MODE").is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Write a minimal nix ValidPaths DB holding the given store paths.
    fn write_valid_paths_db(db: &Path, store_paths: &[&str]) {
        let conn = rusqlite::Connection::open(db).unwrap();
        conn.execute_batch(
            "CREATE TABLE ValidPaths (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                hash TEXT NOT NULL,
                registrationTime INTEGER NOT NULL,
                deriver TEXT, narSize INTEGER, ultimate INTEGER DEFAULT 0,
                sigs TEXT, ca TEXT
            );",
        )
        .unwrap();
        for p in store_paths {
            conn.execute(
                "INSERT INTO ValidPaths (path, hash, registrationTime) VALUES (?1, 'sha256:0', 0)",
                rusqlite::params![p],
            )
            .unwrap();
        }
    }

    /// An unreadable cave DB (here: corrupt bytes where db.sqlite is expected)
    /// must make rooting fail closed — the caller must NOT then run GC, because
    /// deleting a path this cave references is exactly what strands caves. This
    /// is the regression guard for the confirmed root cause.
    #[test]
    fn gc_rooting_fails_closed_when_a_cave_db_is_unreadable() {
        let tmp = std::env::temp_dir().join(format!("cav-gc-closed-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let store_base = tmp.join("nix-stores");
        let shared_store_dir = tmp.join("shared/nix/store");
        let gcroots = tmp.join("shared/nix/var/nix/gcroots/cav");
        std::fs::create_dir_all(&shared_store_dir).unwrap();
        std::fs::create_dir_all(&gcroots).unwrap();
        let pkg = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-pkg";
        std::fs::create_dir_all(shared_store_dir.join(pkg)).unwrap();

        // A healthy cave referencing the shared path.
        let good = store_base.join("agent-1/nix/var/nix/db");
        std::fs::create_dir_all(&good).unwrap();
        write_valid_paths_db(&good.join("db.sqlite"), &[&format!("/nix/store/{pkg}")]);

        // A cave whose DB can't be read (not a valid sqlite file).
        let bad = store_base.join("agent-2/nix/var/nix/db");
        std::fs::create_dir_all(&bad).unwrap();
        std::fs::write(bad.join("db.sqlite"), b"not a database").unwrap();

        let result = root_referenced_paths(&store_base, &shared_store_dir, &gcroots);
        assert!(
            result.is_err(),
            "rooting must fail closed on an unreadable cave DB, not return partial roots",
        );

        // And with all caves readable, it succeeds and roots the shared path.
        std::fs::remove_dir_all(store_base.join("agent-2")).unwrap();
        let _ = std::fs::remove_dir_all(&gcroots);
        std::fs::create_dir_all(&gcroots).unwrap();
        let rooted = root_referenced_paths(&store_base, &shared_store_dir, &gcroots).unwrap();
        assert_eq!(rooted, 1, "the healthy cave's shared path should be rooted");
        assert!(gcroots.join(pkg).exists(), "root symlink not created");

        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Build a cave with a store dir and a gc.lock, returning its AgentState.
    fn make_cave(cave_root: &Path) -> AgentState {
        let store_dir = cave_root.join("nix").join("store");
        std::fs::create_dir_all(&store_dir).unwrap();
        std::fs::create_dir_all(cave_root.join("nix/var/nix")).unwrap();
        std::fs::write(cave_root.join("nix/var/nix/gc.lock"), b"").unwrap();
        AgentState {
            agent_id: cave_root.file_name().unwrap().to_string_lossy().into_owned(),
            store_dir,
            cave_root: cave_root.to_path_buf(),
            db_path: cave_root.join("nix/var/nix/db/db.sqlite"),
        }
    }

    /// Give a cave its own physical copy of a one-file package.
    fn install_in_cave(state: &AgentState, pkg: &str, content: &[u8]) {
        std::fs::create_dir_all(state.store_dir.join(pkg)).unwrap();
        std::fs::write(state.store_dir.join(pkg).join("content"), content).unwrap();
    }

    fn ino(p: &Path) -> u64 {
        use std::os::unix::fs::MetadataExt;
        std::fs::metadata(p).unwrap().ino()
    }

    /// The core dedup: two caves that both hold a path already in the shared
    /// store both collapse onto the one shared inode. Runs the real hardlink
    /// (no nix, no host), which is the path production depends on.
    #[test]
    fn converge_collapses_two_caves_onto_one_shared_inode() {
        let tmp = std::env::temp_dir().join(format!("cav-collapse2-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let shared_root = tmp.join("shared");
        let shared_store_dir = shared_root.join("nix").join("store");
        let host_store_dir = tmp.join("no-host"); // host has nothing.
        let host_valid = HashSet::new();
        let pkg = "cccccccccccccccccccccccccccccccc-pkg";
        std::fs::create_dir_all(shared_store_dir.join(pkg)).unwrap();
        std::fs::write(shared_store_dir.join(pkg).join("content"), b"data").unwrap();
        let shared_ino = ino(&shared_store_dir.join(pkg).join("content"));
        let path = format!("/nix/store/{pkg}");

        for agent in ["agent-1", "agent-2"] {
            let state = make_cave(&tmp.join(agent));
            install_in_cave(&state, pkg, b"data");
            converge(&state, &shared_root, &shared_store_dir, &host_store_dir,
                     &host_valid, &path, OsStr::new(pkg)).unwrap();
            assert_eq!(
                ino(&state.store_dir.join(pkg).join("content")),
                shared_ino,
                "{agent}: cave copy was not collapsed onto the shared inode",
            );
        }
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Convergence is idempotent: a second pass over an already-collapsed cave
    /// changes nothing (same inode, no error). The same-inode skip is what makes
    /// the timer loop cheap.
    #[test]
    fn converge_is_idempotent() {
        let tmp = std::env::temp_dir().join(format!("cav-idem-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let shared_root = tmp.join("shared");
        let shared_store_dir = shared_root.join("nix").join("store");
        let host_store_dir = tmp.join("no-host");
        let host_valid = HashSet::new();
        let pkg = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-pkg";
        std::fs::create_dir_all(shared_store_dir.join(pkg)).unwrap();
        std::fs::write(shared_store_dir.join(pkg).join("content"), b"data").unwrap();
        let shared_ino = ino(&shared_store_dir.join(pkg).join("content"));
        let path = format!("/nix/store/{pkg}");

        let state = make_cave(&tmp.join("agent-1"));
        install_in_cave(&state, pkg, b"data");
        let run = || converge(&state, &shared_root, &shared_store_dir, &host_store_dir,
                              &host_valid, &path, OsStr::new(pkg));
        run().unwrap();
        run().unwrap(); // second pass must be a no-op, not re-link or error.
        assert_eq!(ino(&state.store_dir.join(pkg).join("content")), shared_ino);
        assert_eq!(ino(&shared_store_dir.join(pkg).join("content")), shared_ino);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Self-heal: if the shared inode moves (a package deleted then re-created
    /// under a new inode), the next converge re-collapses the cave onto it. This
    /// is the failure the old one-shot cursor could never recover from — a cave
    /// stranded on the dead inode kept a full private copy forever.
    #[test]
    fn converge_reheals_after_the_shared_inode_moves() {
        let tmp = std::env::temp_dir().join(format!("cav-heal-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let shared_root = tmp.join("shared");
        let shared_store_dir = shared_root.join("nix").join("store");
        let host_store_dir = tmp.join("no-host");
        let host_valid = HashSet::new();
        let pkg = "ffffffffffffffffffffffffffffffff-pkg";
        let content = shared_store_dir.join(pkg).join("content");
        std::fs::create_dir_all(shared_store_dir.join(pkg)).unwrap();
        std::fs::write(&content, b"data").unwrap();
        let path = format!("/nix/store/{pkg}");

        let state = make_cave(&tmp.join("agent-1"));
        install_in_cave(&state, pkg, b"data");
        let run = || converge(&state, &shared_root, &shared_store_dir, &host_store_dir,
                              &host_valid, &path, OsStr::new(pkg));
        run().unwrap();
        assert_eq!(ino(&state.store_dir.join(pkg).join("content")), ino(&content));

        // Move the shared inode: delete + re-create with fresh bytes. The cave's
        // hardlink now points at the dead (old) inode.
        std::fs::remove_file(&content).unwrap();
        std::fs::write(&content, b"data").unwrap();
        let new_ino = ino(&content);
        assert_ne!(ino(&state.store_dir.join(pkg).join("content")), new_ino,
                   "precondition: cave should be stranded on the old inode");

        run().unwrap();
        assert_eq!(ino(&state.store_dir.join(pkg).join("content")), new_ino,
                   "cave did not re-collapse onto the moved shared inode");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Three-level dedup: when the host has the path, host + shared + cave all
    /// end on the host inode, and the host copy is never written (quarantine).
    /// Shared is pre-populated so no nix shell-out is needed.
    #[test]
    fn converge_dedups_host_shared_and_cave_without_touching_host() {
        let tmp = std::env::temp_dir().join(format!("cav-host3-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let shared_root = tmp.join("shared");
        let shared_store_dir = shared_root.join("nix").join("store");
        let host_store_dir = tmp.join("host/nix/store");
        let pkg = "11111111111111111111111111111111-pkg";
        let path = format!("/nix/store/{pkg}");

        // Host copy (canonical) — three distinct physical copies to start.
        std::fs::create_dir_all(host_store_dir.join(pkg)).unwrap();
        std::fs::write(host_store_dir.join(pkg).join("content"), b"data").unwrap();
        let host_ino = ino(&host_store_dir.join(pkg).join("content"));
        std::fs::create_dir_all(shared_store_dir.join(pkg)).unwrap();
        std::fs::write(shared_store_dir.join(pkg).join("content"), b"data").unwrap();

        let mut host_valid = HashSet::new();
        host_valid.insert(path.clone());

        let state = make_cave(&tmp.join("agent-1"));
        install_in_cave(&state, pkg, b"data");
        converge(&state, &shared_root, &shared_store_dir, &host_store_dir,
                 &host_valid, &path, OsStr::new(pkg)).unwrap();

        assert_eq!(ino(&shared_store_dir.join(pkg).join("content")), host_ino,
                   "shared not relinked onto the host inode");
        assert_eq!(ino(&state.store_dir.join(pkg).join("content")), host_ino,
                   "cave not collapsed onto the host inode");
        // Quarantine: the host inode is unchanged and its bytes intact — the
        // daemon only ever reads the host store.
        assert_eq!(ino(&host_store_dir.join(pkg).join("content")), host_ino);
        assert_eq!(std::fs::read(host_store_dir.join(pkg).join("content")).unwrap(), b"data");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Derivations never converge: a .drv import makes nix demand the whole
    /// input-drv closure be valid in the shared store, which caves with GC'd
    /// inputs can't guarantee — the failure mode that flooded the journal on
    /// first deploy. The .drv must be left untouched even when the shared
    /// store already has it, while a normal path in the same pass converges.
    #[test]
    fn converge_cave_skips_derivations() {
        let tmp = std::env::temp_dir().join(format!("cav-drv-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let shared_root = tmp.join("shared");
        let shared_store_dir = shared_root.join("nix").join("store");
        let host_store_dir = tmp.join("no-host");
        let host_valid = HashSet::new();
        let drv = "dddddddddddddddddddddddddddddddd-pkg.drv";
        let pkg = "22222222222222222222222222222222-pkg";
        for name in [drv, pkg] {
            std::fs::create_dir_all(shared_store_dir.join(name)).unwrap();
            std::fs::write(shared_store_dir.join(name).join("content"), b"data").unwrap();
        }

        let state = make_cave(&tmp.join("agent-1"));
        install_in_cave(&state, drv, b"data");
        install_in_cave(&state, pkg, b"data");
        std::fs::create_dir_all(state.db_path.parent().unwrap()).unwrap();
        write_valid_paths_db(
            &state.db_path,
            &[&format!("/nix/store/{drv}"), &format!("/nix/store/{pkg}")],
        );
        let drv_ino = ino(&state.store_dir.join(drv).join("content"));

        converge_cave(&state, &shared_root, &shared_store_dir,
                      &host_store_dir, &host_valid).unwrap();
        assert_eq!(ino(&state.store_dir.join(drv).join("content")), drv_ino,
                   "a .drv must stay cave-private, not collapse onto shared");
        assert_eq!(ino(&state.store_dir.join(pkg).join("content")),
                   ino(&shared_store_dir.join(pkg).join("content")),
                   "the normal path in the same pass should still converge");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Fail-fast: the first path that errors aborts the cave's pass with Err —
    /// the daemon must exit on a serious error, not blacklist the path and
    /// carry on. Once the cause is gone the next run converges normally.
    #[test]
    fn converge_cave_fails_fast_on_the_first_error() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = std::env::temp_dir().join(format!("cav-failfast-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let shared_root = tmp.join("shared");
        let shared_store_dir = shared_root.join("nix").join("store");
        let host_store_dir = tmp.join("no-host");
        let host_valid = HashSet::new();
        let pkg = "33333333333333333333333333333333-pkg";
        std::fs::create_dir_all(shared_store_dir.join(pkg)).unwrap();
        std::fs::write(shared_store_dir.join(pkg).join("content"), b"data").unwrap();

        let state = make_cave(&tmp.join("agent-1"));
        install_in_cave(&state, pkg, b"data");
        std::fs::create_dir_all(state.db_path.parent().unwrap()).unwrap();
        write_valid_paths_db(&state.db_path, &[&format!("/nix/store/{pkg}")]);
        // Read-only package dir: the collapse can't remove the cave's file, so
        // converge fails (the test runs unprivileged).
        let chmod = |mode: u32| {
            std::fs::set_permissions(state.store_dir.join(pkg),
                                     std::fs::Permissions::from_mode(mode)).unwrap();
        };
        let run = || converge_cave(&state, &shared_root, &shared_store_dir,
                                   &host_store_dir, &host_valid);
        chmod(0o555);
        assert!(run().is_err(), "a failing path must abort the pass with Err");

        chmod(0o755);
        run().unwrap();
        assert_eq!(ino(&state.store_dir.join(pkg).join("content")),
                   ino(&shared_store_dir.join(pkg).join("content")),
                   "with the cause gone the next run should converge");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// The deploy-day crash: the shared store's DB claimed a path valid but its
    /// file was gone (crash residue), so `nix copy` no-op'd and the later
    /// hardlink hit ENOENT. repair_shared_if_missing must restore the bytes from
    /// the canonical source (file and directory paths), and no-op when present.
    #[test]
    fn repair_restores_a_shared_copy_the_db_claims_but_disk_lacks() {
        let tmp = std::env::temp_dir().join(format!("cav-repair-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let shared = tmp.join("shared/nix/store");
        let src = tmp.join("host/nix/store");
        std::fs::create_dir_all(&shared).unwrap();
        std::fs::create_dir_all(&src).unwrap();

        // A single-FILE store path (like the CVE patch that crashed the daemon).
        let patch = "1cl09g0szzjgchiy7dz2xs8w5nqi2004-CVE.patch";
        std::fs::write(src.join(patch), b"diff --git").unwrap();
        // Shared DB "claims" it (represented here by the file simply being
        // absent on disk); repair must copy it in.
        repair_shared_if_missing(OsStr::new(patch), &shared.join(patch), &src.join(patch)).unwrap();
        assert!(shared.join(patch).exists(), "single-file shared copy not repaired");
        assert_eq!(std::fs::read(shared.join(patch)).unwrap(), b"diff --git");

        // A directory store path repairs its whole tree.
        let pkg = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-pkg";
        std::fs::create_dir_all(src.join(pkg).join("bin")).unwrap();
        std::fs::write(src.join(pkg).join("bin").join("hi"), b"exe").unwrap();
        repair_shared_if_missing(OsStr::new(pkg), &shared.join(pkg), &src.join(pkg)).unwrap();
        assert_eq!(std::fs::read(shared.join(pkg).join("bin").join("hi")).unwrap(), b"exe");

        // Present already: a no-op that neither errors nor overwrites.
        std::fs::write(shared.join(patch), b"KEEP").unwrap();
        repair_shared_if_missing(OsStr::new(patch), &shared.join(patch), &src.join(patch)).unwrap();
        assert_eq!(std::fs::read(shared.join(patch)).unwrap(), b"KEEP",
                   "repair must not overwrite an existing shared copy");
        let _ = std::fs::remove_dir_all(&tmp);
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
        };
        assert_eq!(s.cave_root, Path::new("/x/store/agent-7"));
        assert_eq!(s.store_dir, Path::new("/x/store/agent-7/nix/store"));
    }
}
