/// Helpers for locating the sandboxed pid inside a cave's process tree.
///
/// A cave is launched as `unshare --pid --fork -- supervisor ... bwrap ...`,
/// which leaves the registry's `cave_pid` pointing at the outer unshare
/// process. unshare sits in a clone of the host's mount ns; the agent's
/// real mount tree (the bind-mounted /projects/<slug>, the cave's tmpfs
/// /tmp, etc.) is set up later by bwrap underneath. To reach files inside
/// the cave we have to walk down from the outer pid until we find a
/// descendant whose mount ns differs — that's the bwrap'd target.
use std::os::unix::fs::MetadataExt;

pub(crate) fn find_inner_pid(outer: u32) -> Option<u32> {
    let outer_mnt = mnt_inode(outer)?;
    let mut queue: std::collections::VecDeque<u32> = std::collections::VecDeque::new();
    queue.push_back(outer);
    while let Some(pid) = queue.pop_front() {
        for child in read_children(pid) {
            if let Some(mnt) = mnt_inode(child) {
                if mnt != outer_mnt {
                    return Some(child);
                }
            }
            queue.push_back(child);
        }
    }
    None
}

pub(crate) fn mnt_inode(pid: u32) -> Option<u64> {
    std::fs::metadata(format!("/proc/{pid}/ns/mnt"))
        .ok()
        .map(|m| m.ino())
}

pub(crate) fn read_children(pid: u32) -> Vec<u32> {
    std::fs::read_to_string(format!("/proc/{pid}/task/{pid}/children"))
        .unwrap_or_default()
        .split_whitespace()
        .filter_map(|s| s.parse().ok())
        .collect()
}
