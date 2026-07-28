# cave-guest-dev

Minimal NixOS guest for the bootstrap vm-root-agent (see `../docs/vm-root-agent.md`).

## Shape

Cloud Hypervisor, direct kernel boot, no NIC. The guest flake exposes the four
artifacts the launcher needs:

    packages.x86_64-linux.{kernel,initrd,disk,toplevel}

`disk` is a bare ext4 filesystem image (`make-disk-image` with
`partitionTableType = "none"`), so the kernel gets `root=/dev/vda` with no
partition table and no bootloader. Because nothing on the disk picks the
generation, the cmdline names `${toplevel}/init` explicitly.

Two disks: `vda` is the persistent root, `vdb` is the read-only seed
(`/etc/cave-host`). The seed is an ext4 image built rootlessly with
`mke2fs -d`, not a filesystem share: CHV has no 9p, and its virtio-fs would
need a virtiofsd the launcher has to supervise.

## Build

crinj's intercepting CA is baked into the guest's system trust store at build
time (`security.pki`), so the build is impure and needs the CA in the
environment, else the eval fails loudly:

    export CODING_CAVE_CRINJ_CA="$(cat ~/.crinj/gateway/ca.pem)"
    nix build --impure ./guest#packages.x86_64-linux.disk

`vmcav` sets this for you. An in-guest redeploy reads the CA from the mounted
seed instead, so the env var is not needed there.

## Networking: there is none

The guest has no network device, so unproxied egress is impossible by
construction rather than by firewall rule. Host services arrive over
virtio-vsock and are re-exposed on localhost by two socat units, so the proxy
env, the agent's git remotes, and everything else stay ignorant of vsock:

    127.0.0.1:10255  ->  vsock 7200  ->  crinj on the host
    127.0.0.1:9418   ->  vsock 7300  ->  the host git daemon

CHV's vsock is the unix-socket hybrid, so no `/dev/vhost-vsock` and no kernel
module are involved. A guest connection to port N surfaces on the host as the
unix socket `<vsock.sock>_N`, which `vmcav` bridges. Host-initiated
connections (ssh) go to `<vsock.sock>` with a `CONNECT <port>` handshake,
which is what `vmcav vsock-connect` does as ssh's `ProxyCommand`.

## Console

The boot log goes to the 8250 serial port (`--serial file=...`), not the
virtio console. `virtio_console` is a module, so `hvc0` stays silent until the
initrd loads it and an early-boot failure would leave no trace at all.

## Requirements

Cloud Hypervisor is KVM-only with no software-emulation fallback, so
`/dev/kvm` is required. Inside a cave, relaunch with `--device /dev/kvm`;
`vmcav start` checks for it and refuses early with that hint.

## State persistence

`vmcav` keeps the guest's root disk at `$STATE_DIR/cave-guest.raw` (default
`~/.local/share/coding-cave/vm/`), in your home rather than a temp dir, so
guest state (the cloned repo, agent session, nix store writes) survives
reboots. It is created sparse from the built image on first start and grown to
the cap in `~/.config/coding-cave/config.toml` (default 100 GB), so it
consumes only what the guest writes. To start clean, stop the VM and delete
it; the next `vmcav start` recreates it. The seed disk is rebuilt every start.

## Redeploy loop (agent's core capability)

`nixos-rebuild switch` works from inside: activation reloads units and keeps
the system up. Two things to know:

- logrotate is disabled. Its config check rejects the store-owned config and
  returned non-zero from `switch-to-configuration`, which the agent's redeploy
  loop would misread as a failed deploy and roll back. journald self-rotates.
- Direct kernel boot means the *kernel* comes from the host at each start, so
  a generation that changes the kernel needs a host-side restart to take
  effect. Userspace changes activate in place. Moving to bootloader boot is
  the graduation step recorded in `../docs/vm-root-agent.md`.
