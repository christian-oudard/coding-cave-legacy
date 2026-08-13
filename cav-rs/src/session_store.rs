//! The permanent session store.
//!
//! A session transcript is written into the agent's own `~/.claude` and is
//! deletable from inside the cave. Registering it hardlinks the live inode
//! into `<data>/sessions/<session_id>.jsonl`, so `rm` in the cave only drops a
//! link: the record survives under the archive name and keeps receiving
//! appends from the running session. That also makes the archive a live view,
//! which is why it can be bind-mounted read-only into every cave as both the
//! permanent record and the cross-agent snoop path.
//!
//! Claude Code appends to the jsonl rather than writing a temp file and
//! renaming over it, so the inode is stable for the life of the session and
//! the hardlink stays attached to the data being written.

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};

use crate::config;
use crate::registry::{self, SessionRecord};
use crate::workspace;

/// The parts of `~/.claude` an agent cannot share with its siblings, bound
/// per agent over the otherwise shared directory. `daemon`, `jobs` and
/// `sessions` are the background-job daemon's: caves that shared them saw
/// each other's jobs and evicted each other's roster. `projects` holds the
/// transcripts an agent owns. Everything else in `~/.claude` -- the login
/// first of all -- belongs to the machine and stays shared.
pub const AGENT_SCOPED: [&str; 4] = ["daemon", "jobs", "projects", "sessions"];

/// The subset of the above that names PIDs and socket paths, so it is
/// meaningless once the cave that produced it is gone.
///
/// Both lists hold directories only. A bind needs a source that exists, and
/// the daemon's files are written on demand: creating an empty `daemon.json`
/// to bind would hand Claude Code a roster that says something rather than
/// nothing. If a Claude version ever keeps daemon state in files at the root
/// of `~/.claude` instead of under `daemon/`, those files are shared again
/// and this approach needs revisiting.
const LAUNCH_SCOPED: [&str; 3] = ["daemon", "jobs", "sessions"];

/// Drop background-job and daemon state left by a previous launch of this
/// agent. Its PIDs refer to a torn-down namespace and its sockets to a `/tmp`
/// tmpfs that no longer exists, so carrying it forward only preserves
/// staleness.
pub fn purge_launch_state(claude_dir: &Path) -> Result<()> {
    for name in LAUNCH_SCOPED {
        let p = claude_dir.join(name);
        if p.is_dir() {
            std::fs::remove_dir_all(&p)?;
        } else if p.exists() {
            std::fs::remove_file(&p)?;
        }
    }
    Ok(())
}

/// Session ids are the transcript file stems.
fn session_id_of(path: &Path) -> Option<String> {
    let stem = path.file_stem()?.to_str()?;
    (path.extension()?.to_str()? == "jsonl").then(|| stem.to_string())
}

/// Every transcript in an agent's Claude directory, across all its projects.
pub fn transcripts(claude_dir: &Path) -> Vec<PathBuf> {
    let projects = claude_dir.join("projects");
    let Ok(entries) = std::fs::read_dir(&projects) else {
        return vec![];
    };
    let mut out = vec![];
    for proj in entries.filter_map(|e| e.ok()) {
        let Ok(files) = std::fs::read_dir(proj.path()) else {
            continue;
        };
        for f in files.filter_map(|e| e.ok()) {
            let p = f.path();
            if session_id_of(&p).is_some() && p.is_file() {
                out.push(p);
            }
        }
    }
    out.sort();
    out
}

/// Link a transcript into the permanent store. Idempotent, and never replaces
/// an existing record: once a session is registered its archive entry is the
/// record, and a second link attempt must leave it alone.
pub fn link_transcript(live: &Path, session_id: &str) -> Result<bool> {
    link_into(live, session_id, &config::sessions_dir())
}

fn link_into(live: &Path, session_id: &str, sessions: &Path) -> Result<bool> {
    let dest = sessions.join(format!("{session_id}.jsonl"));
    if dest.exists() {
        return Ok(false);
    }
    std::fs::create_dir_all(sessions)?;
    // Both paths are under the data dir (see docs/session-memory-architecture.md),
    // so this is expected to always be a same-filesystem hardlink. A failure
    // here means that assumption broke, and the "live view" guarantee the
    // caller relies on would silently break with it -- surface it instead of
    // freezing the archive into a copy of the transcript's first few seconds.
    std::fs::hard_link(live, &dest)
        .with_context(|| format!("hardlinking {} into {}", live.display(), dest.display()))?;
    Ok(true)
}

/// Ensure the live transcript Claude Code will actually look for at launch
/// is present, restoring it if only a permanent record remains. Returns
/// whether the transcript is now (or already was) live and resumable.
///
/// `resolve_continue` decides whether to `--resume` from this, not from the
/// permanent archive alone: the archive can hold a copy the live per-agent
/// dir does not (a session `cav gc` collected, or `rm`'d from inside the
/// cave after its process exited), and the live dir can hold nothing while
/// a pre-per-agent-layout install still keeps the transcript under the old
/// shared home. Both are restorable by hardlink or rename; resuming at a
/// path with neither would just error inside the cave.
pub fn ensure_live_transcript(agent_id: &str, slug: &str, session_id: &str) -> bool {
    let live = workspace::agent_project_dir(agent_id, slug).join(format!("{session_id}.jsonl"));
    if live.is_file() {
        return true;
    }
    let Some(parent) = live.parent() else {
        return false;
    };
    if std::fs::create_dir_all(parent).is_err() {
        return false;
    }

    let archived = config::session_transcript(session_id);
    if archived.is_file() && std::fs::hard_link(&archived, &live).is_ok() {
        return true;
    }

    let legacy = config::claude_project_dir(slug, None).join(format!("{session_id}.jsonl"));
    legacy.is_file() && std::fs::rename(&legacy, &live).is_ok()
}

/// Register every unregistered transcript belonging to an agent. Safe to call
/// repeatedly: already-registered sessions are skipped, so a running session
/// is not disturbed.
pub fn register_agent_sessions(
    agent_id: &str,
    project: &str,
    extra_projects: &[String],
    branch: Option<&str>,
) -> Result<usize> {
    let claude_dir = workspace::agent_claude_dir(agent_id);
    let mut n = 0;
    for live in transcripts(&claude_dir) {
        let Some(sid) = session_id_of(&live) else {
            continue;
        };
        link_transcript(&live, &sid)?;
        let rec = SessionRecord {
            session_id: sid,
            project: project.to_string(),
            extra_projects: extra_projects.to_vec(),
            agent_id: Some(agent_id.to_string()),
            branch: branch.map(str::to_string),
            started_at: None,
            ended_at: None,
        };
        if registry::register_session(&rec)? {
            n += 1;
        }
    }
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::MetadataExt;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("cav-sess-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn transcript(claude: &Path, slug: &str, sid: &str, body: &str) -> PathBuf {
        let dir = claude.join("projects").join(format!("-projects-{slug}"));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join(format!("{sid}.jsonl"));
        std::fs::write(&p, body).unwrap();
        p
    }

    #[test]
    fn ensure_live_transcript_true_when_already_live() {
        let g = crate::test_util::DataDirGuard::new("ensure-live");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        transcript(&workspace::agent_claude_dir(&aid), "proj", "sess-live", "hi\n");

        assert!(ensure_live_transcript(&aid, "proj", "sess-live"));
    }

    #[test]
    fn ensure_live_transcript_restores_from_the_permanent_archive() {
        let g = crate::test_util::DataDirGuard::new("ensure-archive");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();

        let archived = config::session_transcript("sess-arch");
        std::fs::create_dir_all(archived.parent().unwrap()).unwrap();
        std::fs::write(&archived, "archived content\n").unwrap();

        assert!(ensure_live_transcript(&aid, "proj", "sess-arch"));
        let live = workspace::agent_project_dir(&aid, "proj").join("sess-arch.jsonl");
        assert_eq!(std::fs::read_to_string(&live).unwrap(), "archived content\n");
        assert_eq!(
            std::fs::metadata(&live).unwrap().ino(),
            std::fs::metadata(&archived).unwrap().ino(),
            "restored copy should share the archive's inode, not duplicate it",
        );
    }

    #[test]
    fn ensure_live_transcript_migrates_from_the_legacy_shared_home() {
        let g = crate::test_util::DataDirGuard::new("ensure-legacy");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();

        let legacy = config::claude_project_dir("proj", None).join("sess-legacy.jsonl");
        std::fs::create_dir_all(legacy.parent().unwrap()).unwrap();
        std::fs::write(&legacy, "pre-upgrade content\n").unwrap();

        assert!(ensure_live_transcript(&aid, "proj", "sess-legacy"));
        let live = workspace::agent_project_dir(&aid, "proj").join("sess-legacy.jsonl");
        assert_eq!(std::fs::read_to_string(&live).unwrap(), "pre-upgrade content\n");
        assert!(!legacy.exists(), "legacy copy should move, not duplicate");
    }

    #[test]
    fn ensure_live_transcript_false_when_nothing_to_restore() {
        let g = crate::test_util::DataDirGuard::new("ensure-none");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();

        assert!(!ensure_live_transcript(&aid, "proj", "sess-missing"));
    }

    #[test]
    fn purge_removes_launch_state_and_keeps_the_rest() {
        let c = tmp("purge");
        std::fs::create_dir_all(c.join("jobs/abc")).unwrap();
        std::fs::create_dir_all(c.join("daemon")).unwrap();
        std::fs::create_dir_all(c.join("sessions")).unwrap();
        std::fs::create_dir_all(c.join("file-history")).unwrap();
        std::fs::write(c.join("history.jsonl"), "x").unwrap();
        transcript(&c, "p", "s1", "line\n");

        purge_launch_state(&c).unwrap();

        assert!(!c.join("jobs").exists());
        assert!(!c.join("daemon").exists());
        assert!(!c.join("sessions").exists());
        assert!(c.join("file-history").is_dir(), "agent state was purged");
        assert!(c.join("history.jsonl").is_file(), "agent state was purged");
        assert_eq!(transcripts(&c).len(), 1, "transcript was purged");
    }

    #[test]
    fn purge_on_a_fresh_dir_is_not_an_error() {
        let c = tmp("purge-fresh");
        purge_launch_state(&c).unwrap();
    }

    #[test]
    fn transcripts_span_every_project() {
        let c = tmp("scan");
        transcript(&c, "one", "s1", "");
        transcript(&c, "two", "s2", "");
        std::fs::write(c.join("projects").join("-projects-one").join("notes.md"), "").unwrap();
        assert_eq!(transcripts(&c).len(), 2);
    }

    #[test]
    fn linking_shares_the_inode_so_appends_are_visible() {
        let c = tmp("link");
        let sessions = tmp("link-sessions");

        let live = transcript(&c, "p", "sess-a", "one\n");
        assert!(link_into(&live, "sess-a", &sessions).unwrap());

        let dest = sessions.join("sess-a.jsonl");
        assert_eq!(
            std::fs::metadata(&live).unwrap().ino(),
            std::fs::metadata(&dest).unwrap().ino(),
        );

        // A running session keeps appending to the same inode.
        let mut f = std::fs::OpenOptions::new().append(true).open(&live).unwrap();
        std::io::Write::write_all(&mut f, b"two\n").unwrap();
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "one\ntwo\n");
    }

    #[test]
    fn deleting_the_live_copy_leaves_the_record() {
        let c = tmp("delete");
        let sessions = tmp("delete-sessions");

        let live = transcript(&c, "p", "sess-b", "kept\n");
        link_into(&live, "sess-b", &sessions).unwrap();

        std::fs::remove_file(&live).unwrap();

        let dest = sessions.join("sess-b.jsonl");
        assert!(dest.is_file(), "record deleted with the live copy");
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "kept\n");
    }

    #[test]
    fn relinking_never_replaces_an_existing_record() {
        let c = tmp("relink");
        let sessions = tmp("relink-sessions");

        let live = transcript(&c, "p", "sess-c", "original\n");
        assert!(link_into(&live, "sess-c", &sessions).unwrap());
        assert!(!link_into(&live, "sess-c", &sessions).unwrap());

        // Even a transcript replaced wholesale in the cave cannot overwrite it.
        std::fs::remove_file(&live).unwrap();
        let fresh = transcript(&c, "p", "sess-c", "forged\n");
        assert!(!link_into(&fresh, "sess-c", &sessions).unwrap());
        assert_eq!(
            std::fs::read_to_string(sessions.join("sess-c.jsonl")).unwrap(),
            "original\n",
        );
    }
}
