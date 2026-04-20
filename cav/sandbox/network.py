"""Network namespace setup: slirp4netns and iptables firewall.

Every nesting level gets its own network namespace, populated by
slirp4netns on the tap0 interface (default 10.0.2.0/24). An
iptables-restore rule applied via nsenter locks egress down to a
caller-specified allowlist of TCP ports; everything else bound for
SLIRP_GATEWAY is dropped. The rules live in the outer user namespace
so bwrap's inner user namespace cannot modify them.
"""

import os
import shutil
import subprocess

SLIRP_GATEWAY = "10.0.2.2"
# slirp4netns built-in DNS forwarder address.
SLIRP_DNS = "10.0.2.3"


def _apply_firewall(pid: int, allowed_ports: list[int]):
    """Apply iptables firewall rules inside the child's network namespace.

    Uses nsenter to enter the child's user+net namespace and applies rules
    atomically via iptables-restore. Only the listed TCP ports can reach
    the gateway (host loopback). All other gateway traffic is dropped.

    The rules live in the outer user namespace, so bwrap's inner user
    namespace (where the agent runs) cannot modify them.
    """
    rules = "*filter\n"
    for port in allowed_ports:
        rules += (f"-A OUTPUT -d {SLIRP_GATEWAY} -p tcp"
                  f" --dport {port} -j ACCEPT\n")
    rules += f"-A OUTPUT -d {SLIRP_GATEWAY} -j DROP\nCOMMIT\n"

    nsenter = shutil.which("nsenter")
    if not nsenter:
        raise SystemExit("nsenter not found on PATH (install util-linux)")
    iptables_restore = shutil.which("iptables-restore")
    if not iptables_restore:
        raise SystemExit(
            "iptables-restore not found on PATH (install iptables)")

    r = subprocess.run(
        [nsenter, "--preserve-credentials",
         f"--user=/proc/{pid}/ns/user", f"--net=/proc/{pid}/ns/net",
         iptables_restore],
        input=rules, text=True, capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"iptables-restore failed: {r.stderr.strip()}")


def _start_slirp4netns(pid: int) -> subprocess.Popen:
    """Start slirp4netns for a child process.

    Configures tap0 with default 10.0.2.0/24 network. Uses --ready-fd
    to know when networking is fully configured before returning.
    """
    slirp_bin = shutil.which("slirp4netns")
    if not slirp_bin:
        raise SystemExit("slirp4netns not found on PATH")

    ready_r, ready_w = os.pipe()
    slirp_proc = subprocess.Popen(
        [slirp_bin, "--configure", f"--ready-fd={ready_w}",
         str(pid), "tap0"],
        start_new_session=True,
        pass_fds=(ready_w,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(ready_w)

    # Block until slirp4netns signals the network is ready.
    os.read(ready_r, 1)
    os.close(ready_r)

    if slirp_proc.poll() is not None:
        raise SystemExit(
            f"slirp4netns exited with code {slirp_proc.returncode}")

    return slirp_proc
