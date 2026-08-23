#!/usr/bin/env python3
"""Tests for services/actions/system.py — stdlib unittest, no live data.

subprocess.run is patched in every test, so no launchd agent is ever
restarted, and the decisions log lives in a temp directory. Run:
python3 -m pytest test_system.py -q (from this directory), or
python3 services/actions/test_system.py from the repo root.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import system


class SystemTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (subprocess.run, common.STATE_DIR)
        common.STATE_DIR = pathlib.Path(self._tmp.name)
        self.calls = []

    def tearDown(self):
        subprocess.run, common.STATE_DIR = self._saved
        self._tmp.cleanup()

    def patch_run(self, returncode=0, stderr="", stdout="", raises=None):
        def fake(cmd, **kw):
            self.calls.append(cmd)
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        subprocess.run = fake

    def patch_reads(self, listing, ps, raises=None):
        """launchctl list answers `listing`, ps answers `ps`."""
        def fake(cmd, **kw):
            self.calls.append(cmd)
            if raises is not None:
                raise raises
            out = listing if cmd[0] == system.LAUNCHCTL else ps
            return subprocess.CompletedProcess(cmd, 0, out, "")
        subprocess.run = fake

    # ---- the command ----

    def test_gateway_kickstarts_the_gateway_label(self):
        self.patch_run()
        code, body = system.restart("gateway")
        self.assertEqual((code, body), (200, {"restarted": "ai.hermes.gateway"}))
        self.assertEqual(self.calls, [["/bin/launchctl", "kickstart", "-k",
                                       f"gui/{os.getuid()}/ai.hermes.gateway"]])

    def test_webui_kickstarts_the_webui_label(self):
        self.patch_run()
        code, body = system.restart("webui")
        self.assertEqual((code, body),
                         (200, {"restarted": "com.example.iris.webui"}))
        self.assertEqual(self.calls, [["/bin/launchctl", "kickstart", "-k",
                                       f"gui/{os.getuid()}/com.example.iris.webui"]])

    def test_success_is_logged(self):
        self.patch_run()
        system.restart("webui")
        line = next(common.STATE_DIR.glob("decisions-*.jsonl")).read_text()
        self.assertIn("system_restart", line)
        self.assertIn("com.example.iris.webui", line)

    # ---- failures ----

    def test_refused_kickstart_gives_502_with_launchctl_text(self):
        self.patch_run(returncode=3, stderr="Could not find service\n")
        code, body = system.restart("gateway")
        self.assertEqual(code, 502)
        self.assertIn("Could not find service", body["error"])
        self.assertIn("exited 3", body["error"])

    def test_stdout_stands_in_when_stderr_is_empty(self):
        self.patch_run(returncode=1, stdout="Bad request\n")
        code, body = system.restart("webui")
        self.assertEqual(code, 502)
        self.assertIn("Bad request", body["error"])

    def test_missing_launchctl_gives_502(self):
        self.patch_run(raises=OSError("No such file or directory"))
        code, body = system.restart("gateway")
        self.assertEqual(code, 502)
        self.assertIn("No such file or directory", body["error"])

    def test_wedged_launchctl_gives_502(self):
        self.patch_run(raises=subprocess.TimeoutExpired("launchctl", 30))
        code, body = system.restart("gateway")
        self.assertEqual(code, 502)
        self.assertIn("launchctl", body["error"])

    def test_a_failure_is_not_logged(self):
        self.patch_run(returncode=3, stderr="nope")
        system.restart("gateway")
        self.assertEqual(list(common.STATE_DIR.glob("decisions-*.jsonl")), [])

    # ---- how long the process has been up ----

    def test_started_at_reads_the_pid_then_its_start_time(self):
        self.patch_reads('\t"PID" = 4321;\n', "Tue Aug 18 01:51:38 2026\n")
        stamp = system._started_at("ai.hermes.gateway")
        self.assertEqual(self.calls,
                         [["/bin/launchctl", "list", "ai.hermes.gateway"],
                          ["/bin/ps", "-p", "4321", "-o", "lstart="]])
        # the stamp is UTC, so it only matches the local time it came from
        self.assertEqual(
            stamp,
            datetime(2026, 8, 18, 1, 51, 38).astimezone(timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))

    def test_no_pid_gives_none_and_skips_ps(self):
        self.patch_reads("Could not find service\n", "")
        self.assertIsNone(system._started_at("ai.hermes.gateway"))
        self.assertEqual(len(self.calls), 1)

    def test_a_process_gone_between_the_two_calls_gives_none(self):
        self.patch_reads('\t"PID" = 4321;\n', "")
        self.assertIsNone(system._started_at("ai.hermes.gateway"))

    def test_a_failed_command_gives_none(self):
        self.patch_reads("", "", raises=OSError("boom"))
        self.assertIsNone(system._started_at("ai.hermes.gateway"))

    # ---- area interface ----

    def test_state_carries_every_start_time(self):
        self.patch_reads('\t"PID" = 7;\n', "Tue Aug 18 01:51:38 2026\n")
        st = system.state()
        self.assertEqual(sorted(st), ["gateway", "webui"])
        self.assertTrue(all(v and v.endswith("Z") for v in st.values()))

    def test_handlers_reach_every_label(self):
        self.patch_run()
        for path, label in (("/api/system/restart-gateway", "ai.hermes.gateway"),
                            ("/api/system/restart-webui",
                             "com.example.iris.webui")):
            code, body = system.HANDLERS[path]({})
            self.assertEqual((code, body), (200, {"restarted": label}))


if __name__ == "__main__":
    unittest.main()
