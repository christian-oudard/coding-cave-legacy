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

pub fn dir_size(path: &Path) -> u64 {
    let mut seen = std::collections::HashSet::new();
    dir_size_dedup(path, &mut seen)
}

/// Walks a tree, summing file sizes once per unique (dev, inode). Nix
/// store paths and `.git/objects` packs hard-link the same content into
/// many directory entries; counting each link would inflate the total
/// (in some overlays by 2–3×), which is what gc used to "Reclaim".
fn dir_size_dedup(path: &Path, seen: &mut std::collections::HashSet<(u64, u64)>) -> u64 {
    use std::os::unix::fs::MetadataExt;
    let mut total = 0u64;
    let Ok(entries) = std::fs::read_dir(path) else {
        return 0;
    };
    for entry in entries.flatten() {
        let Ok(meta) = entry.metadata() else { continue };
        if meta.is_symlink() {
            total += meta.len();
        } else if meta.is_dir() {
            total += dir_size_dedup(&entry.path(), seen);
        } else if meta.nlink() > 1 {
            if seen.insert((meta.dev(), meta.ino())) {
                total += meta.len();
            }
        } else {
            total += meta.len();
        }
    }
    total
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

fn nix_overlay_dir(agent_id: &str) -> PathBuf {
    workspace::nix_overlay_base_for(&config::persistent_home(), agent_id)
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
        let overlay = nix_overlay_dir(&aid);
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
    let overlay = nix_overlay_dir(&c.agent_id);
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
    /// (path, size) — paths under `<home>/.../nix-overlay/agent-<X>`
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

    let overlay_root = config::persistent_home()
        .join(".local")
        .join("share")
        .join("coding-cave")
        .join("nix-overlay");
    if let Ok(entries) = std::fs::read_dir(&overlay_root) {
        for entry in entries.flatten() {
            if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            let Some(key) = name.strip_prefix("agent-") else { continue };
            if !legit_overlay.contains(key) {
                let p = entry.path();
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
        println!("{}", style::bold("Orphan nix-overlay dirs:"));
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

        let nix_root = config::persistent_home()
            .join(".local")
            .join("share")
            .join("coding-cave")
            .join("nix-overlay");
        fs::create_dir_all(nix_root.join(format!("agent-{aid}"))).unwrap();
        let orphan_overlay = nix_root.join("agent-deadbeef");
        fs::create_dir_all(&orphan_overlay).unwrap();

        let o = find_orphans();

        let ws_paths: Vec<_> = o.worktrees.iter().map(|(p, _)| p.clone()).collect();
        assert_eq!(ws_paths, vec![orphan_ws], "wrong worktree orphans");

        let ov_paths: Vec<_> = o.overlays.iter().map(|(p, _)| p.clone()).collect();
        assert_eq!(ov_paths, vec![orphan_overlay], "wrong overlay orphans");

        assert_eq!(o.empty_slug_dirs, vec![empty_slug]);
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

        // Without dedup this would be ~3000. With dedup, exactly one
        // 1000-byte count plus the surrounding directory inode (which
        // we don't add). Allow slack for filesystem-reported size.
        assert!(
            total < 2000,
            "expected dedup'd total near 1000, got {total}"
        );
        assert!(total >= 1000, "expected at least one copy counted, got {total}");
    }
}
