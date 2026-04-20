"""Generate a seccomp BPF filter for the coding cave sandbox.

Produces raw cBPF bytecode (array of struct sock_filter) suitable for
bwrap's --add-seccomp-fd. Uses a denylist: block specific dangerous
syscalls, allow everything else.

Usage: python3 seccomp_gen.py [--arch x86_64|aarch64] > filter.bpf
"""

import struct
import sys

# struct sock_filter: u16 code, u8 jt, u8 jf, u32 k
FILTER_FMT = "HBBI"

# BPF instruction classes and modes
BPF_LD = 0x00
BPF_JMP = 0x05
BPF_RET = 0x06
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JEQ = 0x10
BPF_K = 0x00

# seccomp return values
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
EPERM = 1

# AUDIT_ARCH values
AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

# struct seccomp_data field offsets
OFFSET_NR = 0    # syscall number
OFFSET_ARCH = 4  # architecture

# Blocked syscall numbers by architecture.
# These have no legitimate use inside a development sandbox.
BLOCKED_X86_64 = {
    # io_uring: massive kernel attack surface, dozens of CVEs.
    # Available with SQPOLL and all features without seccomp.
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    # Kernel keyring: readable from sandbox (session + user keyrings).
    "add_key": 248,
    "request_key": 249,
    "keyctl": 250,
    # eBPF: kernel extension mechanism.
    "bpf": 321,
    # Kernel profiling / tracing. perf_event_open stays allowed so
    # in-cave profiling (perf, cargo flamegraph, py-spy --native) works;
    # perf_event_paranoid + CapEff=0 already restrict it to own-process
    # userspace samples.
    "lookup_dcookie": 212,
    # Kernel modules.
    "init_module": 175,
    "finit_module": 313,
    "delete_module": 176,
    # System administration.
    "reboot": 169,
    "kexec_load": 246,
    "kexec_file_load": 320,
    "acct": 163,
    # I/O port access.
    "ioperm": 173,
    "iopl": 172,
    # Filesystem handle bypass (Docker CVE-2015-1334).
    "open_by_handle_at": 304,
    # Swap.
    "swapon": 167,
    "swapoff": 168,
    # Time manipulation.
    "settimeofday": 164,
    "adjtimex": 159,
    "clock_adjtime": 305,
    # Disk quotas.
    "quotactl": 179,
    # NFS.
    "nfsservctl": 180,
    # userfaultfd (defense in depth).
    "userfaultfd": 323,
    # Host fingerprinting: sysinfo(2) returns totalram/freeram/uptime
    # kernel-globally (not per-cgroup, not per-ns). Masking /proc/meminfo
    # alone doesn't stop runtime GCs (Node libuv, JVM fallback) from
    # reading host RAM via this syscall. Returning EPERM makes them
    # degrade to defaults: Node's os.totalmem()/os.freemem()/os.uptime()
    # return 0; glibc sysconf(_SC_PHYS_PAGES) falls back to /proc/meminfo
    # which is also masked. Go/Rust runtimes don't use sysinfo at all.
    "sysinfo": 99,
}

BLOCKED_AARCH64 = {
    # io_uring (same numbers as x86_64, added post-unification).
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    # Kernel keyring.
    "add_key": 217,
    "request_key": 218,
    "keyctl": 219,
    # eBPF.
    "bpf": 280,
    # Profiling / tracing. perf_event_open stays allowed; see x86_64 note.
    "lookup_dcookie": 18,
    # Kernel modules.
    "init_module": 105,
    "finit_module": 273,
    "delete_module": 106,
    # System administration.
    "reboot": 142,
    "kexec_load": 104,
    "kexec_file_load": 294,
    "acct": 89,
    # open_by_handle_at.
    "open_by_handle_at": 265,
    # Swap.
    "swapon": 224,
    "swapoff": 225,
    # Time manipulation.
    "adjtimex": 171,
    "clock_adjtime": 266,
    # Disk quotas.
    "quotactl": 60,
    # userfaultfd.
    "userfaultfd": 282,
    # Host fingerprinting: sysinfo(2). See x86_64 entry for rationale.
    "sysinfo": 179,
    # Note: ioperm, iopl, nfsservctl, settimeofday not present on aarch64.
}

ARCHES = {
    "x86_64": (AUDIT_ARCH_X86_64, BLOCKED_X86_64),
    "aarch64": (AUDIT_ARCH_AARCH64, BLOCKED_AARCH64),
}


def generate_filter(arch: str) -> bytes:
    """Generate raw BPF bytecode for the given architecture."""
    audit_arch, blocked = ARCHES[arch]
    numbers = sorted(blocked.values())
    n = len(numbers)

    insns = []

    def emit(code, jt, jf, k):
        insns.append(struct.pack(FILTER_FMT, code, jt, jf, k))

    # [0] Load architecture
    emit(BPF_LD | BPF_W | BPF_ABS, 0, 0, OFFSET_ARCH)
    # [1] If wrong arch, skip to ALLOW (don't filter foreign-arch syscalls)
    emit(BPF_JMP | BPF_JEQ | BPF_K, 0, n + 1, audit_arch)
    # [2] Load syscall number
    emit(BPF_LD | BPF_W | BPF_ABS, 0, 0, OFFSET_NR)
    # [3..3+n-1] Check each blocked syscall
    for i, nr in enumerate(numbers):
        # If match, jump to DENY at index [3+n+1] = [n+4].
        # From index [3+i], jt base is [n+4], relative = n+4 - (3+i+1) = n-i.
        emit(BPF_JMP | BPF_JEQ | BPF_K, n - i, 0, nr)
    # [3+n] ALLOW
    emit(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ALLOW)
    # [4+n] DENY (EPERM)
    emit(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ERRNO | EPERM)

    return b"".join(insns)


if __name__ == "__main__":
    arch = "x86_64"
    for arg in sys.argv[1:]:
        if arg.startswith("--arch="):
            arch = arg.split("=", 1)[1]
        elif arg == "--arch" and sys.argv.index(arg) + 1 < len(sys.argv):
            arch = sys.argv[sys.argv.index(arg) + 1]
    if arch not in ARCHES:
        print(f"Unknown arch: {arch}. Supported: {', '.join(ARCHES)}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.buffer.write(generate_filter(arch))
