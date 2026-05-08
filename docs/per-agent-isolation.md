# Per-agent isolation

## Status

Three commits shipped on top of the cav shell baseline:

```
Read-only peer-session view across caves
Hardlink per-agent session JSONLs into host's ~/.claude/projects/
Isolate per-agent home; share a single package cache pool
```

| Threat | Status |
|---|---|
| Cross-agent read of session JSONLs | closed |
| Cross-agent tamper of session JSONLs | closed |
| Cross-agent overlay tampering / planted store paths | closed |
| `rm -rf` reaching other agents' homes/overlays/worktrees | closed |
| Registry destruction (single-agent reach) | closed |
| Host UI visibility for agent sessions | provided |
| Cross-cave session reading by agents (read-only) | provided |
| Pollution of host `.claude/` from a malicious agent | bounded |
| Cache poisoning via shared rw `/cache` | **deferred** — see "Known open issue" |

105 tests passing on the rebased branch (64 unit + 19 hook + 22 lifecycle).

## Problem

Before this work, every host agent's cave bind-mounted a single shared
`~/.local/share/coding-cave/home/` directory wholesale onto
`/home/cave-coder/`. That directory contained:

- `coding-cave.db` (the cross-cave registry)
- `nix-overlay/agent-<N>/` (every host agent's nix overlay upperdir)
- `.claude/projects/*.jsonl` (every agent's session transcripts)
- caches, dotfiles, gnupg trust, etc.

Because all eight host agents shared the same backing directory, any one
of them could:

- Read every other agent's plaintext session transcripts.
- Tamper with another agent's nix overlay upperdir while it's running,
  causing live build corruption and an integrity hole (planted store
  paths that nix would otherwise vouch for).
- Destroy the registry, every agent's overlay state, and every session
  log with a single `rm -rf`.

The bind itself was too coarse. The directory below it was mixing six
different concerns under one rule.

## Design

Five categories of state. Each gets its own bind, with its own sharing
rule.

| Category                      | Sharing rule       | Example contents                                    |
| ----------------------------- | ------------------ | --------------------------------------------------- |
| Sensitive per-agent           | Per-agent, rw      | `.claude/projects/`, `.gitconfig`, gpg trust        |
| Per-agent ephemeral           | Per-agent, rw      | nix overlay upperdir, per-agent venv state          |
| Worktree                      | Per-agent, rw      | `/projects/<slug>/`                                 |
| Content-addressable shared    | Shared, rw         | Nix store lower (already correct), `/cache/<tool>/` |
| Cross-cave admin              | Host-only          | Registry, archived sessions                         |
| Already-pushed code           | Shared, ro         | `cave-shared.git`                                   |
| Declarative                   | Re-laid each session | cave.nix outputs                                  |

## Filesystem layout (host paths)

```
~/.local/share/coding-cave/
├── coding-cave.db                  # registry, host-only
├── agents/<id>/
│   ├── home/                       # bound rw → /home/cave-coder
│   └── nix-overlay/                # overlay upper, lower, work
│       ├── upper/
│       ├── work/
│       └── merged/                 # bound rw → /nix/store
├── worktrees/<slug>/<id>/          # bound rw → /projects/<slug>
├── caches/                         # bound rw → /cache (shared across agents)
└── sessions/<slug>/<sid>.jsonl     # peer-aggregator (hardlinks); ro source
```

## bwrap binds (in cav-rs/src/sandbox.rs)

```
--bind    agents/<id>/home/                       /home/cave-coder
--bind    agents/<id>/nix-overlay/merged          /nix/store
--bind    worktrees/<slug>/<id>/                  /projects/<slug>
--bind    caches/                                 /cache             (shared rw)
--ro-bind <project>/.coding-cave/cave-shared.git  /projects/<slug>/.coding-cave/host-cave-shared.git
+ existing per-feature ro-binds (gitconfig, gnupg, crinj-ca, broker socket, etc.)
```

## Cache strategy

Shared `/cache/` is a single rw bind. Each language tool's cache lives at
a subpath inside it. The mapping from tool to subpath lives in cave.nix
(per-user, declarative), not in cav-rs:

```nix
home.sessionVariables = {
  XDG_CACHE_HOME    = "/cache/xdg";    # uv, pip, yarn, composer, go-build
  CARGO_HOME        = "/cache/cargo";
  npm_config_cache  = "/cache/npm";
  PNPM_STORE_PATH   = "/cache/pnpm";
  GOMODCACHE        = "/cache/go-mod";
  GRADLE_USER_HOME  = "/cache/gradle";
};
```

New tool: one line in cave.nix; no Rust change.

For one popular package across many agents, with the cache populated:

- Network: one upstream download, ever.
- Disk: one copy on the host, regardless of agent count.
- Per-agent disk: directory entries only (hardlinks).
- RAM at runtime: kernel page cache deduplicates by inode.

A 3 GB torch install across 8 agents costs 3 GB of host disk total, not
24 GB. Hardlinks require `caches/` and `worktrees/<slug>/<id>/` to be
on the same physical FS; they are by default (siblings under
`~/.local/share/coding-cave/`).

## Registry as a derivable cache

`coding-cave.db` lives at host-only `~/.local/share/coding-cave/coding-cave.db`,
not bound into any cave. It's treated as a cache, not the source of
truth. The filesystem under `agents/<id>/` and `worktrees/<slug>/<id>/`
is authoritative.

`cav init` reconstructs missing rows from filesystem state:

- Project rows: from `worktrees/<slug>/` existing.
- Agent rows: stub entries for any `agents/<id>/` dir not represented.
- Approval rows: lost on wipe; the diff is recoverable from git anyway.

Live state in the registry (pids, session_ids, started_at) is meaningless
after a wipe because the running agents have crashed; reconstructed rows
are stub "exited" entries.

## Session log management

The host-side aggregator at `~/.local/share/coding-cave/sessions/<slug>/`
is the canonical store of every session JSONL across all agents for a
project. Two operations connect it to per-agent caves:

**At launch** (`run_agent` in `cav-rs/src/sandbox.rs`):
- *Bulk migration* (one-time): on first launch under per-agent layout,
  any JSONL in the legacy shared `home/.claude/projects/<encoded>/` is
  hardlinked into the aggregator at `sessions/<slug>/`. Subsequent
  launches skip files already present.
- *Per-agent import*: every JSONL in `sessions/<slug>/` is **copied**
  (genuine copy, separate inode) into the agent's
  `agents/<id>/home/.claude/projects/-projects-<slug>/`, skipping files
  that already exist locally. So each agent's `/resume` lists every
  project session, but modifications to a peer's session via `/resume`
  fork into the agent's local copy without corrupting the original.
- *Sync resumed sessions*: if the launch is a resume and the per-agent
  JSONL already exists, hardlink it to the host's
  `~/.claude/projects/<host-encoded>/<sid>.jsonl` (so the host's
  Claude Code UI reflects ongoing writes) and to the aggregator (so
  other agents see it on their next import).

**At exit** (`sync_pending_sessions`): walks the agent's
`.claude/projects/-projects-<slug>/` and hardlinks any new sessions
Claude wrote during the run into both the host's `~/.claude/projects/`
and the aggregator. Deferred to exit because pre-creating the per-agent
JSONL at launch would trip Claude Code's "Session ID is already in use"
check (regression that shipped in an earlier draft of this work).

Net result:
- Each agent's `/resume` sees every project session.
- Each agent has rw access only to its own per-agent files. Modifications
  to peers' sessions are local forks and never propagate back to the
  aggregator.
- The host user's `~/.claude/projects/<host-encoded>/` accumulates
  hardlinks for every agent's session — host-side Claude Code lists
  them in its normal session browser.
- A UUID collision with a pre-existing host-side session is detected
  on hardlink and skipped (`exists()` check), so the host user's own
  sessions cannot be clobbered.

`cav agent --resume <uuid>` accepts session UUIDs and routes to the
right agent via `registry::find_agent_by_session`.

## `rm -rf /` failure analysis

After all changes land, a full `rm -rf /` from inside a cave can destroy:

- Its own home (`agents/<id>/home/`) — declarative, cave.nix repopulates.
- Its own overlay upperdir — overlay rebuilds copy-ups on next launch.
- Its own worktree — uncommitted edits lost; pushed commits in
  `cave-shared.git` survive.
- Shared `/cache/` — bandwidth cost on next install. Existing venvs keep
  working via hardlink ref counts.

Cannot reach, by mount-namespace construction:

- Any other agent's home, overlay, worktree, or session logs.
- The registry.
- Any archived sessions.
- The host's `/nix/store` (only the per-agent upperdir is writable).
- The host filesystem.

Recovery: relaunch the cave. cave.nix repopulates the home, overlay
rebuilds, worktree recreates from `cave-shared.git`. The only
unrecoverable loss is uncommitted work in this agent's worktree, which
is fundamental.

## Migration

On first launch under the new layout, each agent:

1. Creates its per-agent home dir at `agents/<id>/home/`.
2. Migrates its session JSONL out of the legacy shared home so
   `claude --resume` keeps working.
3. Migrates its nix overlay upperdir out of the legacy
   `<home>/.local/share/coding-cave/nix-overlay/agent-<id>/` location
   to `agents/<id>/nix-overlay/`.

Other state (file-history, plugins, settings.json, statusline.sh,
caches) gets re-laid-down by the cave.nix entry script or rebuilt on
demand, so it doesn't need explicit migration. The legacy `home/`
directory is left in place for the user to remove once all agents
have migrated.

Existing agents need to be stopped before migration to avoid corrupting
in-flight overlay state. Future runs see the new layout and skip
migration.

## Tests

125 passing:

- 75 unit (`cargo test --bin cav`)
- 19 hook integration (`cargo test --test hook`)
- 31 lifecycle integration (`cargo test --test lifecycle`)

Coverage includes: overlay relocation, agent-id parsing for launch
home, session JSONL migration from legacy shared home, hardlink sync
to host's `~/.claude/projects/`, peer-aggregator hardlinks, registry
rebuild from worktrees, deferred sync at agent exit, bulk legacy
migration, peer-session import (copy semantics, fork preservation).

### Launch-state invariant

The session-sync code has a contract that's not obvious from reading
the function alone but is critical: **the cave-internal JSONL path
that Claude Code will write to must NOT exist before Claude launches.**
Claude treats an existing `~/.claude/projects/<encoded>/<sid>.jsonl`
as proof the session is in use and refuses to start with "Session ID
X is already in use."

An earlier draft of the session-sync work pre-created an empty JSONL
at the per-agent path so it had a hardlink target. That broke every
fresh agent launch and shipped because the unit tests verified the
hardlink mechanics in isolation, never the launch-state invariant.

The current tests in `cav-rs/src/sandbox.rs::tests` enforce the
contract directly:

- `host_sync_does_not_pre_create_jsonl_for_fresh_session`
- `peer_aggregator_does_not_pre_create_jsonl`
- `launch_state_invariant_no_session_jsonl_at_cave_internal_path`

The third runs the same call sequence `run_agent` does for a fresh
launch and asserts no JSONL exists at the cave-internal path
afterward. It would have caught the pre-create regression.

The deferred linking — sync runs at agent exit instead of launch —
is covered by `pending_sync_links_jsonl_after_claude_writes`,
`pending_sync_handles_missing_per_agent_dir`, and
`pending_sync_links_multiple_jsonls`.

The peer-session import (which keeps `/resume` working under per-agent
layout) is covered by `import_creates_local_copies_with_separate_inodes`,
`import_skips_existing_local_files`, `bulk_migrate_legacy_uses_hardlinks_when_possible`,
and `launch_makes_resume_see_peer_sessions`.

### Test gap

End-to-end smoke testing (running `cav agent --prompt` and checking
the agent boots a Claude session that completes) is not yet in the
cargo suite. Subcaves do work — `cav init` inside a cave plus
`cav agent --prompt '<text>'` produces a Claude that runs, prints,
and exits cleanly — so the infrastructure to add this is present;
just no test wired up. An earlier draft of this doc claimed subcaves
were broken; that was a misdiagnosis from a malformed test (no
`cav init` inside the cave, no `--prompt`). Subcave-based smoke
tests would have caught the "Session ID is already in use" bug.

## Known open issue

**Cache poisoning via shared rw `/cache`.** A confused or
prompt-injected agent can overwrite cached wheels/crates/tarballs with
crafted bytes between one agent's first download and another's
install. Tools' content-hash verification catches the typical case on
read, but verification isn't uniformly aggressive across tools, and
mutable index/metadata files (cargo registry index, npm
`_cacache/index-v5/`) are not hash-protected at all.

The intended fix is an HTTP caching proxy in crinj. Each agent gets
its own per-agent on-disk caches; the proxy is the only thing that
writes to a shared host-side cache; agents can no longer poison each
other because they don't share storage. Trade-off: per-agent disk
usage instead of one-copy-per-host on disk for popular packages, plus
proportionally more RAM (no cross-agent kernel page-cache dedup).

Deferred. The current shared rw `/cache` is shipped knowingly to
preserve the disk-and-RAM dedup wins from the squashed commit; the
proxy is the next planned change once the rest of the isolation work
has been exercised in practice.

## Other things considered, not pursued

- **Per-tool overlayfs caches.** CAS-shaped tools work, but pnpm
  hardlinks and cargo's mutable git index don't overlay cleanly.
  The proxy approach is cleaner if we want integrity.

- **Per-agent UIDs.** The mount-namespace separation we get from
  per-agent binds is sufficient because the dangerous data simply
  isn't in the namespace anymore.

- **Cache snapshots.** btrfs/zfs snapshot of `caches/` is cheap
  insurance against accidents, but the failure mode it protects
  against is "shared cache wiped" which is already just bandwidth.

- **Promote nix overlay upper to host `/nix/store`.** Not currently
  needed; would require coordinating with host's nix DB. Worth
  revisiting only if agents are seen repeatedly rebuilding the same
  store paths in their uppers.

- **`cav sessions list` CLI.** Dropped. Host's normal Claude Code
  session browser sees every agent's session via the host hardlinks,
  so a separate listing command was redundant.

- **`cav remove-agent` archive-on-removal.** Discussed; sessions
  currently get unlinked along with the per-agent dir, but their
  inodes survive via the host hardlinks (and via the peer-aggregator
  hardlink) so the data isn't actually gone. Not implemented as an
  explicit archive step.
