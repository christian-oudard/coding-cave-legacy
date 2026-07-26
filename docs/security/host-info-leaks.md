# Host information leaks from inside a cave

What a process inside a cave can learn about the host machine, and which of
it is closable under bwrap. Written after a zeroed `/proc/meminfo` and a
seccomp block on `sysinfo(2)` were found to hide values that leak anyway.

Read this before adding a mask. Several obvious-looking masks protect nothing
because the same value is reachable another way, and two of them broke real
software while protecting nothing.

## Policy

Leave development and process execution unimpeded. Hiding host information is
secondary, and under bwrap it is futile: the cave shares the host kernel and
host filesystems, so the mount table alone gives up the host username,
directory layout, volume names, filesystem sizes and total memory, and none of
that is closable here. Host isolation is being redone with a VM per cave,
which closes this class of leak properly. Do not spend effort extending the
bwrap masks in the meantime.

The masks listed under "Closed" stay because they are free. A *new* mask has
to pay for itself, and the bar is high: if the value is recoverable through a
channel we cannot close, the mask is cost without benefit. Never accept
breakage in exchange for one. Both masks removed in this document's first
revision broke real software while hiding nothing.

## Closed

- Host username and home path, via `/etc/passwd`, `/etc/group`, `--clearenv`
  and `--unsetenv` of the `CODING_CAVE_*` and `PWD` variables. Verified: no
  host username appears in the cave's environment. But see the mount table
  below, which defeats this.
- Host hostname, via `--hostname`, `/etc/hostname`, `/etc/hosts`.
- Host timezone and geographic region, via a UTC `/etc/localtime`.
- Host CPU brand and model, via a synthesized `/proc/cpuinfo`. CPUID itself
  is honest, so feature detection still works; only `/proc/cpuinfo` parsers
  see the generic values.
- Kernel build, config, symbols, modules, and the live side channels
  (`interrupts`, `diskstats`, `partitions`, `swaps`, and so on), via the
  `/proc` mask list.
- Cross-cave correlation via `/proc/sys/kernel/random/boot_id`.
- Host audio and ACPI hardware identity, via masked `/proc/asound` and
  `/proc/acpi`. `/proc/asound/cards` names the exact laptop model.
- The host's PID namespace and every host process's `environ`, `cmdline` and
  `root`. Only cave processes are visible, and `--as-pid-1` keeps bwrap's own
  argv (full of host paths) out of `/proc/1`.
- `/sys` is absent rather than empty. It is the richest fingerprint surface
  in the kernel: DMI board vendor and serials, MAC addresses, device
  topology. Keep it absent. glibc's `_SC_NPROCESSORS_ONLN` falls back to
  `sched_getaffinity` when `/sys/devices/system/cpu/online` is missing, so
  the common consumer degrades correctly.

## Open and structural

These cannot be closed while the cave is a bwrap sandbox sharing the host
kernel and filesystems. Each needs a fresh superblock (a loop-mounted image)
or a VM.

### The mount table

`/proc/self/mountinfo` is world-readable and every bind mount's fourth field
is the mount's path *within its source superblock*, i.e. the host path. This
single file gives up:

- **Host username and directory layout.** Roughly a dozen entries carry
  `/<user>/.local/share/coding-cave/...`, `/<user>/files/code/...` and
  similar. This defeats the fake `/etc/passwd` entirely.
- **Host block devices and volume names**, e.g. the LVM volume group and
  logical volume names carrying `/nix/store` and the cave's data dir.
- **Host filesystem sizes**, via `statfs` on any bind mount. `df` inside a
  cave prints the host's real capacity and usage.
- **Host total memory**, to about four significant figures, from two
  kernel-computed tmpfs `size=` options:

  | Mount | Source | Fraction of MemTotal |
  | - | - | - |
  | `/dev/{null,zero,full,random,urandom,tty}` | host devtmpfs | `boot.devSize`, default `5%` |
  | forwarded gpg-agent socket | host `/run/user/1000` | systemd `RuntimeDirectorySize`, default `10%` |

  Solving each independently gave answers agreeing to 40 kB out of 32 M, and
  their ratio is 1.9999975, as expected from 5% and 10% of a shared base.
  This is an inference, not a measurement: it rests on the documented
  defaults of those two options (`nixos/modules/tasks/filesystems.nix`), and
  a host overriding either would yield a wrong number. It would still yield
  a number, so the conclusion holds.

  Both carriers are load-bearing. The devtmpfs entries exist because char
  device nodes cannot be created in a user namespace, so bwrap must bind the
  host's; the tmpfs entry exists because the gpg-agent socket is forwarded.

`/proc/self/mounts` is a lesser version of the same thing: no source-path
field, but it still carries the tmpfs `size=` options and the device names.

### `/proc/stat`

Not masked, and it hands over more than three of the masks above protect:

- `btime`, the host's boot time as an absolute wall-clock timestamp. This is
  a precise cross-cave correlator, the same thing masking `boot_id` and
  `/proc/uptime` was for.
- Cumulative jiffies for every host CPU, a live host-activity channel
  strictly better than the masked `/proc/loadavg`.
- `processes`, `procs_running`, `procs_blocked`: host-global process counts.

Two caves on one host were confirmed to read a byte-identical `btime`, so the
correlator is demonstrated rather than theorized. `now - btime` also yields
host uptime by subtraction, which means the `/proc/uptime` and
`/proc/loadavg` masks two lines away in the config are already defeated and
protect nothing.

Accepted as-is, deliberately. `/proc/stat` is one file and bwrap can only bind
a whole file over it, so `btime` cannot be rewritten while leaving the CPU
counters live: any static replacement freezes them, making every CPU-percent
readout in the cave read zero forever and risking zero-delta division in
poll-and-diff code, which is the same silent wrongness this document exists to
warn about. Keeping the counters live would need a dynamic provider (FUSE
lxcfs-style, or an in-cave updater). Neither is worth building for a channel
the VM closes for free.

### Other

- `uname(2)` release, version and machine. Kernel-global, read from the UTS
  struct, no per-namespace knob. Leaks the host kernel version and build
  date even though `/proc/version` is masked.
- `clock_gettime(CLOCK_BOOTTIME)`. Reveals host boot time by correlation
  with the wall clock. Blocking it would break systemd, glib, and every
  monotonic timer.
- Host CPU count, via `sched_getaffinity(2)`. Blocking it breaks every build
  system. Note the consequence: software in a cave sees all host cores
  whether or not it can use them, and `make -j$(nproc)` over-commits.

## Deliberately not hidden

**Host memory size.** Recoverable from the mount table as shown above, so
hiding it at the `/proc/meminfo` and `sysinfo(2)` layers only broke callers:

- Blocking `sysinfo(2)` with `SECCOMP_RET_ERRNO`/EPERM is worse than allowing
  it. glibc 2.42's `__get_phys_pages` calls `__sysinfo` and discards the
  return value, so `sysconf(_SC_PHYS_PAGES)` returned uninitialized stack: a
  different absurd number per call. Headless Chromium `CHECK`s physical
  memory and died with SIGILL in 15 of 30 launches. Any errno has this
  effect, ENOSYS included; glibc does not consult it and does not fall back
  to `/proc/meminfo`. Only `SECCOMP_RET_USER_NOTIF` could answer with
  synthetic values, and it cannot hide what the mount table gives away.
- A zero-valued `/proc/meminfo` leaves every memory heuristic in the cave
  guessing. Nothing else in the cave reveals a memory budget: `vmstat`,
  `zoneinfo` and `buddyinfo` are masked, `/sys/fs/cgroup` is absent, and
  `RLIMIT_AS` is unlimited. Runtimes then fall back to fixed defaults, e.g.
  the JVM sizes its ergonomic max heap from the 128 GiB `MaxRAM` default
  rather than from real memory. Real values beat both zeros and a plausible
  fiction, and they keep `/proc/meminfo` consistent with what the syscall
  now reports.

A cave has no memory *budget* to report, only the host's total, so the
over-commit above is honest about the wrong machine. That is the same
tradeoff already accepted for CPU count via `sched_getaffinity`. Giving
caves a real budget needs per-cave cgroup limits plus a synthesized view of
them, which is what lxcfs does for containers and what the VM gets for free.
