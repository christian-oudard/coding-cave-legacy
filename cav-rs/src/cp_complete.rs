//! Dynamic shell completion for `cav cp` paths.
//!
//! `cav cp` accepts cave-prefixed paths like `slug:path`, `cave:path`, or
//! `cave@N:path`. Static clap completion can't enumerate registered slugs
//! or list files inside a worktree, so this module does it at runtime.
//! `cav __complete-cp <word>` prints candidates one per line; the
//! generated shell scripts call it for the cp paths argument.

use std::path::{Path, PathBuf};

use crate::config::{persistent_home, worktree_dir};
use crate::registry;

/// Emit completion candidates for a partial `cp` argument. Each candidate
/// is the full word to insert (replacing what the user typed). Directory
/// candidates end with `/` so the user can keep tabbing.
pub fn complete(word: &str) -> Vec<String> {
    match word.find(':') {
        Some(idx) => complete_cave(word, idx),
        None => complete_top(word),
    }
}

/// No colon yet: emit cave prefixes (slug:, slug@N:) and host paths
/// in the CWD. The user picks one and keeps typing. Candidates always
/// use the fully-qualified `<slug>[@N]:` form; if the user typed the
/// `cave` shorthand we rewrite it to the CWD slug for matching so the
/// shorthand still tab-completes (the shell wrapper passes `-U`/etc.
/// so the rewritten candidate replaces the typed word).
fn complete_top(word: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();

    let projects: Vec<(String, String)> = registry::all_projects()
        .unwrap_or_default()
        .into_iter()
        .filter(|(_, p)| Path::new(p).is_dir())
        .collect();
    let cwd_slug = current_project_slug(&projects);

    let match_word = match cwd_slug.as_deref() {
        Some(slug) if word == "cave" || word.starts_with("cave@") => {
            format!("{slug}{}", &word["cave".len()..])
        }
        _ => word.to_string(),
    };

    for (slug, _) in &projects {
        if pick_worktree(slug, None).is_some() {
            push_if_match(&mut out, &match_word, &format!("{slug}:"));
        }
        for aid in agents_in(slug) {
            push_if_match(&mut out, &match_word, &format!("{slug}@{aid}:"));
        }
    }

    out.extend(host_path_candidates(word));
    out.sort();
    out.dedup();
    out
}

/// Word contains a `:`. Resolve the cave prefix to a host directory, then
/// list entries under it that match the partial leaf the user typed.
fn complete_cave(word: &str, colon: usize) -> Vec<String> {
    let prefix = &word[..colon];
    let path = &word[colon + 1..];

    let Some((slug, agent_id)) = parse_prefix(prefix) else {
        return Vec::new();
    };

    // Always emit the fully-qualified `<slug>[@N]:` form so accepting a
    // candidate replaces the `cave` shorthand with the canonical name.
    let qual_prefix = match agent_id.as_deref() {
        Some(aid) => format!("{slug}@{aid}"),
        None => slug.clone(),
    };

    let (base_dir, base_str) = match resolve_base(&slug, agent_id.as_deref(), path) {
        Some(v) => v,
        // If the user typed `cave:` or `slug:` with nothing after, and
        // the agent is ambiguous, redirect to the `@N`-qualified forms
        // so they can pick one rather than getting silence.
        None if path.is_empty() && agent_id.is_none() => {
            return agents_in(&slug)
                .into_iter()
                .map(|aid| format!("{slug}@{aid}:"))
                .collect();
        }
        None => return Vec::new(),
    };

    // Split the remaining path into a directory portion (already typed,
    // gets re-emitted in each candidate) and a partial leaf to filter on.
    let path_after_base = path.get(base_str.len()..).unwrap_or("");
    let (dir_rel, leaf) = match path_after_base.rsplit_once('/') {
        Some((d, l)) => (d, l),
        None => ("", path_after_base),
    };
    let listing_dir = if dir_rel.is_empty() {
        base_dir.clone()
    } else {
        base_dir.join(dir_rel)
    };

    let entries = match std::fs::read_dir(&listing_dir) {
        Ok(e) => e,
        Err(_) => return Vec::new(),
    };

    let mut out: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(leaf) {
            continue;
        }
        let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
        let suffix = if is_dir { "/" } else { "" };
        let full = if dir_rel.is_empty() {
            format!("{qual_prefix}:{base_str}{name}{suffix}")
        } else {
            format!("{qual_prefix}:{base_str}{dir_rel}/{name}{suffix}")
        };
        out.push(full);
    }
    out.sort();
    out
}

/// Parse a cave prefix like `cave`, `cave@3`, `slug`, or `slug@3` into
/// `(slug, agent_id)`. Returns `None` if the prefix can't be resolved
/// (e.g. bare digits, or unknown slug with no `cave` keyword).
fn parse_prefix(prefix: &str) -> Option<(String, Option<String>)> {
    let projects = registry::all_projects().unwrap_or_default();

    let (head, agent_id) = match prefix.rsplit_once('@') {
        Some((h, a)) if a.chars().all(|c| c.is_ascii_digit()) && !a.is_empty() => {
            (h, Some(a.to_string()))
        }
        _ => (prefix, None),
    };

    let slug = if head == "cave" || head.is_empty() {
        current_project_slug(&projects)?
    } else if projects.iter().any(|(s, _)| s == head) {
        head.to_string()
    } else {
        return None;
    };
    Some((slug, agent_id))
}

/// Resolve `path` (the part after the colon) to a host directory we can
/// list. Handles `~/`, `/projects/<slug>/`, plain relative, and
/// cave-internal absolute paths via `/proc/<pid>/root`.
///
/// Returns `(host_dir, path_consumed)` where `path_consumed` is the
/// leading portion of `path` that maps to `host_dir`. The caller appends
/// the rest (a partial filename) when emitting candidates.
fn resolve_base(slug: &str, agent_id: Option<&str>, path: &str) -> Option<(PathBuf, String)> {
    if path == "~" || path.starts_with("~/") {
        return Some((persistent_home(), "~/".to_string()));
    }

    let project_prefix = format!("/projects/{slug}/");
    if path == project_prefix.trim_end_matches('/') || path.starts_with(&project_prefix) {
        let ws = pick_worktree(slug, agent_id)?;
        return Some((ws, project_prefix));
    }

    if path.starts_with('/') {
        let pid = pick_running_pid(slug, agent_id)?;
        let cave_root = PathBuf::from(format!("/proc/{pid}/root"));
        if !cave_root.exists() {
            return None;
        }
        return Some((cave_root, "/".to_string()));
    }

    let ws = pick_worktree(slug, agent_id)?;
    Some((ws, String::new()))
}

/// Mirror `cp::worktree_for_slug`'s resolution, minus the prompt: an
/// explicit `@N` wins, then a running branch-inferred agent, then a
/// unique candidate, then the unique running agent. Returns `None`
/// when the real cp command would prompt — completion can't, so it
/// must decline rather than guess.
fn pick_worktree(slug: &str, agent_id: Option<&str>) -> Option<PathBuf> {
    if let Some(aid) = agent_id {
        let ws = worktree_dir(slug, aid);
        return ws.is_dir().then_some(ws);
    }
    if let Some(aid) = inferred_agent_for_slug(slug)
        .filter(|a| registry::is_agent_running(a).unwrap_or(false))
    {
        let ws = worktree_dir(slug, &aid);
        return ws.is_dir().then_some(ws);
    }
    let candidates: Vec<String> = registry::all_agent_ids(Some(slug))
        .ok()?
        .into_iter()
        .filter(|aid| worktree_dir(slug, aid).is_dir())
        .collect();
    if candidates.len() == 1 {
        return Some(worktree_dir(slug, &candidates[0]));
    }
    let running: Vec<String> = candidates
        .into_iter()
        .filter(|aid| registry::is_agent_running(aid).unwrap_or(false))
        .collect();
    if running.len() == 1 {
        return Some(worktree_dir(slug, &running[0]));
    }
    None
}

fn pick_running_pid(slug: &str, agent_id: Option<&str>) -> Option<u32> {
    let candidates: Vec<String> = registry::all_agent_ids(Some(slug))
        .ok()?
        .into_iter()
        .filter(|a| {
            registry::is_agent_running(a).unwrap_or(false)
                && registry::cave_pid(a).ok().flatten().is_some()
        })
        .collect();
    let chosen = match agent_id {
        Some(aid) if candidates.iter().any(|c| c == aid) => aid.to_string(),
        Some(_) => return None,
        None => match candidates.len() {
            1 => candidates.into_iter().next().unwrap(),
            _ => return None,
        },
    };
    registry::cave_pid(&chosen).ok().flatten()
}

fn current_project_slug(projects: &[(String, String)]) -> Option<String> {
    let cwd = std::env::current_dir().ok()?;
    for (slug, path) in projects {
        let p = Path::new(path);
        if cwd == p || cwd.starts_with(p) {
            return Some(slug.clone());
        }
    }
    None
}

fn inferred_agent_for_slug(slug: &str) -> Option<String> {
    let projects = registry::all_projects().ok()?;
    let proj = projects.iter().find(|(s, _)| s == slug)?;
    crate::git::current_agent_id(Some(Path::new(&proj.1)))
}

fn agents_in(slug: &str) -> Vec<String> {
    registry::all_agent_ids(Some(slug)).unwrap_or_default()
}

fn host_path_candidates(word: &str) -> Vec<String> {
    let (dir, leaf) = match word.rsplit_once('/') {
        Some((d, l)) => (d, l),
        None => ("", word),
    };
    // The prefix we re-emit on each candidate must reproduce the user's
    // typed text up to and including the final `/`. Splitting on `/`
    // collapses both "" (CWD-relative) and "" (root, when word
    // started with `/`) — keep them distinct here.
    let (listing_dir, prefix) = if dir.is_empty() && word.starts_with('/') {
        (PathBuf::from("/"), "/".to_string())
    } else if dir.is_empty() {
        (PathBuf::from("."), String::new())
    } else {
        (PathBuf::from(dir), format!("{dir}/"))
    };
    let entries = match std::fs::read_dir(&listing_dir) {
        Ok(e) => e,
        Err(_) => return Vec::new(),
    };
    let mut out: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') && !leaf.starts_with('.') {
            continue;
        }
        if !name.starts_with(leaf) {
            continue;
        }
        let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
        let suffix = if is_dir { "/" } else { "" };
        out.push(format!("{prefix}{name}{suffix}"));
    }
    out
}

fn push_if_match(out: &mut Vec<String>, word: &str, candidate: &str) {
    if candidate.starts_with(word) {
        out.push(candidate.to_string());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn complete_top_includes_files_when_word_empty() {
        let candidates = complete_top("");
        // Should at least include some entries from CWD.
        assert!(!candidates.is_empty());
    }

    #[test]
    fn parse_prefix_bare_digits_rejected() {
        assert!(parse_prefix("3").is_none());
    }

    #[test]
    fn complete_does_not_panic_on_partial_project_path() {
        // Regression: path equal to `/projects/<slug>` (no trailing
        // slash) used to slice past the end of `path` because the
        // resolved base_str included a trailing `/`.
        let _ = complete("foo:/projects/foo");
        let _ = complete("foo:/projects/foo/");
        let _ = complete("foo:");
        let _ = complete("foo:~");
        let _ = complete("foo:/");
    }
}
