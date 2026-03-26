# TODO

## Bugs

- [ ] `cav init` is not idempotent and cannot recover from partial init.
  - **Observed:** User ran `cav init` on the dotfiles project. The bare repo
    directory (`<project>/.coding-cave/cave-shared.git`) was created, but the
    `cave` remote URL on the host repo points to a stale path from a previous
    cav version, and the bare repo contains no refs or objects. Running
    `cav init` again prints "Already initialized" and exits, with no way to
    repair the state.
  - **Root cause:** `init()` uses `shared.exists()` as its sole guard. If the
    directory exists (even empty, or from a failed/interrupted run), init bails.
    It never validates that:
    1. The bare repo is a valid git repository with refs.
    2. The `cave` remote exists in the host repo and points to `shared`.
    3. The host repo's branches have been pushed into the bare repo.
  - **Suggested fix:** Make `cav init` idempotent. Instead of a single
    exists-check gate, validate each postcondition independently and repair
    any that are broken:
    1. If bare repo dir exists but is not a valid git repo, remove and recreate.
    2. If `cave` remote exists but URL is wrong, `set-url` to the correct path.
       If missing, add it.
    3. If bare repo has no refs (or is missing branches from the host), push.
    4. Always install/update the pre-receive hook.
  - **Workaround:** Manual cleanup on the host:
    ```
    rm -rf .coding-cave
    git remote remove cave
    cav init
    ```

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
