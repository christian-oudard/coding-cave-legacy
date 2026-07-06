# cave-vm (vm-cave branch)

Cleanroom Go rewrite of Coding Cave around a single rootless VM for all
agents. This branch does not depend on the Rust implementation: no code is
imported from it, and nothing here may reference cav-rs paths or build
outputs.

The Rust implementation remains the reference for behavior (bwrap profile,
seccomp policy, git approval flow, claude config seeding). To consult it,
switch branches: `main` is the live Rust tree, `vm_cave` (underscore) is
Rust plus the planning history. Switch back to `vm-cave` (hyphen) to work.
Lift semantics from the Rust code, not code.

## Documents

- `vm-cave-design.md` is the authoritative plan: architecture, Go module
  layout, and the ten implementation phases with acceptance checks.
- `vm-cave-plan.md` and `vm-cave-store.md` are earlier planning and review
  records carried over for their settled decisions and rationale.
- `store-design-review.md` reviews the Rust implementation's store-sharing
  history: what was tried, why each design failed, and the option analysis
  that led to this architecture.
- `package-dedupe-spec.md` specifies pkgdd, the standalone GC-roots tool
  for language package caches (uv, pnpm, cargo, go). The guest ships it
  and cave-guestd calls it; cave-vm implements none of it.
- `vm-root-agent.md` designs the in-VM bootstrap agent: a root-in-guest
  Claude session that builds the in-guest system from within and redeploys
  itself via NixOS generations, behind the unchanged host boundary.

## Ground rules

- Go module at `cave-vm/`. Host binary `cave-vm`, guest binary
  `cave-guestd`. Static builds, CGO_ENABLED=0.
- Rootless host footprint: /dev/kvm access only. No root, no setuid, no
  system units, no host mounts.
- The guest has no NIC. All host/guest traffic is vsock; crinj is the only
  path to the internet.
- Work phase by phase per vm-cave-design.md; each phase lands runnable with
  its acceptance check before the next begins.
