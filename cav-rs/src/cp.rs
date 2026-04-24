use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

use crate::config::{self, persistent_home, worktree_dir};
use crate::registry;

pub fn cp(srcs: Vec<String>, dest: String, recursive: bool, force: bool) -> Result<()> {
    // Expand globs in cave specs
    let mut expanded: Vec<String> = vec![];
    for src in &srcs {
        expanded.extend(expand_cave_glob(src)?);
    }
    let srcs = expanded;

    let dest_path = resolve_cave_path(&dest, true)?;
    let first_src_path = resolve_cave_path(srcs.first().unwrap(), false)?;

    if let Some(dp) = dest_path {
        // host -> cave
        if let Some(parent) = dp.parent() {
            std::fs::create_dir_all(parent)?;
        }
        if srcs.len() > 1 && !dp.is_dir() {
            bail!("cp: base '{}': not a directory", dest);
        }
        for src in &srcs {
            let src_obj = PathBuf::from(src).canonicalize()
                .unwrap_or_else(|_| PathBuf::from(src));
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
            eprintln!("  {src} -> {dest}");
        }
    } else if first_src_path.is_some() {
        // cave -> host
        let host_dest = PathBuf::from(&dest);
        if let Some(parent) = host_dest.parent() {
            std::fs::create_dir_all(parent)?;
        }
        if srcs.len() > 1 && !host_dest.is_dir() {
            bail!("cp: base '{}': not a directory", dest);
        }
        for src in &srcs {
            let src_path = resolve_cave_path(src, false)?.ok_or_else(|| {
                anyhow::anyhow!("Source not found in cave: {src}")
            })?;
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
                copy_tree_nofollow(&src_path, &base)?;
            } else {
                copy_nofollow(&src_path, &base)?;
                copy_stat(&src_path, &base);
            }
            eprintln!("  {src} -> {dest}");
        }
    } else {
        bail!(
            "One of source or destination must use a cave path prefix.\n\
             cav cp <file>... <slug>:<path>        host -> project worktree\n\
             cav cp <slug>:<path> <dest>           project worktree -> host"
        );
    }
    Ok(())
}

/// Returns Some(host_path) if spec has a cave prefix, else None.
fn resolve_cave_path(spec: &str, is_dest: bool) -> Result<Option<PathBuf>> {
    if !spec.contains(':') {
        return Ok(None);
    }
    let (prefix, path) = spec.split_once(':').unwrap();

    let (slug, agent_id) = if prefix.contains('@') {
        let (s, id_str) = prefix.rsplit_once('@').unwrap();
        let slug = if s == "cave" {
            // Auto-detect slug
            detect_slug()?
        } else {
            s.to_string()
        };
        (slug, Some(id_str.to_string()))
    } else if prefix == "cave" {
        (detect_slug()?, None)
    } else {
        (prefix.to_string(), None)
    };

    if path.starts_with("~/") || path == "~" {
        let home = persistent_home();
        let rel = if path == "~" { "" } else { &path[2..] };
        let result = normalize(&home, rel);
        check_in_dir(&result, &home, is_dest)?;
        return Ok(Some(result));
    }

    if path.starts_with('/') {
        let project_prefix = format!("/projects/{slug}");
        if path == project_prefix || path.starts_with(&format!("{project_prefix}/")) {
            let rel = path[project_prefix.len()..].trim_start_matches('/');
            let (ws, _) = worktree_for_slug(&slug, agent_id.as_deref())?;
            let result = normalize(&ws, rel);
            check_in_dir(&result, &ws, is_dest)?;
            return Ok(Some(result));
        }
        // Cave-internal absolute path via /proc/<pid>/root
        let (cave_root, _) = cave_root_for_slug(&slug, agent_id.as_deref())?;
        let rel = std::fs::canonicalize(format!("/{path}"))
            .unwrap_or_else(|_| PathBuf::from(&path))
            .to_string_lossy()
            .trim_start_matches('/')
            .to_string();
        let result = if rel.is_empty() { cave_root.clone() } else { cave_root.join(&rel) };
        return Ok(Some(result));
    }

    let (ws, _) = worktree_for_slug(&slug, agent_id.as_deref())?;
    let result = normalize(&ws, path);
    check_in_dir(&result, &ws, is_dest)?;
    Ok(Some(result))
}

fn detect_slug() -> Result<String> {
    let cwd = std::env::current_dir()?;
    for (slug, path) in registry::all_projects()? {
        let proj = PathBuf::from(&path);
        if cwd == proj || cwd.starts_with(&proj) {
            return Ok(slug);
        }
    }
    bail!("CWD is not inside a registered project.")
}

fn worktree_for_slug(slug: &str, agent_id: Option<&str>) -> Result<(PathBuf, String)> {
    if let Some(aid) = agent_id {
        let ws = worktree_dir(slug, aid);
        if !ws.exists() {
            bail!("No worktree found for {slug}@{aid}.");
        }
        return Ok((ws, aid.to_string()));
    }
    let slug_dir = config::data_dir().join("worktrees").join(slug);
    if !slug_dir.is_dir() {
        bail!("No worktrees found for project '{slug}'.");
    }
    let registered: std::collections::HashSet<String> =
        registry::all_agent_ids(Some(slug))?.into_iter().collect();
    let mut candidates: Vec<PathBuf> = std::fs::read_dir(&slug_dir)?
        .flatten()
        .filter(|e| e.path().is_dir() && registered.contains(&e.file_name().to_string_lossy().to_string()))
        .map(|e| e.path())
        .collect();
    candidates.sort_by_key(|p| {
        p.file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .parse::<u32>()
            .unwrap_or(u32::MAX)
    });
    if candidates.is_empty() {
        bail!("No worktrees found for project '{slug}'.");
    }
    if candidates.len() == 1 {
        let aid = candidates[0]
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        return Ok((candidates[0].clone(), aid));
    }
    let aids: Vec<String> = candidates
        .iter()
        .map(|p| p.file_name().unwrap_or_default().to_string_lossy().to_string())
        .collect();
    let chosen = pick_agent_prompt(slug, &aids)?;
    let idx = aids.iter().position(|a| a == &chosen).unwrap();
    Ok((candidates[idx].clone(), chosen))
}

fn pick_agent_prompt(slug: &str, agent_ids: &[String]) -> Result<String> {
    eprintln!("Multiple agents for '{slug}':");
    for aid in agent_ids {
        let state = if registry::is_agent_running(aid).unwrap_or(false) { "running" } else { "exited" };
        eprintln!("  {aid}) {slug}@{aid}  [{state}]");
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

    let chosen = if let Some(aid) = agent_id {
        if !candidates.contains(&aid.to_string()) {
            bail!(
                "Agent {slug}@{aid} is not running. Cave-internal absolute paths \
                 require a live cave."
            );
        }
        aid.to_string()
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

    let pid = registry::cave_pid(&chosen)?.unwrap();
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
        Some(p) => p,
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
    fn glob_brackets() {
        assert!(fnmatch_glob("[abc].txt", "a.txt"));
        assert!(fnmatch_glob("[abc].txt", "b.txt"));
        assert!(!fnmatch_glob("[abc].txt", "d.txt"));
        assert!(fnmatch_glob("[a-z].txt", "m.txt"));
        assert!(!fnmatch_glob("[a-z].txt", "A.txt"));
        assert!(fnmatch_glob("[!abc].txt", "d.txt"));
        assert!(!fnmatch_glob("[!abc].txt", "a.txt"));
    }
}
