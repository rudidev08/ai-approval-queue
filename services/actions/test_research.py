#!/usr/bin/env python3
"""Tests for services/actions/research.py — stdlib unittest, no live data.

The topics/reports tree, the driver's runs log, the cron jobs file and the
cleared-flags store are pointed at a temp dir, and subprocess.Popen is
recorded, never run — no driver, no vault, no mail. Run:
python3 -m pytest test_research.py -q (from this directory), or
python3 services/actions/test_research.py from the repo root.
"""

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import research


class ResearchTest(unittest.TestCase):
    """Fresh temp tree per test; Popen recorded, never run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self._saved = (research.TOPICS, research.REPORTS, research.RUNS,
                       research.RUNNING, research.CLEARED, research.CRON_JOBS)
        research.TOPICS = tmp / "topics"
        research.REPORTS = tmp / "reports"
        research.RUNS = tmp / "runs.jsonl"
        research.RUNNING = tmp / "running.json"
        research.CLEARED = tmp / "research-cleared.json"
        research.CRON_JOBS = tmp / "jobs.json"
        research.TOPICS.mkdir()
        research.REPORTS.mkdir()
        self.spawns = []
        self._popen = research.subprocess.Popen

        def fake_popen(argv, **kwargs):
            self.spawns.append(argv)
            if isinstance(self.popen_result, Exception):
                raise self.popen_result

        self.popen_result = None
        research.subprocess.Popen = fake_popen
        self.addCleanup(setattr, research.subprocess, "Popen", self._popen)

    def tearDown(self):
        (research.TOPICS, research.REPORTS, research.RUNS,
         research.RUNNING, research.CLEARED, research.CRON_JOBS) = self._saved
        self._tmp.cleanup()

    def topic(self, slug, question="the question"):
        (research.TOPICS / (slug + ".md")).write_text(question + "\n",
                                                     encoding="utf-8")

    def report(self, slug, updated="2026-08-20"):
        (research.REPORTS / (slug + ".md")).write_text(
            f"---\ntopic: {slug}\nupdated: {updated}\n---\n\nthe report\n",
            encoding="utf-8")

    def runs(self, entries):
        research.RUNS.write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    def cron(self, next_run_at):
        research.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "research-batch", "next_run_at": next_run_at}]}),
            encoding="utf-8")

    def running(self, mapping):
        research.RUNNING.write_text(json.dumps(mapping), encoding="utf-8")


class TestState(ResearchTest):

    def test_no_topics(self):
        self.assertEqual(research.state(), {"topics": [], "next_batch": None})

    def test_topic_without_a_report(self):
        self.topic("daily-thing")
        self.assertEqual(research.state()["topics"], [
            {"slug": "daily-thing", "updated": None, "failing": 0,
             "running": False, "report": False, "error": False,
             "last_run": None}])

    def test_topic_with_a_report_reads_its_updated_stamp(self):
        self.topic("daily-thing")
        self.report("daily-thing")
        t = research.state()["topics"][0]
        self.assertEqual((t["updated"], t["report"]), ("2026-08-20", True))

    def test_failing_comes_from_the_logs_last_entry(self):
        self.topic("daily-thing")
        self.runs([{"slug": "daily-thing", "consecutive_failures": 1},
                   {"slug": "daily-thing", "consecutive_failures": 3}])
        self.assertEqual(research.state()["topics"][0]["failing"], 3)

    def test_a_failure_dump_sets_the_error_flag(self):
        self.topic("daily-thing")
        (research.REPORTS / "error-daily-thing.md").write_text("x",
                                                               encoding="utf-8")
        self.assertTrue(research.state()["topics"][0]["error"])

    def test_a_missing_runs_log_means_no_failures(self):
        self.topic("daily-thing")
        self.assertEqual(research.state()["topics"][0]["failing"], 0)

    def test_last_run_is_the_last_successful_runs_stamp(self):
        self.topic("daily-thing")
        self.runs([{"slug": "daily-thing", "ts": "2026-08-19T03:00:00",
                    "status": "ok", "consecutive_failures": 0},
                   {"slug": "daily-thing", "ts": "2026-08-20T03:00:00",
                    "status": "error", "consecutive_failures": 1}])
        t = research.state()["topics"][0]
        self.assertEqual(t["last_run"], "2026-08-19T03:00:00")
        self.assertEqual(t["failing"], 1)

    def test_a_fresh_running_marker_sets_the_running_flag(self):
        self.topic("daily-thing")
        self.running({"daily-thing":
                      datetime.now().isoformat(timespec="seconds")})
        self.assertTrue(research.state()["topics"][0]["running"])

    def test_a_stale_running_marker_is_ignored(self):
        self.topic("daily-thing")
        self.running({"daily-thing": (
            datetime.now() - timedelta(seconds=research.RUNNING_TTL + 60)
        ).isoformat(timespec="seconds")})
        self.assertFalse(research.state()["topics"][0]["running"])

    def test_a_malformed_running_marker_is_ignored(self):
        self.topic("daily-thing")
        research.RUNNING.write_text("not json", encoding="utf-8")
        self.assertFalse(research.state()["topics"][0]["running"])

    def test_next_batch_is_the_cron_jobs_next_run(self):
        self.cron("2026-08-28T03:00:00-07:00")
        self.assertEqual(research.state()["next_batch"],
                         "2026-08-28T03:00:00-07:00")

    def test_an_unreadable_cron_file_means_no_next_batch(self):
        research.CRON_JOBS.write_text("not json", encoding="utf-8")
        self.assertIsNone(research.state()["next_batch"])


class TestReport(ResearchTest):

    def test_returns_the_prompt_and_the_reports_text(self):
        self.topic("daily-thing", "the question\n\nwith detail")
        self.report("daily-thing")
        code, out = research._h_report({"slug": "daily-thing"})
        self.assertEqual((code, out), (200, {
            "question": "the question\n\nwith detail",
            "report": "the report\n", "updated": "2026-08-20"}))

    def test_a_topic_without_a_report_returns_a_null_report(self):
        self.topic("daily-thing")
        code, out = research._h_report({"slug": "daily-thing"})
        self.assertEqual((code, out), (200, {
            "question": "the question", "report": None, "updated": None}))

    def test_an_unknown_or_malformed_slug_is_400(self):
        for slug in ("daily-nope", "../topics/daily-thing", ""):
            code, _ = research._h_report({"slug": slug})
            self.assertEqual(code, 400)


class TestClear(ResearchTest):

    def failing_topic(self, ts="2026-08-20T03:00:00", failures=2):
        self.topic("daily-thing")
        self.runs([{"slug": "daily-thing", "ts": ts, "status": "error",
                    "consecutive_failures": failures}])

    def test_clear_drops_the_flag_until_the_next_failure(self):
        self.failing_topic()
        self.assertEqual(research.state()["topics"][0]["failing"], 2)
        code, out = research._h_clear({"slug": "daily-thing"})
        self.assertEqual((code, out), (200, {"cleared": "daily-thing"}))
        self.assertEqual(research.state()["topics"][0]["failing"], 0)
        self.runs([{"slug": "daily-thing", "ts": "2026-08-20T03:00:00",
                    "status": "error", "consecutive_failures": 2},
                   {"slug": "daily-thing", "ts": "2026-08-21T03:00:00",
                    "status": "error", "consecutive_failures": 3}])
        self.assertEqual(research.state()["topics"][0]["failing"], 3)

    def test_clear_writes_the_store_0600(self):
        self.failing_topic()
        research._h_clear({"slug": "daily-thing"})
        saved = json.loads(research.CLEARED.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"daily-thing": "2026-08-20T03:00:00"})
        self.assertEqual(research.CLEARED.stat().st_mode & 0o777, 0o600)

    def test_clearing_a_topic_that_is_not_failing_is_400(self):
        self.topic("daily-thing")
        code, out = research._h_clear({"slug": "daily-thing"})
        self.assertEqual(code, 400)
        self.assertIn("not failing", out["error"])
        self.assertFalse(research.CLEARED.exists())

    def test_clearing_an_unknown_or_malformed_slug_is_400(self):
        for slug in ("daily-nope", "../reports/daily-thing", ""):
            code, _ = research._h_clear({"slug": slug})
            self.assertEqual(code, 400)

    def test_a_successful_run_needs_no_clear(self):
        self.topic("daily-thing")
        self.runs([{"slug": "daily-thing", "ts": "2026-08-20T03:00:00",
                    "status": "ok", "consecutive_failures": 0}])
        code, _ = research._h_clear({"slug": "daily-thing"})
        self.assertEqual(code, 400)


class TestRun(ResearchTest):

    def test_spawns_the_driver_for_the_topic(self):
        self.topic("daily-thing")
        code, out = research._h_run({"slug": "daily-thing"})
        self.assertEqual((code, out), (200, {"started": "daily-thing"}))
        self.assertEqual(self.spawns, [[research.PYTHON,
                                        str(research.DRIVER), "--topic",
                                        "daily-thing"]])

    def test_unknown_topic_is_400(self):
        code, out = research._h_run({"slug": "daily-nope"})
        self.assertEqual(code, 400)
        self.assertEqual(self.spawns, [])

    def test_malformed_slug_is_400(self):
        for slug in ("", "../reports/daily-thing", "daily thing"):
            code, _ = research._h_run({"slug": slug})
            self.assertEqual(code, 400)
        self.assertEqual(self.spawns, [])

    def test_a_driver_that_cannot_start_is_500(self):
        self.topic("daily-thing")
        self.popen_result = OSError("no such file")
        code, out = research._h_run({"slug": "daily-thing"})
        self.assertEqual(code, 500)
        self.assertIn("could not start the driver", out["error"])


class TestDelete(ResearchTest):

    def test_deletes_the_report_and_the_failure_dump_keeps_the_topic(self):
        self.topic("daily-thing")
        self.report("daily-thing")
        (research.REPORTS / "error-daily-thing.md").write_text("x",
                                                               encoding="utf-8")
        code, out = research._h_delete({"slug": "daily-thing"})
        self.assertEqual(code, 200)
        self.assertEqual(sorted(out["deleted"]),
                         ["daily-thing.md", "error-daily-thing.md"])
        self.assertEqual(list(research.REPORTS.iterdir()), [])
        self.assertTrue((research.TOPICS / "daily-thing.md").exists())

    def test_nothing_to_delete_is_400(self):
        self.topic("daily-thing")
        code, _ = research._h_delete({"slug": "daily-thing"})
        self.assertEqual(code, 400)

    def test_malformed_slug_is_400(self):
        code, _ = research._h_delete({"slug": "../topics/daily-thing"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
