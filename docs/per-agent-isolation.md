# Per-agent isolation

## Problem

Every host agent's cave currently bind-mounts a single shared
`~/.local/share/coding-cave/home/` directory wholesale onto
`/home/cave-coder/`. That directory contains:

- `coding-cave.db` (the cross-cave registry)
- `nix-overlay/agent-<N>/` (every host agent's nix overlay upperdir)
- `.claude/projects/*.jsonl` (every agent's session transcripts)
- caches, dotfiles, gnupg trust, etc.

Because all eight host agents share the same backing directory, any one of
them can:

- Read every other agent's plaintext session transcripts.
- Tamper with another agent's nix overlay upperdir while it's running,
  causing live build corruption and an integrity hole (planted store paths
  that nix would otherwise vouch for).
- Destroy the registry, every agent's overlay state, and every session log
  with a single `rm -rf`.

The bind itself is too coarse. The directory below it is mixing six
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
└── sessions-archive/<id>-<ts>/     # cav remove-agent moves sessions here
```

## bwrap binds (in cav-rs/src/sandbox.rs)

```
--bind  agents/<id>/home/                       /home/cave-coder
--bind  agents/<id>/nix-overlay/merged          /nix/store
--bind  worktrees/<slug>/<id>/                  /projects/<slug>
--bind  caches/                                 /cache
--ro-bind <project>/.coding-cave/cave-shared.git
                                  /projects/<slug>/.coding-cave/host-cave-shared.git
```

Plus existing per-feature ro-binds for gitconfig, gnupg pubring, gnupg
agent socket, crinj CA, broker socket, etc.

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
  ELAN_HOME         = "/cache/elan";
};
```

New tool: one line in cave.nix; no Rust change.

### Why shared rw caches are acceptable

Caches contain content-addressable bytes verified by each tool at install
time. A rogue `rm -rf /cache/` forces redownload from upstream on the
next install. Existing venvs keep working because their installed files
are hardlinks (uv `--link-mode=clone|hardlink`, pnpm CAS), and inode
references survive an unlink in the cache directory.

For one popular package across many agents:

- Network: one upstream download, ever.
- Disk: one copy on the host, regardless of agent count.
- Per-agent disk: directory entries only (hardlinks).
- RAM at runtime: kernel page cache deduplicates by inode.

A 3 GB torch install across 8 agents costs 3 GB of host disk total, not
24 GB.

### Same filesystem requirement

Hardlinks don't cross filesystems. `caches/` and `worktrees/<slug>/<id>/`
must be on the same physical FS for hardlink installs to work. They are
by default (siblings under `~/.local/share/coding-cave/`).

## Registry as a derivable cache

`coding-cave.db` lives at host-only `~/.local/share/coding-cave/coding-cave.db`,
not bound into any cave. It's treated as a cache, not the source of
truth. The filesystem under `agents/<id>/` and `worktrees/<slug>/<id>/`
is authoritative.

`cav init` (extending commit `103cb27`) reconstructs missing rows from
filesystem state:

- Project rows: from `worktrees/<slug>/` existing.
- Agent rows: stub entries for any `agents/<id>/` dir not represented.
- Approval rows: lost on wipe; the diff is recoverable from git anyway.

Live state in the registry (pids, session_ids, started_at) is meaningless
after a wipe because the running agents have crashed; reconstructed rows
are stub "exited" entries.

## Session log management

`.claude/projects/*.jsonl` lives under per-agent home, so it's only
*writable* from within the owning agent's cave. Three hardlinks point
at the same inode, providing the matrix of access rights:

```
agents/<id>/home/.claude/projects/-projects-<slug>/<sid>.jsonl
                                                                 ↑ rw  (per-agent home bind)
~christian/.claude/projects/<encoded-host-path>/<sid>.jsonl
                                                                 ↑ host user's normal Claude Code UI
~christian/.local/share/coding-cave/sessions/<slug>/<sid>.jsonl
                                                                 ↑ peer-aggregator (agent-only)
```

The peer-aggregator is ro-bound into every cave at
`/home/cave-coder/.claude/peer-sessions/<slug>/`, so each agent can
read every other agent's session for that project but writes through
the bound path return EROFS. The owning agent retains rw access via
its per-agent path because that bind is rw.

Properties:

- **Read-write own session.** The per-agent rw bind is unchanged.
- **Read-only other agents'.** No writable path exists from a cave to
  another agent's inode. The peer-aggregator mount is ro at the kernel
  level; mode bits don't matter.
- **Host UI visibility.** Hardlink into `~/.claude/projects/<host-encoded>/`
  means the host user's normal Claude Code session browser lists
  every agent's session for that project alongside the user's own.
- **Host-user sessions excluded from peer view.** The peer-aggregator
  is populated only by cav at agent launch. Host-user sessions for the
  same project are visible to the host user but not to peer agents.
- **Live updates.** Bind mounts pass through directory changes. New
  agent sessions appear in running caves' `peer-sessions/` without
  restart.
- **No clobber on UUID collision.** If a host-side session has the
  same UUID (vanishingly improbable), the sync skips rather than
  overwriting.

The encoding diverges between cave and host because Claude Code keys
its project dir off the working directory at startup. Inside the cave
that's `/projects/<slug>`; on the host it's the project's actual
location. Both names reference the same inode, so each side sees the
session in the form it expects. The peer-aggregator avoids encoding
entirely, keying directly off the project slug.

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

## Implementation order

Each step is a separate PR, ordered by dependency and value.

1. **Relocate nix overlay upperdir.** Change `nix_overlay_dirs()` and
   `migrate_shared_overlay()` in `cav-rs/src/sandbox.rs:900,914` to
   resolve from `config::data_dir()` directly. Closes the cross-agent
   overlay tampering hole. Smallest change, biggest immediate
   integrity win.

2. **Per-agent home bind.** Change `launch_home()` in
   `cav-rs/src/config.rs:64` to return
   `data_dir().join("agents").join(agent_name).join("home")` for the
   host case. Update the bind in `sandbox.rs:134`. One-shot migration
   moves existing shared `home/` content into `agents/<id>/home/` per
   agent or discards what cave.nix can repopulate.

3. **Shared `/cache/` bind.** Add `--bind caches/ /cache` in
   `sandbox.rs`. Update example/template `cave.nix` to set
   `XDG_CACHE_HOME` and per-tool cache env vars.

4. **Registry self-heal.** Extend `cav init`'s registry repair to
   reconstruct agent rows from `agents/*/` and `worktrees/*/<id>/`.

5. **Session commands.** Add `cav sessions list` and `cav resume <uuid>`,
   plus archive-on-remove behavior in `cav remove-agent`.

## Migration

One-time, run by `cav` on first launch under the new layout:

```
for agent_id in <list of host agents>:
  mkdir -p agents/<id>/home
  mv home/.local/share/coding-cave/nix-overlay/agent-<id>  agents/<id>/nix-overlay
  # split per-agent slice of .claude/ from shared home
  mkdir -p agents/<id>/home/.claude/projects
  mv home/.claude/projects/<encoded>/<this-agent's-uuids>.jsonl ...
  # everything else (dotfiles, caches, etc.) is regenerated by cave.nix on next launch
```

Existing agents need to be stopped before migration to avoid corrupting
in-flight overlay state. Future runs see the new layout and skip
migration.

## Known open issue

- **Cache poisoning via shared rw `/cache`.** A confused or
  prompt-injected agent can overwrite cached wheels/crates/tarballs
  with crafted bytes between one agent's first download and another's
  install. Tools' content-hash verification catches the typical case
  on read, but verification isn't uniformly aggressive across tools,
  and mutable index/metadata files (cargo registry index, npm
  `_cacache/index-v5/`) are not hash-protected at all.

  The intended fix is an HTTP caching proxy in crinj. Each agent
  gets its own per-agent on-disk caches; the proxy is the only
  thing that writes to a shared host-side cache; agents can no
  longer poison each other because they don't share storage.
  Trade-off: per-agent disk usage instead of one-copy-per-host on
  disk for popular packages, plus proportionally more RAM (no
  cross-agent kernel page-cache dedup).

  Deferred. The current shared rw `/cache` is shipped knowingly to
  preserve the disk-and-RAM dedup wins from the squashed commit;
  the proxy is the next planned change once the rest of the
  isolation work has been exercised in practice.

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
