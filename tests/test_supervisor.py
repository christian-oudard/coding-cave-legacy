"""Unit tests for the namespace supervisor (cav/supervisor.py)."""

import os
import subprocess
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
        supervisor._unmount("/bin/fusermount3", "/mnt")
        mock_run.assert_called_once_with(
            ["/bin/fusermount3", "-u", "/mnt"], capture_output=True,
        )

    @patch("subprocess.run")
    def test_unmount_noop_without_binary(self, mock_run):
        supervisor._unmount(None, "/mnt")
        mock_run.assert_not_called()


class TestMountOverlay(unittest.TestCase):
    def _env(self, **overrides):
        base = {
            "CAVE_NS_OVERLAY_MERGED": "/merged",
            "CAVE_NS_OVERLAY_UPPER": "/upper",
            "CAVE_NS_OVERLAY_WORK": "/work",
            "CAVE_NS_FUSE_OVERLAYFS": "/bin/fuse-overlayfs",
            "CAVE_NS_MOUNTPOINT": "/bin/mountpoint",
            "CAVE_NS_FUSERMOUNT": "/bin/fusermount3",
        }
        base.update(overrides)
        return base

    def test_returns_none_without_merged(self):
        env = self._env()
        del env["CAVE_NS_OVERLAY_MERGED"]
        self.assertIsNone(supervisor._mount_overlay(env))

    def test_returns_none_without_fuse_bin(self):
        env = self._env()
        del env["CAVE_NS_FUSE_OVERLAYFS"]
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


class TestSignalSync(unittest.TestCase):
    def test_writes_and_closes(self):
        r, w = os.pipe()
        supervisor._signal_sync(w)
        data = os.read(r, 1)
        os.close(r)
        self.assertEqual(data, b"1")
