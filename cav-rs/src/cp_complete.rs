//! Dynamic shell completion for `cav cp` paths.
//!
//! `cav cp` accepts cave-prefixed paths like `slug:path`, `cave:path`, or
//! `cave@N:path`. Static clap completion can't enumerate registered slugs
//! or list files inside a worktree, so this module does it at runtime.
//! `cav __complete-cp <word>` prints candidates one per line; the
//! generated shell scripts call it for the cp paths argument.

use std::path::{Path, PathBuf};

use crate::config::persistent_home;
use crate::registry;
use crate::workspace::worktree_for;

/// Emit completion candidates for a partial `cp` argument. Each candidate
/// is the full word to insert (replacing what the user typed). Directory
/// candidates end with `/` so the user can keep tabbing.
pub fn complete(word: &str) -> Vec<String> {
    match word.find(':') {
        Some(idx) => complete_cave(word, idx),
        None => complete_top(word),
    }
}

/// No colon yet: emit cave prefixes (slug@N:) and host paths in the
/// CWD. The user picks one and keeps typing. Candidates always use
/// the fully-qualified `<slug>@<aid>:` form so the agent ID lands in
/// the buffer — bare `<slug>:` would let cp pick silently and the
/// command would no longer record which agent received the file.
/// If the user typed the `cave` shorthand we rewrite it to the CWD
/// slug for matching so the shorthand still tab-completes (the shell
/// wrapper passes `-U`/etc. so the rewritten candidate replaces the
/// typed word).
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
        for aid in agents_in(slug) {
            push_if_match(&mut out, &match_word, &format!("{slug}@{aid}:"));
        }
    }

    out.extend(host_path_candidates(word));
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

    // When the user has typed only `slug:` (no agent ID, no path),
    // redirect to the `@N`-qualified forms so they pick an agent
    // explicitly. cp itself would auto-resolve to a unique running
    // agent silently, but for completion we want the buffer to record
    // which one — otherwise the command isn't self-documenting.
    if path.is_empty() && agent_id.is_none() {
        return agents_in(&slug)
            .into_iter()
            .map(|aid| format!("{slug}@{aid}:"))
            .collect();
    }

    let (base_dir, base_str, resolved_aid) = match resolve_base(&slug, agent_id.as_deref(), path) {
        Some(v) => v,
        None => return Vec::new(),
    };

    // Emit the fully-qualified `<slug>@<aid>:` form so accepting a
    // candidate records which agent the cp targets. `~/` paths share a
    // single persistent home across agents, so they stay bare `<slug>:`
    // (matching `cp::qualify_spec`).
    let qual_prefix = match resolved_aid.as_deref() {
        Some(aid) => format!("{slug}@{aid}"),
        None => slug.clone(),
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
/// Returns `(host_dir, path_consumed, resolved_agent_id)`. `path_consumed`
/// is the leading portion of `path` that maps to `host_dir`; the caller
/// appends the rest (a partial filename) when emitting candidates.
/// `resolved_agent_id` is the agent the path pins to (`None` for `~/`,
/// which shares one persistent home across agents).
fn resolve_base(
    slug: &str,
    agent_id: Option<&str>,
    path: &str,
) -> Option<(PathBuf, String, Option<String>)> {
    if path == "~" || path.starts_with("~/") {
        return Some((persistent_home(), "~/".to_string(), None));
    }

    let project_prefix = format!("/projects/{slug}/");
    if path == project_prefix.trim_end_matches('/') || path.starts_with(&project_prefix) {
        let (ws, aid) = crate::cp::resolve_worktree(slug, agent_id)?;
        return Some((ws, project_prefix, Some(aid)));
    }

    if path.starts_with('/') {
        let (pid, aid) = pick_running_pid(slug, agent_id)?;
        let cave_root = PathBuf::from(format!("/proc/{pid}/root"));
        if !cave_root.exists() {
            return None;
        }
        return Some((cave_root, "/".to_string(), Some(aid)));
    }

    let (ws, aid) = crate::cp::resolve_worktree(slug, agent_id)?;
    Some((ws, String::new(), Some(aid)))
}

fn pick_running_pid(slug: &str, agent_id: Option<&str>) -> Option<(u32, String)> {
    let candidates: Vec<(String, u32)> = registry::all_agent_ids(Some(slug))
        .ok()?
        .into_iter()
        .filter(|a| registry::is_agent_running(a).unwrap_or(false))
        .filter_map(|a| registry::cave_pid(&a).ok().flatten().map(|pid| (a, pid)))
        .collect();
    match agent_id {
        Some(aid) => candidates
            .into_iter()
            .find(|(a, _)| a == aid)
            .map(|(a, pid)| (pid, a)),
        None => match candidates.len() {
            1 => candidates.into_iter().next().map(|(a, pid)| (pid, a)),
            _ => None,
        },
    }
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

/// Agents for which a `cav cp <slug>@<id>:` would have somewhere to
/// write. Filters out registered agents whose worktree was torn down
/// (e.g. after `cav approve`); those would never produce a valid
/// destination, so they shouldn't appear in completion.
fn agents_in(slug: &str) -> Vec<String> {
    registry::all_agent_ids(Some(slug))
        .unwrap_or_default()
        .into_iter()
        .filter(|aid| worktree_for(slug, aid).is_dir())
        .collect()
}

fn host_path_candidates(word: &str) -> Vec<String> {
    // Resolve the user's typed word to a (real_dir_to_list, prefix_to_emit,
    // leaf_to_filter_on) triple. The prefix carries `~user/` or `~/` so
    // candidates keep the form the user typed; the leaf is the partial
    // filename being matched.
    let triple = if let Some(tilde) = parse_tilde(word) {
        match tilde.home {
            Some(home) => tilde_listing(word, &home, tilde.user_part),
            // `~` with no trailing slash and no matching user: redirect
            // to `~/` so completion has somewhere to go.
            None if !tilde.has_slash => return vec!["~/".to_string()],
            None => return Vec::new(),
        }
    } else {
        plain_listing(word)
    };
    let (listing_dir, prefix, leaf) = triple;

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
        if !name.starts_with(&leaf) {
            continue;
        }
        let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
        let suffix = if is_dir { "/" } else { "" };
        out.push(format!("{prefix}{name}{suffix}"));
    }
    out
}

/// Plain CWD-relative or absolute path: emit the typed dir component
/// verbatim as the candidate prefix.
fn plain_listing(word: &str) -> (PathBuf, String, String) {
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
    (listing_dir, prefix, leaf.to_string())
}

/// Tilde-rooted word: list the real filesystem dir, but emit candidates
/// with the `~user`/`~` form the user typed.
fn tilde_listing(word: &str, home: &Path, user_part: &str) -> (PathBuf, String, String) {
    // Strip the tilde-and-user prefix; whatever remains is the part
    // under $HOME that the user has typed so far.
    let typed_prefix = format!("~{user_part}");
    let rest = word.strip_prefix(&typed_prefix).unwrap_or("");
    let rest = rest.strip_prefix('/').unwrap_or(rest);
    let (sub_dir, leaf) = match rest.rsplit_once('/') {
        Some((d, l)) => (d, l),
        None => ("", rest),
    };
    let listing_dir = if sub_dir.is_empty() {
        home.to_path_buf()
    } else {
        home.join(sub_dir)
    };
    let prefix = if sub_dir.is_empty() {
        format!("{typed_prefix}/")
    } else {
        format!("{typed_prefix}/{sub_dir}/")
    };
    (listing_dir, prefix, leaf.to_string())
}

struct Tilde<'a> {
    /// The chars between `~` and the first `/`, e.g. "" or "user".
    user_part: &'a str,
    has_slash: bool,
    /// The home dir for `user_part`, or None if no such user exists.
    home: Option<PathBuf>,
}

/// Parse a leading `~`, `~/...`, `~user`, or `~user/...`. Returns None
/// when the word doesn't start with `~`.
fn parse_tilde(word: &str) -> Option<Tilde<'_>> {
    let rest = word.strip_prefix('~')?;
    let (user_part, has_slash) = match rest.find('/') {
        Some(i) => (&rest[..i], true),
        None => (rest, false),
    };
    let home = if user_part.is_empty() {
        std::env::var_os("HOME").map(PathBuf::from)
    } else {
        passwd_home(user_part)
    };
    Some(Tilde { user_part, has_slash, home })
}

/// Look up `user`'s home directory in /etc/passwd. Lines are
/// `name:passwd:uid:gid:gecos:home:shell`. Returning None on any
/// parse/IO failure is fine — caller falls through to "no candidates".
pub(crate) fn passwd_home(user: &str) -> Option<PathBuf> {
    let contents = std::fs::read_to_string("/etc/passwd").ok()?;
    for line in contents.lines() {
        let mut parts = line.splitn(7, ':');
        if parts.next()? != user {
            continue;
        }
        let home = parts.nth(4)?;
        if home.is_empty() {
            return None;
        }
        return Some(PathBuf::from(home));
    }
    None
}

fn push_if_match(out: &mut Vec<String>, word: &str, candidate: &str) {
    if candidate.starts_with(word) {
        out.push(candidate.to_string());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::DataDirGuard;

    #[test]
    fn complete_top_includes_files_when_word_empty() {
        let _g = DataDirGuard::new("cpc-top");
        let candidates = complete_top("");
        // Should at least include some entries from CWD.
        assert!(!candidates.is_empty());
    }

    #[test]
    fn parse_prefix_bare_digits_rejected() {
        let _g = DataDirGuard::new("cpc-parse");
        assert!(parse_prefix("3").is_none());
    }

    #[test]
    fn host_tilde_expands_to_home() {
        let home = std::env::var("HOME").expect("HOME set in test env");
        assert!(Path::new(&home).is_dir());
        // Bare `~`: when $HOME is set we list it (matching `_files`);
        // emitted candidates keep the `~/` prefix.
        let bare = host_path_candidates("~");
        assert!(
            bare.iter().all(|c| c.starts_with("~/")),
            "expected `~/` prefix on every candidate, got: {bare:?}"
        );
        // `~/` should list $HOME and emit `~/<entry>` candidates.
        let listed = host_path_candidates("~/");
        assert!(
            listed.iter().all(|c| c.starts_with("~/")),
            "expected `~/` prefix on every candidate, got: {listed:?}"
        );
        assert!(!listed.is_empty(), "expected at least one entry under $HOME");
    }

    #[test]
    fn host_tilde_user_expands_via_passwd() {
        // Resolve a user that exists on every system: pull the first
        // line out of /etc/passwd and use that name. Skip if /etc/passwd
        // isn't readable (e.g. a stripped-down sandbox).
        let Ok(passwd) = std::fs::read_to_string("/etc/passwd") else { return };
        let Some(first) = passwd.lines().find(|l| !l.starts_with('#') && !l.is_empty()) else { return };
        let mut parts = first.splitn(7, ':');
        let name = parts.next().unwrap();
        let home = parts.nth(4).unwrap();
        if !Path::new(home).is_dir() {
            return; // user exists but home isn't readable; skip
        }
        let listed = host_path_candidates(&format!("~{name}/"));
        let expected_prefix = format!("~{name}/");
        assert!(
            listed.iter().all(|c| c.starts_with(&expected_prefix)),
            "expected `~{name}/` prefix on every candidate, got: {listed:?}"
        );
    }

    #[test]
    fn host_tilde_unknown_user_returns_empty() {
        // Pick a name that's vanishingly unlikely to exist.
        let listed = host_path_candidates("~zzz_no_such_user_zzz/");
        assert!(listed.is_empty(), "expected no candidates, got: {listed:?}");
    }

    #[test]
    fn complete_does_not_panic_on_partial_project_path() {
        // Regression: path equal to `/projects/<slug>` (no trailing
        // slash) used to slice past the end of `path` because the
        // resolved base_str included a trailing `/`.
        let _g = DataDirGuard::new("cpc-partial");
        let _ = complete("foo:/projects/foo");
        let _ = complete("foo:/projects/foo/");
        let _ = complete("foo:");
        let _ = complete("foo:~");
        let _ = complete("foo:/");
    }
}
