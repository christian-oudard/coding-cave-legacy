/// Generate a seccomp BPF filter for the coding cave sandbox.
/// Outputs raw cBPF bytecode suitable for bwrap's --add-seccomp-fd.
use std::io::Write;
use std::process::exit;

// BPF instruction classes
const BPF_LD: u16 = 0x00;
const BPF_JMP: u16 = 0x05;
const BPF_RET: u16 = 0x06;
const BPF_W: u16 = 0x00;
const BPF_ABS: u16 = 0x20;
const BPF_JEQ: u16 = 0x10;
const BPF_K: u16 = 0x00;

const SECCOMP_RET_ALLOW: u32 = 0x7FFF_0000;
const SECCOMP_RET_ERRNO: u32 = 0x0005_0000;
const EPERM: u32 = 1;

const AUDIT_ARCH_X86_64: u32 = 0xC000_003E;
const AUDIT_ARCH_AARCH64: u32 = 0xC000_00B7;

const OFFSET_NR: u32 = 0;
const OFFSET_ARCH: u32 = 4;

fn blocked_x86_64() -> Vec<u32> {
    let mut v = vec![
        425, 426, 427, // io_uring_setup/enter/register
        248, 249, 250, // add_key/request_key/keyctl
        321,           // bpf
        212,           // lookup_dcookie
        175, 313, 176, // init_module/finit_module/delete_module
        169, 246, 320, 163, // reboot/kexec_load/kexec_file_load/acct
        173, 172,      // ioperm/iopl
        304,           // open_by_handle_at
        167, 168,      // swapon/swapoff
        164, 159, 305, // settimeofday/adjtimex/clock_adjtime
        179,           // quotactl
        180,           // nfsservctl
        323,           // userfaultfd
        99,            // sysinfo
    ];
    v.sort();
    v
}

fn blocked_aarch64() -> Vec<u32> {
    let mut v = vec![
        425, 426, 427, // io_uring
        217, 218, 219, // add_key/request_key/keyctl
        280,           // bpf
        18,            // lookup_dcookie
        105, 273, 106, // init_module/finit_module/delete_module
        142, 104, 294, 89, // reboot/kexec_load/kexec_file_load/acct
        265,           // open_by_handle_at
        224, 225,      // swapon/swapoff
        171, 266,      // adjtimex/clock_adjtime
        60,            // quotactl
        282,           // userfaultfd
        179,           // sysinfo
    ];
    v.sort();
    v
}

#[repr(C, packed)]
struct SockFilter {
    code: u16,
    jt: u8,
    jf: u8,
    k: u32,
}

fn insn(code: u16, jt: u8, jf: u8, k: u32) -> [u8; 8] {
    let f = SockFilter { code, jt, jf, k };
    let mut out = [0u8; 8];
    out[0..2].copy_from_slice(&f.code.to_le_bytes());
    out[2] = f.jt;
    out[3] = f.jf;
    out[4..8].copy_from_slice(&f.k.to_le_bytes());
    out
}

fn generate_filter(arch: &str) -> Vec<u8> {
    let (audit_arch, numbers) = match arch {
        "x86_64" => (AUDIT_ARCH_X86_64, blocked_x86_64()),
        "aarch64" => (AUDIT_ARCH_AARCH64, blocked_aarch64()),
        _ => {
            eprintln!("Unknown arch: {arch}");
            exit(1);
        }
    };
    let n = numbers.len();
    let mut out = Vec::new();

    // [0] Load arch
    out.extend_from_slice(&insn(BPF_LD | BPF_W | BPF_ABS, 0, 0, OFFSET_ARCH));
    // [1] If wrong arch, jump to ALLOW
    out.extend_from_slice(&insn(
        BPF_JMP | BPF_JEQ | BPF_K,
        0,
        (n + 1) as u8,
        audit_arch,
    ));
    // [2] Load syscall number
    out.extend_from_slice(&insn(BPF_LD | BPF_W | BPF_ABS, 0, 0, OFFSET_NR));
    // [3..3+n-1] Check each blocked syscall
    for (i, &nr) in numbers.iter().enumerate() {
        let jt = (n - i) as u8; // jump to DENY
        out.extend_from_slice(&insn(BPF_JMP | BPF_JEQ | BPF_K, jt, 0, nr));
    }
    // ALLOW
    out.extend_from_slice(&insn(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ALLOW));
    // DENY
    out.extend_from_slice(&insn(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ERRNO | EPERM));

    out
}

fn main() {
    let mut arch = "x86_64".to_string();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        if let Some(a) = args[i].strip_prefix("--arch=") {
            arch = a.to_string();
        } else if args[i] == "--arch" && i + 1 < args.len() {
            i += 1;
            arch = args[i].clone();
        }
        i += 1;
    }
    let filter = generate_filter(&arch);
    std::io::stdout().write_all(&filter).unwrap();
}
