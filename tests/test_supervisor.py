"""Unit tests for the namespace supervisor (cav/supervisor.py)."""

import os
import stat
import subprocess
import time
import unittest
from unittest.mock import MagicMock, patch

from cav import supervisor


class TestIsMounted(unittest.TestCase):
    @patch("subprocess.run")
    def test_mounted(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(supervisor._is_mounted("/bin/mountpoint", "/mnt"))
        mock_run.assert_called_once_with(
            ["/bin/mountpoint", "-q", "/mnt"], capture_output=True,
        )

    @patch("subprocess.run")
    def test_not_mounted(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(supervisor._is_mounted("/bin/mountpoint", "/mnt"))


class TestOverlayFunctional(unittest.TestCase):
    @patch("os.unlink")
    @patch("os.close")
    @patch("os.open", return_value=3)
    def test_functional(self, mock_open, mock_close, mock_unlink):
        self.assertTrue(supervisor._overlay_functional("/mnt"))
        mock_open.assert_called_once_with(
            "/mnt/.overlay_probe", os.O_CREAT | os.O_WRONLY, 0o644,
        )
        mock_close.assert_called_once_with(3)
        mock_unlink.assert_called_once_with("/mnt/.overlay_probe")

    @patch("os.open", side_effect=OSError("Transport endpoint not connected"))
    def test_dead_mount(self, _):
        self.assertFalse(supervisor._overlay_functional("/mnt"))


class TestUnmount(unittest.TestCase):
    @patch("subprocess.run")
    def test_unmount_calls_fusermount(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        supervisor._unmount("/bin/fusermount3", "/mnt")
        mock_run.assert_called_once_with(
            ["/bin/fusermount3", "-u", "/mnt"], capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_unmount_noop_without_binary(self, mock_run):
        supervisor._unmount(None, "/mnt")
        mock_run.assert_not_called()


class TestMountOverlay(unittest.TestCase):
    def _env(self, **overrides):
        base = {
            "CODING_CAVE_NS_OVERLAY_MERGED": "/merged",
            "CODING_CAVE_NS_OVERLAY_UPPER": "/upper",
            "CODING_CAVE_NS_OVERLAY_WORK": "/work",
            "CODING_CAVE_NS_FUSE_OVERLAYFS": "/bin/fuse-overlayfs",
            "CODING_CAVE_NS_MOUNTPOINT": "/bin/mountpoint",
            "CODING_CAVE_NS_FUSERMOUNT": "/bin/fusermount3",
        }
        base.update(overrides)
        return base

    def test_returns_none_without_merged(self):
        env = self._env()
        del env["CODING_CAVE_NS_OVERLAY_MERGED"]
        self.assertIsNone(supervisor._mount_overlay(env))

    def test_returns_none_without_fuse_bin(self):
        env = self._env()
        del env["CODING_CAVE_NS_FUSE_OVERLAYFS"]
        self.assertIsNone(supervisor._mount_overlay(env))

    @patch.object(supervisor, "_overlay_functional", return_value=True)
    @patch.object(supervisor, "_is_mounted", return_value=True)
    def test_skips_when_already_functional(self, mock_mounted, mock_func):
        self.assertIsNone(supervisor._mount_overlay(self._env()))
        mock_mounted.assert_called_once()
        mock_func.assert_called_once()

    @patch.object(supervisor, "_unmount")
    @patch.object(supervisor, "_overlay_functional", return_value=False)
    @patch.object(supervisor, "_is_mounted")
    @patch("subprocess.Popen")
    def test_remounts_stale_overlay(self, mock_popen, mock_mounted,
                                    mock_func, mock_unmount):
        # First call: is_mounted returns True (stale), triggers unmount.
        # Second call (from poll loop): returns True (newly mounted).
        mock_mounted.side_effect = [True, True]
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc

        result = supervisor._mount_overlay(self._env())
        self.assertIs(result, proc)
        mock_unmount.assert_called_once_with("/bin/fusermount3", "/merged")

    @patch.object(supervisor, "_is_mounted", return_value=False)
    @patch("subprocess.Popen")
    def test_detects_fuse_crash(self, mock_popen, mock_mounted):
        proc = MagicMock()
        proc.poll.side_effect = [None, 1]  # dies on second poll
        proc.stderr.read.return_value = b"some error"
        mock_popen.return_value = proc

        with self.assertRaises(SystemExit):
            supervisor._mount_overlay(self._env())

    @patch.object(supervisor, "_is_mounted", return_value=False)
    @patch("time.sleep")
    @patch("subprocess.Popen")
    def test_timeout(self, mock_popen, mock_sleep, mock_mounted):
        proc = MagicMock()
        proc.poll.return_value = None  # never dies, never mounts
        mock_popen.return_value = proc

        with self.assertRaises(SystemExit):
            supervisor._mount_overlay(self._env())
        proc.terminate.assert_called_once()

    @patch.object(supervisor, "_is_mounted")
    @patch("subprocess.Popen")
    def test_fuse_started_in_foreground_mode(self, mock_popen, mock_mounted):
        mock_mounted.side_effect = [False, True]  # not mounted, then mounted
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc

        supervisor._mount_overlay(self._env())
        args = mock_popen.call_args[0][0]
        self.assertEqual(args[0], "/bin/fuse-overlayfs")
        self.assertIn("-f", args)


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


class TestSignalSync(unittest.TestCase):
    def test_writes_and_closes(self):
        r, w = os.pipe()
        supervisor._signal_sync(w)
        data = os.read(r, 1)
        os.close(r)
        self.assertEqual(data, b"1")
