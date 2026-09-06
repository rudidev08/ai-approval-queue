#!/usr/bin/env python3
"""Tests for services/actions/jobs.py — stdlib unittest, no live data.

The jobs file and the executions ledger are pointed at a temp dir, the
iris-status states are set directly, and subprocess.Popen is recorded, never
run — no hermes, no real jobs. Run:
python3 -m pytest test_jobs.py -q (from this directory), or
python3 services/actions/test_jobs.py from the repo root.
"""

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import jobs


class JobsTest(unittest.TestCase):
    """Fresh temp files per test; Popen recorded, never run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self._saved = (jobs.JOBS_FILE, jobs.EXECUTIONS, jobs.RETRIES_LOG,
                       jobs._IRIS_STATES, jobs._IRIS_GENERATED)
        jobs.JOBS_FILE = tmp / "jobs.json"
        jobs.EXECUTIONS = tmp / "executions.db"
        jobs.RETRIES_LOG = tmp / "cron-retries.log"
        jobs._IRIS_STATES = {}
        jobs._IRIS_GENERATED = None
        self.spawns = []
        self._popen = jobs.subprocess.Popen

        def fake_popen(argv, **kwargs):
            self.spawns.append(argv)
            if isinstance(self.popen_result, Exception):
                raise self.popen_result

        self.popen_result = None
        jobs.subprocess.Popen = fake_popen
        self.addCleanup(setattr, jobs.subprocess, "Popen", self._popen)

    def tearDown(self):
        (jobs.JOBS_FILE, jobs.EXECUTIONS, jobs.RETRIES_LOG,
         jobs._IRIS_STATES, jobs._IRIS_GENERATED) = self._saved
        self._tmp.cleanup()

    def write_jobs(self, entries):
        jobs.JOBS_FILE.write_text(json.dumps({"jobs": entries}),
                                  encoding="utf-8")

    def job(self, name="backup", **kw):
        j = {"name": name, "id": "id-" + name, "enabled": True,
             "last_run_at": "2026-08-25T03:31:24-07:00", "last_status": "ok",
             "next_run_at": "2026-08-26T03:30:00-07:00"}
        j.update(kw)
        return j

    def executions(self, rows):
        """rows: (id, job_id, status, claimed_at, started_at, finished_at)."""
        conn = sqlite3.connect(jobs.EXECUTIONS)
        conn.execute("CREATE TABLE executions (id TEXT, job_id TEXT, "
                     "status TEXT, claimed_at TEXT, started_at TEXT, "
                     "finished_at TEXT)")
        conn.executemany("INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?)",
                         rows)
        conn.commit()
        conn.close()


class TestState(JobsTest):

    def test_an_unreadable_jobs_file_reports_the_error(self):
        self.assertEqual(jobs.state(), {
            "jobs": [], "runs_24h": 0,
            "error": "could not read the cron jobs file"})

    def test_a_disabled_job_is_left_out(self):
        self.write_jobs([self.job("a"), self.job("b", enabled=False)])
        self.assertEqual([j["name"] for j in jobs.state()["jobs"]], ["a"])

    def test_rows_carry_the_stamps_and_sort_a_to_z(self):
        self.write_jobs([self.job("b"), self.job("a")])
        rows = jobs.state()["jobs"]
        self.assertEqual([j["name"] for j in rows], ["a", "b"])
        self.assertEqual(rows[0], {
            "name": "a", "every": "", "at": "",
            "last_run_at": "2026-08-25T03:31:24-07:00",
            "last_status": "ok", "next_run_at": "2026-08-26T03:30:00-07:00",
            "running_since": None, "took": [],
            "fails_7d": [], "retries_7d": 0, "last_retry": "",
            "state": "ok", "detail": ""})

    def test_a_job_that_never_ran_is_idle(self):
        self.write_jobs([self.job(last_run_at=None, last_status=None)])
        self.assertEqual(jobs.state()["jobs"][0]["state"], "idle")

    def test_a_failed_last_run_is_bad_without_iris_status(self):
        self.write_jobs([self.job(last_status="failed")])
        self.assertEqual(jobs.state()["jobs"][0]["state"], "bad")

    def test_iris_status_wins_and_its_detail_rides_a_bad_row(self):
        self.write_jobs([self.job()])
        jobs.set_iris_states({"backup": {"state": "!!",
                                         "detail": "backup.tar is 3d old"}},
                             "2026-08-25T10:00:00-07:00")
        row = jobs.state()["jobs"][0]
        self.assertEqual((row["state"], row["detail"]),
                         ("bad", "backup.tar is 3d old"))

    def test_a_bad_details_leading_schedule_segment_is_dropped(self):
        self.write_jobs([self.job()])
        jobs.set_iris_states({"backup": {
            "state": "!!", "detail": "03:30 · FAILED 16h ago: exit 1"}},
            "2026-08-25T10:00:00-07:00")
        self.assertEqual(jobs.state()["jobs"][0]["detail"],
                         "FAILED 16h ago: exit 1")

    def test_an_ok_iris_state_carries_no_detail(self):
        self.write_jobs([self.job()])
        jobs.set_iris_states({"backup": {"state": "ok",
                                         "detail": "daily 03:30, ok 10h ago"}},
                             "2026-08-25T10:00:00-07:00")
        row = jobs.state()["jobs"][0]
        self.assertEqual((row["state"], row["detail"]), ("ok", ""))

    def test_a_run_newer_than_the_pass_overrides_its_state(self):
        # the pass ran before the job's last run finished, so its FAILED
        # verdict is about a previous run — the fresh last_status wins
        self.write_jobs([self.job()])
        jobs.set_iris_states({"backup": {"state": "!!",
                                         "detail": "FAILED 5m ago: exit 1"}},
                             "2026-08-25T03:00:00-07:00")
        row = jobs.state()["jobs"][0]
        self.assertEqual((row["state"], row["detail"]), ("ok", ""))

    def test_a_running_newest_attempt_sets_running_since(self):
        self.write_jobs([self.job()])
        self.executions([
            ("e1", "id-backup", "completed", "2026-08-24T03:30:00",
             None, None),
            ("e2", "id-backup", "running", "2026-08-25T03:30:00",
             "2026-08-25T03:30:05", None)])
        self.assertEqual(jobs.state()["jobs"][0]["running_since"],
                         "2026-08-25T03:30:05")

    def test_a_completed_newest_attempt_is_not_running(self):
        self.write_jobs([self.job()])
        self.executions([
            ("e1", "id-backup", "running", "2026-08-24T03:30:00",
             None, None),
            ("e2", "id-backup", "completed", "2026-08-25T03:30:00",
             None, None)])
        self.assertIsNone(jobs.state()["jobs"][0]["running_since"])

    def test_a_claimed_attempt_without_started_at_uses_claimed_at(self):
        self.write_jobs([self.job()])
        self.executions([
            ("e1", "id-backup", "claimed", "2026-08-25T03:30:00",
             None, None)])
        self.assertEqual(jobs.state()["jobs"][0]["running_since"],
                         "2026-08-25T03:30:00")

    def test_a_missing_ledger_means_not_running(self):
        self.write_jobs([self.job()])
        state = jobs.state()
        self.assertIsNone(state["jobs"][0]["running_since"])
        self.assertEqual(state["runs_24h"], 0)

    def test_took_lists_the_last_three_finished_runs_newest_first(self):
        self.write_jobs([self.job()])
        self.executions([
            ("e1", "id-backup", "completed", "2026-08-21T03:30:00",
             "2026-08-21T03:30:00", "2026-08-21T03:30:10"),
            ("e2", "id-backup", "failed", "2026-08-22T03:30:00",
             "2026-08-22T03:30:00", "2026-08-22T03:30:20"),
            ("e3", "id-backup", "completed", "2026-08-23T03:30:00",
             "2026-08-23T03:30:00", "2026-08-23T03:31:15"),
            ("e4", "id-backup", "completed", "2026-08-24T03:30:00",
             "2026-08-24T03:30:00.500000", "2026-08-24T03:30:03.250000")])
        self.assertEqual(jobs.state()["jobs"][0]["took"], [2.8, 75.0, 20.0])

    def test_took_skips_runs_without_both_stamps(self):
        # a claimed/running attempt has no finish, a dead one may have
        # neither, and a mangled stamp cannot be parsed — none of them
        # belong in the list
        self.write_jobs([self.job()])
        self.executions([
            ("e1", "id-backup", "completed", "2026-08-23T03:30:00",
             "2026-08-23T03:30:00", "2026-08-23T03:30:30"),
            ("e2", "id-backup", "completed", "2026-08-24T03:30:00",
             None, "2026-08-24T03:30:10"),
            ("e3", "id-backup", "completed", "2026-08-25T02:30:00",
             "not a stamp", "2026-08-25T02:30:10"),
            ("e4", "id-backup", "running", "2026-08-25T03:30:00",
             "2026-08-25T03:30:05", None)])
        self.assertEqual(jobs.state()["jobs"][0]["took"], [30.0])

    def test_runs_24h_counts_only_the_last_day(self):
        self.write_jobs([self.job()])
        now = datetime.now().astimezone()
        stamp = lambda h: (now - timedelta(hours=h)).isoformat()
        self.executions([
            ("e1", "id-backup", "completed", stamp(1), None, None),
            ("e2", "id-backup", "completed", stamp(23), None, None),
            ("e3", "id-backup", "completed", stamp(25), None, None),
            ("e4", "id-backup", "completed", "not a stamp", None, None)])
        self.assertEqual(jobs.state()["runs_24h"], 2)

    def test_fails_7d_lists_last_weeks_failed_runs_newest_first(self):
        self.write_jobs([self.job()])
        now = datetime.now().astimezone()
        stamp = lambda h: (now - timedelta(hours=h)).isoformat()
        self.executions([
            ("e1", "id-backup", "failed", stamp(1), None, None),
            ("e2", "id-backup", "failed", stamp(100), None, None),
            ("e3", "id-backup", "completed", stamp(2), None, None),
            ("e4", "id-backup", "failed", stamp(24 * 8), None, None)])
        self.assertEqual(jobs.state()["jobs"][0]["fails_7d"],
                         [stamp(1), stamp(100)])

    def test_fails_7d_keeps_at_most_three(self):
        self.write_jobs([self.job()])
        now = datetime.now().astimezone()
        stamp = lambda h: (now - timedelta(hours=h)).isoformat()
        self.executions([
            ("e%d" % h, "id-backup", "failed", stamp(h), None, None)
            for h in (1, 2, 3, 4)])
        self.assertEqual(jobs.state()["jobs"][0]["fails_7d"],
                         [stamp(1), stamp(2), stamp(3)])

    def test_retries_7d_come_from_the_log_matched_by_script_name(self):
        self.write_jobs([self.job(script="backup.sh"),
                         self.job("other", script="other.sh")])
        now = datetime.now().astimezone()
        stamp = lambda h: (now - timedelta(hours=h)).isoformat()
        jobs.RETRIES_LOG.write_text(
            stamp(30) + "\tbackup\texit 1, retry in 300s\n"
            + stamp(2) + "\tbackup\thelper failed: TIMED_OUT\n"
            + stamp(1) + "\tother\texit 1, retry in 300s\n",
            encoding="utf-8")
        rows = jobs.state()["jobs"]
        self.assertEqual((rows[0]["retries_7d"], rows[0]["last_retry"]),
                         (2, "helper failed: TIMED_OUT"))
        self.assertEqual(rows[1]["retries_7d"], 1)

    def test_old_and_mangled_retry_lines_are_left_out(self):
        self.write_jobs([self.job(script="backup.sh")])
        now = datetime.now().astimezone()
        stamp = lambda h: (now - timedelta(hours=h)).isoformat()
        jobs.RETRIES_LOG.write_text(
            stamp(24 * 8) + "\tbackup\ttoo old\n"
            + "not a stamp\tbackup\tbad line\n"
            + "no tabs at all\n",
            encoding="utf-8")
        row = jobs.state()["jobs"][0]
        self.assertEqual((row["retries_7d"], row["last_retry"]), (0, ""))

    def test_a_job_without_a_script_has_no_retries(self):
        self.write_jobs([self.job()])
        jobs.RETRIES_LOG.write_text("", encoding="utf-8")
        row = jobs.state()["jobs"][0]
        self.assertEqual((row["retries_7d"], row["last_retry"]), (0, ""))


class TestSchedule(JobsTest):

    def pair(self, expr):
        return jobs._schedule({"expr": expr, "display": expr})

    def test_daily_single_time(self):
        self.assertEqual(self.pair("30 3 * * *"), ("1d", "03:30"))

    def test_several_runs_a_day_list_every_firing(self):
        self.assertEqual(self.pair("24 */3 * * *"),
                         ("3h", "00:24, 03:24, 06:24, 09:24, 12:24, "
                                "15:24, 18:24, 21:24"))
        self.assertEqual(self.pair("48 6,13,20 * * *"),
                         ("8h", "06:48, 13:48, 20:48"))

    def test_more_than_twelve_firings_keep_the_bare_minute(self):
        self.assertEqual(self.pair("30 * * * *"), ("1h", ":30"))
        self.assertEqual(self.pair("*/15 * * * *"), ("15m", ""))

    def test_weekly_names_the_day_in_the_time_column(self):
        self.assertEqual(self.pair("10 8 * * 0"), ("7d", "Sun 08:10"))
        self.assertEqual(self.pair("0 3 * * 5"), ("7d", "Fri 03:00"))

    def test_unparsed_shapes_fall_back_to_the_display_string(self):
        for expr in ("0 3 1 * *", "0 3 * 6 *", "0 3 * * 1,4"):
            self.assertEqual(self.pair(expr), (expr, ""))
        self.assertEqual(jobs._schedule(
            {"display": "every 5 minutes"}), ("every 5 minutes", ""))
        self.assertEqual(jobs._schedule({}), ("", ""))


class TestRun(JobsTest):

    def test_starts_hermes_cron_run_detached(self):
        self.write_jobs([self.job("backup")])
        code, out = jobs._h_run({"name": "backup"})
        self.assertEqual((code, out), (200, {"started": "backup"}))
        self.assertEqual(self.spawns, [["hermes", "cron", "run", "backup"]])

    def test_an_unknown_job_is_400(self):
        self.write_jobs([self.job("backup")])
        code, _ = jobs._h_run({"name": "nope"})
        self.assertEqual(code, 400)
        self.assertEqual(self.spawns, [])

    def test_a_disabled_job_is_400(self):
        self.write_jobs([self.job("backup", enabled=False)])
        code, _ = jobs._h_run({"name": "backup"})
        self.assertEqual(code, 400)
        self.assertEqual(self.spawns, [])

    def test_an_unreadable_jobs_file_is_400(self):
        code, _ = jobs._h_run({"name": "backup"})
        self.assertEqual(code, 400)
        self.assertEqual(self.spawns, [])

    def test_a_hermes_that_cannot_start_is_500(self):
        self.write_jobs([self.job("backup")])
        self.popen_result = OSError("no such file")
        code, out = jobs._h_run({"name": "backup"})
        self.assertEqual(code, 500)
        self.assertIn("could not start hermes", out["error"])


if __name__ == "__main__":
    unittest.main()
