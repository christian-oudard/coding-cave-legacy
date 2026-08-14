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
use std::collections::HashSet;
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

/// The session Claude last had open for the cave's cwd, from the agent's own
/// `.claude.json`. This is the value `claude -c` follows.
fn last_session_id(agent_id: &str, slug: &str) -> Option<String> {
    let text = std::fs::read_to_string(workspace::agent_claude_json(agent_id)).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    // Claude keys its per-project state by the cwd it was launched in, which
    // inside the cave is always /projects/<slug>.
    v.get("projects")?
        .get(format!("/projects/{slug}"))?
        .get("lastSessionId")?
        .as_str()
        .map(str::to_string)
}

/// When a transcript was last written, live copy or permanent record. The
/// archive entry is the live inode, so either answers for a registered
/// session and the fallback only covers the seconds before the watcher runs.
fn transcript_written_at(agent_id: &str, slug: &str, sid: &str) -> Option<std::time::SystemTime> {
    let live = workspace::agent_project_dir(agent_id, slug).join(format!("{sid}.jsonl"));
    let path = if live.is_file() { live } else { config::session_transcript(sid) };
    std::fs::metadata(path).ok()?.modified().ok()
}

/// The session the agent is actually on, given the id cav asked claude for.
///
/// `--session-id` is binding only until the user types `/clear`, which mints
/// a fresh session id inside the cave. cav never sees that happen, so the id
/// it recorded at launch stays pinned to the pre-clear conversation and every
/// later `cav -c`/`cav -r <n>` reattaches to it. Claude does follow the
/// change, in `.claude.json`, so prefer that id when its transcript is the
/// one written last.
///
/// Only the two named ids are considered. Scanning for "the newest transcript
/// in the dir" would sweep up background-job sessions, which are written to
/// the same place and are not the agent's conversation.
pub fn current_session(agent_id: &str, slug: &str, requested: Option<&str>) -> Option<String> {
    let Some(moved) = last_session_id(agent_id, slug) else {
        return requested.map(str::to_string);
    };
    let Some(req) = requested else {
        return Some(moved);
    };
    if moved == req {
        return Some(moved);
    }
    // A requested id with no transcript at all is left alone: it may still be
    // restorable from the legacy shared home, which ensure_live_transcript
    // handles and this cannot see.
    match (
        transcript_written_at(agent_id, slug, &moved),
        transcript_written_at(agent_id, slug, req),
    ) {
        (Some(m), Some(r)) if m > r => Some(moved),
        _ => Some(req.to_string()),
    }
}

/// Registers an agent's transcripts for the life of one launch.
///
/// The cave is polled every couple of seconds, so everything that does not
/// change across a launch is resolved once, here: the agent's uuid (a
/// registry read in itself), the project, the branch, and which sessions are
/// already recorded. A steady-state pass is then a readdir and nothing else.
/// Before, each pass opened a connection and took a write transaction per
/// transcript to insert rows that were already there, against the same file
/// `continue_agent_id` locks with BEGIN IMMEDIATE.
pub struct Registrar {
    claude_dir: PathBuf,
    agent_id: String,
    project: String,
    extra_projects: Vec<String>,
    branch: Option<String>,
    seen: HashSet<String>,
}

impl Registrar {
    pub fn new(
        agent_id: &str,
        project: &str,
        extra_projects: &[String],
        branch: Option<&str>,
    ) -> Self {
        Self {
            claude_dir: workspace::agent_claude_dir(agent_id),
            agent_id: agent_id.to_string(),
            project: project.to_string(),
            extra_projects: extra_projects.to_vec(),
            branch: branch.map(str::to_string),
            seen: HashSet::new(),
        }
    }

    /// Register every transcript this launch has not recorded yet, returning
    /// how many were new. Safe to call repeatedly: a running session whose
    /// record already exists is never touched.
    ///
    /// `seen` starts empty at each launch rather than persisting, so a crash
    /// between the hardlink and the insert is repaired by the next launch's
    /// first pass. A session joins it only once both halves have succeeded.
    pub fn run(&mut self) -> Result<usize> {
        let mut n = 0;
        for live in transcripts(&self.claude_dir) {
            let Some(sid) = session_id_of(&live) else {
                continue;
            };
            if self.seen.contains(&sid) {
                continue;
            }
            link_transcript(&live, &sid)?;
            let rec = SessionRecord {
                session_id: sid.clone(),
                project: self.project.clone(),
                extra_projects: self.extra_projects.clone(),
                agent_id: Some(self.agent_id.clone()),
                branch: self.branch.clone(),
                started_at: None,
                ended_at: None,
            };
            if registry::register_session(&rec)? {
                n += 1;
            }
            self.seen.insert(sid);
        }
        Ok(n)
    }
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

    /// Write a transcript and stamp its mtime, so ordering in these tests
    /// does not depend on the filesystem's timestamp resolution.
    fn transcript_at(claude: &Path, slug: &str, sid: &str, secs_ago: u64) -> PathBuf {
        let p = transcript(claude, slug, sid, "line\n");
        let t = std::time::SystemTime::now() - std::time::Duration::from_secs(secs_ago);
        std::fs::File::options()
            .write(true)
            .open(&p)
            .unwrap()
            .set_modified(t)
            .unwrap();
        p
    }

    fn claude_json(agent_id: &str, slug: &str, last: &str) {
        let p = workspace::agent_claude_json(agent_id);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        let body = serde_json::json!({
            "projects": { format!("/projects/{slug}"): { "lastSessionId": last } }
        });
        std::fs::write(&p, body.to_string()).unwrap();
    }

    #[test]
    fn current_session_follows_a_clear() {
        let g = crate::test_util::DataDirGuard::new("current-clear");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        let claude = workspace::agent_claude_dir(&aid);
        transcript_at(&claude, "proj", "before-clear", 60);
        transcript_at(&claude, "proj", "after-clear", 0);
        claude_json(&aid, "proj", "after-clear");

        assert_eq!(
            current_session(&aid, "proj", Some("before-clear")).as_deref(),
            Some("after-clear"),
        );
    }

    /// Clearing repeatedly leaves a chain of transcripts in one launch. Only
    /// the last one is the agent's conversation; the intermediates stay in
    /// the archive, resumable by uuid, but must not win the row.
    #[test]
    fn current_session_follows_the_last_of_several_clears() {
        let g = crate::test_util::DataDirGuard::new("current-chain");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        let claude = workspace::agent_claude_dir(&aid);
        for (sid, age) in [("launched", 40), ("clear-1", 30), ("clear-2", 20), ("clear-3", 10)] {
            transcript_at(&claude, "proj", sid, age);
        }
        claude_json(&aid, "proj", "clear-3");

        assert_eq!(
            current_session(&aid, "proj", Some("launched")).as_deref(),
            Some("clear-3"),
        );
    }

    #[test]
    fn current_session_keeps_the_requested_id_when_claude_did_not_move() {
        let g = crate::test_util::DataDirGuard::new("current-same");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        transcript_at(&workspace::agent_claude_dir(&aid), "proj", "sess", 0);
        claude_json(&aid, "proj", "sess");

        assert_eq!(
            current_session(&aid, "proj", Some("sess")).as_deref(),
            Some("sess"),
        );
    }

    /// claude records lastSessionId per cwd, so a stale entry from an earlier
    /// launch of the same agent must not outrank the session cav just ran.
    #[test]
    fn current_session_ignores_a_stale_last_session_id() {
        let g = crate::test_util::DataDirGuard::new("current-stale");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        let claude = workspace::agent_claude_dir(&aid);
        transcript_at(&claude, "proj", "older", 60);
        transcript_at(&claude, "proj", "requested", 0);
        claude_json(&aid, "proj", "older");

        assert_eq!(
            current_session(&aid, "proj", Some("requested")).as_deref(),
            Some("requested"),
        );
    }

    /// A requested id with no transcript anywhere may still be restorable
    /// from the legacy shared home, which only ensure_live_transcript sees.
    #[test]
    fn current_session_leaves_an_unwritten_requested_id_alone() {
        let g = crate::test_util::DataDirGuard::new("current-unwritten");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        transcript_at(&workspace::agent_claude_dir(&aid), "proj", "other", 0);
        claude_json(&aid, "proj", "other");

        assert_eq!(
            current_session(&aid, "proj", Some("never-written")).as_deref(),
            Some("never-written"),
        );
    }

    #[test]
    fn current_session_is_the_recorded_one_when_nothing_was_requested() {
        let g = crate::test_util::DataDirGuard::new("current-none");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        claude_json(&aid, "proj", "recorded");

        assert_eq!(
            current_session(&aid, "proj", None).as_deref(),
            Some("recorded"),
        );
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

    /// The poll runs every two seconds for the life of the cave, so a session
    /// it has already registered must cost no database work at all.
    #[test]
    fn a_registered_session_is_not_re_registered_by_the_next_pass() {
        let g = crate::test_util::DataDirGuard::new("register-seen");
        let aid = registry::acquire_agent_id(&g.path.join("proj")).unwrap();
        transcript(&workspace::agent_claude_dir(&aid), "proj", "sess-poll", "line\n");
        let mut registrar = Registrar::new(&aid, "proj", &[], None);
        // Built while the registry is intact, since resolving the agent's
        // uuid reads it. Stands in for the next launch's first pass.
        let mut next_launch = Registrar::new(&aid, "proj", &[], None);

        assert_eq!(registrar.run().unwrap(), 1);

        // Deleting the registry is how a test observes "never opened it":
        // any pass that reaches sqlite recreates the file on the way in.
        let db = g.path.join("coding-cave.db");
        assert!(db.is_file());
        std::fs::remove_file(&db).unwrap();

        assert_eq!(registrar.run().unwrap(), 0);
        assert!(!db.exists(), "a known session still opened the registry");

        // A launch that has not seen the session still records it, which is
        // what repairs a crash between the hardlink and the insert.
        assert_eq!(next_launch.run().unwrap(), 1);
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
