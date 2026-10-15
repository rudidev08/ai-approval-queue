#!/usr/bin/env python3
"""Tests for services/ops/system.py — stdlib unittest, no live data.

subprocess.run is patched in every test, so hermes never runs, and the
decisions log lives in a temp directory. Run:
python3 services/ops/test_system.py from the repo root.
"""

import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import system


class SystemTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {
            "ASSISTANT_OPS_STATE": self._tmp.name,
            "ASSISTANT_LLM_PAUSE": os.path.join(self._tmp.name, "assistant-llm-pause.json"),
            "ASSISTANT_LAUNCHD_SOURCE": os.path.join(self._tmp.name, "launchd")})
        self._env.start()
        os.mkdir(os.path.join(self._tmp.name, "launchd"))
        self._saved = subprocess.run
        self._saved_plist = system.GATEWAY_PLIST
        system.GATEWAY_PLIST = pathlib.Path(self._tmp.name) / "live.plist"
        self._saved_status = system._STATUS
        self.calls = []
        self.patch_run()

    def tearDown(self):
        subprocess.run = self._saved
        system._STATUS = self._saved_status
        system.GATEWAY_PLIST = self._saved_plist
        self._env.stop()
        self._tmp.cleanup()

    def patch_run(self, returncode=0, stderr="", stdout="", raises=None):
        def fake(cmd, **kw):
            self.calls.append(cmd)
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        subprocess.run = fake

    # ---- doctor --fix ----

    def test_doctor_fix_runs_doctor_and_reports_the_fixed_count(self):
        self.patch_run(stdout="  ✓ macOS TCC anchor installed\n"
                              "  Fixed 1 issue(s). 2 issue(s) require manual "
                              "intervention.\n")
        code, body = system.doctor_fix()
        self.assertEqual((code, body), (200, {"fixed": 1}))
        self.assertEqual(self.calls, [["hermes", "doctor", "--fix"]])
        line = next(common.state_dir().glob("decisions-*.jsonl")).read_text()
        self.assertIn("doctor_fix", line)
        self.assertIn("fixed 1", line)

    def test_doctor_fix_with_nothing_to_fix_reports_zero(self):
        self.patch_run(stdout="  ✓ all good\n")
        self.assertEqual(system.doctor_fix(), (200, {"fixed": 0}))

    def test_doctor_fix_crash_gives_502_with_doctors_text(self):
        self.patch_run(returncode=1, stderr="Traceback: boom\n")
        code, body = system.doctor_fix()
        self.assertEqual(code, 502)
        self.assertIn("exited 1", body["error"])
        self.assertIn("boom", body["error"])
        self.assertEqual(list(common.state_dir().glob("decisions-*.jsonl")), [])

    def test_doctor_fix_timeout_gives_502(self):
        self.patch_run(raises=subprocess.TimeoutExpired("hermes", 120))
        code, body = system.doctor_fix()
        self.assertEqual(code, 502)
        self.assertIn("hermes doctor", body["error"])

    # ---- gateway plist ----

    def write_live_plist(self, path="/venv/bin:/usr/bin"):
        system.GATEWAY_PLIST.write_bytes(plistlib.dumps(
            {"Label": "ai.hermes.gateway",
             "EnvironmentVariables": {"PATH": path}}))

    def test_gateway_plist_runs_the_launcher_with_the_gateway_path(self):
        self.write_live_plist()
        seen = {}
        def fake(cmd, **kw):
            seen.update(cmd=cmd, path=kw["env"]["PATH"])
            return subprocess.CompletedProcess(cmd, 0, "✓ Service started", "")
        subprocess.run = fake
        self.assertEqual(system.gateway_plist(), (200, {"refreshed": True}))
        self.assertEqual(seen["cmd"], [str(system.HERMES_LAUNCHER),
                                       "gateway", "start"])
        self.assertEqual(seen["path"], "/venv/bin:/usr/bin")
        line = next(common.state_dir().glob("decisions-*.jsonl")).read_text()
        self.assertIn("gateway_plist", line)

    def test_gateway_plist_copies_the_new_plist(self):
        self.write_live_plist()
        def fake(cmd, **kw):
            self.write_live_plist("/rewritten")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        subprocess.run = fake
        system.gateway_plist()
        copy = pathlib.Path(self._tmp.name) / "launchd" / "live.plist"
        self.assertEqual(copy.read_bytes(), system.GATEWAY_PLIST.read_bytes())

    def test_gateway_plist_unreadable_runs_nothing(self):
        self.patch_run()
        code, body = system.gateway_plist()
        self.assertEqual(code, 502)
        self.assertIn("gateway PATH", body["error"])
        self.assertEqual(self.calls, [])

    def test_gateway_plist_failure_gives_502_and_copies_nothing(self):
        self.write_live_plist()
        self.patch_run(returncode=1, stderr="launchctl: boom\n")
        code, body = system.gateway_plist()
        self.assertEqual(code, 502)
        self.assertIn("exited 1", body["error"])
        self.assertIn("boom", body["error"])
        self.assertFalse(
            (pathlib.Path(self._tmp.name) / "launchd" / "live.plist").exists())

    def test_plist_written_at_reads_the_gateway_plist(self):
        self.write_live_plist()
        os.utime(system.GATEWAY_PLIST, (0, 1790000000))
        self.assertEqual(system.plist_written_at(),
                         datetime.fromtimestamp(1790000000).isoformat(
                             timespec="seconds"))

    def test_plist_written_at_without_a_gateway_plist_says_none(self):
        self.assertIsNone(system.plist_written_at())

    def test_gateway_plist_handler(self):
        self.write_live_plist()
        self.patch_run()
        self.assertEqual(system.HANDLERS["/api/system/gateway-plist"]({}),
                         (200, {"refreshed": True}))

    # ---- the pause ----

    def test_pause_writes_the_file_and_state_carries_it(self):
        code, body = system._h_llm_pause({"hours": 4})
        self.assertEqual(code, 200)
        self.assertEqual(body["source"], "ops")
        self.assertEqual(system.state()["llm_pause"], body)
        line = next(common.state_dir().glob("decisions-*.jsonl")).read_text()
        self.assertIn("llm_pause", line)

    def test_pause_refuses_other_lengths(self):
        for hours in (0, 3, "2", None):
            code, body = system._h_llm_pause({"hours": hours})
            self.assertEqual(code, 400)
        self.assertIsNone(system.state()["llm_pause"])

    def test_resume_deletes_the_file(self):
        system._h_llm_pause({"hours": 1})
        self.assertEqual(system._h_llm_resume({}), (200, {}))
        self.assertIsNone(system.state()["llm_pause"])
        self.assertEqual(system._h_llm_resume({}), (200, {}))

    # ---- the status check's problem items ----

    def test_state_has_no_status_items_until_the_first_check(self):
        system._STATUS = {"items": None, "checked_at": None}
        self.assertEqual(system.state()["status"],
                         {"items": None, "checked_at": None})

    def test_state_carries_the_status_items_once_set(self):
        items = [{"name": "voice-audio", "state": "!!", "detail": "http fail"}]
        system.set_status(items, "2026-09-13T00:00:00Z")
        self.assertEqual(system.state()["status"],
                         {"items": items, "checked_at": "2026-09-13T00:00:00Z"})

    def test_a_failed_check_gives_none_items_with_a_stamp(self):
        system.set_status(None, "2026-09-13T00:00:00Z")
        self.assertEqual(system.state()["status"],
                         {"items": None, "checked_at": "2026-09-13T00:00:00Z"})

    def test_doctor_fix_handler(self):
        self.patch_run(stdout="  Fixed 2 issue(s).\n")
        self.assertEqual(system.HANDLERS["/api/system/doctor-fix"]({}),
                         (200, {"fixed": 2}))

    # ---- status report and health check ----

    def test_status_report_returns_the_script_text_even_on_problems(self):
        self.patch_run(returncode=1, stdout="assistant status · 1 problem\n  !!  lmstudio\n")
        code, body = system.GET_HANDLERS["/api/system/status-report"]({})
        self.assertEqual((code, body), (200, {"text": "assistant status · 1 problem\n  !!  lmstudio"}))
        self.assertEqual(self.calls, [[str(system.STATUS_SCRIPT)]])

    def test_status_report_without_output_fails(self):
        self.patch_run(returncode=2, stderr="manifest missing")
        self.assertEqual(system.GET_HANDLERS["/api/system/status-report"]({}),
                         (500, {"error": "status script failed: manifest missing"}))

    def test_health_check_passes(self):
        code, body = system.GET_HANDLERS["/api/system/health-check"]({})
        self.assertEqual((code, body["passed"]), (200, True))
        self.assertEqual(self.calls, [[str(system.UV), "run", "--no-project",
                                       str(system.HEALTH_CHECK)]])

    def test_health_check_failure_carries_the_output(self):
        self.patch_run(returncode=1, stdout="router: wan_check timed out\n")
        code, body = system.GET_HANDLERS["/api/system/health-check"]({})
        self.assertEqual((code, body), (200, {"passed": False, "text":
            "health check FAILED:\nrouter: wan_check timed out"}))


if __name__ == "__main__":
    unittest.main()
