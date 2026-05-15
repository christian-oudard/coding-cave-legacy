# cave-vm: Go bring-up plan

Forward-looking notes for the cleanroom Go rewrite of VM-per-cave on
branch `vm_cave`. Prior Nix/bash exploration is archived in commit
`c74e21a` and removed in `401482a`; the tree currently matches
`host-cave/main` plus those two history entries.

## Carry-over conclusions from the Nix exploration

These are inputs to the Go rewrite, not things to redo:

- **VMM: Cloud Hypervisor.** Picked for nested KVM, which keeps
  `cav agent`'s cave-on-cav subcave workflow intact without a
  host-side spawn daemon. Measured at ~130 ms plain boot, ~550 ms
  for CHV-in-CHV nested cold start (~290 ms nested overhead).
- **Store sharing: virtio-fs of host `/nix/store`, RO**, with a
  writable upper-layer overlay backed by a per-VM volume. Avoids
  the nix-daemon database race that RW shared-store would create.
- **Env mirroring: virtio-fs of host home, RW.** The user's home
  on the host already has the home-manager-materialised view of
  their `cave.nix` (`.gitconfig`, `.zshrc`, `.vimrc`,
  `.nix-profile` → store path); sharing the dir 1:1 gets git/vim/
  zsh/python/etc. inside the VM with no separate build step.
- **VM lifecycle is long-running, not lifetime-coupled to a shell
  session.** The bash-wrapper approach got this wrong; the Go
  rewrite separates "start the VM" from "attach a shell to it"
  from day one.
- **microvm.nix's stock virtiofsd-run is unusable in an
  unprivileged cave** (`supervisord --dropPrivileges`,
  `chown :kvm` on the socket). The launcher must drive virtiofsd
  directly.
- **Kernel config invariants the VM needs:** `CONFIG_VIRTUALIZATION`
  is the Kconfig parent that gates the entire KVM subtree (silent
  drops if off); `CONFIG_KVM` + `CONFIG_KVM_INTEL` + `CONFIG_KVM_AMD`
  for nested; `CONFIG_PCI` + `CONFIG_VIRTIO_PCI` because CHV puts
  every device on virtio-pci; `CONFIG_PVPANIC_PCI` for defence in
  depth; `CONFIG_ACPI_SLEEP` so `poweroff` triggers ACPI S5 and
  CHV exits cleanly.
- **Sanity check the build, don't trust Kconfig:** assert that
  required symbols (`vmx_init`, `pvpanic_pci_probe`,
  `__pci_register_driver`, ...) are in the linked kernel; silent
  Kconfig drops cost us multiple 12-min rebuild cycles.

## Open scope questions (asked, awaiting answer)

1. **Directory layout.** Options:
   - `cave-vm/` at repo root, its own Go module.
   - `cave-vm/` inside `cav-rs/` (co-located, mixed-language).
   - Separate repo, vendored via flake input.
2. **"Bare VM" MVP definition.** Two candidate floors:
   - Smallest Go program that exec's cloud-hypervisor with an
     off-the-shelf kernel + initramfs from nixpkgs and exits. No
     NixOS, no virtio-fs, no shared store, no guest config.
   - One step smaller: a shell script equivalent first, then start
     wrapping it in Go once we know the moving parts work.

Default assumption while waiting: option **1a** (`cave-vm/` at repo
root) + the smaller-MVP definition (Go binary, off-the-shelf kernel,
no shares).

## Phased plan

Each phase is one commit (or a small handful) with something that
runs end-to-end. No "wire it all up later" deferrals.

### Phase 0 — Project skeleton

- `cave-vm/go.mod` declaring the module.
- `cave-vm/main.go` with cobra-style or stdlib `flag` argv parsing
  for the subcommand grammar we'll grow into.
- `cave-vm/cmd/` directory layout TBD (probably `start`, `shell`,
  `stop` as the eventual subcommands; Phase 0 only ships `start`).
- `flake.nix` builds the Go binary as a package via `buildGoModule`,
  exposed as `packages.cave-vm`. Vendor deps if any; ideally
  zero-dep stdlib for Phase 0.

Acceptance: `nix run .#cave-vm -- --help` prints usage.

### Phase 1 — Boot any kernel under CHV from Go

- `cave-vm start` exec's cloud-hypervisor with an off-the-shelf
  nixpkgs kernel (the linux package's `bzImage`, or a vmlinux if
  the package exposes one) and a minimal initramfs.
- Initramfs can be the simplest possible busybox cpio. Defer the
  slim cave-kernel; use whatever boots.
- CHV invoked with `--serial tty`, so the user's terminal is the
  guest serial console. Exit on guest shutdown.

Acceptance: `nix run .#cave-vm -- start` boots, prints something,
exits cleanly.

### Phase 2 — Slim cave kernel

- Re-introduce a slim Linux kernel derivation (we know the working
  CONFIG_* set from the prior exploration). System.map symbol
  assertions stay; they paid for themselves.
- `cave-vm start` switches to the slim kernel.

Acceptance: boot time on cave-kernel matches the ~130 ms we
measured before.

### Phase 3 — virtio-fs share for /nix/store

- Go spawns virtiofsd as a child process, waits for the socket,
  exec's CHV with `--fs tag=ro-store,socket=...`.
- Guest initramfs (or NixOS, depending on Phase 4 sequencing)
  mounts the share at `/nix/.ro-store` plus an overlay for writes.

Acceptance: a host-built derivation visible from inside the guest.

### Phase 4 — Real guest distro

- Either a hand-rolled NixOS config (no microvm.nix dependency) or
  reuse microvm.nix's NixOS module while replacing its bash runner
  with our Go binary.
- Auto-login as `cave-coder` (uid 1000), zsh login shell.

Acceptance: `cave-vm start` lands on a `[cave-coder@cave-vm ~]$`
prompt with all the system services up.

### Phase 5 — virtio-fs share for user home

- Second virtiofsd, host `$HOME` → guest `/home/cave-coder`.
- Read `$HOME` at run time (the prior bash launcher hardcoded
  `/home/cave-coder`, which broke when invoked from the host).
- uid alignment with the host user's uid (read at run time).

Acceptance: user's git config / vim config / zsh prompt all show
up inside the guest.

### Phase 6 — Long-running lifecycle

- `cave-vm start` daemonises the VM (or registers it with
  systemd-user, depending on the surrounding env).
- `cave-vm shell` attaches an extra console session via vsock
  (guest runs a vsock-getty) so the launching shell exit doesn't
  kill the VM.
- `cave-vm stop` sends an ACPI shutdown via CHV's REST API socket
  (or `ch-remote`).

Acceptance: start a VM, open three shells into it, exit one, the
VM and the other two shells survive.

### Phase 7 — Subcave + nested

- Re-introduce the CHV-in-CHV subcave path now that the basics
  work. Kernel needs `CONFIG_KVM_INTEL`+`CONFIG_VIRTUALIZATION`;
  CHV needs `--cpus nested=on`; host needs
  `kvm_intel.nested=1`.

Acceptance: `cav agent` (or a stand-in) running inside a cave-vm
can spawn another cave-vm.

### Phase 8 — cav integration

- `cav agent --vm` spawns a cave-vm instead of bwrap.
- `cav shell` attaches via the same mechanism Phase 6 added.
- This is when the work crosses from `cave-vm/` Go into `cav-rs/`
  Rust. Treat it as a separate milestone, plan it then.

## Things to watch

- **Go ↔ CHV API.** CHV exposes a REST API over a Unix socket;
  `ch-remote` is the existing client. Go side will probably either
  shell out to ch-remote or implement the small subset of the API
  it needs natively. Decide in Phase 6, not earlier.
- **vsock for shell multiplexing.** Guest needs a vsock-listening
  getty (or an SSH-over-vsock-equivalent). NixOS module options for
  this exist; if not, a small custom systemd unit on top of
  `socat VSOCK-LISTEN:...`.
- **uid mapping.** Host user's uid (often 1000, but not always) has
  to align with the guest cave-coder uid for virtio-fs file
  ownership to stay sane. Run-time configurable, not baked.
- **Don't re-add `vm-transition-plan.md`.** That doc was a moving
  target during exploration; this file replaces it. Once the Go
  bring-up is real, fold the load-bearing decisions into SPEC.md
  and delete this plan.
