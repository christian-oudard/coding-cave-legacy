# package-dedupe

**Specification, v0.1 (draft)**

## 1. Problem statement

Many concurrent agent sessions on one machine (typically a VM) install packages through per-language package managers (uv, pnpm, cargo, go). Naively, each session materializes its own copies: the same wheel or tarball is downloaded and stored once per session, and disk usage grows without bound because nothing knows when a package stops being needed.

package-dedupe makes many environments share one physical copy of each package artifact, and reclaims artifacts no surviving environment references.

One sentence: Nix-style GC roots for the shared caches of mainstream language package managers.

## 2. Scope

The system is two layers in one project. Layer 1 (the kernel) is reusable by anyone with shared caches; layer 2 (the provisioner) targets the many-agent deployment.

### 2.1 In scope, layer 1: roots + liveness + sweep

- A registry of **roots**: workspaces whose lockfiles pin cache artifacts as live.
- A **liveness computation**: the union of artifacts referenced by all registered roots, per ecosystem.
- A **sweep**: deletion of unreferenced artifacts from each shared cache, safely concurrent with in-flight installs.
- A **plugin interface** per ecosystem (uv, pnpm, cargo, go, nix, ...) that maps workspaces to artifact keys and artifact keys to cache paths.
- A CLI and a library crate.

### 2.2 In scope, layer 2: environment provisioner

- `pkgdd env create/destroy`: turn "new session" into a ready-to-use environment: workspace dir, environment file, declarative mount plan, automatic root registration (Section 6).
- Runtime adapters that render the mount plan for bwrap, OCI, systemd, or plain processes. The provisioner owns *what* must be visible with what permissions; the runtime owns *how*.
- Storage-integrity isolation only (store write model, Section 7). Security isolation (namespaces, seccomp, egress) is the runtime's job.

### 2.3 Out of scope (deployment context, composes around this tool)

- Package resolution, fetching, or installation. The native tools do this.
- Materialization dedup. uv and pnpm already hardlink from their caches; this tool relies on that behavior rather than reimplementing it.
- Registry proxies (devpi, Verdaccio, Athens), egress control, credential injection.
- Session lifecycle *policy* (leases on sessions, heartbeats, reaping dead sessions, agent orchestration, LLM harnesses). Callers own their roots; `env create/destroy` are mechanisms the caller invokes.
- Multi-machine synchronization. The manifest schema must serialize cleanly to permit a future cross-VM aggregation layer (a G-Set union of live sets), but no such layer is built now.

## 3. Design principles

1. **Derived state, never authoritative.** The set of registered roots plus the workspaces' native lockfiles are the only sources of truth. Everything else (live set, refcounts, sweep decisions) is recomputed. The daemonless core holds no state that cannot be rebuilt by a rescan; crash recovery is a rescan.
2. **Native manifests are the per-environment manifest.** `uv.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `go.sum` are ingested as-is. Agents and humans use their tools unmodified; there is no bespoke install command to bypass.
3. **Monotone everywhere except the sweep.** Adds to shared caches are idempotent, commutative, coordination-free (the caches are content-addressed). The only non-monotone operation is deletion, so it is the only serialized operation (CALM dichotomy). Concentrate correctness effort there.
4. **Filesystem-native primitives.** Roots are symlinks. Locks are lock files. In-flight protection is tempfiles. Atomic rename for all state transitions. No database.
5. **VM-agnostic.** The tool operates on paths. Same binary on bare NixOS, in a VM, or in a container.

## 4. Terminology

| Term | Meaning |
|---|---|
| **Store** | A shared, content-addressed (or content-keyed) package cache owned by a native tool, e.g. `$UV_CACHE_DIR`, the pnpm store, `$CARGO_HOME/registry`, `$GOMODCACHE`. |
| **Artifact** | One deduplicatable unit in a store, keyed at minimum by `(ecosystem, name, version, platform/abi tags, content hash)`. Keys must include platform/ABI (full wheel tag tuple for Python) to avoid falsely unifying incompatible builds. |
| **Workspace** | A directory containing native manifests/lockfiles, e.g. an agent session's project dir. |
| **Root** | A symlink in the roots directory pointing at a workspace. Registration = `ln -s`; deregistration = `rm`. A dangling symlink is a dead root and is ignored (and pruned). |
| **View** | A materialized environment (venv, `node_modules`) hardlinked/reflinked out of a store. Views are produced by the native tools, not by package-dedupe. |
| **Live set** | `⋃ over live roots of artifacts(workspace)`, computed per ecosystem. |
| **Lease** | A temporary root protecting artifacts that are mid-install and not yet referenced by any committed lockfile (temproots pattern). |

## 5. Architecture

```
roots dir                     stores (owned by native tools)
/var/lib/pkgdedupe/roots/     $UV_CACHE_DIR
  sess-a3f1 -> /work/a3f1     $PNPM_STORE_DIR
  sess-9c02 -> /work/9c02     $CARGO_HOME/registry (+ sccache)
  ci-build7 -> /builds/7      $GOMODCACHE
        |                            ^
        v                            |
   [core: mark]                 [core: sweep]
        |                            |
        +-------- plugins ----------+
          uv | pnpm | cargo | go | nix
```

The core is a single algorithm:

```
sweep(store) = enumerate(store) − ( ⋃_{r ∈ roots} artifacts(r) ∪ leased )
```

executed under the GC lock, per store.

### 5.1 Roots directory

- Default `/var/lib/pkgdedupe/roots/` (configurable). One symlink per root, named by the registrant (session id, CI job id, hostname-user, ...).
- Indirect roots only: the symlink target is the workspace, and liveness is derived from the workspace's current lockfiles at sweep time. A workspace that updates its lockfile automatically updates its pins; no re-registration needed. This is the level-triggered reconciliation property: sweep correctness depends only on current state, not on a history of add/remove events.
- Ownership/permissions: the roots dir is world-writable-with-sticky-bit or group-writable per deployment policy. Registrants own their symlinks.

### 5.2 Plugin interface

A plugin implements, per ecosystem:

```rust
trait EcosystemPlugin {
    /// Parse the workspace's lockfiles into artifact keys.
    /// Pure. Missing/absent lockfiles => empty set, never an error.
    fn artifacts(&self, workspace: &Path) -> Result<BTreeSet<ArtifactKey>>;

    /// Enumerate all artifact keys currently present in the store.
    fn enumerate(&self, store: &Path) -> Result<BTreeSet<ArtifactKey>>;

    /// Delete one artifact from the store. Must be idempotent.
    fn delete(&self, store: &Path, key: &ArtifactKey) -> Result<Freed>;

    /// Which store paths this plugin owns (for lock scoping).
    fn stores(&self) -> Vec<PathBuf>;
}
```

- Plugins are compiled-in Rust implementations, with a subprocess escape hatch (executable speaking newline-delimited JSON over stdio) so ecosystems are contributable without touching the core.
- **Version pinning:** `enumerate`/`delete` depend on tool-internal cache layouts that are not stable APIs. Each release pins the supported version ranges of uv/pnpm/cargo/go and refuses (or warns and skips the store) outside them. This is the primary maintenance burden of the project; track upstream layout changes per release.
- Preferred long-term direction: upstream a `--keep-from-lockfiles <list>` prune mode into uv and pnpm, shrinking plugins to `artifacts()` only.

### 5.3 Ecosystem notes

- **Python (uv):** artifacts are wheels/sdists in the uv cache; views are venvs hardlinked from it. `st_nlink > 1` on cache files is a cross-check, not the source of truth (lockfiles are).
- **JS (pnpm):** content-addressed store, hardlinked `node_modules`. Enforce pnpm over npm/yarn in deployments via corepack; npm's cache is a download cache only and gains little here.
- **Rust (cargo):** registry/source cache dedups normally. Compiled artifacts are the expensive shared object and are keyed by `(toolchain, features, profile)`, not package identity; share them via sccache or shared `CARGO_TARGET_DIR` per equivalence class. The cargo plugin covers registry + git caches; sccache has its own size-based eviction and is out of sweep scope in v0.
- **Go:** `GOMODCACHE`/`GOCACHE` are already content-addressed and concurrency-safe; plugin maps `go.sum` to module cache entries.
- **Nix:** delegate entirely (`nix-store --gc` with its own gcroots). Included for uniformity of the CLI, not because Nix needs help.

### 5.4 Filesystem requirements

- Stores and views must reside on the same filesystem for hardlinks; deploy stores on XFS or btrfs so reflink is available as fallback for tools that copy.
- Views must not be writable through into store inodes (see Section 7).

## 6. The environment interface

The interface between an agent environment and the dedupe system is a directory convention plus an environment contract, both POSIX-native, with zero runtime coupling. No API calls, no protocol, no daemon. Rationale: the counterparty is arbitrary existing tooling (uv, pnpm, cargo) that already speaks env vars + paths; anything richer would require wrapping the tools, reintroducing the bypass/drift problem that lockfile adoption eliminated.

### 6.1 The contract, three parts

1. **Workspace directory (agent → dedupe).** One directory per environment. Whatever lockfiles exist in it define that environment's live artifacts. This is the entire inbound interface; an agent that only ever runs `uv add` fulfills it without knowing it exists.
2. **Environment file (dedupe → agent).** One sourceable file per session: `UV_CACHE_DIR`, `PNPM_STORE_DIR`, `CARGO_HOME`, `GOMODCACHE`, plus index/proxy URLs. This is the whole mechanism by which native tools land in the shared stores. Lowest common denominator: works for a bare process, bwrap, a container, a devshell.
3. **Registration.** A symlink in the roots dir (`pkgdd root add`, or literally `ln -s`).

### 6.2 Invariants the deployment must uphold

- **Same-filesystem invariant:** workspace views and stores share a filesystem, or `link(2)` fails and tools silently degrade to copy mode, i.e. duplication. Violations are acceptable but must be detectable.
- **Link-mode invariant:** installs must materialize as hardlinks or reflinks. A hardlink writes only a directory entry pointing at the store's inode; no content bytes are duplicated (`du` shows the size, the blocks are shared). Tools degrade to copy silently on cross-fs caches, some permission failures, or `UV_LINK_MODE=copy`. `pkgdd doctor` verifies empirically: install a known artifact, check `st_nlink > 1` or FIEMAP-shared extents. Never trust configuration.
- **Visibility invariant:** store paths in the env file resolve inside whatever mount namespace the agent runs in.

Unavoidable per-workspace cost floor (not duplication bugs): `.pyc` files compiled per venv (mitigate with `--compile-bytecode` and identical interpreter paths), pnpm's symlink/dirent overhead, files a package writes at first import.

### 6.3 The provisioner (layer 2)

`pkgdd env create <id>` produces: the workspace dir, the env file, automatic root registration, and a declarative **mount plan** (JSON): the list of `(src, dst, mode)` bind specs an environment needs: workspace rw; stores rw under write model A (insert-only by ownership, Section 7) or ro under model B. Adapters render the plan per runtime: `bwrap` argv, `docker run -v` flags, systemd-nspawn/unit snippets, or nothing for plain processes (source the env file). `pkgdd env destroy <id>` is the inverse (deregister root, optionally delete workspace).

Bind mounts preserve the underlying filesystem, so hardlinks across a bind mount of the same fs work; the same-fs invariant is about the backing store, not mount topology. The provisioner deliberately stops here: create, env file, plan, root, and the inverse. Lifecycle policy stays with the caller.

## 7. Store write model

Every install performs two distinct writes:

1. **Workspace write** (venv, `node_modules`): hardlink metadata only, always permitted.
2. **Store write**: occurs only on cache miss, when a tool downloads a new artifact and inserts it. Cache hits are pure reads plus links.

The hazard is only write #2 under mutation: hardlinks share inodes, so an agent mutating a store inode (chmod, truncate, append) corrupts every environment linking it. Options, increasing paranoia:

- **A (default): insert-only by ownership.** Store mounted rw, but store files owned by a dedicated `pkgstore` user, mode 444, each session running as a distinct non-owning UID. Agents cannot mutate existing inodes; they can only add. Adds are monotone, hence safe. This is the multi-user Nix model minus the daemon. **Open verification item:** whether uv's cache insertion and hardlink-out work cleanly with a shared-group-writable cache dir across UIDs (locking/mtime assumptions); requires an experiment before v1 commits to A.
- **B (hardening path): install broker.** Store mounted ro in sandboxes; the cache-miss path is served by the single sanctioned writer outside the mount boundary, most naturally the registry proxy teeing artifacts into the store layout so the in-sandbox tool sees a hit. Cost: maintaining knowledge of cache insert layouts, exactly the fragility Section 5.2 minimizes. Only adopt if a threat model demands ro mounts.
- **C (rejected): full nix-daemon-style mediation.** Option A's UID discipline gets the same guarantee for free.

No option adds storage or downloads: a miss is downloaded once, inserted once, then linked by every subsequent environment.

## 8. GC algorithm

### 8.1 Phases

1. **Acquire** the GC lock for the target store (one lock file per store, `flock`).
2. **Snapshot leases**: read the lease directory; any artifact named by a live lease (PID exists, lease not expired) is pinned.
3. **Mark**: iterate roots dir; skip and prune dangling symlinks; for each live root, call `artifacts(workspace)`. Union with leases = live set.
4. **Enumerate** the store.
5. **Sweep**: delete `enumerated − live`, honoring `--max-freed` and `--dry-run`. Deletion order: artifacts first, then empty parent dirs.
6. **Release** the lock.

### 8.2 The fetch/sweep race (the only real hazard)

An installer may have fetched an artifact into the store that no committed lockfile references yet. Ordering rules (acquire-before-scan, made explicit):

- **Installer side:** before or atomically with the first write into the store, create a lease file `leases/<pid>-<nonce>` listing the artifact keys (or key prefixes) being installed; remove it after the workspace lockfile is committed. Wrappers/environment in the deployment set this up; for tools invoked outside any wrapper, a fallback grace period applies (see below).
- **Collector side:** leases are read *after* acquiring the GC lock and *before* enumeration. A lease created after the lock is acquired is still honored if it appears before enumeration completes (enumerate reads leases dir last, not first).
- **Fallback:** artifacts younger than a configurable grace window (default 24h by mtime) are never swept, protecting installs by unwrapped tools at the cost of delayed reclamation. This is a mitigation, not a correctness guarantee; deployments wanting guarantees use leases.
- **Crash safety:** leases carry PIDs and expiry; a lease whose PID is dead and expiry passed is ignored. All lease and root operations are atomic (symlink/rename/unlink).

### 8.3 Serialization

GC is single-writer per store (the lock). Root registration/deregistration and installs are never blocked by GC and never block each other (adds are monotone). Long stores can be swept incrementally with `--max-freed` to bound lock hold time.

Prior art for *when* to sweep: containerd's collector schedules itself on measured pause time (a target fraction of wall clock with the database unlocked) plus mutation and deletion thresholds, rather than on a fixed timer. If a periodic timer plus disk-pressure trigger turns out to stall installs behind the lock, that feedback loop is the shape to borrow.

## 9. CLI

```
pkgdd root add <name> <workspace>     # ln -s, idempotent
pkgdd root rm <name>
pkgdd root ls
pkgdd env create <id> [--template <t>] # workspace + env file + mount plan + root (layer 2)
pkgdd env destroy <id> [--keep-workspace]
pkgdd env plan <id> --format bwrap|oci|systemd|env
pkgdd status [--ecosystem <e>]        # live vs total per store, reclaimable bytes
pkgdd gc [--dry-run] [--max-freed N] [--ecosystem <e>] [--grace <dur>]
pkgdd lease hold <keys...>            # for wrapper integration
pkgdd lease release <lease-id>
pkgdd doctor                          # version pins, same-fs + empirical link-mode check, drift report
```

Binary name `pkgdd`; project name `package-dedupe`. Exit codes and `--json` output on every command for orchestrator consumption.

`doctor` includes a drift check: views whose contents diverge from their workspace's lockfile (e.g. an out-of-band `pip install` into a venv). Drift is reported, not repaired; repair is the deployment's reconciler's job.

## 10. Configuration

TOML at `/etc/pkgdedupe/config.toml`, overridable by env/flags:

```toml
roots_dir = "/var/lib/pkgdedupe/roots"
leases_dir = "/var/lib/pkgdedupe/leases"
grace_period = "24h"

[stores.uv]
path = "/srv/cache/uv"          # = UV_CACHE_DIR in deployments
[stores.pnpm]
path = "/srv/cache/pnpm-store"
[stores.cargo]
path = "/srv/cache/cargo"
[stores.go]
path = "/srv/cache/gomod"
```

Ships as a Nix flake: package, NixOS module (creates dirs, tmpfiles rules, optional `pkgdd gc` timer with disk-pressure trigger), and a devShell.

## 11. Dependencies

- **Runtime:** POSIX filesystem with hardlinks, atomic rename, flock. Nothing else. Nix is the packaging/deployment substrate, not a runtime dependency; the nix ecosystem is just one plugin.
- **Build:** Rust stable; serde + toml/serde_yaml for lockfile parsing; no daemon, no database, no network code in the core.
- **Prior art used as patterns, not code:** Nix gcroots/temproots, Kubernetes level-triggered reconciliation, pnpm/uv hardlink CAS (relied upon at runtime, version-pinned).

## 12. Non-goals and rejected designs (rationale log)

- **CRDT-based manifest merge:** unnecessary; the store is grow-only between sweeps and per-workspace manifests are single-writer, so no merge ever occurs (CALM: all operations except deletion are monotone).
- **Authoritative aggregate manifest:** rejected; the aggregate is a derived view, storing it independently reinvents the merge problem.
- **Custom install command / daemon-mediated installs:** rejected; adopting native lockfiles eliminates the bypass/drift class instead of policing it.
- **Own store format:** rejected; the native tools' CAS layouts are battle-tested, and physical dedup is their job.
- **Closure-addressing (Nix-style) for language packages:** deferred; artifact-addressing dedups better across divergent dependency graphs for wheel/tarball ecosystems. Measure real sharing ratios before revisiting.

## 13. Acceptance tests

1. Two workspaces lock the same wheel: one copy on disk (`st_nlink` ≥ 3: store + two venvs); second materialization performs zero downloads.
2. Deregister one root, `pkgdd gc`: nothing freed (other root still pins). Deregister both: artifact freed.
3. Kill an installer mid-fetch, run gc concurrently under load: no live artifact ever deleted (lease + grace paths both exercised); property-test with fault injection.
4. Crash the machine mid-gc: restart, rescan, converge; no state repair step required.
5. `pkgdd gc --dry-run` output equals subsequent real run's deletions under a frozen root set.
6. Stock coding agent in a configured deployment: `uv add requests && uv run python -c 'import requests'` succeeds untouched; a second session doing the same starts with warm cache and no downloads.

## 14. Open questions

- Lease integration depth: env-var/wrapper-based lease creation vs. fanotify-style store watching vs. grace-period-only for v0.
- sccache/`CARGO_TARGET_DIR` lifecycle: fold into the roots model later or leave to sccache's LRU permanently.
- Closure-level sharing measurement: instrument real agent workloads to decide whether Nix-plugin scope should expand.
- Upstreaming: propose `--keep-from-lockfiles` prune modes to uv and pnpm.
- Store write model A verification: shared-group uv cache behavior across UIDs (Section 7); result decides whether option B is needed sooner.
- Template workspaces for `env create --template`: pre-materialized common baselines cloned via CoW snapshot for millisecond starts; scope carefully to avoid the provisioner becoming a session manager.
