# TODO

## Security

- [ ] Drop push-option fallback from pre-receive hook. If `CAV_AGENT_ID` is not set, reject agent pushes entirely. Push options are forgeable and re-open the bypass for any cave started without the daemon.
- [ ] Unix sockets instead of TCP ports. Each agent mounts only its own socket into the sandbox. Closes the port-hopping vulnerability where agent N pushes to agent M's daemon port and gets identity M.
- [ ] Protect non-branch refs. The hook only checks `refs/heads/`. Tags, notes, and other refs are unprotected (red team 2 exploited this).
- [ ] Update SPEC.md: agent identity section still says "communicated via git push options". Should describe `CAV_AGENT_ID` set by pkt-line handler.
## Done

- [x] Fix daemon identity: replaced `git daemon` (strips env vars) with direct pkt-line handler that preserves `CAV_AGENT_ID` (`a948440`).
- [x] Terminal resize fix: recursive `_signal_descendants` for SIGWINCH (`1c862b2`).
- [x] Red team reports filed in `docs/security/`.
- [x] `cav pull`/`cav status` fetch with `--prune` to clean stale remote-tracking refs.
- [x] Fix `cav status` duplicate agents with multiple branches (group by agent ID).
- [x] `cav pull` shows oneline log of incoming commits.
- [x] Fix `cav -p` parsing: check subcmds before defaulting to agent.
- [x] `cav version` command.
