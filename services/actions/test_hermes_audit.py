#!/usr/bin/env python3
"""Tests for services/actions/hermes_audit.py — stdlib unittest, no live data.

Every path the module reads is pointed at a temp directory and Popen is
recorded, so nothing here starts an audit run or touches the real
~/.local/state/hermes-audit. The run lock is a real flock: locks belong to the
open file description, so a second open in this process conflicts with the
first exactly like another process would. Run:
python3 -m pytest test_hermes_audit.py -q (from this directory), or
python3 services/actions/test_hermes_audit.py from the repo root.
"""

import fcntl
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import hermes_audit


def stamp(minutes_ago):
    """audit.py writes local wall-clock stamps; the stderr check compares them
    against a file mtime, so a fixture's stamps have to be real times."""
    return (datetime.now()
            - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def record(labels_states):
    """A run record: (label, state) pairs, findings on every warn category."""
    return {"step": 4, "total": 9, "category": "tool servers",
            "step_started": stamp(12), "llm": None,
            "llm_started": None, "started_at": stamp(30),
            "finished_at": stamp(5),
            "categories": [{"label": label, "state": state,
                            "findings": ["a finding"] if state == "warn" else [],
                            "notes": []} for label, state in labels_states]}


class Fake:
    """A recorded Popen: never a real child."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return self

    def poll(self):
        return None


class HermesAuditTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self._saved = (hermes_audit.STATE, hermes_audit.LOCK, hermes_audit.RUN,
                       hermes_audit.ERR, hermes_audit.EXPECTED,
                       hermes_audit._child, common.STATE_DIR,
                       common.CRON_JOBS,
                       subprocess.Popen, subprocess.run)
        hermes_audit.STATE = tmp
        hermes_audit.LOCK = tmp / "lock"
        hermes_audit.RUN = tmp / "run.json"
        hermes_audit.ERR = tmp / "last-run.err"
        hermes_audit.EXPECTED = tmp / "expected.yaml"
        hermes_audit._child = None
        common.STATE_DIR = tmp
        # absent stand-in: no test here may read the real cron files
        common.CRON_JOBS = tmp / "no-jobs.json"
        self.held = None

    def tearDown(self):
        if self.held is not None:
            self.held.close()
        (hermes_audit.STATE, hermes_audit.LOCK, hermes_audit.RUN, hermes_audit.ERR,
         hermes_audit.EXPECTED,
         hermes_audit._child, common.STATE_DIR,
         common.CRON_JOBS,
         subprocess.Popen, subprocess.run) = self._saved
        self._tmp.cleanup()

    def hold_lock(self):
        """Take the audit's run lock, as a run in progress would."""
        self.held = open(hermes_audit.LOCK, "w")
        fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def write_run(self, rec):
        hermes_audit.RUN.write_text(json.dumps(rec))

    # ---- state ----

    # state() carries the cron job's stamps too; with the stand-in files
    # absent they read as Nones
    NO_JOB = {"job_last_run_at": None, "job_next_run_at": None,
              "job_last_failed": False}

    def test_state_before_any_run(self):
        self.assertEqual(hermes_audit.state(),
                         {"running": False, "run": None, "error": None,
                          **self.NO_JOB})

    def test_state_returns_the_finished_record(self):
        rec = record([("tool approvals", "ok"), ("tool servers", "warn")])
        self.write_run(rec)
        got = hermes_audit.state()
        self.assertEqual((got["running"], got["error"]), (False, None))
        self.assertEqual(got["run"], rec)

    def test_state_is_running_while_the_lock_is_held(self):
        self.hold_lock()
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = None
        self.write_run(rec)
        got = hermes_audit.state()
        # unfinished plus running is a run in progress, never a failure
        self.assertEqual((got["running"], got["error"]), (True, None))

    def test_state_reports_a_run_that_died_without_a_report(self):
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = None
        self.write_run(rec)
        hermes_audit.ERR.write_text("AUDIT FAILED: cannot read expected.yaml\n")
        got = hermes_audit.state()
        self.assertEqual(got["running"], False)
        self.assertIn("cannot read expected.yaml", got["error"])

    def test_state_reports_a_run_that_never_started(self):
        # a finished record plus newer stderr: the start died before audit.py
        # wrote anything, so the record is the pass before it
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = "2020-01-01T00:00:00"
        self.write_run(rec)
        hermes_audit.ERR.write_text("error: Failed to spawn: `audit.py`\n")
        got = hermes_audit.state()
        self.assertEqual(got["running"], False)
        self.assertIn("Failed to spawn", got["error"])
        self.assertEqual(got["run"]["finished_at"], "2020-01-01T00:00:00")

    def test_state_ignores_stderr_older_than_the_pass(self):
        # the same files, the other way round: the pass came after that error
        hermes_audit.ERR.write_text("an error from an older run\n")
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = "2099-01-01T00:00:00"
        self.write_run(rec)
        self.assertIsNone(hermes_audit.state()["error"])

    def test_state_survives_an_unreadable_record(self):
        hermes_audit.RUN.write_text("{ not json")
        self.assertEqual(hermes_audit.state(),
                         {"running": False, "run": None, "error": None,
                          **self.NO_JOB})

    def test_state_survives_a_record_that_is_not_an_object(self):
        # one bad file must not throw inside /api/state and blank every area
        hermes_audit.RUN.write_text("[1, 2, 3]")
        self.assertEqual(hermes_audit.state(),
                         {"running": False, "run": None, "error": None,
                          **self.NO_JOB})

    # ---- cron job stamps ----

    def test_job_fields_read_the_job_record(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "hermes-audit", "id": "x",
             "last_run_at": "2026-08-16T08:10:00-07:00",
             "next_run_at": "2026-08-23T08:10:00-07:00",
             "last_status": "ok"},
            {"name": "other-job", "last_status": "error"}]}))
        out = hermes_audit._job_fields()
        self.assertEqual(out["job_last_run_at"], "2026-08-16T08:10:00-07:00")
        self.assertEqual(out["job_next_run_at"], "2026-08-23T08:10:00-07:00")
        self.assertFalse(out["job_last_failed"])

    def test_job_fields_flag_a_failed_run(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "hermes-audit", "last_run_at": "2026-08-16T08:10:00",
             "next_run_at": None, "last_status": "error"}]}))
        self.assertTrue(hermes_audit._job_fields()["job_last_failed"])

    def test_job_fields_before_the_job_exists(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": []}))
        self.assertEqual(hermes_audit._job_fields(),
                         {"job_last_run_at": None, "job_next_run_at": None,
                          "job_last_failed": False})

    # ---- starting a run ----

    def test_start_spawns_the_script_detached(self):
        fake = Fake()
        subprocess.Popen = fake
        code, body = hermes_audit.start()
        self.assertEqual((code, body), (200, {"started": True}))
        argv, kw = fake.calls[0]
        self.assertEqual(argv[:3], [str(hermes_audit.UV), "run", "--no-project"])
        self.assertTrue(argv[3].endswith("hermes-audit/audit.py"))
        self.assertIs(kw["start_new_session"], True)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)

    def test_start_refuses_while_a_run_holds_the_lock(self):
        self.hold_lock()
        fake = Fake()
        subprocess.Popen = fake
        code, body = hermes_audit.start()
        self.assertEqual(code, 409)
        self.assertIn("already in progress", body["error"])
        self.assertEqual(fake.calls, [])

    # ---- stop and reset ----

    def test_control_relays_the_scripts_message(self):
        def fake_run(argv, **kw):
            self.argv = argv
            return subprocess.CompletedProcess(argv, 0, "audit run 42 stopped", "")
        subprocess.run = fake_run
        code, body = hermes_audit.control("--stop", "hermes_audit_stopped")
        self.assertEqual((code, body), (200, {"result": "audit run 42 stopped"}))
        self.assertEqual(self.argv[-1], "--stop")

    def test_control_reports_a_refusal_as_an_error(self):
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 1, "", "AUDIT FAILED: no audit run in progress")
        subprocess.run = fake_run
        code, body = hermes_audit.control("--stop", "hermes_audit_stopped")
        self.assertEqual(code, 500)
        self.assertIn("no audit run in progress", body["error"])

    def test_control_reports_a_timeout_instead_of_raising(self):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 120)
        subprocess.run = fake_run
        code, body = hermes_audit.control("--reset", "hermes_audit_reset")
        self.assertEqual(code, 500)
        self.assertIn("TimeoutExpired", body["error"])

    # ---- ignoring doctor warnings for good ----

    YAML = ('# comment above\n'
            'doctor_known:\n'
            '  - "⚠ browser (system dependency not met)"\n'
            '\n'
            'other_list:\n'
            '  - "kept"\n')

    def doctored(self, *findings):
        """A finished pass whose diagnostics category carries `findings`, and
        an expected.yaml with a one-entry doctor_known list."""
        rec = record([("tool approvals", "ok"), ("hermes diagnostics", "warn")])
        rec["categories"][1]["findings"] = list(findings)
        self.write_run(rec)
        hermes_audit.EXPECTED.write_text(self.YAML)
        return rec

    def test_ignore_doctor_appends_to_the_known_list(self):
        warn = "⚠ a2a (system dependency not met)"
        rec = self.doctored(f"iris: new doctor warning — {warn}",
                            f"ops: new doctor warning — {warn}")
        code, body = hermes_audit.ignore_doctor(
            {"category": "hermes diagnostics",
             "finding": f"iris: new doctor warning — {warn}"})
        self.assertEqual((code, body), (200, {"ignored": True}))
        # the entry lands at the end of the block; comments and the list
        # after it are untouched
        self.assertEqual(hermes_audit.EXPECTED.read_text(),
                         self.YAML.replace('\n\nother_list',
                                           f'\n  - "{warn}"\n\nother_list'))

    def test_ignore_doctor_skips_an_entry_already_known(self):
        warn = "⚠ browser (system dependency not met)"
        self.doctored(f"iris: new doctor warning — {warn}")
        code, _ = hermes_audit.ignore_doctor(
            {"category": "hermes diagnostics",
             "finding": f"iris: new doctor warning — {warn}"})
        self.assertEqual(code, 200)
        self.assertEqual(hermes_audit.EXPECTED.read_text(), self.YAML)

    def test_ignore_doctor_refuses_a_finding_without_the_marker(self):
        self.doctored("iris: hermes doctor exited 2")
        code, body = hermes_audit.ignore_doctor(
            {"category": "hermes diagnostics",
             "finding": "iris: hermes doctor exited 2"})
        self.assertEqual(code, 400)
        self.assertIn("only doctor warnings", body["error"])
        self.assertEqual(hermes_audit.EXPECTED.read_text(), self.YAML)

    def test_ignore_doctor_refuses_a_finding_not_in_the_pass(self):
        self.doctored("iris: new doctor warning — ⚠ x (y)")
        code, body = hermes_audit.ignore_doctor(
            {"category": "hermes diagnostics",
             "finding": "iris: new doctor warning — ⚠ stale (z)"})
        self.assertEqual(code, 404)
        self.assertIn("reload the page", body["error"])
        self.assertEqual(hermes_audit.EXPECTED.read_text(), self.YAML)

    def test_ignore_doctor_reports_a_missing_known_list(self):
        self.doctored("iris: new doctor warning — ⚠ x (y)")
        hermes_audit.EXPECTED.write_text("other_list:\n  - \"kept\"\n")
        code, body = hermes_audit.ignore_doctor(
            {"category": "hermes diagnostics",
             "finding": "iris: new doctor warning — ⚠ x (y)"})
        self.assertEqual(code, 500)
        self.assertIn("doctor_known", body["error"])

    # ---- area interface ----

    def test_handlers_cover_the_four_endpoints(self):
        self.assertEqual(sorted(hermes_audit.HANDLERS),
                         ["/api/hermes-audit/ignore-doctor",
                          "/api/hermes-audit/reset",
                          "/api/hermes-audit/run", "/api/hermes-audit/stop"])


if __name__ == "__main__":
    unittest.main()
