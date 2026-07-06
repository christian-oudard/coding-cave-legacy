# cave-guest-dev

Minimal NixOS guest for the bootstrap vm-root-agent (see `../vm-root-agent.md`).

## Build

crinj's intercepting CA is baked into the guest's system trust store at build
time (`security.pki`), so the build is impure and needs the CA in the
environment, else the eval fails loudly:

    export CODING_CAVE_CRINJ_CA="$(cat ~/.crinj/gateway/ca.pem)"
    nix build --impure \
      ./guest#nixosConfigurations.cave-guest-dev.config.system.build.toplevel

`vmcav` and the smoke test set this for you. An in-guest redeploy reads the CA
from the mounted seed share instead, so the env var is not needed there.

## Boot (bootstrap era)

Production boots via Cloud Hypervisor, which requires `/dev/kvm`. The
`packages.vm` output is a QEMU runner that also boots under TCG software
emulation when KVM is absent (same CA requirement as the build above):

    export CODING_CAVE_CRINJ_CA="$(cat ~/.crinj/gateway/ca.pem)"
    nix run --impure ./guest#vm

## State persistence

`vmcav` keeps the guest's disk image at `$STATE_DIR/cave-guest.qcow2`
(default `~/.local/share/coding-cave/vm/`), in your home directory rather
than a temp dir, so guest state (the cloned repo, agent session, nix store
writes) survives reboots and cave sessions. The image is sparse, so it
consumes only what the guest actually writes, up to the cap in
`~/.config/coding-cave/config.toml` (default 100 GB). To start clean, stop
the VM and delete the image by hand (`rm "$STATE_DIR/cave-guest.qcow2"`);
the next `vmcav start` recreates it. The smoke test uses a throwaway disk
under its work dir, never touching this one.

## Environment constraint

This was developed inside a coding-cave that has **no `/dev/kvm`** (no
virtualization passthrough) and no CPU virt flags. Consequences:

- Cloud Hypervisor cannot run here (it has no non-KVM fallback).
- The QEMU runner works but under TCG emulation, slowly.
- To boot-test with acceleration, relaunch the cave with `--device /dev/kvm`
  (nested KVM), which the VM-per-cave workflow already assumes.

Config validity is verified every iteration by evaluating the toplevel
derivation, which needs neither KVM nor a full build.

## Verified boot (TCG, no KVM)

`../scripts/smoke-boot.sh` builds the guest, boots it headless under TCG,
and asserts it reached the milestones that matter. A run inside this
KVM-less cave confirmed the full path end to end:

- initrd -> switch-root -> NixOS activation -> Multi-User System
- `seed-claude-config.service` finished (wrote `/root/.claude.json`)
- `clone-repo.service` finished (cloned the repo from a git daemon the
  smoke test runs, reached over user-net at 10.0.2.2)
- `vm-root-agent.service` started (claude launched in tmux as root)

Not covered without crinj: live API auth. The guest reaches Anthropic only
through crinj (absent in the smoke test), so the agent starts claude but
cannot complete a real API call here. That path is exercised by
`../scripts/vmcav start` on a host with crinj and `/dev/kvm`.

## Redeploy loop (agent's core capability)

Verified in a live guest that `nixos-rebuild` works from inside:

- Activation (`switch-to-configuration`) reloads units and keeps the system
  up. It now returns 0; previously the stock `logrotate-checkconf` failure
  (logrotate rejects the store-owned config through the 9p mount) made it
  return non-zero, which the agent's redeploy loop would misread as a failed
  deploy and roll back. logrotate is therefore disabled.
- Evaluation resolves `nixpkgs` offline from the 9p-shared host store, but
  the `claude-code` flake input is re-fetched from github, so a full
  `nixos-rebuild switch --flake` needs network, which crinj provides on a
  real host. Vendoring/pinning the flake inputs to remove that github
  dependency is a worthwhile robustness follow-up.
