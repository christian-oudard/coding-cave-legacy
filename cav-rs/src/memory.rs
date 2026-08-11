//! Memory index reconciliation.
//!
//! `MEMORY.md` is a single index file that every agent on a project rewrites,
//! so concurrent updates are lost and memories end up on disk with no index
//! line pointing at them. Reconciling the index from the memory files
//! themselves at launch makes those losses self-healing.
//!
//! This is a reconcile, not a regenerate: existing lines are preserved
//! verbatim and in order, so a hand-edited summary survives. Only entries
//! whose file is gone are dropped, and only files with no entry are appended.

use anyhow::Result;
use std::path::Path;

const HEADER: &str = "# Memory Index";
const INDEX_FILE: &str = "MEMORY.md";

/// Read a frontmatter field from a memory file's `---` block.
fn frontmatter_field(text: &str, key: &str) -> Option<String> {
    let body = text.strip_prefix("---\n")?;
    let end = body.find("\n---")?;
    let prefix = format!("{key}:");
    body[..end]
        .lines()
        .find_map(|l| l.strip_prefix(&prefix))
        .map(|v| v.trim().to_string())
}

/// If `line` is an entry this module generates for a memory file -- a
/// bullet linking a sibling file to itself, `- [name.md](name.md) - desc`
/// -- return the file name. Anything else (headings, prose, blank
/// separators, links to something other than a sibling .md file) is not
/// a line reconciliation manages, and is preserved untouched.
fn self_linked_file(line: &str) -> Option<&str> {
    let rest = line.trim_start().strip_prefix("- [")?;
    let close = rest.find(']')?;
    let label = &rest[..close];
    let rest = rest[close + 1..].strip_prefix('(')?;
    let end = rest.find(')')?;
    let target = &rest[..end];
    (target == label && target.ends_with(".md")).then_some(target)
}

/// The memory filenames an index already has a self-link entry for.
fn indexed_files(index: &str) -> Vec<String> {
    index.lines().filter_map(self_linked_file).map(str::to_string).collect()
}

/// Memory files in `dir`, sorted, excluding the index itself.
fn memory_files(dir: &Path) -> Result<Vec<String>> {
    let mut names: Vec<String> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with(".md") && n != INDEX_FILE)
        .collect();
    names.sort();
    Ok(names)
}

/// Reconcile the index for `dir` against the memory files beside it.
pub fn reconcile_index(dir: &Path) -> Result<String> {
    let files = memory_files(dir)?;
    let existing = std::fs::read_to_string(dir.join(INDEX_FILE)).unwrap_or_default();
    let linked = indexed_files(&existing);

    let mut out = String::from(HEADER);
    out.push_str("\n\n");

    // The header (and the blank line conventionally under it) is already
    // written above; skip it here so a reconcile of already-reconciled
    // content doesn't duplicate it.
    let mut lines = existing.lines();
    if lines.clone().next() == Some(HEADER) {
        lines.next();
        if lines.clone().next() == Some("") {
            lines.next();
        }
    }
    for line in lines {
        let dangling = self_linked_file(line)
            .map(|f| !files.iter().any(|n| n == f))
            .unwrap_or(false);
        if !dangling {
            out.push_str(line);
            out.push('\n');
        }
    }
    for name in &files {
        if linked.contains(name) {
            continue;
        }
        let text = std::fs::read_to_string(dir.join(name)).unwrap_or_default();
        let desc = frontmatter_field(&text, "description").unwrap_or_default();
        out.push_str(&format!("- [{name}]({name}) - {desc}\n"));
    }
    Ok(out)
}

/// Write the reconciled index if it differs. Returns whether it changed.
pub fn reconcile(dir: &Path) -> Result<bool> {
    if !dir.is_dir() {
        return Ok(false);
    }
    let path = dir.join(INDEX_FILE);
    let next = reconcile_index(dir)?;
    if std::fs::read_to_string(&path).unwrap_or_default() == next {
        return Ok(false);
    }
    std::fs::write(&path, next)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn memo(dir: &Path, name: &str, desc: &str) {
        let text = format!("---\nname: {name}\ndescription: {desc}\ntype: project\n---\n\nbody\n");
        std::fs::write(dir.join(name), text).unwrap();
    }

    fn tmp(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("cav-mem-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn orphaned_memory_gets_indexed() {
        let d = tmp("orphan");
        memo(&d, "a.md", "first fact");
        memo(&d, "b.md", "second fact");
        std::fs::write(d.join(INDEX_FILE), "# Memory Index\n\n- [a.md](a.md) - first fact\n")
            .unwrap();

        assert!(reconcile(&d).unwrap());
        let idx = std::fs::read_to_string(d.join(INDEX_FILE)).unwrap();
        assert!(idx.contains("- [b.md](b.md) - second fact"), "{idx}");
    }

    #[test]
    fn dangling_entry_is_dropped() {
        let d = tmp("dangling");
        memo(&d, "a.md", "first fact");
        std::fs::write(
            d.join(INDEX_FILE),
            "# Memory Index\n\n- [a.md](a.md) - first fact\n- [gone.md](gone.md) - vanished\n",
        )
        .unwrap();

        assert!(reconcile(&d).unwrap());
        let idx = std::fs::read_to_string(d.join(INDEX_FILE)).unwrap();
        assert!(!idx.contains("gone.md"), "{idx}");
        assert!(idx.contains("a.md"), "{idx}");
    }

    #[test]
    fn non_link_lines_survive_reconciliation() {
        let d = tmp("prose");
        memo(&d, "a.md", "x");
        let original = "# Memory Index\n\n\
             ## Project facts\n\
             - [a.md](a.md) - x\n\n\
             See also [the design doc](https://example/doc)\n";
        std::fs::write(d.join(INDEX_FILE), original).unwrap();

        assert!(!reconcile(&d).unwrap(), "should already be stable");
        let idx = std::fs::read_to_string(d.join(INDEX_FILE)).unwrap();
        assert_eq!(idx, original, "headings/prose/blank lines must survive verbatim");
    }

    #[test]
    fn existing_lines_are_preserved_verbatim_and_in_order() {
        let d = tmp("verbatim");
        memo(&d, "a.md", "regenerated text");
        memo(&d, "b.md", "second fact");
        std::fs::write(
            d.join(INDEX_FILE),
            "# Memory Index\n\n- [b.md](b.md) - hand edited\n- [a.md](a.md) - curated summary\n",
        )
        .unwrap();

        reconcile(&d).unwrap();
        let idx = std::fs::read_to_string(d.join(INDEX_FILE)).unwrap();
        let b = idx.find("b.md").unwrap();
        let a = idx.find("a.md").unwrap();
        assert!(b < a, "order not preserved: {idx}");
        assert!(idx.contains("- [a.md](a.md) - curated summary"), "{idx}");
    }

    #[test]
    fn no_change_leaves_index_untouched() {
        let d = tmp("stable");
        memo(&d, "a.md", "first fact");
        reconcile(&d).unwrap();
        assert!(!reconcile(&d).unwrap(), "second pass should be a no-op");
    }

    #[test]
    fn missing_index_is_created() {
        let d = tmp("create");
        memo(&d, "a.md", "first fact");
        assert!(reconcile(&d).unwrap());
        let idx = std::fs::read_to_string(d.join(INDEX_FILE)).unwrap();
        assert!(idx.starts_with(HEADER), "{idx}");
        assert!(idx.contains("- [a.md](a.md) - first fact"), "{idx}");
    }

    #[test]
    fn absent_dir_is_not_an_error() {
        let d = tmp("absent").join("nope");
        assert!(!reconcile(&d).unwrap());
    }
}
