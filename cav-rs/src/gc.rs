use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Result};

use crate::config;
use crate::git;
use crate::registry;
use crate::style;
use crate::workspace;

const DEFAULT_OLDER_THAN: &str = "24h";

/// One paused agent's GC-able on-disk footprint.
pub struct Candidate {
    pub agent_id: String,
    /// (slug, project_base) for the agent's primary and extra projects.
    pub targets: Vec<(String, PathBuf)>,
    /// Sum of worktree + nix overlay sizes, in bytes.
    pub size: u64,
}

pub fn default_older_than() -> &'static str {
    DEFAULT_OLDER_THAN
}

pub fn parse_older_than(s: &str) -> Result<Duration> {
    let s = s.trim();
    if s.is_empty() {
        bail!("empty duration; try e.g. 24h, 7d, 1w");
    }
    let split_at = s
        .find(|c: char| !c.is_ascii_digit() && c != '.')
        .unwrap_or(s.len());
    let (num, unit) = s.split_at(split_at);
    let num: f64 = num
        .parse()
        .map_err(|_| anyhow::anyhow!("not a number: '{num}'"))?;
    let mul = match unit {
        "" | "s" => 1.0,
        "m" => 60.0,
        "h" => 3600.0,
        "d" => 86400.0,
        "w" => 604800.0,
        other => bail!("unknown duration unit '{other}'; use s, m, h, d, or w"),
    };
    Ok(Duration::from_secs_f64(num * mul))
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// On-disk size of a tree, summing `st_blocks * 512` (matches `du -s`)
/// so hardlinked content shared into the store still gets credited once
/// — and only once: the shared `seen` set keeps a (dev, inode) ledger.
/// Subdirectories fan out onto rayon's pool so a 17 GB nix-overlay
/// finishes in roughly real-time-divided-by-cores instead of pinning
/// one thread.
pub fn dir_size(path: &Path) -> u64 {
    use rayon::prelude::*;
    use std::os::unix::fs::MetadataExt;

    let seen: std::sync::Mutex<std::collections::HashSet<(u64, u64)>> =
        std::sync::Mutex::new(std::collections::HashSet::new());

    fn walk(
        path: &Path,
        seen: &std::sync::Mutex<std::collections::HashSet<(u64, u64)>>,
    ) -> u64 {
        let Ok(entries) = std::fs::read_dir(path) else { return 0 };
        let entries: Vec<_> = entries.flatten().collect();
        entries
            .par_iter()
            .map(|entry| {
                let Ok(meta) = entry.metadata() else { return 0 };
                if meta.file_type().is_symlink() {
                    return meta.blocks() * 512;
                }
                if meta.is_dir() {
                    return walk(&entry.path(), seen);
                }
                if meta.nlink() > 1 {
                    let mut g = seen.lock().unwrap();
                    if !g.insert((meta.dev(), meta.ino())) {
                        return 0;
                    }
                }
                meta.blocks() * 512
            })
            .sum()
    }

    walk(path, &seen)
}

pub fn format_size(bytes: u64) -> String {
    const KB: u64 = 1024;
    const MB: u64 = 1024 * KB;
    const GB: u64 = 1024 * MB;
    if bytes >= GB {
        format!("{:.1} GB", bytes as f64 / GB as f64)
    } else if bytes >= MB {
        format!("{:.1} MB", bytes as f64 / MB as f64)
    } else if bytes >= KB {
        format!("{:.1} KB", bytes as f64 / KB as f64)
    } else {
        format!("{bytes} B")
    }
}

fn cave_store_dir(agent_id: &str) -> PathBuf {
    workspace::cave_store_root_for(agent_id)
}

/// Collect everything currently eligible for `cav gc --older-than <dur>`.
/// An agent is eligible when it's paused (exited_at is set), was last alive
/// at least `older_than` ago, and has any on-disk artifacts still around.
pub fn candidates(older_than: Duration) -> Vec<Candidate> {
    let cutoff = now_secs() - older_than.as_secs_f64();
    let Ok(all) = registry::all_agent_ids(None) else {
        return vec![];
    };
    let mut out = Vec::new();
    for aid in all {
        // Running agents and freshly-paused agents are off limits.
        let exited = registry::agent_exit_time(&aid).ok().flatten();
        let Some(exited_at) = exited else { continue };
        if exited_at > cutoff {
            continue;
        }
        let targets = resolve_targets(&aid);
        let mut size = 0u64;
        for (slug, _) in &targets {
            let ws = workspace::worktree_for(slug, &aid);
            if ws.exists() {
                size += dir_size(&ws);
            }
        }
        let overlay = cave_store_dir(&aid);
        if overlay.exists() {
            size += dir_size(&overlay);
        }
        if size == 0 {
            // Already collected, nothing left to free.
            continue;
        }
        out.push(Candidate {
            agent_id: aid,
            targets,
            size,
        });
    }
    out
}

fn resolve_targets(agent_id: &str) -> Vec<(String, PathBuf)> {
    let slugs = registry::agent_all_slugs(agent_id).unwrap_or_default();
    slugs
        .into_iter()
        .filter_map(|s| {
            registry::project_path(&s)
                .ok()
                .flatten()
                .map(|p| (s, PathBuf::from(p)))
        })
        .collect()
}

/// Remove on-disk artifacts for one agent. Registry row and session log
/// are left in place: the row stays as a tombstone so `cav -r N` produces
/// a useful "this agent was GC'd, session at <path>" message instead of
/// silently re-cloning a fresh worktree.
pub fn collect(c: &Candidate) {
    // Final safety check against a stale "exited" record: never reclaim an
    // agent whose cave is still mounting its overlay or a worktree. Removing
    // those strands the live cave's bind-mounted nix DB.
    let overlay = cave_store_dir(&c.agent_id);
    let live = dir_in_use_by_mount(&overlay)
        || c.targets
            .iter()
            .any(|(slug, _)| dir_in_use_by_mount(&workspace::worktree_for(slug, &c.agent_id)));
    if live {
        eprintln!(
            "cav gc: skipping agent-{} — its cave is still running",
            c.agent_id
        );
        return;
    }
    workspace::reclaim(&c.agent_id, &c.targets);
    // Also drop the host-side local agent branches; workspace::reclaim
    // only touches the bare repo's refs.
    for (_, base) in &c.targets {
        let prefix = format!("agent-{}/", c.agent_id);
        if let Ok(local_refs) = git::git_output(
            &[
                "for-each-ref",
                "--format=%(refname:short)",
                &format!("refs/heads/{prefix}"),
            ],
            Some(base),
        ) {
            for br in local_refs.lines() {
                git::git(&["branch", "-D", br], Some(base), false, true, false).ok();
            }
        }
    }
    let overlay = cave_store_dir(&c.agent_id);
    if overlay.exists() {
        workspace::force_remove_dir_all(&overlay);
    }
}

/// Worktree/overlay directories on disk that don't correspond to any
/// registered agent. Surfaced separately from regular candidates because
/// `cav gc` has no idea what they once were — they could be from
/// rename-project leftovers, a manual delete from the registry, or a
/// crashed init. The user gets a confirm prompt before removal.
pub struct Orphans {
    /// (path, size) — paths under `<data_dir>/worktrees/<slug>/<X>`
    /// whose `<slug>, X` pair isn't claimed by any registered agent.
    pub worktrees: Vec<(PathBuf, u64)>,
    /// (path, size) — per-cave store dirs under `<data>/store/agent-<X>`
    /// whose `<X>` isn't a registered agent's uuid or display id.
    pub overlays: Vec<(PathBuf, u64)>,
    /// Empty `<data_dir>/worktrees/<slug>/` dirs — cosmetic, but no
    /// reason to leave them around once their agents are gone.
    pub empty_slug_dirs: Vec<PathBuf>,
}

impl Orphans {
    pub fn is_empty(&self) -> bool {
        self.worktrees.is_empty() && self.overlays.is_empty() && self.empty_slug_dirs.is_empty()
    }
    pub fn total_size(&self) -> u64 {
        self.worktrees.iter().map(|(_, s)| s).sum::<u64>()
            + self.overlays.iter().map(|(_, s)| s).sum::<u64>()
    }
}

/// True if `dir` (or a path beneath it) is an active mount source or target
/// in any process's mount table. A running cave bind-mounts its store dir
/// (`agent-<key>/nix/store` and `agent-<key>/nix/var`) over /nix/store and
/// /nix/var, so its `/proc/<pid>/mountinfo` references these host paths. GC
/// must never remove such a dir: unlinking the inode the cave's nix store or
/// DB is bind-mounted from strands it (link-count 0, then `big-lock: No such
/// file or directory`) and kills nix in the live cave until it restarts.
fn dir_in_use_by_mount(dir: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return false;
    };
    for entry in entries.flatten() {
        // Only numeric pid dirs have a readable mountinfo; the rest just Err.
        if let Ok(data) = std::fs::read_to_string(entry.path().join("mountinfo")) {
            if mountinfo_references(&data, dir) {
                return true;
            }
        }
    }
    false
}

/// Whether a mountinfo dump names `dir` or any path beneath it. The cave's
/// store dir shows up as a bind-source field (`.../agent-<key>/nix/store`),
/// so match it as a substring but require a path boundary after it —
/// otherwise `agent-abc` would spuriously match `agent-abcdef`. The boundary
/// set also covers a path embedded in a comma-joined mount option, so the
/// check is robust to either shape. Split out from the `/proc` walk so the
/// matching is unit-testable.
fn mountinfo_references(mountinfo: &str, dir: &Path) -> bool {
    let needle = dir.to_string_lossy();
    let bytes = mountinfo.as_bytes();
    let mut from = 0;
    while let Some(pos) = mountinfo[from..].find(needle.as_ref()) {
        let end = from + pos + needle.len();
        // A real reference ends at the dir itself or continues with `/`; any
        // other trailing char means we landed inside a longer name.
        if matches!(
            bytes.get(end),
            None | Some(b'/') | Some(b',') | Some(b' ') | Some(b'\t') | Some(b'\n')
        ) {
            return true;
        }
        from += pos + 1;
    }
    false
}

pub fn find_orphans() -> Orphans {
    // Build the legitimate sets from the registry. A worktree subdir is
    // legit if its name matches the owning agent's uuid OR its display
    // id (the latter for pre-uuid-migration agents that settle hasn't
    // touched yet). Same idea for overlay dirs, minus the slug scope.
    let mut legit_wt: std::collections::HashSet<(String, String)> =
        std::collections::HashSet::new();
    let mut legit_overlay: std::collections::HashSet<String> = std::collections::HashSet::new();
    if let Ok(agents) = registry::all_agent_ids(None) {
        for aid in agents {
            let uuid = registry::agent_uuid(&aid).ok().flatten();
            let slugs = registry::agent_all_slugs(&aid).unwrap_or_default();
            for slug in &slugs {
                legit_wt.insert((slug.clone(), aid.clone()));
                if let Some(ref u) = uuid {
                    legit_wt.insert((slug.clone(), u.clone()));
                }
            }
            legit_overlay.insert(aid.clone());
            if let Some(u) = uuid {
                legit_overlay.insert(u);
            }
        }
    }

    let mut out = Orphans {
        worktrees: Vec::new(),
        overlays: Vec::new(),
        empty_slug_dirs: Vec::new(),
    };

    let wt_root = config::data_dir().join("worktrees");
    if let Ok(slug_dirs) = std::fs::read_dir(&wt_root) {
        for slug_entry in slug_dirs.flatten() {
            if !slug_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let slug = slug_entry.file_name().to_string_lossy().to_string();
            let slug_path = slug_entry.path();
            let mut had_any = false;
            if let Ok(agent_dirs) = std::fs::read_dir(&slug_path) {
                for agent_entry in agent_dirs.flatten() {
                    if !agent_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                        continue;
                    }
                    had_any = true;
                    let key = agent_entry.file_name().to_string_lossy().to_string();
                    if !legit_wt.contains(&(slug.clone(), key)) {
                        let p = agent_entry.path();
                        // A live cave still mounting this dir is not an orphan,
                        // even if the registry has lost track of it.
                        if dir_in_use_by_mount(&p) {
                            continue;
                        }
                        let size = dir_size(&p);
                        out.worktrees.push((p, size));
                    }
                }
            }
            if !had_any {
                out.empty_slug_dirs.push(slug_path);
            }
        }
    }

    let store_root = workspace::cave_store_base();
    if let Ok(entries) = std::fs::read_dir(&store_root) {
        for entry in entries.flatten() {
            if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            let Some(key) = name.strip_prefix("agent-") else { continue };
            if !legit_overlay.contains(key) {
                let p = entry.path();
                // A live cave still mounting this overlay is not an orphan,
                // even if the registry has lost track of it.
                if dir_in_use_by_mount(&p) {
                    continue;
                }
                let size = dir_size(&p);
                out.overlays.push((p, size));
            }
        }
    }

    out
}

fn print_orphans(o: &Orphans) {
    if !o.worktrees.is_empty() {
        println!("{}", style::bold("Orphan worktree dirs:"));
        for (p, s) in &o.worktrees {
            println!("  {} ({})", p.display(), style::yellow(&format_size(*s)));
        }
    }
    if !o.overlays.is_empty() {
        println!("{}", style::bold("Orphan cave-store dirs:"));
        for (p, s) in &o.overlays {
            println!("  {} ({})", p.display(), style::yellow(&format_size(*s)));
        }
    }
    if !o.empty_slug_dirs.is_empty() {
        println!(
            "{} (empty)",
            style::bold(&format!("Empty slug dirs: {}", o.empty_slug_dirs.len())),
        );
    }
    println!("Total: {}", style::bold(&format_size(o.total_size())));
}

fn remove_orphans(o: &Orphans) {
    for (p, _) in &o.worktrees {
        workspace::force_remove_dir_all(p);
    }
    for (p, _) in &o.overlays {
        workspace::force_remove_dir_all(p);
    }
    for p in &o.empty_slug_dirs {
        let _ = std::fs::remove_dir(p);
    }
}

fn confirm_orphans() -> bool {
    use std::io::{self, Write};
    print!("Remove these? [y/N] ");
    let _ = io::stdout().flush();
    let mut answer = String::new();
    io::stdin().read_line(&mut answer).unwrap_or(0);
    matches!(answer.trim().to_lowercase().as_str(), "y" | "yes")
}

pub fn run(older_than: Duration, dry_run: bool, yes: bool) -> Result<()> {
    let cands = candidates(older_than);
    let mut total = 0u64;
    for c in &cands {
        let name = registry::display_name(Path::new("/"), &c.agent_id);
        total += c.size;
        if dry_run {
            println!(
                "Would GC {} ({})",
                style::agent_name(&name),
                style::yellow(&format_size(c.size)),
            );
        } else {
            collect(c);
            println!(
                "Collected {} ({})",
                style::agent_name(&name),
                style::yellow(&format_size(c.size)),
            );
        }
    }
    if !cands.is_empty() {
        if dry_run {
            println!("Total: {}", style::bold(&format_size(total)));
        } else {
            println!("Reclaimed: {}", style::bold(&format_size(total)));
        }
    }

    let orphans = find_orphans();
    if orphans.is_empty() {
        if cands.is_empty() {
            println!("Nothing to GC.");
        }
        return Ok(());
    }

    if !cands.is_empty() {
        println!();
    }
    print_orphans(&orphans);
    if dry_run {
        return Ok(());
    }
    if yes || confirm_orphans() {
        remove_orphans(&orphans);
        println!(
            "Removed orphans: {}",
            style::bold(&format_size(orphans.total_size())),
        );
    } else {
        println!("Orphans left in place.");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::DataDirGuard;
    use std::fs;

    #[test]
    fn mountinfo_references_matches_dir_and_descendants_only() {
        let store = Path::new("/home/u/.local/share/coding-cave/store/agent-abc");
        // A live cave binds its store dir over /nix/store and /nix/var.
        let store_bind = "26 25 0:24 / /nix/store rw - none \
            /home/u/.local/share/coding-cave/store/agent-abc/nix/store";
        assert!(mountinfo_references(store_bind, store));
        let var_bind = "30 25 0:24 / /nix/var rw - none \
            /home/u/.local/share/coding-cave/store/agent-abc/nix/var";
        assert!(mountinfo_references(var_bind, store));
        // A sibling agent's mounts must not match (no prefix-bleed onto agent-abcdef).
        let other = "26 25 0:24 / /nix/store rw - none \
            /home/u/.local/share/coding-cave/store/agent-abcdef/nix/store";
        assert!(!mountinfo_references(other, store));
        assert!(!mountinfo_references("", store));
    }

    #[test]
    fn find_orphans_keeps_registered_agent_dirs_drops_strangers() {
        let g = DataDirGuard::new("orphans");
        let base = g.path.join("proj");
        fs::create_dir_all(&base).unwrap();
        let aid = registry::acquire_agent_id(&base).unwrap();
        let uuid = registry::agent_uuid(&aid).ok().flatten().unwrap();

        let wt = g.path.join("worktrees").join("proj");
        // Legit: both keying styles should be recognized.
        fs::create_dir_all(wt.join(&aid)).unwrap();
        fs::create_dir_all(wt.join(&uuid)).unwrap();
        // Orphan: unknown id, has content.
        let orphan_ws = wt.join("999");
        fs::create_dir_all(&orphan_ws).unwrap();
        fs::write(orphan_ws.join("blob"), b"content").unwrap();
        // Empty slug dir, no agents at all.
        let empty_slug = g.path.join("worktrees").join("ghost-slug");
        fs::create_dir_all(&empty_slug).unwrap();

        let store_root = workspace::cave_store_base();
        fs::create_dir_all(store_root.join(format!("agent-{aid}"))).unwrap();
        let orphan_store = store_root.join("agent-deadbeef");
        fs::create_dir_all(&orphan_store).unwrap();

        let o = find_orphans();

        let ws_paths: Vec<_> = o.worktrees.iter().map(|(p, _)| p.clone()).collect();
        assert_eq!(ws_paths, vec![orphan_ws], "wrong worktree orphans");

        let store_paths: Vec<_> = o.overlays.iter().map(|(p, _)| p.clone()).collect();
        assert_eq!(store_paths, vec![orphan_store], "wrong cave-store orphans");

        assert_eq!(o.empty_slug_dirs, vec![empty_slug]);
    }

    #[test]
    #[ignore = "manual bench; pass a path via CAV_DIR_SIZE_BENCH env var"]
    fn dir_size_bench() {
        let Ok(p) = std::env::var("CAV_DIR_SIZE_BENCH") else { return };
        let start = std::time::Instant::now();
        let n = dir_size(Path::new(&p));
        eprintln!("dir_size({p}) = {} in {:?}", format_size(n), start.elapsed());
    }

    #[test]
    fn dir_size_counts_hard_linked_files_once() {
        let tmp = std::env::temp_dir().join(format!("cav-dirsize-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir(&tmp).unwrap();
        let payload = vec![b'x'; 1000];
        let original = tmp.join("orig");
        std::fs::write(&original, &payload).unwrap();
        std::fs::hard_link(&original, tmp.join("link1")).unwrap();
        std::fs::hard_link(&original, tmp.join("link2")).unwrap();

        let total = dir_size(&tmp);
        let _ = std::fs::remove_dir_all(&tmp);

        // dir_size charges `st_blocks * 512`, so a 1000-byte payload
        // rounds up to one filesystem block (typically 4096). Without
        // dedup we'd see ~3× that for the two extra hardlinks; with
        // dedup we get exactly one allocation.
        assert!(
            total <= 8192,
            "expected dedup'd allocation (≤ one fs block, ~4096), got {total}"
        );
        assert!(total >= 1000, "expected at least one copy counted, got {total}");
    }
}
