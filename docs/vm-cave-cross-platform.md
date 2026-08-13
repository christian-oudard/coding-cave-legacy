# cave-vm across Linux and macOS

What running the design in `vm-cave-design.md` on both a Linux host and an
Apple silicon host costs, what it cannot make identical, and which
differences must be absorbed rather than exposed.

Evidence is from a `Macmini9,1` (M1, 8 GB, macOS 26.6.1) running vfkit v0.6.4
against the real cave guest, not from documentation.

## What is already identical

Everything above the VMM. The same NixOS configuration, the same systemd
units, the same service plane, the same bwrap caves under per-uid separation,
one VM shared by every agent. The guest never learns which VMM it is under:
it speaks plain `AF_VSOCK` either way.

The rest of this document is the exceptions.

## The VMM layer

**Linux runs Cloud Hypervisor, macOS runs vfkit, both as separate processes
behind one interface.**

The deciding argument is a project requirement, not a feature comparison.
Phase 10 requires the VM to outlive its supervisor ("kill the supervisor,
restart it, the VM and every agent session survive"), recorded because
containerd's shim exists for exactly that. libkrun's `krun_start_enter()`
makes the calling thread become the VM, so linking it puts the guest inside
the supervisor, which cannot then be restarted. The VMM must therefore be a
separate process on both platforms — and once it is, libkrun's single-API
advantage evaporates, because we talk to it over a socket rather than through
its C API. The cgo tension with the `CGO_ENABLED=0` ground rule is moot rather
than traded away.

vfkit is the only macOS VMM process covering CHV's full feature set:

| design need | Cloud Hypervisor | vfkit |
| --- | --- | --- |
| direct kernel boot | `--kernel/--initramfs/--cmdline` | `--bootloader linux,kernel=,initrd=,cmdline=` |
| root disk | `--disk path=,image_type=raw` | `--device virtio-blk,path=` |
| guest-initiated vsock | `--vsock cid=N,socket=` | `--device virtio-vsock,port=N,socketURL=` |
| host-initiated vsock | same socket, `CONNECT` handshake | same, plus `,connect`, one per port |
| console | `--serial file=` | `--device virtio-serial,logFilePath=` (also `pty`, `stdio`) |
| supervisor re-attach | `--api-socket path=` | `--restful-uri unix://` |
| shutdown | `ch-remote power-button` | REST `/vm/state` |
| no NIC | omit | omit |
| privilege | `/dev/kvm` | entitlement, no device |

It also ships a **pre-signed** binary carrying
`com.apple.security.virtualization`, which a nix-built binary would not have
and which is otherwise a codesigning problem.

Rejected, with reasons that may expire: **krunkit** is EFI-only, so it would
force the guest flake to emit an EFI disk instead of the kernel, initrd and
squashfs it already builds — it stays a later option, being deliberately
CLI-compatible with vfkit. **apple/containerization** is Swift, the wrong side
of a language boundary. **Tart** targets whole-VM workflows, not a service
plane. **Lima** carries its own guest agent and mount opinions, and wants a
NIC; it is a useful proof vehicle and a bad dependency.

Windows, if it ever matters, is a third backend and unaffected by any of this.

## The vsock plane

**Guest-initiated ports are identical on both VMMs**, which is worth stating
because it is easy to assume otherwise. The *host* creates and listens on a
unix socket per port, and the VMM dials it when the guest connects. `vmcav`'s
`start_bridges` does exactly this under CHV (`socat
UNIX-LISTEN:${VSOCK}_7300,fork ...`), and vfkit's `listen` mode is the same.
No porting work in this direction. Note the consequence: something host-side
must create these sockets on *both* platforms. On Linux that is socat; macOS
has no socat, so `cave-vm` has to do it in-process.

**The entire delta is host-initiated.** CHV multiplexes every inbound port
over one socket, selected at connect time by a `CONNECT <port>` text handshake
answered `OK <n>`, so ports are dynamic. vfkit takes one `--device
virtio-vsock,port=N,...,connect` per port at VM launch, one unix socket each,
no handshake — so **the inbound port table is fixed when the VM boots**.

Normalise at `internal/plane`, which the design already scopes for this ("vsock
port map, host unix-socket dialer/listener, guest forwarder"). Two methods
cover it:

- `Listen(port)` — identical on both platforms
- `Dial(port)` — CHV writes the `CONNECT` handshake; vfkit connects to the
  per-port socket

**The constraint to honour on Linux too, even though CHV permits more: no
feature may allocate an inbound vsock port after boot.** The design's table is
already fixed (22, 7001, 7100, 7200, 7300), so nothing gives today. But a
future "one inbound port per agent session" would work on Linux and be
unportable, and that would be discovered late.

### Guest-side: bind AF_VSOCK explicitly

`systemd-ssh-generator` creates an `AF_VSOCK` listener by itself under Cloud
Hypervisor, and does not under vfkit, where it binds "AF_UNIX Local" only.
Terminal attach then has nothing to reach, with no error anywhere — it simply
does not connect.

vfkit is not at fault. Running a listener by hand in the guest returned the
banner on the host at once:

    # socat VSOCK-LISTEN:22,fork,reuseaddr TCP:127.0.0.1:22
    SSH-2.0-OpenSSH_10.3

So the guest binds it explicitly with a socat unit alongside the outbound
bridges, removing the dependency on a generator heuristic that differs per
VMM. This is the class of bug to expect from vsock generally: it is a
second-class path in a lot of software, and failures are silent.

## What cannot be unified: the guest architecture

A Linux host runs an `x86_64` guest, an Apple silicon host runs `aarch64`.
Same NixOS, same units, different machine code.

Nix-built content is equivalent across both. What is not equivalent is
everything an agent pulls that nix never sees: npm optional dependencies, pip
wheels, Playwright browsers, elan and lean toolchains, `go install` output,
container images. A cave that works on one host can fail on the other for
reasons the agent cannot diagnose or fix. `aarch64-linux` cache coverage is
good but not identical, so a package that substitutes on x86 may build from
source, which on four cores is not free.

The mitigation is `vfkit --device rosetta`, which shares Rosetta into the
guest over virtio-fs and registers it through `binfmt_misc`, letting the
aarch64 guest execute x86_64 Linux binaries. Designing it in is what makes the
two environments behaviourally equivalent rather than merely similar.

Two smaller architecture consequences: the kernel artifact is `bzImage` on
x86 and `Image` on aarch64, and the console is `ttyS0` on x86 (the 8250 port,
deliberately not the virtio console, because `virtio_console` is a module and
`hvc0` would be silent through early boot) but must be `hvc0` on aarch64,
where Virtualization.framework exposes no legacy UART at all. The early-boot
blind spot is unavoidable there.

Fortunately the Apple silicon uncompressed-kernel requirement costs nothing:
nixpkgs emits `Image`, not `Image.gz`, for `aarch64-linux`, so
`system.build.kernel` is directly bootable.

## Guest time: unhandled on both, worse on macOS

The guest has no time synchronisation of any kind — no timesyncd, no NTP, no
`qemu-guest-agent` — and no NIC to reach a time server with anyway.

x86 KVM guests get `kvm-clock`, which tracks host adjustments across suspend.
Under HVF on aarch64 the guest uses `arch_sys_counter`, which does not. That
is why vfkit ships `--timesync` at all, and that flag wants
`qemu-guest-agent` on a vsock port, which this guest does not run.

A laptop lid closing therefore leaves the guest with a stale clock, and a
stale clock fails TLS certificate validation, and therefore every agent
request. The failure is maximally confusing: everything looks correctly
configured. This is the highest-value unfixed item in this document.

## Host-side portability

`vmcav` is a Linux shell script and breaks in seven concrete places:

| Break | macOS |
| --- | --- |
| `grep /proc/$p/cmdline` for `is_running` | no `/proc` at all |
| `setsid` to detach the VM | absent |
| `readlink -f` | BSD readlink has no `-f` |
| `cp --sparse=always` | `cp -c` clones on APFS, and is better |
| `truncate -s` | absent |
| `socat`, `mke2fs`, `gpg` in `require_tools` | none present |
| `$BOOT/kernel/bzImage`, `packages.x86_64-linux.$a` | aarch64 |

plus hardcoded `--memory size=4096M --cpus boot=4`, which is half of an 8 GB
machine.

**Nearly all of this disappears when the VM lifecycle moves out of the script
into `cave-vm`.** Go's `os/exec` and `syscall.Setsid` are portable, liveness
is a pidfile plus a signal, and the unix sockets the plane needs are
`net.Listen`. The script is where the portability debt lives, which argues for
pulling that move forward rather than porting the script.

## Bootstrapping the guest image

New problem the Linux design does not have. Building the `aarch64-linux`
NixOS guest requires a Linux builder, and a Mac has none until it has a VM.
No phase in `vm-cave-design.md` covers this, and an `x86_64-linux` machine
cannot produce them either.

Resolved in practice: **a Lima NixOS VM is the aarch64 Linux builder.**
Everything substitutes from cache.nixos.org rather than compiling, and the
disk image builds through nested QEMU under TCG without trouble even on an M1
with no nested virtualisation. The alternatives are shipping prebuilt
artifacts (what nixos-lima does), `nix-darwin`'s `linux-builder` or
`nix-rosetta-builder`, or building on a Linux machine and copying.

## Operational differences worth deciding deliberately

- **No nested virtualization before M3.** Apple gates it to M3 and later on
  macOS 15+; M1 and M2 cannot do it at all, in the hypervisor, whatever VMM
  runs on top. `vm-cave-design.md` cites "nested KVM keeps the subcave
  workflow" as part of choosing CHV; on pre-M3 hardware there is no subcave
  workflow to keep. Docker Sandboxes runs on M1 and offers no nesting either.
- **macOS `ulimit -n` defaults to 256**, tight once there is a VMM plus one
  unix socket per vsock port per agent.
- **APFS is case-insensitive by default.** Safe for the guest store, which
  lives inside a raw image, but not for host-side state.
- **XDG resolution** puts state under `~/.local/share` on macOS, against
  platform convention. Consistency across hosts is defensible, but it should
  be a choice rather than an accident.

## Evidence

Validated against vfkit v0.6.4 on the M1, booting the real cave guest:

| Property | Result |
| --- | --- |
| vfkit runs, pre-signed entitlement honoured | yes |
| direct kernel boot of the NixOS `Image` | yes |
| unmodified `chvDisk` raw image as virtio-blk, `root=/dev/vda` | yes |
| NixOS 26.11 aarch64 to multi-user, sshd up | yes |
| virtio-serial console, including `pty` for an interactive shell | yes |
| REST API over unix socket, pause/resume/stop | yes |
| guest-initiated vsock, real traffic | yes |
| host-initiated vsock, real traffic | yes |

The guest-initiated proof is the guest's own clone-repo service reaching the
host, carrying real git wire protocol:

    003fgit-upload-pack /cave-shared.git\0host=127.0.0.1\0\0version=2\0

The full egress path, guest to internet, with crinj logging the CONNECT:

    guest curl -> 127.0.0.1:10255 -> socat -> vsock 7200 -> vfkit
      -> host unix socket -> relay -> crinj -> example.com   (status=200)

### The macOS recipe, and what each step needs

1. **VMM.** `vfkit` from its GitHub release, pre-signed. No Homebrew, no nix.
2. **Guest artifacts.** Built for `aarch64-linux` in the Lima NixOS VM.
3. **Seed disk.** `mke2fs -d` in the Lima VM too, since macOS has no mke2fs.
   Carries `authorized_keys`, `ca.pem`, `gitconfig`. Without it there is no
   `/etc/cave-host` and sshd has nothing to authenticate against.
4. **crinj.** Cross-compiles to darwin/arm64 unmodified with
   `CGO_ENABLED=0`, because the sqlite driver is pure-Go
   `modernc.org/sqlite`.
5. **A unix-to-TCP relay** for each guest-initiated port, since macOS has no
   socat. Around twenty lines of Go, and exactly what `internal/plane` owns.

What remains is above this line rather than below it: `vm-root-agent` has
`requires = [ seed-claude-config.service clone-repo.service ]`, so a running
agent needs Claude credentials staged into the seed and the real git daemon on
vsock 7300. Neither is a macOS problem.
