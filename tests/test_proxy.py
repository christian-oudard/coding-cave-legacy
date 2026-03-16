"""Tests for proxy-based credential isolation (onecli integration)."""
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_cave.config import _setup_proxy, setup_env

FLAKE_NIX = Path(__file__).parent.parent / "flake.nix"
LIFECYCLE_PY = FLAKE_NIX.parent / "coding_cave" / "lifecycle.py"


class TestProxyConfig:
    """Config tests for _setup_proxy and setup_env proxy interaction."""

    def test_onecli_proxy_sets_default_port(self, tmp_path):
        """onecli = true sets port 10255 by default."""
        ca = tmp_path / "ca.pem"
        ca.write_text("--- cert ---")
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODING_CAVE_PROXY_PORT", None)
            with patch("coding_cave.config.Path.expanduser", return_value=ca):
                _setup_proxy(cfg)
            assert os.environ.get("CODING_CAVE_PROXY_PORT") == "10255"

    def test_no_oauth_token_when_proxy_enabled(self, tmp_path):
        """CLAUDE_CODE_OAUTH_TOKEN is NOT set when proxy is configured."""
        ca = tmp_path / "ca.pem"
        ca.write_text("--- cert ---")
        cfg = {
            "proxy": {"onecli": True},
            "credentials": {"claude-oauth-token": "secret-token"},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            os.environ.pop("CODING_CAVE_PROXY_PORT", None)
            with patch("coding_cave.config._git_config", return_value=None), \
                 patch("coding_cave.config._detect_gpg_agent_socket", return_value=None), \
                 patch("coding_cave.config.Path.expanduser", return_value=ca):
                setup_env(cfg)
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ

    def test_oauth_token_set_without_proxy(self):
        """Backward compat: token IS set without proxy config."""
        cfg = {"credentials": {"claude-oauth-token": "secret-token"}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            os.environ.pop("CODING_CAVE_PROXY_PORT", None)
            with patch("coding_cave.config._git_config", return_value=None), \
                 patch("coding_cave.config._detect_gpg_agent_socket", return_value=None):
                setup_env(cfg)
            assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "secret-token"

    def test_onecli_custom_port(self, tmp_path):
        """Port can be overridden."""
        ca = tmp_path / "ca.pem"
        ca.write_text("--- cert ---")
        cfg = {"proxy": {"onecli": True, "port": 8080}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODING_CAVE_PROXY_PORT", None)
            with patch("coding_cave.config.Path.expanduser", return_value=ca):
                _setup_proxy(cfg)
            assert os.environ.get("CODING_CAVE_PROXY_PORT") == "8080"

    def test_onecli_ca_cert_from_default_path(self, tmp_path):
        """CA cert loaded from ~/.onecli/gateway/ca.pem."""
        ca = tmp_path / "ca.pem"
        ca.write_text("--- cert ---")
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODING_CAVE_PROXY_CA_CERT", None)
            with patch("coding_cave.config.Path.expanduser", return_value=ca):
                _setup_proxy(cfg)
            assert os.environ.get("CODING_CAVE_PROXY_CA_CERT") == str(ca.resolve())

    def test_onecli_ca_cert_missing_skipped(self):
        """CA cert not set when gateway hasn't generated it yet."""
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODING_CAVE_PROXY_CA_CERT", None)
            _setup_proxy(cfg)
            assert "CODING_CAVE_PROXY_CA_CERT" not in os.environ

    def test_no_proxy_returns_false(self):
        """_setup_proxy returns False when no proxy configured."""
        assert _setup_proxy({}) is False
        assert _setup_proxy({"proxy": {}}) is False
        assert _setup_proxy({"proxy": {"onecli": False}}) is False


class TestFlakeNixProxy:
    """Static analysis of flake.nix for proxy support."""

    @staticmethod
    def _extract_blocks(text, block_type):
        """Extract shell code from (block_type '' ... '') blocks in flake.nix."""
        blocks = []
        pattern = re.compile(
            r"\(" + re.escape(block_type) + r"\s+''(.*?)(?<!')''\)",
            re.DOTALL,
        )
        for m in pattern.finditer(text):
            blocks.append(m.group(1))
        return blocks

    def test_proxy_addr_forwarded(self):
        """CODING_CAVE_PROXY_ADDR has try-fwd-env in flake.nix."""
        flake = FLAKE_NIX.read_text()
        assert '(try-fwd-env "CODING_CAVE_PROXY_ADDR")' in flake

    def test_proxy_env_in_wrap_entry(self):
        """HTTP_PROXY/HTTPS_PROXY/NO_PROXY set in wrap-entry."""
        flake = FLAKE_NIX.read_text()
        # These must be in wrap-entry (inside cave), not add-runtime (host)
        add_runtime_blocks = self._extract_blocks(flake, "add-runtime")
        for block in add_runtime_blocks:
            clean = block.replace("''$", "$").replace("''\\", "\\")
            assert "HTTP_PROXY" not in clean, \
                "HTTP_PROXY must be set in wrap-entry, not add-runtime"

        # Verify they exist somewhere in the flake (wrap-entry)
        assert "HTTP_PROXY" in flake
        assert "HTTPS_PROXY" in flake
        assert "NO_PROXY" in flake

    def test_combined_ca_bundle_created(self):
        """Combined CA bundle created from system + proxy CA in wrap-entry."""
        flake = FLAKE_NIX.read_text()
        assert "/tmp/combined-ca.pem" in flake
        assert "SSL_CERT_FILE" in flake

    def test_node_extra_ca_certs_set(self):
        """NODE_EXTRA_CA_CERTS set in wrap-entry for onecli CA."""
        flake = FLAKE_NIX.read_text()
        assert "NODE_EXTRA_CA_CERTS" in flake
        assert "/etc/onecli-ca.pem" in flake

    def test_proxy_addr_unset_after_use(self):
        """CODING_CAVE_PROXY_ADDR is in the unset line (setup-only var)."""
        flake = FLAKE_NIX.read_text()
        # Find the unset block
        assert "CODING_CAVE_PROXY_ADDR" in flake
        # Verify it's in the unset statement (same line group)
        unset_match = re.search(r"unset\s+.*CODING_CAVE_PROXY_ADDR", flake, re.DOTALL)
        assert unset_match, "CODING_CAVE_PROXY_ADDR should be in the unset statement"


class TestProxyLifecycle:
    """Verify proxy support in lifecycle.py."""

    def test_proxy_vars_in_allowlist(self):
        """CODING_CAVE_PROXY_ADDR is in BWRAP_ENV_ALLOWLIST."""
        src = LIFECYCLE_PY.read_text()
        assert '"CODING_CAVE_PROXY_ADDR"' in src

    def test_ca_cert_bind_mounted(self):
        """CA cert bind-mounted at /etc/onecli-ca.pem when configured."""
        src = LIFECYCLE_PY.read_text()
        assert "/etc/onecli-ca.pem" in src
        assert "CODING_CAVE_PROXY_CA_CERT" in src

    def test_proxy_pasta_uses_loopback_mapping(self):
        """Proxy mode uses --map-host-loopback 10.0.2.2."""
        src = LIFECYCLE_PY.read_text()
        assert "10.0.2.2" in src

    def test_no_proxy_blocks_loopback(self):
        """Without proxy, loopback is mapped to 192.0.2.1 (blocked)."""
        src = LIFECYCLE_PY.read_text()
        assert "192.0.2.1" in src

    def test_credentials_not_in_allowlist_comment(self):
        """CODING_CAVE_PROXY_PORT and CODING_CAVE_PROXY_CA_CERT are NOT in allowlist.

        These are host-side only vars consumed by lifecycle.py before bwrap starts.
        They should not leak into the cave's env.
        """
        src = LIFECYCLE_PY.read_text()
        # Extract the allowlist set
        match = re.search(r"BWRAP_ENV_ALLOWLIST\s*=\s*\{([^}]+)\}", src, re.DOTALL)
        assert match, "BWRAP_ENV_ALLOWLIST not found"
        allowlist_text = match.group(1)
        assert "CODING_CAVE_PROXY_PORT" not in allowlist_text, \
            "PROXY_PORT is host-side only, must not be in allowlist"
        assert "CODING_CAVE_PROXY_CA_CERT" not in allowlist_text, \
            "PROXY_CA_CERT is host-side only, must not be in allowlist"
