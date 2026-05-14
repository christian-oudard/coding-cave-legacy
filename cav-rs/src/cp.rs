use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::persistent_home;
use crate::cp_complete::passwd_home;
use crate::git;
use crate::proc_ns::find_inner_pid;
use crate::registry;
use crate::workspace::worktree_for;
use crate::style;

/// Expand a leading `~` or `~user` in a host-side path. Shell tilde
/// expansion normally handles this, but tab completion of `~/...`
/// inserts a backslash-escaped tilde to suppress reexpansion, so the
/// shell hands us the literal `~/...` and we have to expand ourselves.
fn tilde_expand(s: &str) -> PathBuf {
    let Some(rest) = s.strip_prefix('~') else {
        return PathBuf::from(s);
    };
    let (user, sub) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i + 1..]),
        None => (rest, ""),
    };
    let home = if user.is_empty() {
        std::env::var_os("HOME").map(PathBuf::from)
    } else {
        passwd_home(user)
    };
    match home {
        Some(h) if sub.is_empty() => h,
        Some(h) => h.join(sub),
        None => PathBuf::from(s),
    }
}

pub fn cp(srcs: Vec<String>, dest: String, recursive: bool, force: bool) -> Result<()> {
    // Expand globs in cave specs
    let mut expanded: Vec<String> = vec![];
    for src in &srcs {
        expanded.extend(expand_cave_glob(src)?);
    }
    let srcs = expanded;

    // Pre-resolve ALL src cave paths before any prompting so we only ask for
    // agent selection once, even when there are multiple sources.
    let resolved_srcs: Vec<Option<ResolvedCave>> = srcs.iter()
        .map(|s| resolve_cave_path(s, false))
        .collect::<Result<_>>()?;

    let dest_resolved = resolve_cave_path(&dest, true)?;

    if let Some(dr) = dest_resolved.as_ref() {
        let dp = &dr.path;
        // host -> cave
        if let Some(parent) = dp.parent() {
            std::fs::create_dir_all(parent)?;
        }
        if srcs.len() > 1 && !dp.is_dir() {
            bail!("cp: base '{}': not a directory", dest);
        }
        let dest_qual = qualify_spec(&dest, dest_resolved.as_ref());
        for src in &srcs {
            let src_obj = tilde_expand(src).canonicalize()
                .unwrap_or_else(|_| tilde_expand(src));
            if !src_obj.exists() {
                bail!("Source not found: {src}");
            }
            let base = if dp.is_dir() {
                dp.join(src_obj.file_name().unwrap_or_default())
            } else {
                dp.clone()
            };
            if !force && base.exists() {
                print!("Overwrite {}? [y/N] ", base.display());
                io::stdout().flush()?;
                let mut answer = String::new();
                io::stdin().read_line(&mut answer)?;
                if answer.trim().to_lowercase() != "y" {
                    bail!("Aborted.");
                }
            }
            if src_obj.is_dir() {
                if !recursive {
                    bail!("cp: -R not specified; omitting directory '{src}'");
                }
                copy_tree_nofollow(&src_obj, &base)?;
            } else {
                copy_nofollow(&src_obj, &base)?;
                copy_stat(&src_obj, &base);
            }
            eprintln!("  {src} -> {dest_qual}");
        }
    } else if resolved_srcs.iter().any(|p| p.is_some()) {
        // cave -> host (use pre-resolved paths)
        let host_dest = tilde_expand(&dest);
        if let Some(parent) = host_dest.parent() {
            std::fs::create_dir_all(parent)?;
        }
        if srcs.len() > 1 && !host_dest.is_dir() {
            bail!("cp: base '{}': not a directory", dest);
        }
        for (src, src_resolved) in srcs.iter().zip(resolved_srcs.iter()) {
            let sr = src_resolved.as_ref().ok_or_else(|| {
                anyhow::anyhow!("Source not found in cave: {src}")
            })?;
            let src_path = &sr.path;
            if src_path.is_symlink() {
                bail!("Refusing to copy symlink from cave: {src}");
            }
            if !src_path.exists() {
                bail!("Source not found in cave: {src}");
            }
            let base = if host_dest.is_dir() {
                host_dest.join(src_path.file_name().unwrap_or_default())
            } else {
                host_dest.clone()
            };
            if src_path.is_dir() && !recursive {
                bail!("cp: -R not specified; omitting directory '{src}'");
            }
            if !force && base.exists() {
                print!("Overwrite {}? [y/N] ", base.display());
                io::stdout().flush()?;
                let mut answer = String::new();
                io::stdin().read_line(&mut answer)?;
                if answer.trim().to_lowercase() != "y" {
                    bail!("Aborted.");
                }
            }
            if src_path.is_dir() {
                copy_tree_nofollow(src_path, &base)?;
            } else {
                copy_nofollow(src_path, &base)?;
                copy_stat(src_path, &base);
            }
            let src_qual = qualify_spec(src, Some(sr));
            eprintln!("  {src_qual} -> {dest}");
        }
    } else {
        bail!(
            "One of source or destination must use a cave path prefix.\n\
             {}        host -> project worktree\n\
             {}           project worktree -> host",
            style::bold_cyan("cav cp <file>... <slug>:<path>"),
            style::bold_cyan("cav cp <slug>:<path> <dest>"),
        );
    }
    Ok(())
}

#[cfg_attr(test, derive(Debug))]
struct ResolvedCave {
    path: PathBuf,
    slug: String,
    /// `Some` when the path pins to a specific agent (worktree or live cave).
    /// `None` for `~` paths, which share a single persistent home across agents.
    agent_id: Option<String>,
}

/// Returns Some(ResolvedCave) if spec has a cave prefix, else None.
fn resolve_cave_path(spec: &str, is_dest: bool) -> Result<Option<ResolvedCave>> {
    if !spec.contains(':') {
        return Ok(None);
    }
    let (prefix, path) = spec.split_once(':').unwrap();

    let (slug, agent_id) = if prefix.contains('@') {
        let (s, id_str) = prefix.rsplit_once('@').unwrap();
        let registry_slug = registry::agent_project(id_str).ok_or_else(|| {
            anyhow::anyhow!("No registered agent with id '{id_str}'.")
        })?;
        if !s.is_empty() && s != "cave" && s != registry_slug {
            bail!(
                "Agent {id_str} belongs to project '{registry_slug}', not '{s}'."
            );
        }
        (registry_slug, Some(id_str.to_string()))
    } else if prefix == "cave" {
        (detect_slug()?, None)
    } else if prefix.chars().all(|c| c.is_ascii_digit()) && !prefix.is_empty() {
        bail!(
            "Use {} to specify agent {}, not bare {prefix}:{path}.",
            style::bold_cyan(&format!("cave@{prefix}:{path}")),
            style::bold(prefix),
        );
    } else {
        (prefix.to_string(), None)
    };

    if path.starts_with("~/") || path == "~" {
        let home = persistent_home();
        let rel = if path == "~" { "" } else { &path[2..] };
        let result = normalize(&home, rel);
        check_in_dir(&result, &home, is_dest)?;
        return Ok(Some(ResolvedCave { path: result, slug, agent_id: None }));
    }

    if path.starts_with('/') {
        let project_prefix = format!("/projects/{slug}");
        if path == project_prefix || path.starts_with(&format!("{project_prefix}/")) {
            let rel = path[project_prefix.len()..].trim_start_matches('/');
            let (ws, aid) = worktree_for_slug(&slug, agent_id.as_deref())?;
            let result = normalize(&ws, rel);
            check_in_dir(&result, &ws, is_dest)?;
            return Ok(Some(ResolvedCave { path: result, slug, agent_id: Some(aid) }));
        }
        // Cave-internal absolute path via /proc/<pid>/root
        let (cave_root, aid) = cave_root_for_slug(&slug, agent_id.as_deref())?;
        let rel = path.trim_start_matches('/');
        let result = if rel.is_empty() { cave_root } else { cave_root.join(rel) };
        return Ok(Some(ResolvedCave { path: result, slug, agent_id: Some(aid) }));
    }

    let (ws, aid) = worktree_for_slug(&slug, agent_id.as_deref())?;
    let result = normalize(&ws, path);
    check_in_dir(&result, &ws, is_dest)?;
    Ok(Some(ResolvedCave { path: result, slug, agent_id: Some(aid) }))
}

/// Rewrite the prefix of a cave spec to the fully qualified `slug@agent_id:path`
/// form (or `slug:path` when the path is not agent-specific, e.g. `~/...`).
fn qualify_spec(spec: &str, resolved: Option<&ResolvedCave>) -> String {
    match resolved {
        Some(r) => {
            let path = spec.splitn(2, ':').nth(1).unwrap_or("");
            let qualified = match &r.agent_id {
                Some(aid) => style::agent_name(&format!("{}@{aid}", r.slug)),
                None => style::blue(&r.slug),
            };
            format!("{qualified}:{path}")
        }
        None => spec.to_string(),
    }
}

fn detect_slug() -> Result<String> {
    detect_project()?
        .map(|(slug, _)| slug)
        .ok_or_else(|| anyhow::anyhow!("CWD is not inside a registered project."))
}

fn detect_project() -> Result<Option<(String, PathBuf)>> {
    let cwd = std::env::current_dir()?;
    for (slug, path) in registry::all_projects()? {
        let proj = PathBuf::from(&path);
        if cwd == proj || cwd.starts_with(&proj) {
            return Ok(Some((slug, proj)));
        }
    }
    Ok(None)
}

/// Infer the agent id from the current git branch when CWD is the host
/// project for `slug` and HEAD is on a dev branch like `agent-N/<base>`.
pub(crate) fn infer_agent_from_branch(slug: &str) -> Option<String> {
    let (cwd_slug, proj) = detect_project().ok().flatten()?;
    if cwd_slug != slug {
        return None;
    }
    git::current_agent_id(Some(&proj))
}

/// Resolve a worktree for `slug` without prompting. Returns `Some` when
/// resolution is unambiguous — explicit `@N`, branch-inferred running
/// agent, the unique registered worktree, or the unique running agent.
/// Returns `None` when the caller would need to prompt or error. Shared
/// between `cav cp` and tab-completion so the two cannot diverge on
/// which agent gets picked silently.
pub(crate) fn resolve_worktree(slug: &str, agent_id: Option<&str>) -> Option<(PathBuf, String)> {
    if let Some(aid) = agent_id {
        let ws = worktree_for(slug, aid);
        return ws.is_dir().then(|| (ws, aid.to_string()));
    }
    // Branch-based inference is only honored when the inferred agent is
    // currently running — otherwise we'd silently target the worktree of
    // a defunct session just because the host CWD is checked out on its
    // branch.
    if let Some(aid) = infer_agent_from_branch(slug)
        .filter(|a| registry::is_agent_running(a).unwrap_or(false))
    {
        let ws = worktree_for(slug, &aid);
        return ws.is_dir().then_some((ws, aid));
    }
    let candidates: Vec<String> = registry::all_agent_ids(Some(slug))
        .ok()?
        .into_iter()
        .filter(|aid| worktree_for(slug, aid).is_dir())
        .collect();
    if candidates.len() == 1 {
        let aid = candidates.into_iter().next().unwrap();
        return Some((worktree_for(slug, &aid), aid));
    }
    // Prefer the unique running agent when present, so a single live cave
    // wins over stale worktrees from stopped sessions.
    let running: Vec<String> = candidates
        .into_iter()
        .filter(|aid| registry::is_agent_running(aid).unwrap_or(false))
        .collect();
    if running.len() == 1 {
        let aid = running.into_iter().next().unwrap();
        return Some((worktree_for(slug, &aid), aid));
    }
    None
}

fn worktree_for_slug(slug: &str, agent_id: Option<&str>) -> Result<(PathBuf, String)> {
    if let Some(r) = resolve_worktree(slug, agent_id) {
        return Ok(r);
    }
    if let Some(aid) = agent_id {
        bail!("No worktree found for {}.", style::agent_name(&format!("{slug}@{aid}")));
    }
    let mut aids: Vec<String> = registry::all_agent_ids(Some(slug))?
        .into_iter()
        .filter(|aid| worktree_for(slug, aid).is_dir())
        .collect();
    if aids.is_empty() {
        bail!("No worktrees found for project '{}'.", style::blue(slug));
    }
    aids.sort_by_key(|a| a.parse::<u32>().unwrap_or(u32::MAX));
    let chosen = pick_agent_prompt(slug, &aids)?;
    let ws = worktree_for(slug, &chosen);
    Ok((ws, chosen))
}

fn pick_agent_prompt(slug: &str, agent_ids: &[String]) -> Result<String> {
    eprintln!("Multiple agents for '{}':", style::blue(slug));
    for aid in agent_ids {
        let running = registry::is_agent_running(aid).unwrap_or(false);
        let state = if running {
            style::green("running")
        } else {
            style::dim("exited")
        };
        eprintln!(
            "  {}) {}  [{state}]",
            style::bold(aid),
            style::agent_name(&format!("{slug}@{aid}")),
        );
    }
    eprint!("Agent: ");
    io::stderr().flush()?;
    let mut choice = String::new();
    io::stdin().read_line(&mut choice)?;
    let choice = choice.trim().to_string();
    if !agent_ids.iter().any(|a| a == &choice) {
        bail!("Invalid choice.");
    }
    Ok(choice)
}

fn cave_root_for_slug(slug: &str, agent_id: Option<&str>) -> Result<(PathBuf, String)> {
    let all_ids = registry::all_agent_ids(Some(slug))?;
    let candidates: Vec<String> = all_ids
        .into_iter()
        .filter(|aid| {
            registry::is_agent_running(aid).unwrap_or(false)
                && registry::cave_pid(aid).ok().flatten().is_some()
        })
        .collect();

    // Explicit `@N:` from the spec is honored even if the agent is stopped
    // (we'll error below). Branch-based inference is dropped if the agent
    // isn't a live cave so we fall through to the running candidates.
    let inferred = match agent_id {
        Some(aid) => Some(aid.to_string()),
        None => infer_agent_from_branch(slug)
            .filter(|aid| candidates.contains(aid)),
    };
    let chosen = if let Some(aid) = inferred {
        if !candidates.contains(&aid) {
            bail!(
                "Agent {slug}@{aid} is not running. Cave-internal absolute paths \
                 require a live cave."
            );
        }
        aid
    } else if candidates.is_empty() {
        bail!(
            "No running cave for project '{slug}'. Cave-internal absolute paths \
             require a live cave."
        );
    } else if candidates.len() == 1 {
        candidates[0].clone()
    } else {
        pick_agent_prompt(slug, &candidates)?
    };

    // The registry's cave_pid is unshare's pid; its mount ns is a clone of
    // the host's, not bwrap's sandboxed view. Walk descendants for one whose
    // mount ns differs to land in the cave's actual filesystem tree.
    let outer_pid = registry::cave_pid(&chosen)?.unwrap();
    let pid = find_inner_pid(outer_pid).ok_or_else(|| {
        anyhow::anyhow!(
            "Cannot find sandboxed target inside cave {slug}@{chosen}: no \
             descendant has a different mount namespace. The cave may still \
             be initializing or may have crashed during setup."
        )
    })?;
    let cave_root = PathBuf::from(format!("/proc/{pid}/root"));
    std::fs::metadata(&cave_root).map_err(|e| {
        anyhow::anyhow!("Cannot reach cave {slug}@{chosen} via /proc: {e}")
    })?;
    Ok((cave_root, chosen))
}

fn normalize(base: &Path, rel: &str) -> PathBuf {
    if rel.is_empty() {
        base.to_path_buf().clean_path()
    } else {
        base.join(rel).clean_path()
    }
}

trait CleanPath {
    fn clean_path(&self) -> PathBuf;
}
impl CleanPath for PathBuf {
    fn clean_path(&self) -> PathBuf {
        let mut components: Vec<std::path::Component> = vec![];
        for comp in self.components() {
            match comp {
                std::path::Component::ParentDir => { components.pop(); }
                std::path::Component::CurDir => {}
                c => components.push(c),
            }
        }
        components.iter().collect()
    }
}

fn resolve_existing(path: &Path) -> PathBuf {
    let path = path.to_path_buf().clean_path();
    if path.exists() || path.is_symlink() {
        return path.canonicalize().unwrap_or(path);
    }
    let mut parts: Vec<std::ffi::OsString> = vec![];
    let mut current = path.as_path();
    while !current.exists() && !current.is_symlink() {
        parts.push(current.file_name().unwrap_or_default().to_os_string());
        let parent = current.parent();
        match parent {
            Some(p) if p != current => current = p,
            _ => break,
        }
    }
    let mut resolved = current.canonicalize().unwrap_or_else(|_| current.to_path_buf());
    for part in parts.into_iter().rev() {
        resolved = resolved.join(part);
    }
    resolved
}

fn check_in_dir(path: &Path, directory: &Path, is_dest: bool) -> Result<()> {
    let real_dir = directory.canonicalize().unwrap_or_else(|_| directory.to_path_buf());
    let real_dir_str = real_dir.to_string_lossy();
    if is_dest {
        // Check parent only (leaf symlink OK for destinations)
        let parent = path.parent().unwrap_or(path);
        let parent_real = resolve_existing(parent);
        let final_path = parent_real.join(path.file_name().unwrap_or_default());
        let final_str = final_path.to_string_lossy();
        if final_str != real_dir_str.as_ref()
            && !final_str.starts_with(&format!("{real_dir_str}/"))
        {
            bail!("Path escapes worktree: {}", path.display());
        }
    } else {
        let real_path = resolve_existing(path);
        let real_str = real_path.to_string_lossy();
        if real_str != real_dir_str.as_ref()
            && !real_str.starts_with(&format!("{real_dir_str}/"))
        {
            bail!("Path escapes worktree: {}", path.display());
        }
    }
    Ok(())
}

fn expand_cave_glob(spec: &str) -> Result<Vec<String>> {
    if !spec.contains(':') || !spec.contains('*') && !spec.contains('?') && !spec.contains('[') {
        return Ok(vec![spec.to_string()]);
    }
    let (prefix, path) = spec.split_once(':').unwrap();
    // Find first component with magic
    let components: Vec<&str> = path.split('/').collect();
    let magic_idx = components.iter().position(|c| {
        c.chars().any(|ch| ch == '*' || ch == '?' || ch == '[')
    });
    let magic_idx = match magic_idx {
        Some(i) => i,
        None => return Ok(vec![spec.to_string()]),
    };

    let static_path = components[..magic_idx].join("/");
    let glob_pattern = components[magic_idx..].join("/");

    let base_spec = if static_path.is_empty() {
        format!("{prefix}:")
    } else {
        format!("{prefix}:{static_path}")
    };
    let base_host = match resolve_cave_path(&base_spec, false)? {
        Some(r) => r.path,
        None => return Ok(vec![spec.to_string()]),
    };
    if !base_host.is_dir() {
        bail!("Not a directory: {prefix}:{static_path}");
    }

    let mut matches: Vec<PathBuf> = base_host
        .read_dir()
        .map(|d| d.flatten().map(|e| e.path()).collect())
        .unwrap_or_default();

    matches.retain(|p| {
        let name = p.file_name().unwrap_or_default().to_string_lossy();
        fnmatch_glob(&glob_pattern, &name)
    });
    matches.sort();
    if matches.is_empty() {
        bail!("No matches for: {spec}");
    }
    let results = matches
        .iter()
        .map(|m| {
            let rel = m.strip_prefix(&base_host)
                .unwrap_or(m)
                .to_string_lossy()
                .to_string();
            if static_path.is_empty() {
                format!("{prefix}:{rel}")
            } else {
                format!("{prefix}:{static_path}/{rel}")
            }
        })
        .collect();
    Ok(results)
}

fn fnmatch_glob(pattern: &str, name: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let n: Vec<char> = name.chars().collect();
    fnmatch_chars(&p, &n)
}

fn fnmatch_chars(pat: &[char], name: &[char]) -> bool {
    match pat.first() {
        None => name.is_empty(),
        Some('*') => (0..=name.len()).any(|i| fnmatch_chars(&pat[1..], &name[i..])),
        Some('?') => !name.is_empty() && fnmatch_chars(&pat[1..], &name[1..]),
        Some('[') => {
            let (negate, class_end) = parse_bracket(pat);
            match class_end {
                None => name.first() == Some(&'[') && fnmatch_chars(&pat[1..], &name[1..]),
                Some(end) => match name.first() {
                    None => false,
                    Some(&c) => {
                        let start = if negate { 2 } else { 1 };
                        let in_class = char_in_class(c, &pat[start..end]);
                        (if negate { !in_class } else { in_class }) && fnmatch_chars(&pat[end + 1..], &name[1..])
                    }
                },
            }
        }
        Some(&p) => name.first() == Some(&p) && fnmatch_chars(&pat[1..], &name[1..]),
    }
}

fn parse_bracket(pat: &[char]) -> (bool, Option<usize>) {
    let negate = pat.get(1) == Some(&'!');
    let mut i = if negate { 2 } else { 1 };
    // Allow ] as first char in class
    if i < pat.len() && pat[i] == ']' {
        i += 1;
    }
    while i < pat.len() && pat[i] != ']' {
        i += 1;
    }
    if i >= pat.len() { (negate, None) } else { (negate, Some(i)) }
}

fn char_in_class(c: char, class: &[char]) -> bool {
    let mut i = 0;
    while i < class.len() {
        if i + 2 < class.len() && class[i + 1] == '-' {
            if c >= class[i] && c <= class[i + 2] {
                return true;
            }
            i += 3;
        } else {
            if c == class[i] {
                return true;
            }
            i += 1;
        }
    }
    false
}

fn copy_nofollow(src: &Path, dst: &Path) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    // O_NOFOLLOW = 0x20000 on Linux
    const O_NOFOLLOW: i32 = 0o400000;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(src)
        .map_err(|e| anyhow::anyhow!("Cannot open {} (symlink?): {e}", src.display()))?;
    let meta = file.metadata()?;
    if !meta.file_type().is_file() {
        return Ok(());
    }
    if dst.exists() && !std::fs::metadata(dst).map(|m| m.permissions().readonly()).unwrap_or(false) {
        let _ = std::fs::remove_file(dst);
    }
    let mut dst_file = std::fs::File::create(dst)?;
    let mut src_file = file;
    std::io::copy(&mut src_file, &mut dst_file)?;
    Ok(())
}

fn copy_tree_nofollow(src: &Path, dst: &Path) -> Result<()> {
    std::fs::create_dir_all(dst)?;
    copy_stat(src, dst);
    for entry in std::fs::read_dir(src)?.flatten() {
        let path = entry.path();
        if path.is_symlink() {
            continue; // Skip symlinks from cave
        }
        let name = entry.file_name();
        let dst_path = dst.join(&name);
        if path.is_dir() {
            copy_tree_nofollow(&path, &dst_path)?;
        } else {
            copy_nofollow(&path, &dst_path)?;
            copy_stat(&path, &dst_path);
        }
    }
    Ok(())
}

fn copy_stat(src: &Path, dst: &Path) {
    if let Ok(meta) = src.metadata() {
        let _ = std::fs::set_permissions(dst, meta.permissions());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::symlink;

    fn make_tmpdir(suffix: &str) -> PathBuf {
        let t = std::env::temp_dir()
            .join(format!("cav-cp-test-{}-{}", std::process::id(), suffix));
        fs::create_dir_all(&t).unwrap();
        t
    }

    #[test]
    fn check_in_dir_allows_normal_subdir() {
        let dir = make_tmpdir("normal");
        let subdir = dir.join("subdir");
        fs::create_dir_all(&subdir).unwrap();
        let path = normalize(&dir, "subdir/file.txt");
        check_in_dir(&path, &dir, false).unwrap();
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn check_in_dir_allows_root() {
        let dir = make_tmpdir("root");
        let path = normalize(&dir, "");
        check_in_dir(&path, &dir, false).unwrap();
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn check_in_dir_rejects_dotdot_escape() {
        let dir = make_tmpdir("dotdot");
        let path = normalize(&dir, "../escape");
        let result = check_in_dir(&path, &dir, false);
        // normalize already removes .., so the path would be a sibling dir
        assert!(result.is_err(), "Expected error for path-traversal escape");
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn check_in_dir_rejects_symlink_escape() {
        let dir = make_tmpdir("symlink");
        let outside = std::env::temp_dir()
            .join(format!("cav-cp-outside-{}", std::process::id()));
        fs::create_dir_all(&outside).unwrap();
        let evil = dir.join("evil");
        symlink(&outside, &evil).unwrap();

        let path = normalize(&dir, "evil/target");
        let result = check_in_dir(&path, &dir, false);
        assert!(result.is_err(), "Expected error for symlink escape");

        fs::remove_dir_all(&dir).unwrap();
        fs::remove_dir_all(&outside).unwrap();
    }

    #[test]
    fn copy_nofollow_rejects_symlink() {
        let dir = make_tmpdir("nofollow");
        let real = dir.join("real.txt");
        fs::write(&real, "secret").unwrap();
        let link = dir.join("link.txt");
        symlink(&real, &link).unwrap();
        let dst = dir.join("dst.txt");

        let result = copy_nofollow(&link, &dst);
        assert!(result.is_err(), "copy_nofollow must reject symlinks");
        assert!(!dst.exists(), "Destination must not be created for symlink source");

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn copy_tree_nofollow_skips_symlinks() {
        let dir = make_tmpdir("tree-nofollow");
        let src = dir.join("src");
        let dst = dir.join("dst");
        fs::create_dir_all(&src).unwrap();

        let secret = dir.join("secret.txt");
        fs::write(&secret, "TOP SECRET").unwrap();
        fs::write(src.join("safe.txt"), "safe").unwrap();
        symlink(&secret, src.join("evil.txt")).unwrap();

        copy_tree_nofollow(&src, &dst).unwrap();

        assert!(dst.join("safe.txt").exists(), "safe.txt must be copied");
        assert!(!dst.join("evil.txt").exists(), "symlink must be skipped");

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn glob_star() {
        assert!(fnmatch_glob("*.rs", "main.rs"));
        assert!(fnmatch_glob("*.rs", "lib.rs"));
        assert!(!fnmatch_glob("*.rs", "main.py"));
        assert!(fnmatch_glob("*", "anything"));
        assert!(fnmatch_glob("foo*bar", "fooXbar"));
        assert!(!fnmatch_glob("foo*bar", "bazXbar"));
    }

    #[test]
    fn glob_question() {
        assert!(fnmatch_glob("foo?.rs", "fooa.rs"));
        assert!(fnmatch_glob("foo?.rs", "foob.rs"));
        assert!(!fnmatch_glob("foo?.rs", "foo.rs"));
        assert!(!fnmatch_glob("foo?.rs", "fooab.rs"));
    }

    #[test]
    fn tilde_expand_home() {
        let home = std::env::var("HOME").expect("HOME set in test env");
        assert_eq!(tilde_expand("~"), PathBuf::from(&home));
        assert_eq!(tilde_expand("~/foo/bar"), PathBuf::from(&home).join("foo/bar"));
    }

    #[test]
    fn tilde_expand_passes_through_plain_path() {
        assert_eq!(tilde_expand("/abs/path"), PathBuf::from("/abs/path"));
        assert_eq!(tilde_expand("rel/path"), PathBuf::from("rel/path"));
    }

    #[test]
    fn tilde_expand_unknown_user_is_literal() {
        // Falls back to the original string when the user doesn't exist.
        assert_eq!(
            tilde_expand("~zzz_no_such_user_zzz/foo"),
            PathBuf::from("~zzz_no_such_user_zzz/foo")
        );
    }

    #[test]
    fn glob_brackets() {
        assert!(fnmatch_glob("[abc].txt", "a.txt"));
        assert!(fnmatch_glob("[abc].txt", "b.txt"));
        assert!(!fnmatch_glob("[abc].txt", "d.txt"));
        assert!(fnmatch_glob("[a-z].txt", "m.txt"));
        assert!(!fnmatch_glob("[a-z].txt", "A.txt"));
        assert!(fnmatch_glob("[!abc].txt", "d.txt"));
        assert!(!fnmatch_glob("[!abc].txt", "a.txt"));
    }

    mod resolve_at_n {
        use super::*;
        use crate::test_util::DataDirGuard;

        fn register_agent_in(data: &Path, slug: &str) -> String {
            let base = data.join(slug);
            fs::create_dir_all(&base).unwrap();
            registry::acquire_agent_id(&base).unwrap()
        }

        fn make_worktree(data: &Path, slug: &str, aid: &str) -> PathBuf {
            let ws = data.join("worktrees").join(slug).join(aid);
            fs::create_dir_all(&ws).unwrap();
            ws
        }

        #[test]
        fn at_n_resolves_via_registry_not_cwd() {
            let g = DataDirGuard::new("cp-at-n");
            let aid = register_agent_in(&g.path, "real-proj");
            let ws = make_worktree(&g.path, "real-proj", &aid);

            let resolved = resolve_cave_path(&format!("@{aid}:file.txt"), true)
                .unwrap()
                .expect("@N should resolve");
            assert_eq!(resolved.slug, "real-proj");
            assert!(
                resolved.path.starts_with(&ws),
                "{} must land under {}",
                resolved.path.display(),
                ws.display(),
            );
        }

        #[test]
        fn slug_mismatch_is_rejected() {
            // Agent is in real-proj, but the user typed wrong-proj@N.
            // Even if a stray worktrees/wrong-proj/<aid>/ exists on disk,
            // we must refuse so cp can never write into an unrelated tree.
            let g = DataDirGuard::new("cp-mismatch");
            let aid = register_agent_in(&g.path, "real-proj");
            make_worktree(&g.path, "real-proj", &aid);
            let stray = make_worktree(&g.path, "wrong-proj", &aid);
            fs::write(stray.join("seed"), "stray").unwrap();

            let err = resolve_cave_path(&format!("wrong-proj@{aid}:file.txt"), true)
                .expect_err("slug mismatch must error");
            let msg = err.to_string();
            assert!(
                msg.contains("real-proj") && msg.contains("wrong-proj"),
                "error should name both projects, got: {msg}",
            );
        }

        #[test]
        fn unknown_agent_is_rejected() {
            let g = DataDirGuard::new("cp-no-agent");
            let _ = register_agent_in(&g.path, "real-proj");
            // 999 is not registered.
            let err = resolve_cave_path("@999:file.txt", true)
                .expect_err("unknown agent must error");
            assert!(
                err.to_string().contains("999"),
                "error should name the missing id, got: {err}",
            );
        }
    }
}
