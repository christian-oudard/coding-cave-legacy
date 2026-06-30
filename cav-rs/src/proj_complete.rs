//! Dynamic shell completion for `cav -p`/`--project` values.
//!
//! `-p` accepts a registered project slug or a path to a project dir.
//! Static clap completion can't enumerate registered slugs, so this
//! emits them at runtime. `cav __complete-projects <word>` prints
//! matching slugs one per line; the generated shell scripts call it and
//! merge the result with the shell's own directory completion so plain
//! paths still complete too.

use std::path::Path;

use crate::registry;

/// Registered project slugs (with a live directory) that start with `word`.
pub fn complete(word: &str) -> Vec<String> {
    registry::all_projects()
        .unwrap_or_default()
        .into_iter()
        .filter(|(_, path)| Path::new(path).is_dir())
        .map(|(slug, _)| slug)
        .filter(|slug| slug.starts_with(word))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::DataDirGuard;
    use std::fs;

    #[test]
    fn lists_registered_slugs_matching_word() {
        let g = DataDirGuard::new("proj-complete");
        for slug in ["alpha", "alphabet", "beta"] {
            let base = g.path.join(slug);
            fs::create_dir_all(&base).unwrap();
            registry::register_project(&base).unwrap();
        }
        let mut got = complete("alph");
        got.sort();
        assert_eq!(got, vec!["alpha".to_string(), "alphabet".to_string()]);
    }

    #[test]
    fn skips_projects_whose_dir_is_gone() {
        let g = DataDirGuard::new("proj-complete-gone");
        let base = g.path.join("ghost");
        fs::create_dir_all(&base).unwrap();
        registry::register_project(&base).unwrap();
        fs::remove_dir_all(&base).unwrap();
        assert!(complete("gh").is_empty());
    }
}
