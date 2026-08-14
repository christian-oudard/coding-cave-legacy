# Session and memory storage

## Problem

All caves at one level bind-mount a single persistent home at `/home/coder`.
Claude Code keeps three kinds of state under `~/.claude`, and they want
different lifetimes:

- **Machine-scoped.** The background-job daemon: `daemon.lock`,
  `daemon.status.json`, `daemon/roster.json`, `jobs/`. These name PIDs and
  socket paths. Sharing them across caves makes each cave's job list show
  other caves' jobs, and worse, each cave finds the other's `daemon.lock` PID
  absent from its own PID namespace, concludes the daemon is dead, starts its
  own supervisor, and overwrites `roster.json`, evicting the other's workers.
- **Agent-scoped.** Transcripts, `file-history/`, `history.jsonl`. One agent's
  line of work.
- **Project-scoped.** `memory/`. Must be shared between agents and must
  outlive them.

Memory has a second, independent problem. `memory/*.md` is one fact per
slug-named file and unions cleanly, but `MEMORY.md` is a single index every
agent rewrites, so concurrent updates are lost. The index already drifts in
practice: memories exist on disk that no index line points at, making them
permanently unreachable.

## Scopes

Match the storage grain to the lifetime:

- Launch-scoped state is deleted at startup.
- Agent-scoped state lives in a per-agent directory, collected by `cav gc`.
- Project-scoped state is shared read-write and never collected.
- Session records are permanent, global, and read-only to every cave.

## Layout

Under `~/.local/share/coding-cave/`:

- `claude/agents/<agent_id>/`, private, writable. Transcripts, `file-history/`,
  `history.jsonl`, `session-env/`, `shell-snapshots/`, and the home-manager
  config mirror. Collected with the agent.
- `claude/agents/<agent_id>.json`, the agent's `.claude.json`.
- `memory/<project_slug>/`, the shared memory pool for a project.
- `sessions/<session_id>.jsonl`, permanent session records, flat.
- `coding-cave.db`, registry, including the permanent `sessions` table.

`sessions/` is flat because a session has no single owning project (a cave can
mount several) and because `rename_project` and `merge_project` would otherwise
have to rewrite an immutable store. Session ids are UUIDs, so the path never
needs migrating. Project, agent, and branch live in the `sessions` table.

## Mounts

Everything in `~/.claude` is shared, because it is part of the one persistent
home: the login, settings, skills and caches are the machine's, and a cave with
its own copy of them would start logged out. Bound over it are only the parts
an agent cannot share:

- `claude/agents/<agent_id>/{daemon,jobs,sessions}` at the matching name under
  `~/.claude` (rw): the background-job daemon's state. Caves that shared it saw
  each other's jobs and evicted each other's roster. cav deletes these before
  every launch, since a job dies with the cave and its roster entry would
  otherwise carry a stale PID and a socket path in a `/tmp` tmpfs that no
  longer exists.
- `claude/agents/<agent_id>/projects` at `~/.claude/projects` (rw): the
  transcripts the agent owns.
- `claude/agents/<agent_id>.json` at `~/.claude.json` (rw)
- `memory/<project_slug>` at `~/.claude/projects/<encoded>/memory` (rw, one per
  mounted project; nested inside the projects bind, so it is applied after)
- `sessions/` at `/cave/sessions` (ro)

Only directories can be bound this way: a bind needs a source that exists, and
the daemon's files are written on demand, so an empty `daemon.json` created to
serve as a bind source would read as a roster rather than as no roster.

`/cave/sessions` is deliberately not a path Claude Code scans, so the resume
picker stays scoped to the agent's own sessions.

## Session registration

A host-side watcher on `claude/agents/<agent_id>/projects/` registers each new
`*.jsonl` as it appears: it writes the `sessions` row and hardlinks
`sessions/<session_id>.jsonl` to the live inode. Both paths are under the data
dir, so the link is always possible.

Consequences:

- Registration is immediate, so there is no window in which a session exists
  unrecorded.
- `rm` inside the cave drops a link, not the record. The inode survives under
  the archive name and keeps receiving appends from the running session.
- Background jobs, whose session ids cav never sees in advance, are registered
  by the same mechanism as the primary session.
- Because the archive entry is the live inode, `/cave/sessions` is both the
  permanent record and a real-time view. No separate live mount is needed.

`ended_at` is stamped at teardown. `sweep_dead_pids` and `estimate_exit_time`
already infer exit times from transcripts and cover the crash path. Rows are
never deleted.

## Cross-agent access

Every agent can read every session at `/cave/sessions`, across all projects.
The mount is read-only, so an agent cannot write or delete another's record;
the only writable path to any transcript is the agent's own `~/.claude`.

## Which session an agent is on

The agent row holds one session id, minted by cav and passed as
`--session-id`. That id is binding only until the user types `/clear`, which
mints a new one inside the cave. cav never sees it happen, so the row stays
pinned to the pre-clear conversation and every later `cav -c` / `cav -r <n>`
reattaches to it.

Claude does follow the change: it records `lastSessionId` per launch cwd in
`.claude.json`, which is per agent and so already on cav's side of the
boundary. Both at launch and at teardown, cav takes whichever of the two ids
-- the one it asked for and the one Claude moved to -- has the more recently
written transcript, and writes that back to the row.

Only those two ids are ever considered. Picking "the newest transcript in the
agent's directory" would sweep up background-job sessions, which land in the
same place and are not the agent's conversation.

`cav agent --resume <session-id>` resolves the id through the `sessions`
table, restoring the transcript into the resuming agent's own directory first
if it isn't already there (from the permanent archive, or, for a session that
predates the per-agent layout, the old shared home), then forks rather than
continuing in place, leaving the original record unappended.

## Memory

`memory/<project_slug>/` is shared read-write by every agent on the project.
Files are slug-named and union; the only conflict is two agents writing the same
slug, where last writer wins.

`MEMORY.md` is regenerated at launch from the `name:` and `description:`
frontmatter of the memories beside it, written only when the derived content
differs. Making the index derived rather than authored means a lost concurrent
update self-heals and orphaned memories are relinked on contact.

This is the only shared-writable surface in the design.

## Verified

- Claude Code appends to the transcript jsonl rather than writing a temp file
  and renaming over it: the inode stays fixed while the file grows. A
  rename-based writer would have swapped the inode and frozen the hardlinked
  record at its old contents.
- Plain `--resume` appends in place; `--fork-session` is a separate flag. cav
  passes it whenever the resuming agent is not the session's owner, since two
  agents on one transcript would otherwise interleave into a registered
  record. Ownership comes from the `sessions` table rather than the agent row,
  because gc removes the agent while the record, and the need to fork, remain.

## Not covered

- Truncating a transcript in place mutates the shared inode, damaging the
  archive copy until teardown breaks the link into an independent copy.
- `home/` outside `~/.claude` stays shared read-write, so agents can still
  overwrite each other's `~/.config`, `~/bin`, and scratch directories.
- Memories tagged `type: user` or `type: feedback` are cross-project by nature
  but remain filed under whichever project was open when they were written.
