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
separate process on both platforms, and once it is libkrun's single-API
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

One row the table cannot carry, because the two are not the same size:
**pause and resume exist on both, snapshot exists only on Linux.** Cloud
Hypervisor pauses, writes `config.json`, `memory-ranges` and `state.json` to a
directory, and restores that into a fresh process, which comes back paused.
vfkit's `POST /vm/state` accepts `Pause`, `Resume`, `Stop` and `HardStop` and
nothing else. Virtualization.framework has had save and restore since macOS 14
and vfkit's state enum names `Saving` and `Restoring`, so this is an unexposed
capability rather than a missing one, but there is no way to ask for it today.
Pause and resume therefore belong in the backend interface and snapshot does
not: it is a Linux-only operational convenience, and no feature may be built
on it. The consequence for agent hibernation is recorded in
`vm-cave-todo.md`.

vfkit also ships a **pre-signed** binary carrying
`com.apple.security.virtualization`, which a nix-built binary would not have
and which is otherwise a codesigning problem.

Rejected, with reasons that may expire: **krunkit** is EFI-only, so it would
force the guest flake to emit an EFI disk instead of the kernel, initrd and
squashfs it already builds. It stays a later option, being deliberately
CLI-compatible with vfkit. **apple/containerization** is Swift, the wrong side
of a language boundary. **Tart** targets whole-VM workflows, not a service
plane. **Lima** carries its own guest agent and mount opinions, and wants a
NIC; it is a useful proof vehicle and a bad dependency.

Windows, if it ever matters, is a third backend and unaffected by any of this.

## The vsock plane

**Guest-initiated ports are identical on both VMMs**, which is worth stating
because it is easy to assume otherwise. The *host* creates and listens on a
unix socket per port, and the VMM dials it when the guest connects. This was
a socat process per port under CHV (`socat UNIX-LISTEN:${VSOCK}_7300,fork
...`), and vfkit's `listen` mode is the same shape. No porting work in this
direction. Note the consequence: something host-side must create these sockets
on *both* platforms, and macOS has no socat, so `cave-vm` does it in-process
on both. Half-close has to be propagated: git's client stops sending and keeps
reading, and a relay that tears down both directions at once truncates the
fetch.

**The entire delta is host-initiated.** CHV multiplexes every inbound port
over one socket, selected at connect time by a `CONNECT <port>` text handshake
answered `OK <n>`, so ports are dynamic. vfkit takes one `--device
virtio-vsock,port=N,...,connect` per port at VM launch, one unix socket each,
no handshake, so **the inbound port table is fixed when the VM boots**.

Normalized at `internal/vmm`, which is one package with the VMM rather than
the separate `internal/plane` the design first scoped: the port number, socket
path and direction are all vfkit launch arguments, so neither half can be
built without the other. Two methods cover the delta:

- `Listen(port)`, identical on both platforms
- `Dial(port)`: CHV writes the `CONNECT` handshake; vfkit connects to the
  per-port socket

The direction flag is the one place this fails silently. vfkit's `connect`
names the *host's* role, so a host-initiated port is the one that carries it
and a guest-initiated port is the bare default. Reversed, the socket still
appears and nothing ever connects to it.

**The constraint to honour on Linux too, even though CHV permits more: no
feature may allocate an inbound vsock port after boot.** The design's table is
already fixed (22, 7001, 7100, 7200, 7300), so nothing gives today. But a
future "one inbound port per agent session" would work on Linux and be
unportable, and that would be discovered late.

### Guest-side: bind AF_VSOCK explicitly

`systemd-ssh-generator` creates an `AF_VSOCK` listener by itself under Cloud
Hypervisor, and does not under vfkit, where it binds "AF_UNIX Local" only.
Terminal attach then has nothing to reach, with no error anywhere. It simply
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

The mitigation available is `vfkit --device rosetta`, which shares Rosetta
into the guest over virtio-fs and registers it through `binfmt_misc`, letting
the aarch64 guest execute x86_64 Linux binaries. It is deliberately not
designed in: the aarch64 guest runs native, and the two environments are
accepted as similar rather than equivalent. Rosetta becomes worth building
when an agent is actually blocked on an x86-only toolchain, which is the
entry it has in `vm-cave-todo.md`.

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

The guest has no time synchronization of any kind, no timesyncd, no NTP and
no `qemu-guest-agent`, and no NIC to reach a time server with anyway.

x86 KVM guests get `kvm-clock`, which tracks host adjustments across suspend.
Under HVF on aarch64 the guest uses `arch_sys_counter`, which does not. That
is why vfkit ships `--timesync` at all, and that flag wants
`qemu-guest-agent` on a vsock port, which this guest does not run.

A laptop lid closing therefore leaves the guest with a stale clock, and a
stale clock fails TLS certificate validation, and therefore every agent
request. The failure is maximally confusing: everything looks correctly
configured. This is the highest-value unfixed item in this document.

## Host-side portability

`vmcav` was a Linux shell script that broke in seven concrete places. Moving
the VM lifecycle into `cave-vm` closed five of them:

| Break | macOS | Now |
| --- | --- | --- |
| `grep /proc/$p/cmdline` for `is_running` | no `/proc` at all | `ps -o state=,comm=` in Go |
| `setsid` to detach the VM | absent | `syscall.Setsid` |
| `socat` in `require_tools` | absent | in-process relay |
| `python3` for the vsock handshake | present but needless | `cave-vm vsock-connect` |
| hardcoded `--memory size=4096M --cpus boot=4` | half of an 8 GB machine | `--memory/--cpus` flags |
| `readlink -f`, `cp --sparse=always`, `truncate -s` | BSD variants differ | the sparse copy and the grow are `cave-vm disk`; `readlink -f` is still in the script |
| `mke2fs`, `gpg` in `require_tools` | neither present | still in the script |
| `$BOOT/kernel/bzImage`, `packages.x86_64-linux.$a` | aarch64 | the flake output is derived from `uname -m` now; the bzImage path is still in the script |

What remains in the script is image creation and seed building, which is also
where the guest-architecture problem lives, so the two should be solved
together rather than ported twice.

One host assumption is worth naming because it is invisible on Linux: a guest
store path is not a host path. `init=/nix/store/...` names a path inside the
guest's filesystem, and on a Linux host the same path usually exists on the
host as well, so checking for it appears to work. On macOS the guest artifacts
are built in a Lima VM and that path exists nowhere on the host, so any check
against it rejects a VM that boots perfectly.

## Bootstrapping the guest image

New problem the Linux design does not have. Building the `aarch64-linux`
NixOS guest requires a Linux builder, and a Mac has none until it has a VM.
No phase in `vm-cave-design.md` covers this, and an `x86_64-linux` machine
cannot produce them either.

Resolved in practice: **a Lima NixOS VM is the aarch64 Linux builder.**
Everything substitutes from cache.nixos.org rather than compiling, and the
disk image builds through nested QEMU under TCG without trouble even on an M1
with no nested virtualization. The alternatives are shipping prebuilt
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

### Under `cave-vm` rather than by hand

Re-run once the lifecycle moved into Go, with `cave-vm start|stop` driving
vfkit and no shell script involved:

| Property | Result |
| --- | --- |
| `cave-vm start` boots the guest to a login prompt | yes |
| direction mapping (22 `connect`, 7200/7300/7400 `listening`) | yes |
| in-process relay carries the same git wire protocol | yes |
| host-initiated `Dial` reaches vfkit | yes, then `vsock:22` reset |
| REST power-down, filesystems unmounted, "System Power Off" | yes |
| teardown leaves no process, socket or pidfile | yes |

The `vsock:22` reset is guest-side and expected: this disk image predates the
explicit socat unit, so nothing in the guest is listening on vsock 22, which
is the same generator gap recorded above.

Two host bugs surfaced only against the real binary, neither reachable by
reading documentation:

- vfkit answers `POST /vm/state` with **202**, not the 200 its own docs
  promise. Treating anything but 200 as failure reports every clean shutdown
  as an error.
- `os.Process.Release()` sets `Pid` to -1. A pid read after it and written to
  a pidfile turns the later shutdown into `kill(-1)`, which signals every
  process the user owns.

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
   socat. Around twenty lines of Go, and what `internal/vmm` now owns on both
   platforms.

What remains is above this line rather than below it: `vm-root-agent` has
`requires = [ seed-claude-config.service clone-repo.service ]`, so a running
agent needs Claude credentials staged into the seed and the real git daemon on
vsock 7300. Neither is a macOS problem.
