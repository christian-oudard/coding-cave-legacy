# coding-cave-legacy

Earlier implementations of coding-cave, a sandbox for running many Claude Code
agents with full autonomy. Each attempt is kept as its own branch, named for
the date of its last commit.

## Current work

Current research is on the topic of how best to run a swarm of agents
on a server, in a vm. The agents inside run with `--dangerously-skip-permissions`, and we
rely on the containment instead of permissions checks. Token usage is metered and
regulated, so we use LLM subscriptions as well as possible.

## Archived implementations

| Branch | Dates | Language | What it was |
| --- | --- | --- | --- |
| [archive/2026-03-25](https://github.com/christian-oudard/coding-cave-legacy/tree/archive/2026-03-25) | Feb 25 to Mar 25, 2026 | Python | One agent in a bubblewrap sandbox built with jail.nix, with a Nix-defined environment, Claude Code running with permissions skipped, and credentials injected by the crinj proxy. Includes the first security audit. |
| [archive/2026-04-21](https://github.com/christian-oudard/coding-cave-legacy/tree/archive/2026-04-21) | Mar 26 to Apr 21, 2026 | Python | A fresh start around many agents working in parallel on one project, with git as the only coordination mechanism and the `cav` command. |
| [archive/2026-09-01](https://github.com/christian-oudard/coding-cave-legacy/tree/archive/2026-09-01) | Apr 21 to Sep 1, 2026 | Rust | The same design rewritten in Rust and taken to full size: bubblewrap and seccomp caves, the git approval gate, session archiving, and three attempts at sharing one nix store between caves. |
| [archive/2026-05-08](https://github.com/christian-oudard/coding-cave-legacy/tree/archive/2026-05-08) | to May 8, 2026 | Rust | A side branch of the Rust line: per-agent homes, a shared package cache, and a prototype package manager for uv. |
| [archive/2026-05-19](https://github.com/christian-oudard/coding-cave-legacy/tree/archive/2026-05-19) | May 15 to May 19, 2026 | Go | A first KVM prototype, one NixOS VM per cave. |
| [archive/2026-10-08](https://github.com/christian-oudard/coding-cave-legacy/tree/archive/2026-10-08) | Jul 3 to Oct 8, 2026 | Go | One rootless VM for all agents: no network interface, every crossing over vsock, a binary cache of the host store, and its own agent session management. Its `docs/` holds the design, the boundary map and two red-team reviews. |

The most usable of these is `archive/2026-09-01`. It does not work well with
Claude Code's own agent management (`claude --bg`, `claude agents`), because
it manages agent sessions itself.

Why the restarts happened, where the record says:

- The Rust caves gave way to a VM because kernel privilege bugs showed that
  bubblewrap is not a host boundary, and because sharing a nix store between
  caves cost weeks of incidents.
- The single-VM implementation gave way to the current one when Claude Code
  gained its own background sessions and agent view, which made the cave's
  session management redundant.
