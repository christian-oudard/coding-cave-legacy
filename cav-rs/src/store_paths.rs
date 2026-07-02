//! Single source of truth for where the per-cave and shared nix stores live.
//!
//! `cav` (the launcher) and `cav-store` (the host store manager) are separate
//! binaries with no shared lib crate. Both include this module — the manager
//! via `#[path]` — so they can never disagree on the store location. A past
//! desync, where the launcher derived its store base from `$HOME` while the
//! manager hardcoded `/home/cave-coder`, silently disabled dedup: the manager
//! watched an empty directory and promoted nothing. Keep this the one
//! definition both sides resolve through.

use std::path::PathBuf;

/// Root-owned shared store that every cave's packages are promoted into and
/// deduplicated against. A nix chroot store ("local?root=<this>"), so its
/// paths are logically `/nix/store/...` like any other store's. Only the
/// daemon touches the shared store; the launcher includes this module too (for
/// the per-cave base) but never references the shared-store items.
#[allow(dead_code)]
pub const SHARED_STORE_ROOT: &str = "/var/lib/coding-cave/store";

/// Base holding every per-cave nix chroot store as `agent-<uuid>/`. The host
/// manager creates it root-owned but sticky and world-writable (mode 1777,
/// like `/tmp`): any user that launches a cave creates its own store subdir,
/// owned by that user, and the sticky bit stops users removing each other's.
/// On the same filesystem as [`SHARED_STORE_ROOT`] so the dedup hardlink works,
/// and a fixed path (not a `$HOME`) so the launcher and the root daemon always
/// agree without the daemon needing to know which user runs caves.
pub const CAVE_STORES_BASE: &str = "/var/lib/coding-cave/nix-stores";

/// The per-cave store base. Production leaves the override unset and uses
/// [`CAVE_STORES_BASE`]; tests set `CODING_CAVE_NIX_STORES_DIR` so the launcher
/// and the manager point at one tmp dir instead of the real `/var/lib`.
pub fn cave_stores_base() -> PathBuf {
    std::env::var_os("CODING_CAVE_NIX_STORES_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(CAVE_STORES_BASE))
}

/// The shared store root. `CODING_CAVE_SHARED_STORE` overrides it for tests.
#[allow(dead_code)] // daemon-only; the launcher includes this module for cave_stores_base
pub fn shared_store_root() -> PathBuf {
    std::env::var_os("CODING_CAVE_SHARED_STORE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(SHARED_STORE_ROOT))
}

/// The host's real nix store directory (level 0 of the dedup tree). The daemon
/// reads it to decide whether a package already lives on the host, and — when it
/// does — hardlinks the shared-store copy onto the host inodes so host and
/// shared share one copy on disk. Cave content never flows *into* it; the daemon
/// only ever reads it. `CODING_CAVE_HOST_NIX_STORE` overrides it for tests.
#[allow(dead_code)] // daemon-only
pub fn host_nix_store() -> PathBuf {
    std::env::var_os("CODING_CAVE_HOST_NIX_STORE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/nix/store"))
}

/// The host's nix database, read (never written) to learn which paths the host
/// has. `CODING_CAVE_HOST_NIX_DB` overrides it for tests.
#[allow(dead_code)] // daemon-only
pub fn host_nix_db() -> PathBuf {
    std::env::var_os("CODING_CAVE_HOST_NIX_DB")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/nix/var/nix/db/db.sqlite"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn production_defaults_are_under_var_lib() {
        // Both stores must sit under one /var/lib tree (one filesystem) for the
        // dedup hardlink to work. If either constant drifts off /var/lib, the
        // hardlink silently becomes a cross-device error.
        assert_eq!(CAVE_STORES_BASE, "/var/lib/coding-cave/nix-stores");
        assert_eq!(SHARED_STORE_ROOT, "/var/lib/coding-cave/store");
        assert!(PathBuf::from(SHARED_STORE_ROOT)
            .parent()
            .unwrap()
            .ends_with("coding-cave"));
        assert_eq!(
            PathBuf::from(CAVE_STORES_BASE).parent(),
            PathBuf::from(SHARED_STORE_ROOT).parent(),
        );
    }
}
