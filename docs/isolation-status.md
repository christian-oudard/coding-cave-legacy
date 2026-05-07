# Per-agent isolation: status

A snapshot of where the rm-rf-damage response stands. The full design
lives in [per-agent-isolation.md](per-agent-isolation.md); this file
records what's shipped, what's open, and the threat-by-threat reading.

## What was wrong

A single `~/.local/share/coding-cave/home/` was bind-mounted wholesale
into every cave. That directory contained every agent's plaintext
`.claude/projects/*.jsonl` transcripts, every agent's nix overlay
upperdir, the cross-cave registry, and the shared dotfiles tree. From
inside any one cave you could:

- Read every other agent's full conversation history with the user.
- Plant store paths into another agent's overlay that nix would then
  treat as content-addressed.
- `rm -rf` the registry plus every agent's overlay state in one go.

## What's shipped

Three commits on top of `host-cave/main`:

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
| Cache poisoning via shared rw `/cache` | **deferred** |

### Bind layout per cave

```
--bind        agents/<id>/home/                       /home/cave-coder
--bind        agents/<id>/nix-overlay/merged          /nix/store
--bind        worktrees/<slug>/<id>/                  /projects/<slug>
--bind        caches/                                 /cache             (shared rw)
--ro-bind-try sessions/<slug>/                        /home/cave-coder/.claude/peer-sessions/<slug>/
--ro-bind     <project>/.coding-cave/cave-shared.git  /projects/<slug>/.coding-cave/host-cave-shared.git
+ existing per-feature ro-binds (gitconfig, gnupg, crinj-ca, broker socket, etc.)
```

### Session log access matrix per agent A

| Path inside cave | A's session | Other agents' | Host user's |
|---|---|---|---|
| `~/.claude/projects/-projects-<slug>/<sid>.jsonl` | rw | unreachable | unreachable |
| `~/.claude/peer-sessions/<slug>/<sid>.jsonl` | ro | ro | not present |

Same inode is referenced from three host paths:

```
agents/<id>/home/.claude/projects/-projects-<slug>/<sid>.jsonl   (per-agent rw)
~christian/.claude/projects/<encoded-host-path>/<sid>.jsonl       (host UI visibility)
~christian/.local/share/coding-cave/sessions/<slug>/<sid>.jsonl   (peer-aggregator, ro source)
```

### `rm -rf /` from inside any cave can reach

- Its own home, overlay, worktree.
- Shared `/cache` (failure mode: bandwidth on next install).

Cannot reach:

- Any other agent's home, overlay, worktree, or session log.
- The registry.
- Archived sessions.
- The host's `/nix/store` (only the per-agent upperdir is writable).
- The host filesystem outside `caches/` and the per-agent dirs.

## What's open

**Cache poisoning via shared rw `/cache`.** A confused or
prompt-injected agent can overwrite cached wheels/crates/tarballs with
crafted bytes between one agent's first download and another agent's
install. Tools' content-hash verification catches the typical case on
read, but it isn't uniformly aggressive across tools, and mutable
index/metadata files (cargo registry index, npm
`_cacache/index-v5/`) aren't hash-protected at all.

The planned fix is an HTTP caching proxy in crinj:

- Each agent has its own per-agent on-disk caches (no shared rw).
- crinj is the only writer to a shared host-side cache.
- Agents can't poison each other because they don't share storage.

Trade-off: lose disk and kernel page-cache dedup for popular packages
(torch-class artifacts cost N× disk and proportionally more RAM
across N agents). Crinj already does TLS interception via the cave's
CA, so the implementation extends an existing component rather than
introducing a new one.

Shipping the shared rw `/cache` knowingly preserves the dedup win
from the squashed commit. The proxy is the next planned change once
the rest of the isolation work has been exercised in practice.

## Tests

105 passing on the rebased branch:

- 64 unit (`cargo test --bin cav`)
- 19 hook integration (`cargo test --test hook`)
- 22 lifecycle integration (`cargo test --test lifecycle`)

Coverage includes: overlay relocation, agent-id parsing for launch
home, session JSONL migration from legacy shared home, hardlink
sync to host's `~/.claude/projects/`, peer-aggregator hardlinks,
per-slug ro-bind argv emission, registry rebuild from worktrees.

## Migration

On first launch under the new layout, each agent:

1. Creates its per-agent home dir at
   `~/.local/share/coding-cave/agents/<id>/home/`.
2. Migrates its session JSONL out of the legacy shared home so
   `claude --resume` keeps working.
3. Migrates its nix overlay upperdir out of the legacy
   `<home>/.local/share/coding-cave/nix-overlay/agent-<id>/` location
   to `~/.local/share/coding-cave/agents/<id>/nix-overlay/`.

Other state (file-history, plugins, settings.json, statusline.sh,
caches) gets re-laid-down by the cave.nix entry script or rebuilt on
demand, so it doesn't need explicit migration. The legacy
`~/.local/share/coding-cave/home/` directory is left in place for
the user to remove once all agents have migrated.

## Out-of-scope review

- **Per-tool overlayfs caches:** CAS-shaped tools work, pnpm
  hardlinks and cargo's mutable git index don't overlay cleanly.
  The proxy approach is cleaner if we want integrity.
- **Per-agent UIDs:** mount-namespace separation suffices because
  dangerous data isn't in the namespace anymore.
- **Cache snapshots:** btrfs/zfs snapshot of `caches/` is cheap
  insurance against accidents, but the failure mode it protects
  against is "shared cache wiped" which is already just bandwidth.
- **Promote nix overlay upper to host `/nix/store`:** not currently
  needed; would require coordinating with host's nix DB. Worth
  revisiting only if agents are seen repeatedly rebuilding the same
  store paths in their uppers.
- **`cav sessions list` CLI:** dropped. Host's normal Claude Code
  session browser sees every agent's session via the host hardlinks,
  so a separate listing command was redundant.
