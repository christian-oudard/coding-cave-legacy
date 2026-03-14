"""Tests for nix store overlay: stale upper entries and user-env visibility.

The nix store overlay uses fuse-overlayfs with a shared upper dir.
When nix inside the cave touches a host store path (chmod, xattr),
fuse-overlayfs copies up an empty or opaque directory to the upper.
This shadows the real contents in the lower layer, making packages
invisible.

Integration tests use unshare to get mount capabilities for real
fuse-overlayfs testing.  Unit tests verify cleanup logic and bind
mount argument generation without requiring mounts.
"""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_cave import _state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _can_unshare():
    """Check if unshare --user --mount works (needed for fuse-overlayfs tests)."""
    try:
        r = subprocess.run(
            ["unshare", "--user", "--mount", "--map-root-user", "--",
             "true"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


requires_unshare = pytest.mark.skipif(
    not _can_unshare(),
    reason="unshare --user --mount not available",
)


# Script run inside unshare namespace for fuse-overlayfs tests.
_OVERLAY_TEST = textwrap.dedent(r"""
    import json
    import os
    import subprocess
    import sys

    lower, upper, work, merged = sys.argv[1:5]
    action = sys.argv[5] if len(sys.argv) > 5 else "list"

    # Mount fuse-overlayfs
    r = subprocess.run(
        ["fuse-overlayfs",
         "-o", f"lowerdir={lower},upperdir={upper},workdir={work}",
         merged],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(json.dumps({"error": f"mount failed: {r.stderr.strip()}"}))
        sys.exit(1)

    result = {}
    try:
        if action == "list":
            # List a specific subdir
            target = sys.argv[6] if len(sys.argv) > 6 else ""
            path = os.path.join(merged, target) if target else merged
            result["exists"] = os.path.exists(path)
            result["isdir"] = os.path.isdir(path)
            try:
                result["contents"] = sorted(os.listdir(path))
            except OSError as e:
                result["contents"] = []
                result["listdir_error"] = str(e)
    finally:
        subprocess.run(["fusermount3", "-u", merged],
                       capture_output=True)

    print(json.dumps(result))
""")


def _run_overlay_test(lower, upper, work, merged, action="list", target=""):
    """Run a fuse-overlayfs test inside a user namespace."""
    import json
    args = [
        "unshare", "--user", "--mount", "--map-root-user", "--",
        sys.executable, "-c", _OVERLAY_TEST,
        str(lower), str(upper), str(work), str(merged),
        action, target,
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"error": f"exit {r.returncode}: {r.stderr}"}
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {"error": f"bad json: {r.stdout}"}


def _run_overlay_test_script(lower, upper, work, merged, op_script):
    """Mount fuse-overlayfs, run arbitrary Python code, unmount."""
    import json
    # op_script is indented with 4 spaces for the try block
    script = (
        "import json, os, subprocess, sys\n"
        f"lower = {str(lower)!r}\n"
        f"upper = {str(upper)!r}\n"
        f"work = {str(work)!r}\n"
        f"merged = {str(merged)!r}\n"
        "r = subprocess.run(\n"
        '    ["fuse-overlayfs",\n'
        '     "-o", f"lowerdir={lower},upperdir={upper},workdir={work}",\n'
        "     merged],\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "if r.returncode != 0:\n"
        '    print(json.dumps({"error": f"mount: {r.stderr.strip()}"}))\n'
        "    sys.exit(1)\n"
        "try:\n"
        + op_script + "\n"
        "finally:\n"
        '    subprocess.run(["fusermount3", "-u", merged], capture_output=True)\n'
    )
    r = subprocess.run(
        ["unshare", "--user", "--mount", "--map-root-user", "--",
         sys.executable, "-c", script],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return {"error": f"exit {r.returncode}: {r.stderr}"}
    try:
        return json.loads(r.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return {"output": r.stdout, "stderr": r.stderr}


# ---------------------------------------------------------------------------
# Integration: fuse-overlayfs stale upper entry behavior
# ---------------------------------------------------------------------------

class TestFuseOverlayfsCopyUp:
    """Determine which operations create opaque upper entries.

    Only opaque entries hide lower layer contents.  Non-opaque upper
    entries (from chmod, setxattr, file creation) merge with lower.
    Only rmdir+mkdir creates opaque entries in fuse-overlayfs.
    """

    @requires_unshare
    def test_chmod_does_not_hide_lower(self, tmp_path):
        """chmod creates an upper entry but does NOT hide lower contents."""
        lower, upper, work, merged = self._setup(tmp_path)

        # Phase 1: chmod inside overlay
        result = self._run_operation(lower, upper, work, merged, """
    import os, json
    os.chmod(os.path.join(merged, "test-package"), 0o755)
    print(json.dumps({"op": "chmod"}))
""")
        assert "error" not in result

        # Upper entry was created by copy-up
        upper_pkg = upper / "test-package"
        assert upper_pkg.is_dir()

        # Phase 2: remount and check — lower contents should be visible
        work2 = tmp_path / "work2"; work2.mkdir()
        result2 = _run_overlay_test(lower, upper, work2, merged,
                                    target="test-package")
        assert "bin" in result2.get("contents", []), \
            f"chmod should not hide lower contents, got: {result2}"

    @requires_unshare
    def test_setxattr_does_not_hide_lower(self, tmp_path):
        """setxattr creates an upper entry but does NOT hide lower contents."""
        lower, upper, work, merged = self._setup(tmp_path)

        result = self._run_operation(lower, upper, work, merged, """
    import os, json
    os.setxattr(os.path.join(merged, "test-package"), b"user.test", b"val")
    print(json.dumps({"op": "setxattr"}))
""")
        assert "error" not in result

        work2 = tmp_path / "work2"; work2.mkdir()
        result2 = _run_overlay_test(lower, upper, work2, merged,
                                    target="test-package")
        assert "bin" in result2.get("contents", []), \
            f"setxattr should not hide lower contents, got: {result2}"

    @requires_unshare
    def test_file_creation_does_not_hide_lower(self, tmp_path):
        """Creating a new file in a lower dir does NOT hide other contents."""
        lower, upper, work, merged = self._setup(tmp_path)

        result = self._run_operation(lower, upper, work, merged, """
    import os, json
    with open(os.path.join(merged, "test-package", "newfile"), "w") as f:
        f.write("test")
    print(json.dumps({"op": "create"}))
""")
        assert "error" not in result

        work2 = tmp_path / "work2"; work2.mkdir()
        result2 = _run_overlay_test(lower, upper, work2, merged,
                                    target="test-package")
        assert "bin" in result2.get("contents", []), \
            f"file creation should not hide lower contents, got: {result2}"

    @requires_unshare
    def test_rmdir_mkdir_creates_opaque(self, tmp_path):
        """rmdir+mkdir is the ONLY operation that hides lower contents."""
        lower, upper, work, merged = self._setup(tmp_path)

        result = self._run_operation(lower, upper, work, merged, """
    import os, json, shutil
    path = os.path.join(merged, "test-package")
    shutil.rmtree(path)
    os.makedirs(path)
    print(json.dumps({"op": "rmdir+mkdir"}))
""")
        assert "error" not in result

        # Upper entry should be opaque
        upper_pkg = upper / "test-package"
        xattrs = os.listxattr(str(upper_pkg))
        assert any("opaque" in x for x in xattrs), \
            f"rmdir+mkdir should create opaque entry, xattrs={xattrs}"

        # After remount, lower contents should be HIDDEN
        work2 = tmp_path / "work2"; work2.mkdir()
        result2 = _run_overlay_test(lower, upper, work2, merged,
                                    target="test-package")
        assert "bin" not in result2.get("contents", []), \
            f"rmdir+mkdir should hide lower contents, got: {result2}"

    @requires_unshare
    def test_stat_readdir_no_upper_entry(self, tmp_path):
        """stat and readdir do NOT create any upper entry."""
        lower, upper, work, merged = self._setup(tmp_path)

        result = self._run_operation(lower, upper, work, merged, """
    import os, json
    s = os.stat(os.path.join(merged, "test-package"))
    c = os.listdir(os.path.join(merged, "test-package"))
    print(json.dumps({"op": "stat+readdir", "contents": c}))
""")
        assert "error" not in result
        assert not (upper / "test-package").exists(), \
            "stat/readdir should not create upper entry"

    def _setup(self, tmp_path):
        lower = tmp_path / "lower"
        pkg = lower / "test-package"
        pkg.mkdir(parents=True)
        (pkg / "bin").mkdir()
        (pkg / "bin" / "tool").write_text("#!/bin/sh\n")
        upper = tmp_path / "upper"; upper.mkdir()
        work = tmp_path / "work"; work.mkdir()
        merged = tmp_path / "merged"; merged.mkdir()
        return lower, upper, work, merged

    def _run_operation(self, lower, upper, work, merged, op_script):
        """Mount overlay, run operation, unmount."""
        return _run_overlay_test_script(lower, upper, work, merged, op_script)


class TestFuseOverlayfsStaleUpper:
    """Reproduce the exact bug: opaque upper dir shadows lower contents."""

    @requires_unshare
    def test_clean_overlay_shows_lower_contents(self, tmp_path):
        """Baseline: overlay without stale upper shows lower layer files."""
        lower = tmp_path / "lower"
        pkg = lower / "test-package"
        pkg.mkdir(parents=True)
        (pkg / "bin").mkdir()
        (pkg / "bin" / "tool").write_text("#!/bin/sh\n")
        upper = tmp_path / "upper"; upper.mkdir()
        work = tmp_path / "work"; work.mkdir()
        merged = tmp_path / "merged"; merged.mkdir()

        result = _run_overlay_test(lower, upper, work, merged,
                                   target="test-package")
        assert result.get("exists") is True
        assert "bin" in result.get("contents", [])

    @requires_unshare
    def test_empty_upper_entry_merges_with_lower(self, tmp_path):
        """An empty upper dir entry gets MERGED with lower (not opaque)."""
        lower = tmp_path / "lower"
        pkg = lower / "test-package"
        pkg.mkdir(parents=True)
        (pkg / "bin").mkdir()
        (pkg / "bin" / "tool").write_text("#!/bin/sh\n")
        upper = tmp_path / "upper"; upper.mkdir()
        # Create empty upper entry (simulates partial copy-up)
        (upper / "test-package").mkdir()
        work = tmp_path / "work"; work.mkdir()
        merged = tmp_path / "merged"; merged.mkdir()

        result = _run_overlay_test(lower, upper, work, merged,
                                   target="test-package")
        # Empty mkdir does NOT create opaque dir — contents merge
        assert result.get("exists") is True
        assert "bin" in result.get("contents", []), \
            f"Empty upper dir should merge with lower, got: {result}"

    @requires_unshare
    def test_opaque_upper_hides_lower_contents(self, tmp_path):
        """An opaque upper dir hides ALL lower layer contents."""
        lower = tmp_path / "lower"
        pkg = lower / "test-package"
        pkg.mkdir(parents=True)
        (pkg / "bin").mkdir()
        (pkg / "bin" / "tool").write_text("#!/bin/sh\n")
        upper = tmp_path / "upper"; upper.mkdir()
        # Create opaque upper entry (what fuse-overlayfs does on rmdir+mkdir)
        opaque = upper / "test-package"
        opaque.mkdir()
        # fuse-overlayfs uses this xattr for opaque marking
        try:
            os.setxattr(str(opaque), b"user.fuseoverlayfs.opaque", b"y")
        except OSError:
            pytest.skip("can't set xattr (needed for opaque test)")
        work = tmp_path / "work"; work.mkdir()
        merged = tmp_path / "merged"; merged.mkdir()

        result = _run_overlay_test(lower, upper, work, merged,
                                   target="test-package")
        # Opaque dir should hide lower contents
        assert result.get("exists") is True
        assert result.get("contents") == [], \
            f"Opaque upper should hide lower contents, got: {result}"

    @requires_unshare
    def test_rmtree_cleanup_restores_visibility(self, tmp_path):
        """shutil.rmtree on opaque upper entry restores lower visibility."""
        lower = tmp_path / "lower"
        pkg = lower / "test-package"
        pkg.mkdir(parents=True)
        (pkg / "bin").mkdir()
        (pkg / "bin" / "tool").write_text("#!/bin/sh\n")
        upper = tmp_path / "upper"; upper.mkdir()
        opaque = upper / "test-package"
        opaque.mkdir()
        try:
            os.setxattr(str(opaque), b"user.fuseoverlayfs.opaque", b"y")
        except OSError:
            pytest.skip("can't set xattr")

        # Simulate the cleanup from lifecycle.py
        for entry in os.scandir(str(upper)):
            if entry.is_dir(follow_symlinks=False):
                host_path = lower / entry.name
                if host_path.is_dir():
                    shutil.rmtree(entry.path)

        work = tmp_path / "work"; work.mkdir()
        merged = tmp_path / "merged"; merged.mkdir()

        result = _run_overlay_test(lower, upper, work, merged,
                                   target="test-package")
        assert "bin" in result.get("contents", []), \
            f"After rmtree cleanup, lower contents should be visible: {result}"

    @requires_unshare
    def test_cave_only_upper_entries_preserved(self, tmp_path):
        """Upper entries NOT in lower (cave-installed packages) survive cleanup."""
        lower = tmp_path / "lower"; lower.mkdir()
        upper = tmp_path / "upper"; upper.mkdir()
        # Cave-installed package: only in upper, not in lower
        cave_pkg = upper / "cave-installed-pkg"
        cave_pkg.mkdir()
        (cave_pkg / "bin").mkdir()
        (cave_pkg / "bin" / "cave-tool").write_text("#!/bin/sh\n")

        # Run cleanup logic
        for entry in os.scandir(str(upper)):
            if entry.is_dir(follow_symlinks=False):
                host_path = lower / entry.name
                if host_path.is_dir():
                    shutil.rmtree(entry.path)

        # Cave package should still exist
        assert cave_pkg.is_dir()
        assert (cave_pkg / "bin" / "cave-tool").exists()


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
