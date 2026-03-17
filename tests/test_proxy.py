"""Tests for proxy-based credential isolation (onecli integration)."""
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_cave.config import (
    _setup_proxy, setup_env, stop_gateway_if_idle,
    _gateway_config_changed, _start_gateway, _reload_gateway,
)

FLAKE_NIX = Path(__file__).parent.parent / "flake.nix"
LIFECYCLE_PY = FLAKE_NIX.parent / "coding_cave" / "lifecycle.py"

# All config tests mock _start_gateway — no real gateway needed.
MOCK_GATEWAY = patch("coding_cave.config._start_gateway", return_value=True)


class TestProxyConfig:
    """Config tests for _setup_proxy and setup_env proxy interaction."""

    def test_onecli_proxy_sets_default_port(self, tmp_path):
        """onecli = true sets port 10255 by default."""
        ca = tmp_path / "ca.pem"
        ca.write_text("--- cert ---")
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False), MOCK_GATEWAY:
            os.environ.pop("CODING_CAVE_PROXY_PORT", None)
            with patch("coding_cave.config.Path.expanduser", return_value=ca):
                _setup_proxy(cfg)
            assert os.environ.get("CODING_CAVE_PROXY_PORT") == "10255"

    def test_oauth_token_forwarded_with_proxy(self, tmp_path):
        """CLAUDE_CODE_OAUTH_TOKEN is still set when proxy is configured (temporary fallback)."""
        ca = tmp_path / "ca.pem"
        ca.write_text("--- cert ---")
        cfg = {
            "proxy": {"onecli": True},
            "credentials": {"claude-oauth-token": "secret-token"},
        }
        with patch.dict(os.environ, {}, clear=False), MOCK_GATEWAY:
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            os.environ.pop("CODING_CAVE_PROXY_PORT", None)
            with patch("coding_cave.config._git_config", return_value=None), \
                 patch("coding_cave.config._detect_gpg_agent_socket", return_value=None), \
                 patch("coding_cave.config.Path.expanduser", return_value=ca):
                setup_env(cfg)
            assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "secret-token"

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
        with patch.dict(os.environ, {}, clear=False), MOCK_GATEWAY:
            os.environ.pop("CODING_CAVE_PROXY_PORT", None)
            with patch("coding_cave.config.Path.expanduser", return_value=ca):
                _setup_proxy(cfg)
            assert os.environ.get("CODING_CAVE_PROXY_PORT") == "8080"

    def test_onecli_ca_cert_from_default_path(self, tmp_path):
        """CA cert loaded from ~/.onecli/gateway/ca.pem."""
        ca = tmp_path / "ca.pem"
        ca.write_text("--- cert ---")
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False), MOCK_GATEWAY:
            os.environ.pop("CODING_CAVE_PROXY_CA_CERT", None)
            with patch("coding_cave.config.Path.expanduser", return_value=ca):
                _setup_proxy(cfg)
            assert os.environ.get("CODING_CAVE_PROXY_CA_CERT") == str(ca.resolve())

    def test_onecli_ca_cert_missing_skipped(self):
        """CA cert not set when gateway hasn't generated it yet."""
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False), MOCK_GATEWAY:
            os.environ.pop("CODING_CAVE_PROXY_CA_CERT", None)
            _setup_proxy(cfg)
            assert "CODING_CAVE_PROXY_CA_CERT" not in os.environ

    def test_no_proxy_returns_false(self):
        """_setup_proxy returns False when no proxy configured."""
        assert _setup_proxy({}) is False
        assert _setup_proxy({"proxy": {}}) is False
        assert _setup_proxy({"proxy": {"onecli": False}}) is False

    def test_gateway_started_when_not_running(self):
        """_setup_proxy calls _start_gateway with the configured port."""
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False), \
             patch("coding_cave.config._start_gateway", return_value=True) as mock_start:
            _setup_proxy(cfg)
            mock_start.assert_called_once_with(10255)

    def test_proxy_returns_false_when_gateway_fails(self):
        """_setup_proxy returns False if gateway can't start."""
        cfg = {"proxy": {"onecli": True}}
        with patch.dict(os.environ, {}, clear=False), \
             patch("coding_cave.config._start_gateway", return_value=False):
            assert _setup_proxy(cfg) is False
            assert "CODING_CAVE_PROXY_PORT" not in os.environ


class TestGatewayLifecycle:
    """Tests for gateway start/stop lifecycle."""

    def test_stop_kills_when_no_sessions(self, tmp_path):
        """Gateway killed when no sessions remain."""
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file), \
             patch("coding_cave.sessions.list_sessions", return_value=[]), \
             patch("os.kill", side_effect=ProcessLookupError) as mock_kill:
            stop_gateway_if_idle()
            mock_kill.assert_any_call(99999, __import__("signal").SIGTERM)
        assert not pid_file.exists()

    def test_stop_skips_when_sessions_active(self, tmp_path):
        """Gateway left running when sessions exist."""
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file), \
             patch("coding_cave.sessions.list_sessions", return_value=[{"sid": "x"}]), \
             patch("os.kill") as mock_kill:
            stop_gateway_if_idle()
            mock_kill.assert_not_called()
        assert pid_file.exists()

    def test_stop_cleans_stale_pid_file(self, tmp_path):
        """PID file cleaned up even if process already dead."""
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file), \
             patch("coding_cave.sessions.list_sessions", return_value=[]), \
             patch("os.kill", side_effect=ProcessLookupError):
            stop_gateway_if_idle()
        assert not pid_file.exists()

    def test_stop_noop_without_pid_file(self):
        """No error when PID file doesn't exist."""
        with patch("coding_cave.config._gateway_pid_file",
                    return_value=Path("/tmp/nonexistent-pid-12345")), \
             patch("coding_cave.sessions.list_sessions", return_value=[]):
            stop_gateway_if_idle()  # should not raise


class TestGatewayConfigReload:
    """Tests for gateway config change detection and restart."""

    def test_config_changed_detects_newer_rules(self, tmp_path):
        """Config change detected when rules.toml is newer than pid file."""
        config_dir = tmp_path / "onecli"
        config_dir.mkdir()
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        import time
        time.sleep(0.05)
        rules = config_dir / "rules.toml"
        rules.write_text("[rule]\nkey = 'val'")
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file), \
             patch("coding_cave.config._onecli_config_dir", return_value=config_dir):
            assert _gateway_config_changed() is True

    def test_config_changed_detects_newer_secret(self, tmp_path):
        """Config change detected when a secrets/ file is newer than pid file."""
        config_dir = tmp_path / "onecli"
        secrets_dir = config_dir / "secrets"
        secrets_dir.mkdir(parents=True)
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        import time
        time.sleep(0.05)
        (secrets_dir / "api_key").write_text("secret")
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file), \
             patch("coding_cave.config._onecli_config_dir", return_value=config_dir):
            assert _gateway_config_changed() is True

    def test_config_unchanged_when_older(self, tmp_path):
        """No config change when rules.toml is older than pid file."""
        config_dir = tmp_path / "onecli"
        config_dir.mkdir()
        rules = config_dir / "rules.toml"
        rules.write_text("[rule]\nkey = 'val'")
        import time
        time.sleep(0.05)
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file), \
             patch("coding_cave.config._onecli_config_dir", return_value=config_dir):
            assert _gateway_config_changed() is False

    def test_config_changed_false_without_pid_file(self, tmp_path):
        """No config change when there's no pid file (gateway not running)."""
        pid_file = tmp_path / "nonexistent.pid"
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file):
            assert _gateway_config_changed() is False

    def test_start_gateway_sends_sighup_on_config_change(self, tmp_path):
        """Gateway receives SIGHUP when already listening but config changed."""
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        with patch("coding_cave.config._gateway_listening", return_value=True), \
             patch("coding_cave.config._gateway_config_changed", return_value=True), \
             patch("coding_cave.config._reload_gateway") as mock_reload, \
             patch("coding_cave.config._gateway_pid_file", return_value=pid_file):
            result = _start_gateway(10255)
            mock_reload.assert_called_once()
            assert result is True

    def test_start_gateway_skips_reload_when_config_unchanged(self):
        """Gateway not reloaded when config hasn't changed."""
        with patch("coding_cave.config._gateway_listening", return_value=True), \
             patch("coding_cave.config._gateway_config_changed", return_value=False):
            result = _start_gateway(10255)
            assert result is True

    def test_reload_gateway_sends_sighup(self, tmp_path):
        """_reload_gateway sends SIGHUP and touches pid file."""
        pid_file = tmp_path / "onecli-gateway.pid"
        pid_file.write_text("99999")
        import time
        old_mtime = pid_file.stat().st_mtime
        time.sleep(0.05)
        with patch("coding_cave.config._gateway_pid_file", return_value=pid_file), \
             patch("os.kill") as mock_kill:
            result = _reload_gateway()
            mock_kill.assert_called_once_with(99999, __import__("signal").SIGHUP)
            assert result is True
            assert pid_file.stat().st_mtime > old_mtime


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

    def test_requests_ca_bundle_set(self):
        """REQUESTS_CA_BUNDLE set so Python requests trusts the proxy CA."""
        flake = FLAKE_NIX.read_text()
        assert "REQUESTS_CA_BUNDLE=/tmp/combined-ca.pem" in flake

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

    def test_gateway_local_bin_substituted(self):
        """GATEWAY_LOCAL_BIN is substituted in flake.nix replaceVars."""
        flake = FLAKE_NIX.read_text()
        assert "GATEWAY_LOCAL_BIN" in flake
        assert "onecli-gateway" in flake


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
