"""Security tests for the git daemon and protocol handler.

Tests attack vectors against the daemon's security boundary. Each test
simulates a malicious agent attempting to escape its isolation.

To add a new attack scenario:
1. Add a method to a test class inheriting DaemonSecurityBase
2. Use self.raw_pkt() for raw protocol-level attacks
3. Use self.git() for git-level attacks
4. Use self.git_to_port() for cross-agent port hopping

Run with: python3 -m unittest -v tests.test_redteam
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cav import config, daemon, workspace
from cav.handler import parse_request, validate_service, resolve_repo

TEST_DAEMON_BASE_PORT = 19500  # distinct from test_daemon's 19400


def git(*args, cwd=None, check=True, env=None):
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, check=check,
        capture_output=True, text=True, env=merged,
    )


# -- Unit tests for handler functions (no daemon needed) --

class TestHandlerParsing(unittest.TestCase):
    """Unit tests for handler.py parsing functions."""

    def test_valid_upload_pack(self):
        result = parse_request(b"git-upload-pack /project\x00host=localhost\x00")
        self.assertEqual(result, ("git-upload-pack", "/project"))

    def test_valid_receive_pack(self):
        result = parse_request(b"git-receive-pack /my-repo\x00")
        self.assertEqual(result, ("git-receive-pack", "/my-repo"))

    def test_no_space_rejected(self):
        self.assertIsNone(parse_request(b"git-upload-pack\x00"))

    def test_empty_payload(self):
        self.assertIsNone(parse_request(b""))

    def test_non_ascii_rejected(self):
        self.assertIsNone(parse_request(b"\xff\xfe git-upload-pack /x\x00"))

    def test_validate_allowed_services(self):
        self.assertTrue(validate_service("git-upload-pack"))
        self.assertTrue(validate_service("git-receive-pack"))

    def test_validate_rejects_arbitrary_service(self):
        self.assertFalse(validate_service("git-daemon"))
        self.assertFalse(validate_service("/bin/sh"))
        self.assertFalse(validate_service(""))
        self.assertFalse(validate_service("git-upload-pack; rm -rf /"))


class TestPathResolution(unittest.TestCase):
    """Unit tests for resolve_repo path validation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = os.path.join(self.tmpdir, "repos")
        os.makedirs(os.path.join(self.base, "project"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_simple_path(self):
        result = resolve_repo(self.base, "/project")
        self.assertEqual(result, os.path.realpath(os.path.join(self.base, "project")))

    def test_dotdot_escape_rejected(self):
        self.assertIsNone(resolve_repo(self.base, "/../../../etc/passwd"))

    def test_dotdot_mid_path_rejected(self):
        self.assertIsNone(resolve_repo(self.base, "/project/../../etc/passwd"))

    def test_prefix_attack_rejected(self):
        """Path like /repos-evil should not match /repos base."""
        evil = os.path.join(self.tmpdir, "repos-evil")
        os.makedirs(evil)
        result = resolve_repo(self.base, "/../repos-evil")
        self.assertIsNone(result)

    def test_double_slash_normalized(self):
        result = resolve_repo(self.base, "//project")
        self.assertIsNotNone(result)
        self.assertIn("project", result)

    def test_null_in_path_rejected(self):
        result = resolve_repo(self.base, "/project\x00evil")
        self.assertIsNone(result)

    def test_symlink_followed_after_containment_check(self):
        """Symlinks in base_dir are OK (that's how repos are mounted)."""
        base = os.path.join(self.tmpdir, "actual-repo")
        os.makedirs(base)
        link = os.path.join(self.base, "linked")
        os.symlink(base, link)
        result = resolve_repo(self.base, "/linked")
        self.assertEqual(result, os.path.realpath(base))

    def test_empty_path_stays_in_base(self):
        result = resolve_repo(self.base, "/")
        if result is not None:
            self.assertTrue(result.startswith(os.path.realpath(self.base))
                            or result == os.path.realpath(self.base))


# -- Integration tests against a running daemon --

class DaemonSecurityBase(unittest.TestCase):
    """Base class for daemon security tests.

    Provides a running daemon for agent 1 and helpers for sending
    raw protocol data and git commands to arbitrary ports.
    """

    @classmethod
    def setUpClass(cls):
        cls._orig_base_port = config.DAEMON_BASE_PORT
        config.DAEMON_BASE_PORT = TEST_DAEMON_BASE_PORT

    @classmethod
    def tearDownClass(cls):
        config.DAEMON_BASE_PORT = cls._orig_base_port

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.user_repo = Path(self.tmpdir) / "project"
        self.data_dir = Path(self.tmpdir) / "data"

        os.environ["CODING_CAVE_DATA_DIR"] = str(self.data_dir)
        config.DATA_DIR = self.data_dir

        self.user_repo.mkdir()
        git("init", "-b", "main", cwd=self.user_repo)
        git("config", "user.email", "test@test.com", cwd=self.user_repo)
        git("config", "user.name", "Test", cwd=self.user_repo)
        (self.user_repo / "file.txt").write_text("content\n")
        git("add", "file.txt", cwd=self.user_repo)
        git("commit", "-m", "init", cwd=self.user_repo)

        from cav.init import init
        init(str(self.user_repo))

        self.shared = config.shared_repo_path(self.user_repo)
        self.slug = config.project_slug(self.user_repo)

        self.daemon_proc, self.daemon_dir = daemon.start(
            "1", [(self.shared, self.slug)])
        self.port = daemon._agent_port("1")

    def tearDown(self):
        daemon.stop(self.daemon_proc, self.daemon_dir)
        shutil.rmtree(self.tmpdir)

    def _ws(self, agent_id="1"):
        """Create workspace with daemon remote."""
        ws = workspace.create(self.user_repo, agent_id)
        git("config", "user.email", f"{agent_id}@test.com", cwd=ws)
        git("config", "user.name", f"Agent {agent_id}", cwd=ws)
        url = daemon.remote_url(agent_id, self.slug)
        git("remote", "set-url", config.HOST_SHARED_REMOTE, url, cwd=ws)
        return ws

    def raw_pkt(self, data: bytes, port: int | None = None) -> int:
        """Send raw bytes to the daemon port and return the exit behavior.

        Returns: number of bytes received (0 = connection closed/rejected).
        """
        port = port or self.port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            try:
                s.connect(("127.0.0.1", port))
                s.sendall(data)
                # Give handler time to process and respond or close
                time.sleep(0.1)
                try:
                    resp = s.recv(4096)
                    return len(resp)
                except (socket.timeout, ConnectionResetError):
                    return 0
            except ConnectionRefusedError:
                return -1

    def pkt_line(self, service: str, path: str) -> bytes:
        """Build a valid pkt-line request."""
        payload = f"{service} {path}\x00host=localhost\x00".encode()
        length = len(payload) + 4
        return f"{length:04x}".encode() + payload

    def git_to_port(self, port, *args, cwd=None, check=False):
        """Run a git command targeting a specific port."""
        env = {**os.environ}
        return subprocess.run(
            ["git"] + list(args), cwd=cwd, check=check,
            capture_output=True, text=True, env=env,
        )


class TestProtocolSecurity(DaemonSecurityBase):
    """Raw protocol-level attacks against the daemon handler."""

    def test_empty_connection(self):
        """Empty connection should be handled gracefully."""
        result = self.raw_pkt(b"")
        self.assertLessEqual(result, 0)

    def test_short_header(self):
        """Truncated pkt-line header (< 4 bytes)."""
        result = self.raw_pkt(b"00")
        self.assertLessEqual(result, 0)

    def test_invalid_hex_header(self):
        """Non-hex pkt-line header."""
        result = self.raw_pkt(b"ZZZZ")
        self.assertLessEqual(result, 0)

    def test_zero_length_pkt(self):
        """pkt-len of 0000 (flush packet)."""
        result = self.raw_pkt(b"0000")
        self.assertLessEqual(result, 0)

    def test_oversized_pkt_length(self):
        """Claimed length far exceeds actual data."""
        result = self.raw_pkt(b"ffff" + b"A" * 100)
        self.assertLessEqual(result, 0)

    def test_disallowed_service(self):
        """Request for a service not in the allowlist."""
        pkt = self.pkt_line("/bin/sh", "/project")
        result = self.raw_pkt(pkt)
        self.assertLessEqual(result, 0)

    def test_command_injection_in_service(self):
        """Semicolon injection in service name."""
        pkt = self.pkt_line("git-upload-pack; cat /etc/passwd", "/project")
        result = self.raw_pkt(pkt)
        self.assertLessEqual(result, 0)

    def test_path_traversal(self):
        """Path traversal via .. in request path."""
        pkt = self.pkt_line("git-upload-pack", "/../../../etc/passwd")
        result = self.raw_pkt(pkt)
        self.assertLessEqual(result, 0)

    def test_path_traversal_encoded(self):
        """Path traversal with normalized path that still escapes."""
        pkt = self.pkt_line("git-upload-pack", "/project/../../etc")
        result = self.raw_pkt(pkt)
        self.assertLessEqual(result, 0)

    def test_valid_request_succeeds(self):
        """Sanity check: valid request gets a git protocol response."""
        pkt = self.pkt_line("git-upload-pack", f"/{self.slug}")
        result = self.raw_pkt(pkt)
        self.assertGreater(result, 0)


class TestAgentIsolation(DaemonSecurityBase):
    """Tests for agent identity and cross-agent access controls."""

    def test_client_env_cannot_override_agent_id(self):
        """Client-side CODING_CAVE_AGENT_ID doesn't affect server-side identity."""
        ws = self._ws("1")
        (ws / "x.txt").write_text("x\n")
        git("add", "x.txt", cwd=ws)
        git("commit", "-m", "work", cwd=ws)
        result = git(
            "push", "host-cave", "HEAD:refs/heads/agent-2/main",
            cwd=ws, check=False, env={"CODING_CAVE_AGENT_ID": "2"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)

    def test_push_option_forgery_ignored(self):
        """Push options claiming a different agent ID are ignored."""
        ws = self._ws("1")
        (ws / "x.txt").write_text("x\n")
        git("add", "x.txt", cwd=ws)
        git("commit", "-m", "work", cwd=ws)
        result = git(
            "push", "host-cave", "HEAD:refs/heads/agent-2/main",
            "--push-option=agent=2",
            cwd=ws, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)

    def test_cross_daemon_port_hopping(self):
        """Agent 1 connecting to agent 2's daemon port gains agent-2 identity.

        At the daemon level, identity is bound to which daemon serves the
        connection, not which client connects. This test confirms the daemon
        behavior. The actual mitigation is in sandbox.py: each top-level
        agent runs inside a slirp4netns network namespace that only forwards
        its own daemon port, making other agents' ports unreachable.
        """
        # Start a second daemon for agent 2
        daemon2_proc, daemon2_dir = daemon.start(
            "2", [(self.shared, self.slug)])
        try:
            ws1 = self._ws("1")
            (ws1 / "poison.txt").write_text("injected by agent 1\n")
            git("add", "poison.txt", cwd=ws1)
            git("commit", "-m", "malicious commit", cwd=ws1)

            # Agent 1 pushes through agent 2's daemon port
            port2 = daemon._agent_port("2")
            url2 = f"git://127.0.0.1:{port2}/{self.slug}"
            result = git(
                "push", url2, "HEAD:refs/heads/agent-2/main",
                cwd=ws1, check=False,
            )
            # At the daemon level this succeeds (agent 2's daemon sets
            # CODING_CAVE_AGENT_ID=2). Mitigated by slirp4netns isolation.
            self.assertEqual(result.returncode, 0,
                "Expected port-hop push to succeed (mitigated by slirp4netns)")
        finally:
            daemon.stop(daemon2_proc, daemon2_dir)

    def test_non_branch_ref_push_rejected(self):
        """Agent cannot push tags or other non-branch refs."""
        ws = self._ws("1")
        (ws / "x.txt").write_text("x\n")
        git("add", "x.txt", cwd=ws)
        git("commit", "-m", "work", cwd=ws)
        result = git(
            "push", "host-cave", "HEAD:refs/tags/v1.0",
            cwd=ws, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REJECTED", result.stderr)


class TestNamespaceFirewall(unittest.TestCase):
    """Verify iptables rules applied via nsenter from the parent survive
    attacks from the inner user namespace (simulating bwrap's sandbox)."""

    def _have(self, *bins):
        for b in bins:
            if not shutil.which(b):
                self.skipTest(f"{b} not available")

    def test_inner_namespace_cannot_modify_outer_iptables(self):
        """Agent in bwrap's inner user namespace cannot flush, delete,
        insert, or change policy on iptables rules set via nsenter
        from the parent."""
        self._have("iptables-restore", "nsenter")
        result = subprocess.run(
            [sys.executable, "-c", _IPTABLES_TEST_CODE],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
            f"Namespace firewall test failed:\n{result.stderr}")

    def test_iptables_restore_works_in_empty_netns(self):
        """Firewall rules can be applied before slirp4netns starts."""
        self._have("iptables-restore", "nsenter")
        result = subprocess.run(
            [sys.executable, "-c", _IPTABLES_EMPTY_NETNS_CODE],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
            f"Empty netns iptables test failed:\n{result.stderr}")


# Test: parent applies iptables via nsenter, inner namespace can't modify.
# Mirrors the real startup: parent creates rules, bwrap inner ns attacks.
_IPTABLES_TEST_CODE = r'''
import os, subprocess, sys
from pathlib import Path

sync_r, sync_w = os.pipe()

child = os.fork()
if child == 0:
    os.close(sync_r)
    uid, gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS | os.CLONE_NEWNET)
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"0 {uid} 1\n")
    Path("/proc/self/gid_map").write_text(f"0 {gid} 1\n")
    os.write(sync_w, b"1")
    os.close(sync_w)

    # Wait for parent to apply firewall, then test from inner namespace.
    import time; time.sleep(1)

    inner = os.fork()
    if inner == 0:
        os.unshare(os.CLONE_NEWUSER)
        Path("/proc/self/setgroups").write_text("deny")
        Path("/proc/self/uid_map").write_text("0 0 1\n")
        Path("/proc/self/gid_map").write_text("0 0 1\n")
        attacks = [
            ["iptables", "-F"],
            ["iptables", "-D", "OUTPUT", "-d", "10.0.2.2", "-j", "DROP"],
            ["iptables", "-I", "OUTPUT", "-d", "10.0.2.2", "-j", "ACCEPT"],
            ["iptables", "-P", "OUTPUT", "ACCEPT"],
        ]
        for cmd in attacks:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                sys.stderr.write(f"ATTACK SUCCEEDED: {cmd}\n")
                os._exit(1)
        os._exit(0)
    _, status = os.waitpid(inner, 0)
    os._exit(os.WEXITSTATUS(status))

os.close(sync_w)
os.read(sync_r, 1)
os.close(sync_r)

# Parent applies firewall via nsenter (mirrors _apply_firewall).
rules = "*filter\n-A OUTPUT -d 10.0.2.2 -j DROP\nCOMMIT\n"
r = subprocess.run(
    ["nsenter", "--preserve-credentials",
     f"--user=/proc/{child}/ns/user", f"--net=/proc/{child}/ns/net",
     "iptables-restore"],
    input=rules, text=True, capture_output=True)
assert r.returncode == 0, f"iptables-restore failed: {r.stderr}"

_, status = os.waitpid(child, 0)
sys.exit(os.WEXITSTATUS(status))
'''

# Test: iptables-restore works in an empty namespace (no TAP, no routes).
# Confirms the firewall-before-network invariant.
_IPTABLES_EMPTY_NETNS_CODE = r'''
import os, subprocess, sys
from pathlib import Path

sync_r, sync_w = os.pipe()

child = os.fork()
if child == 0:
    os.close(sync_r)
    uid, gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS | os.CLONE_NEWNET)
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"0 {uid} 1\n")
    Path("/proc/self/gid_map").write_text(f"0 {gid} 1\n")
    os.write(sync_w, b"1")
    os.close(sync_w)
    import time; time.sleep(1)
    # Verify rules are present in the empty namespace.
    r = subprocess.run(["iptables", "-L", "OUTPUT", "-n"],
                       capture_output=True, text=True)
    os._exit(0 if "DROP" in r.stdout else 1)

os.close(sync_w)
os.read(sync_r, 1)
os.close(sync_r)

# Apply firewall to empty namespace (no interfaces configured yet).
rules = "*filter\n-A OUTPUT -d 10.0.2.2 -j DROP\nCOMMIT\n"
r = subprocess.run(
    ["nsenter", "--preserve-credentials",
     f"--user=/proc/{child}/ns/user", f"--net=/proc/{child}/ns/net",
     "iptables-restore"],
    input=rules, text=True, capture_output=True)
assert r.returncode == 0, f"iptables-restore failed: {r.stderr}"

_, status = os.waitpid(child, 0)
sys.exit(os.WEXITSTATUS(status))
'''


class TestDaemonStress(DaemonSecurityBase):
    """Concurrent connection stress tests."""

    def test_rapid_connections(self):
        """20 rapid connections don't crash the daemon."""
        for _ in range(20):
            self.raw_pkt(b"0000")
        pkt = self.pkt_line("git-upload-pack", f"/{self.slug}")
        result = self.raw_pkt(pkt)
        self.assertGreater(result, 0)

    def test_mixed_valid_invalid(self):
        """Alternating valid and invalid requests don't corrupt state."""
        for i in range(5):
            self.raw_pkt(b"ZZZZ")
            pkt = self.pkt_line("git-upload-pack", f"/{self.slug}")
            result = self.raw_pkt(pkt)
            self.assertGreater(result, 0, f"Failed on iteration {i}")


if __name__ == "__main__":
    unittest.main()
