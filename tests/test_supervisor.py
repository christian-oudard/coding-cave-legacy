"""Unit tests for the namespace supervisor (cav/sandbox/supervisor.py)."""

import os
import stat
import time
import unittest
from unittest.mock import MagicMock, patch

from cav.sandbox import supervisor


class TestMountKernelOverlay(unittest.TestCase):
    """Kernel overlayfs mount function."""

    @patch.object(supervisor._libc, 'mount', return_value=0)
    def test_success_first_variant(self, mock_mount):
        supervisor._mount_kernel_overlay("/merged", "/upper", "/work")
        self.assertEqual(mock_mount.call_count, 1)
        args = mock_mount.call_args[0]
        self.assertIn(b"userxattr", args[4])
        self.assertIn(b"uuid=off", args[4])

    @patch.object(supervisor._libc, 'mount')
    def test_fallback_to_second_variant(self, mock_mount):
        # First call fails (uuid=off not supported), second succeeds.
        mock_mount.side_effect = [-1, 0]
        supervisor._mount_kernel_overlay("/merged", "/upper", "/work")
        self.assertEqual(mock_mount.call_count, 2)
        # Second call should not have uuid=off
        second_opts = mock_mount.call_args_list[1][0][4]
        self.assertNotIn(b"uuid=off", second_opts)
        self.assertIn(b"userxattr", second_opts)

    @patch.object(supervisor._libc, 'mount', return_value=-1)
    def test_all_variants_fail(self, mock_mount):
        with self.assertRaises(SystemExit):
            supervisor._mount_kernel_overlay("/merged", "/upper", "/work")
        self.assertEqual(mock_mount.call_count, 2)

    @patch.object(supervisor._libc, 'mount', return_value=0)
    def test_mount_args(self, mock_mount):
        supervisor._mount_kernel_overlay("/mnt/merged", "/mnt/upper", "/mnt/work")
        args = mock_mount.call_args[0]
        self.assertEqual(args[0], b"overlay")
        self.assertEqual(args[1], b"/mnt/merged")
        self.assertEqual(args[2], b"overlay")
        self.assertIn(b"lowerdir=/nix/store", args[4])
        self.assertIn(b"upperdir=/mnt/upper", args[4])
        self.assertIn(b"workdir=/mnt/work", args[4])


class TestUmountKernel(unittest.TestCase):
    @patch.object(supervisor._libc, 'umount2', return_value=0)
    def test_success(self, mock_umount):
        supervisor._umount_kernel("/merged")
        mock_umount.assert_called_once_with(b"/merged", 2)  # MNT_DETACH

    @patch.object(supervisor._libc, 'umount2', return_value=-1)
    def test_failure_prints_warning(self, mock_umount):
        # Should not raise, just print warning.
        supervisor._umount_kernel("/merged")
        mock_umount.assert_called_once()


class TestIsNixLockFile(unittest.TestCase):
    def test_empty_file_is_lock(self):
        with patch("os.stat") as mock_stat:
            s = MagicMock()
            s.st_mode = stat.S_IFREG | 0o600
            s.st_size = 0
            mock_stat.return_value = s
            self.assertTrue(supervisor._is_nix_lock_file("/nix/store/x.lock"))

    def test_nonempty_file_is_not_lock(self):
        with patch("os.stat") as mock_stat:
            s = MagicMock()
            s.st_mode = stat.S_IFREG | 0o444
            s.st_size = 13960
            mock_stat.return_value = s
            self.assertFalse(supervisor._is_nix_lock_file("/nix/store/x-Cargo.lock"))

    def test_stat_failure_returns_false(self):
        with patch("os.stat", side_effect=OSError):
            self.assertFalse(supervisor._is_nix_lock_file("/nix/store/x.lock"))


class TestShadowHostLockFiles(unittest.TestCase):
    LOCK = "/nix/store/abc-foo.lock"
    CARGO = "/nix/store/def-Cargo.lock"

    @patch("os.close")
    @patch("os.open", return_value=3)
    @patch("os.lstat", side_effect=FileNotFoundError)
    @patch.object(supervisor, "_is_nix_lock_file",
                  side_effect=lambda p: p == TestShadowHostLockFiles.LOCK)
    @patch("glob.glob")
    def test_shadows_only_empty_lock_files(self, mock_glob, _is, _lstat, mock_open, mock_close):
        mock_glob.return_value = [self.LOCK, self.CARGO]
        supervisor._shadow_host_lock_files("/upper")
        mock_open.assert_called_once_with(
            "/upper/abc-foo.lock", os.O_CREAT | os.O_WRONLY, 0o600,
        )
        mock_close.assert_called_once_with(3)

    @patch("os.close")
    @patch("os.open", return_value=3)
    @patch("os.lstat")
    @patch.object(supervisor, "_is_nix_lock_file", return_value=True)
    @patch("glob.glob")
    def test_skips_existing_upper_entries(self, mock_glob, _is, _lstat, mock_open, _close):
        mock_glob.return_value = [self.LOCK]
        supervisor._shadow_host_lock_files("/upper")
        mock_open.assert_not_called()

    @patch("glob.glob", side_effect=OSError("not accessible"))
    def test_propagates_glob_errors(self, _glob):
        with self.assertRaises(OSError):
            supervisor._shadow_host_lock_files("/upper")

    @patch("os.lstat", side_effect=FileNotFoundError)
    @patch.object(supervisor, "_is_nix_lock_file", return_value=True)
    @patch("glob.glob")
    @patch("os.open", side_effect=OSError("disk full"))
    def test_propagates_create_errors(self, _open, mock_glob, _is, _lstat):
        mock_glob.return_value = [self.LOCK]
        with self.assertRaises(OSError):
            supervisor._shadow_host_lock_files("/upper")


class TestLockWatcher(unittest.TestCase):
    """The inotify watcher must shadow lock files created at runtime."""

    def test_lock_file_created_after_startup_is_shadowed(self):
        """A host lock file created after the watcher starts must
        have a shadow in the upper directory."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store")
            upper = os.path.join(tmp, "upper")
            os.makedirs(store)
            os.makedirs(upper)

            stop = supervisor.start_lock_watcher(store, upper)
            self.assertIsNotNone(stop)
            try:
                # Host creates a lock file.
                lock_path = os.path.join(store, "abc-foo.lock")
                fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)

                # Give inotify a moment to deliver the event.
                time.sleep(0.05)

                shadow = os.path.join(upper, "abc-foo.lock")
                self.assertTrue(os.path.exists(shadow))
            finally:
                stop()

    def test_ignores_non_lock_files(self):
        """Non-.lock files in the store should not create shadows."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store")
            upper = os.path.join(tmp, "upper")
            os.makedirs(store)
            os.makedirs(upper)

            stop = supervisor.start_lock_watcher(store, upper)
            try:
                # Create a regular store path.
                os.makedirs(os.path.join(store, "abc-some-package"))
                time.sleep(0.05)

                self.assertEqual(os.listdir(upper), [])
            finally:
                stop()

    def test_shadow_is_idempotent(self):
        """Creating the same lock file twice doesn't fail."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store")
            upper = os.path.join(tmp, "upper")
            os.makedirs(store)
            os.makedirs(upper)

            # Pre-existing shadow.
            fd = os.open(os.path.join(upper, "abc.lock"),
                         os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

            stop = supervisor.start_lock_watcher(store, upper)
            try:
                # Host creates same lock file.
                fd = os.open(os.path.join(store, "abc.lock"),
                             os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
                time.sleep(0.05)

                # Shadow still exists, no error.
                self.assertTrue(os.path.exists(
                    os.path.join(upper, "abc.lock")))
            finally:
                stop()

    def test_stop_terminates_watcher(self):
        """The stop function cleanly shuts down the watcher thread."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store")
            upper = os.path.join(tmp, "upper")
            os.makedirs(store)
            os.makedirs(upper)

            stop = supervisor.start_lock_watcher(store, upper)
            stop()

            # After stop, new lock files are not shadowed.
            fd = os.open(os.path.join(store, "abc.lock"),
                         os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            time.sleep(0.05)

            self.assertFalse(os.path.exists(
                os.path.join(upper, "abc.lock")))


class TestMain(unittest.TestCase):
    """Tests for main() overlay mount and cleanup."""

    def _env(self, **overrides):
        base = {
            "CODING_CAVE_NS_SYNC_FD": "99",
            "CODING_CAVE_NS_OVERLAY_MERGED": "/merged",
            "CODING_CAVE_NS_OVERLAY_UPPER": "/upper",
            "CODING_CAVE_NS_OVERLAY_WORK": "/work",
            "CODING_CAVE_NS_MOUNTPOINT": "/bin/mountpoint",
        }
        base.update(overrides)
        return base

    @patch("subprocess.run", return_value=MagicMock(returncode=0))
    @patch.object(supervisor, "start_lock_watcher", return_value=None)
    @patch.object(supervisor, "_mount_kernel_overlay")
    @patch.object(supervisor, "_umount_kernel")
    @patch.object(supervisor, "_shadow_host_lock_files")
    @patch.object(supervisor, "_signal_sync")
    def test_kernel_overlay_mounted(self, _sync, _shadow, mock_umount_k,
                                     mock_kernel, _watcher, _run):
        with patch.dict(os.environ, self._env(), clear=True), \
             self.assertRaises(SystemExit):
            supervisor.main()
        mock_kernel.assert_called_once_with("/merged", "/upper", "/work")
        mock_umount_k.assert_called_once_with("/merged")

    @patch("subprocess.run", return_value=MagicMock(returncode=0))
    @patch.object(supervisor, "start_lock_watcher", return_value=None)
    @patch.object(supervisor, "_mount_kernel_overlay")
    @patch.object(supervisor, "_umount_kernel")
    @patch.object(supervisor, "_shadow_host_lock_files")
    @patch.object(supervisor, "_signal_sync")
    def test_no_overlay_without_env(self, _sync, _shadow, mock_umount_k,
                                    mock_kernel, _watcher, _run):
        env = self._env()
        del env["CODING_CAVE_NS_OVERLAY_MERGED"]
        del env["CODING_CAVE_NS_OVERLAY_UPPER"]
        with patch.dict(os.environ, env, clear=True), \
             self.assertRaises(SystemExit):
            supervisor.main()
        mock_kernel.assert_not_called()
        mock_umount_k.assert_not_called()


class TestSignalSync(unittest.TestCase):
    def test_writes_and_closes(self):
        r, w = os.pipe()
        supervisor._signal_sync(w)
        data = os.read(r, 1)
        os.close(r)
        self.assertEqual(data, b"1")
