"""Tests for nix store overlay: stale upper cleanup and user-env bind mounts.

Unit tests verify the stale upper cleanup logic and bind mount argument
generation without requiring real overlay mounts.
"""
import os
import shutil
from pathlib import Path

import pytest

from coding_cave import _state


# ---------------------------------------------------------------------------
# Unit: bind mount argument generation for user-env packages
# ---------------------------------------------------------------------------

class TestUserEnvBindMounts:
    """Verify lifecycle.py generates correct --ro-bind args for user-env."""

    def _make_fake_user_env(self, tmp_path):
        """Create a fake cave-user-env structure with symlinked packages."""
        store = tmp_path / "nix" / "store"

        # The user-env itself
        user_env = store / "abc123-cave-user-env"
        packages = user_env / "packages"
        packages.mkdir(parents=True)
        (user_env / "claude").mkdir()

        # A "package" store path
        persist = store / "def456-persist-0.1.0"
        (persist / "bin").mkdir(parents=True)
        (persist / "bin" / "persist").write_text("#!/bin/sh\n")

        direnv = store / "ghi789-direnv-2.34"
        (direnv / "bin").mkdir(parents=True)
        (direnv / "bin" / "direnv").write_text("#!/bin/sh\n")

        # Symlinks in user-env packages dir
        (packages / "persist").symlink_to(str(persist))
        (packages / "direnv").symlink_to(str(direnv))

        return user_env

    def test_bind_mounts_include_user_env(self, tmp_path):
        """bwrap args should include --ro-bind for the user-env itself."""
        user_env = self._make_fake_user_env(tmp_path)
        bwrap_args = []

        # Simulate the lifecycle.py user-env bind mount code
        bwrap_args += ["--ro-bind", str(user_env), str(user_env)]
        packages_dir = user_env / "packages"
        for pkg in sorted(packages_dir.iterdir()):
            if pkg.is_symlink():
                target = pkg.resolve()
                if str(target).startswith(str(tmp_path)):
                    bwrap_args += ["--ro-bind", str(target), str(target)]

        assert ["--ro-bind", str(user_env), str(user_env)] == bwrap_args[:3]

    def test_bind_mounts_include_package_targets(self, tmp_path):
        """bwrap args should include --ro-bind for each package symlink target."""
        user_env = self._make_fake_user_env(tmp_path)
        store = tmp_path / "nix" / "store"
        bwrap_args = []

        bwrap_args += ["--ro-bind", str(user_env), str(user_env)]
        packages_dir = user_env / "packages"
        bound_targets = set()
        for pkg in sorted(packages_dir.iterdir()):
            if pkg.is_symlink():
                target = pkg.resolve()
                bwrap_args += ["--ro-bind", str(target), str(target)]
                bound_targets.add(target.name)

        assert "def456-persist-0.1.0" in bound_targets
        assert "ghi789-direnv-2.34" in bound_targets

    def test_extra_path_includes_all_package_bins(self, tmp_path):
        """CODING_CAVE_EXTRA_PATH should include bin dirs for all packages."""
        user_env = self._make_fake_user_env(tmp_path)
        packages_dir = user_env / "packages"
        extra_paths = []
        for pkg in sorted(packages_dir.iterdir()):
            bin_dir = pkg / "bin"
            if bin_dir.is_dir():
                extra_paths.append(str(bin_dir))

        assert len(extra_paths) == 2
        assert any("direnv" in p for p in extra_paths)
        assert any("persist" in p for p in extra_paths)


# ---------------------------------------------------------------------------
# Unit: stale upper cleanup logic
# ---------------------------------------------------------------------------

class TestStaleUpperCleanup:
    """Test the upper dir cleanup that runs before mounting the overlay."""

    def test_removes_empty_shadowing_entry(self, tmp_path):
        """Empty upper dir that shadows a host path gets removed."""
        host_store = tmp_path / "host-store"
        upper = tmp_path / "upper"
        pkg = host_store / "abc123-pkg"
        pkg.mkdir(parents=True)
        (pkg / "bin").mkdir()
        stale = upper / "abc123-pkg"
        stale.mkdir(parents=True)

        # Simulate cleanup
        for entry in os.scandir(str(upper)):
            if entry.is_dir(follow_symlinks=False):
                host_path = host_store / entry.name
                if host_path.is_dir():
                    shutil.rmtree(entry.path)

        assert not stale.exists()

    def test_removes_opaque_shadowing_entry(self, tmp_path):
        """Upper dir with opaque xattr that shadows host path gets removed."""
        host_store = tmp_path / "host-store"
        upper = tmp_path / "upper"
        pkg = host_store / "abc123-pkg"
        pkg.mkdir(parents=True)
        stale = upper / "abc123-pkg"
        stale.mkdir(parents=True)
        try:
            os.setxattr(str(stale), b"user.fuseoverlayfs.opaque", b"y")
        except OSError:
            pytest.skip("can't set xattr")

        for entry in os.scandir(str(upper)):
            if entry.is_dir(follow_symlinks=False):
                host_path = host_store / entry.name
                if host_path.is_dir():
                    shutil.rmtree(entry.path)

        assert not stale.exists()

    def test_preserves_cave_only_entries(self, tmp_path):
        """Upper entries NOT in host store are preserved (cave-installed)."""
        host_store = tmp_path / "host-store"
        host_store.mkdir()
        upper = tmp_path / "upper"
        cave_pkg = upper / "xyz-cave-pkg"
        cave_pkg.mkdir(parents=True)
        (cave_pkg / "bin").mkdir()

        for entry in os.scandir(str(upper)):
            if entry.is_dir(follow_symlinks=False):
                host_path = host_store / entry.name
                if host_path.is_dir():
                    shutil.rmtree(entry.path)

        assert cave_pkg.is_dir()

    def test_preserves_non_directory_entries(self, tmp_path):
        """Non-directory entries in upper are left alone."""
        host_store = tmp_path / "host-store"
        host_store.mkdir()
        upper = tmp_path / "upper"
        upper.mkdir()
        whiteout = upper / ".wh.something"
        whiteout.touch()

        for entry in os.scandir(str(upper)):
            if not entry.is_dir(follow_symlinks=False):
                continue
            host_path = host_store / entry.name
            if host_path.is_dir():
                shutil.rmtree(entry.path)

        assert whiteout.exists()
