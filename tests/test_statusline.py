"""Render tests for defaults/statusline.sh."""

import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
STATUSLINE = REPO / "defaults" / "statusline.sh"


def _render(persist_json=None, agent="test@1"):
    with TemporaryDirectory() as td:
        if persist_json is not None:
            dot = Path(td) / ".claude"
            dot.mkdir()
            (dot / "persist.json").write_text(persist_json)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": "/does-not-exist",
            "CODING_CAVE_AGENT_NAME": agent,
        }
        r = subprocess.run(
            ["bash", str(STATUSLINE)],
            cwd=td, env=env, input="{}\n",
            capture_output=True, text=True, check=True,
        )
    return r.stdout.strip()


@unittest.skipIf(shutil.which("jq") is None, "jq required on PATH")
class StatuslineTest(unittest.TestCase):
    AGENT = "Coding Cave - test@1"

    def test_no_persist_session(self):
        self.assertEqual(_render(), self.AGENT)

    def test_total_bound(self):
        data = '{"iteration":2,"total":5,"deadline":null,"started":0,"prompt":"x"}'
        self.assertEqual(_render(data), f"{self.AGENT} [persist 2/5]")

    def test_deadline_bound(self):
        now = int(time.time())
        data = ('{"iteration":3,"total":null,"deadline":%d,"started":%d,"prompt":"x"}'
                % (now + 1800, now - 1800))
        self.assertEqual(_render(data), f"{self.AGENT} [persist 30m/1h00m]")

    def test_forever(self):
        data = '{"iteration":7,"total":null,"deadline":null,"started":0,"prompt":"x"}'
        self.assertEqual(_render(data), f"{self.AGENT} [persist forever]")

    def test_locked_total(self):
        data = ('{"iteration":2,"total":10,"deadline":null,"started":0,'
                '"prompt":"x","lock":true}')
        self.assertEqual(_render(data), f"{self.AGENT} [persist --lock 2/10]")

    def test_locked_deadline(self):
        now = int(time.time())
        data = ('{"iteration":3,"total":null,"deadline":%d,"started":%d,'
                '"prompt":"x","lock":true}' % (now + 1800, now - 1800))
        self.assertEqual(_render(data), f"{self.AGENT} [persist --lock 30m/1h00m]")

    def test_locked_forever(self):
        data = ('{"iteration":4,"total":null,"deadline":null,"started":0,'
                '"prompt":"x","lock":true}')
        self.assertEqual(_render(data), f"{self.AGENT} [persist --lock forever]")


if __name__ == "__main__":
    unittest.main()
