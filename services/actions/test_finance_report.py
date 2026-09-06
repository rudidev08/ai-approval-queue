#!/usr/bin/env python3
"""Tests for services/actions/finance_report.py — stdlib unittest, no live data.

subprocess.run is patched in every test, so the report script never runs,
nothing reads the budget and no draft is made. Run:
python3 -m pytest test_finance_report.py -q (from this directory), or
python3 services/actions/test_finance_report.py from the repo root.
"""

import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import finance_report


class Done:
    """What subprocess.run returns."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class ReportTest(unittest.TestCase):
    """Fresh module state per test; subprocess.run recorded, never run; the
    recipients and the save folder pointed at test values."""

    def setUp(self):
        finance_report._building_since = None
        finance_report._building_section = None
        finance_report._drafting_since = None
        finance_report._drafting_section = None
        finance_report._reports = {}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # the email stamps read hermes' jobs file; pointed here, state()
        # never sees the real report jobs
        self.addCleanup(setattr, common, "CRON_JOBS", common.CRON_JOBS)
        common.CRON_JOBS = pathlib.Path(self._tmp.name) / "jobs.json"
        for name, value in (("REPORT_DIR", pathlib.Path(self._tmp.name) / "reports"),
                            ("recipients", lambda: ["a@example.com",
                                                     "b@example.com"])):
            self.addCleanup(setattr, finance_report, name,
                            getattr(finance_report, name))
            setattr(finance_report, name, value)
        self.calls = []
        self.result = Done(stdout="the report\n")
        self._saved = finance_report.subprocess.run

        def fake_run(argv, **kwargs):
            self.calls.append({"argv": argv, "input": kwargs.get("input")})
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        finance_report.subprocess.run = fake_run
        self.addCleanup(setattr, finance_report.subprocess, "run", self._saved)

    def build(self, section="full", report="daily", detailed=False,
              categories=False, combine_personal=False):
        """The thread body, run inline — the endpoint's own thread start is
        covered separately."""
        finance_report._building_since = finance_report._now()
        finance_report._building_section = section
        finance_report._build(section, report, detailed, categories,
                              combine_personal)

    def wait(self, ready):
        for _ in range(200):
            if ready():
                break
            time.sleep(0.01)

    def base_argv(self):
        return [finance_report.PYTHON, str(finance_report.SCRIPT), "--email",
                "--preview"]


class TestBuild(ReportTest):

    def test_runs_the_script_write_free(self):
        self.build()
        self.assertEqual([c["argv"] for c in self.calls], [self.base_argv()])

    def test_section_run_adds_the_flag_and_caches_under_its_name(self):
        self.build("cashflow")
        self.assertEqual(self.calls[0]["argv"][-2:], ["--section", "cashflow"])
        self.assertEqual(finance_report._reports["cashflow"]["text"],
                         "the report\n")
        self.assertIsNone(finance_report._building_section)

    def test_settings_become_flags_and_are_kept_with_the_build(self):
        self.build("full", "2026-08", True, True, True)
        self.assertEqual(self.calls[0]["argv"],
                         self.base_argv() + ["--month", "2026-08",
                                             "--detailed", "--categories",
                                             "--combine-personal"])
        rep = finance_report._reports["full"]
        self.assertEqual((rep["report"], rep["detailed"], rep["categories"],
                          rep["combine_personal"]),
                         ("2026-08", True, True, True))
        self.assertEqual(rep["label"], "2026-08")

    def test_weekly_becomes_its_flag_and_is_labelled_by_date(self):
        self.build("cashflow", "weekly")
        self.assertEqual(self.calls[0]["argv"],
                         self.base_argv() + ["--weekly", "--section",
                                             "cashflow"])
        rep = finance_report._reports["cashflow"]
        self.assertEqual((rep["report"], rep["label"]),
                         ("weekly", date.today().isoformat()))

    def test_todays_build_is_labelled_by_date(self):
        self.build()
        self.assertEqual(finance_report._reports["full"]["label"],
                         date.today().isoformat())
        self.assertEqual(finance_report._reports["full"]["report"], "daily")

    def test_good_run_stores_the_body(self):
        self.build()
        self.assertEqual(finance_report._reports["full"]["text"],
                         "the report\n")
        self.assertIsNone(finance_report._building_since)

    def test_each_part_keeps_its_own_last_build(self):
        self.build()
        self.result = Done(stdout="## Cash flow\n")
        self.build("cashflow")
        self.assertEqual(finance_report._reports["full"]["text"],
                         "the report\n")
        self.assertEqual(finance_report._reports["cashflow"]["text"],
                         "## Cash flow\n")

    def test_failed_run_stores_the_stderr_tail(self):
        self.result = Done(returncode=1, stderr="x" * 2000 + "llm failed")
        self.build()
        report = finance_report._reports["full"]
        self.assertNotIn("text", report)
        self.assertTrue(report["error"].endswith("llm failed"))
        self.assertEqual(len(report["error"]), finance_report.ERR_TAIL)

    def test_failure_without_stderr_names_the_exit_code(self):
        self.result = Done(returncode=2)
        self.build()
        self.assertIn("exited 2", finance_report._reports["full"]["error"])

    def test_timeout_is_an_error_not_a_crash(self):
        self.result = subprocess.TimeoutExpired("finance_jobs.py", 600)
        self.build()
        self.assertIn("TimeoutExpired",
                      finance_report._reports["full"]["error"])
        self.assertIsNone(finance_report._building_since)

    def test_missing_interpreter_is_an_error(self):
        self.result = OSError("no such file")
        self.build()
        self.assertIn("OSError", finance_report._reports["full"]["error"])

    def test_a_new_build_replaces_that_part_only(self):
        self.build()
        self.build("outliers")
        self.result = Done(stdout="newer\n")
        self.build()
        self.assertEqual(finance_report._reports["full"]["text"], "newer\n")
        self.assertEqual(finance_report._reports["outliers"]["text"],
                         "the report\n")

    def test_a_new_build_drops_the_old_send_and_save_marks(self):
        self.build()
        finance_report._reports["full"]["drafted_at"] = "2026-08-18T09:00:00Z"
        self.build()
        self.assertNotIn("drafted_at", finance_report._reports["full"])


class TestPreview(ReportTest):

    def test_starts_a_build(self):
        code, out = finance_report.preview()
        self.assertEqual((code, out), (200, {"started": True}))
        # the thread is daemonised; wait for the result it stores
        self.wait(lambda: finance_report._reports)
        self.assertEqual(finance_report._reports["full"]["text"],
                         "the report\n")

    def test_absent_and_full_both_mean_the_whole_body(self):
        code, _ = finance_report.preview("full")
        self.assertEqual(code, 200)
        self.wait(lambda: self.calls)
        self.assertEqual(self.calls[0]["argv"], self.base_argv())

    def test_settings_reach_the_build(self):
        code, _ = finance_report.preview("cashflow", "2026-08", True, False,
                                         True)
        self.assertEqual(code, 200)
        self.wait(lambda: self.calls)
        self.assertEqual(self.calls[0]["argv"],
                         self.base_argv() + ["--month", "2026-08",
                                             "--detailed",
                                             "--combine-personal",
                                             "--section", "cashflow"])

    def test_current_month_is_todays_build(self):
        code, _ = finance_report.preview("full", date.today().strftime("%Y-%m"))
        self.assertEqual(code, 200)
        self.wait(lambda: finance_report._reports)
        self.assertEqual(self.calls[0]["argv"], self.base_argv())
        self.assertEqual(finance_report._reports["full"]["label"],
                         date.today().isoformat())

    def test_second_call_while_building_is_409(self):
        finance_report._building_since = finance_report._now()
        code, out = finance_report.preview()
        self.assertEqual(code, 409)
        self.assertIn("already building", out["error"])
        self.assertEqual(self.calls, [])

    def test_refused_while_drafting(self):
        finance_report._drafting_since = finance_report._now()
        code, out = finance_report.preview()
        self.assertEqual(code, 409)
        self.assertIn("being drafted", out["error"])

    def test_unknown_section_is_400(self):
        code, out = finance_report.preview("junk")
        self.assertEqual(code, 400)
        self.assertIn("unknown section", out["error"])
        self.assertEqual(self.calls, [])
        self.assertIsNone(finance_report._building_since)

    def test_bad_report_is_400(self):
        for bad in ("2026-8", "monthly", "Weekly"):
            code, out = finance_report.preview("full", bad)
            self.assertEqual(code, 400, bad)
            self.assertIn("daily, weekly or YYYY-MM", out["error"])
        self.assertEqual(self.calls, [])

    def test_absent_report_is_daily(self):
        code, _ = finance_report.preview("full", "")
        self.assertEqual(code, 200)
        self.wait(lambda: finance_report._reports)
        self.assertEqual(self.calls[0]["argv"], self.base_argv())
        self.assertEqual(finance_report._reports["full"]["report"], "daily")


class TestDraft(ReportTest):
    """The draft key puts the cached text into the hi@ Drafts folder
    through draft_mail.py, addressed to the preset addresses, and marks the
    outcome on the part."""

    def test_drafts_the_cached_text_to_the_recipients(self):
        self.build("full", "2026-08")
        self.result = Done()
        code, out = finance_report.draft("full")
        self.assertEqual((code, out), (200, {"started": True}))
        self.wait(lambda: finance_report._drafting_since is None)
        call = self.calls[-1]
        self.assertEqual(call["argv"],
                         [finance_report.JMAP_PYTHON,
                          str(finance_report.DRAFT_MAIL),
                          "Finance — 2026-08",
                          "a@example.com", "b@example.com"])
        self.assertEqual(call["input"], "the report\n")
        self.assertIn("drafted_at", finance_report._reports["full"])
        self.assertNotIn("draft_error", finance_report._reports["full"])

    def test_component_subject_names_the_part(self):
        self.build("cashflow")
        self.result = Done()
        finance_report.draft("cashflow")
        self.wait(lambda: finance_report._drafting_since is None)
        self.assertEqual(self.calls[-1]["argv"][2],
                         f"Finance cashflow — {date.today().isoformat()}")

    def test_weekly_subject_names_the_kind(self):
        self.build("full", "weekly")
        self.result = Done()
        finance_report.draft("full")
        self.wait(lambda: finance_report._drafting_since is None)
        self.assertEqual(self.calls[-1]["argv"][2],
                         f"Finance weekly — {date.today().isoformat()}")

    def test_failure_lands_on_the_part(self):
        self.build()
        self.result = Done(returncode=1, stderr="REJECTED: not an email "
                                                "address: junk")
        finance_report.draft()
        self.wait(lambda: finance_report._drafting_since is None)
        rep = finance_report._reports["full"]
        self.assertIn("not an email address", rep["draft_error"])
        self.assertNotIn("drafted_at", rep)
        # a later good draft clears the error
        self.result = Done()
        finance_report.draft()
        self.wait(lambda: finance_report._drafting_since is None)
        self.assertNotIn("draft_error", finance_report._reports["full"])
        self.assertIn("drafted_at", finance_report._reports["full"])

    def test_nothing_built_is_400(self):
        code, out = finance_report.draft("full")
        self.assertEqual(code, 400)
        self.assertIn("build the part first", out["error"])
        self.result = Done(returncode=1, stderr="boom")
        self.build()
        code, _ = finance_report.draft("full")
        self.assertEqual(code, 400)

    def test_broken_env_file_is_400(self):
        self.build()

        def broken():
            raise RuntimeError("FINANCE_MODEL missing in finance.env")
        finance_report.recipients = broken
        code, out = finance_report.draft()
        self.assertEqual(code, 400)
        self.assertIn("FINANCE_MODEL", out["error"])

    def test_no_recipients_is_400(self):
        self.build()
        finance_report.recipients = lambda: []
        code, out = finance_report.draft()
        self.assertEqual(code, 400)
        self.assertIn("REPORT_MAIL_TO", out["error"])

    def test_refused_while_building_or_drafting(self):
        self.build()
        finance_report._building_since = finance_report._now()
        self.assertEqual(finance_report.draft()[0], 409)
        finance_report._building_since = None
        finance_report._drafting_since = finance_report._now()
        self.assertEqual(finance_report.draft()[0], 409)
        self.assertEqual(len(self.calls), 1)   # the build only


class TestSave(ReportTest):

    def test_writes_the_cached_text_under_the_label(self):
        finance_report.REPORT_DIR.mkdir()
        self.build("full", "2026-08")
        code, out = finance_report.save("full")
        self.assertEqual(code, 200)
        path = pathlib.Path(out["path"])
        self.assertEqual(path, finance_report.REPORT_DIR / "2026-08-finance.md")
        self.assertEqual(path.read_text(), "the report\n")

    def test_file_name_carries_the_settings_and_the_part(self):
        fn = finance_report.file_name
        self.assertEqual(fn("2026-08", "2026-08", "full", False, False, False),
                         "2026-08-finance.md")
        self.assertEqual(fn("2026-08", "2026-08", "full", True, False, False),
                         "2026-08-finance-details.md")
        self.assertEqual(fn("2026-08", "2026-08", "cashflow", True, True, True),
                         "2026-08-finance-details-categories-personal"
                         "-cashflow.md")
        self.assertEqual(fn("2026-09-01", "daily", "outliers", False, True,
                            False),
                         "2026-09-01-finance-categories-outliers.md")
        self.assertEqual(fn("2026-09-07", "weekly", "full", True, True, False),
                         "2026-09-07-finance-weekly-details-categories.md")
        finance_report.REPORT_DIR.mkdir()
        self.build("cashflow", "daily", True, False)
        _, out = finance_report.save("cashflow")
        self.assertEqual(pathlib.Path(out["path"]).name,
                         f"{date.today().isoformat()}-finance-details-cashflow.md")

    def test_missing_folder_is_made(self):
        self.build()
        code, out = finance_report.save()
        self.assertEqual(code, 200)
        self.assertTrue(pathlib.Path(out["path"]).is_file())

    def test_nothing_built_is_400(self):
        finance_report.REPORT_DIR.mkdir()
        code, _ = finance_report.save()
        self.assertEqual(code, 400)

    def test_saved_files_read_the_settings_back(self):
        d = finance_report.REPORT_DIR
        d.mkdir()
        for name in ("2026-08-finance.md", "2026-08-finance-details-categories.md",
                     "2026-08-finance-categories-personal.md",
                     "2026-09-01-finance-categories-cashflow.md",
                     "2026-09-07-finance-weekly-details.md",
                     "notes.md", "2026-08-finance-junk.md"):
            (d / name).write_text("x")
        files = finance_report.saved_files()
        self.assertEqual([f["name"] for f in files],
                         ["2026-08-finance-categories-personal.md",
                          "2026-08-finance-details-categories.md",
                          "2026-08-finance.md",
                          "2026-09-01-finance-categories-cashflow.md",
                          "2026-09-07-finance-weekly-details.md"])
        by_name = {f["name"]: f for f in files}
        self.assertEqual(by_name["2026-08-finance.md"],
                         {"name": "2026-08-finance.md", "report": "2026-08",
                          "detailed": False, "categories": False,
                          "combine_personal": False, "section": "full",
                          "saved_at": by_name["2026-08-finance.md"]["saved_at"]})
        f = by_name["2026-08-finance-details-categories.md"]
        self.assertEqual((f["detailed"], f["categories"], f["combine_personal"],
                          f["section"]),
                         (True, True, False, "full"))
        f = by_name["2026-08-finance-categories-personal.md"]
        self.assertEqual((f["detailed"], f["categories"], f["combine_personal"],
                          f["section"]),
                         (False, True, True, "full"))
        f = by_name["2026-09-01-finance-categories-cashflow.md"]
        self.assertEqual((f["report"], f["detailed"], f["categories"],
                          f["combine_personal"], f["section"]),
                         ("daily", False, True, False, "cashflow"))
        self.assertTrue(f["saved_at"].endswith("+00:00"))
        f = by_name["2026-09-07-finance-weekly-details.md"]
        self.assertEqual((f["report"], f["detailed"], f["section"]),
                         ("weekly", True, "full"))

    def test_saved_files_empty_without_the_folder(self):
        self.assertEqual(finance_report.saved_files(), [])
        self.assertEqual(finance_report.state()["saved"], [])


class TestRecipients(unittest.TestCase):

    def test_comma_list_from_the_env_file(self):
        saved = finance_report.env_config
        self.addCleanup(setattr, finance_report, "env_config", saved)
        finance_report.env_config = lambda: {"REPORT_MAIL_TO":
                                             " a@example.org, b@y.com ,"}
        self.assertEqual(finance_report.recipients(), ["a@example.org", "b@y.com"])
        finance_report.env_config = lambda: {}
        self.assertEqual(finance_report.recipients(), [])


class TestStateView(ReportTest):

    def test_empty_before_the_first_build(self):
        never = {"last_run_at": None, "last_failed": False,
                 "next_run_at": None}
        self.assertEqual(finance_report.state(),
                         {"building_since": None, "building_section": None,
                          "drafting_since": None, "drafting_section": None,
                          "reports": {},
                          "first_month": finance_report.cash_flow.FIRST_MONTH,
                          "saved": [],
                          "jobs": {"daily": never, "weekly": never}})

    def test_email_stamps_come_from_the_two_report_jobs(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "finance-daily",
             "last_run_at": "2026-08-25T07:00:14-07:00",
             "last_status": "ok",
             "next_run_at": "2026-08-26T07:00:00-07:00"},
            {"name": "finance-weekly",
             "last_run_at": "2026-08-24T07:00:09-07:00",
             "last_status": "ok",
             "next_run_at": "2026-08-31T07:00:00-07:00"}]}))
        jobs = finance_report.state()["jobs"]
        self.assertEqual(jobs["daily"],
                         {"last_run_at": "2026-08-25T07:00:14-07:00",
                          "last_failed": False,
                          "next_run_at": "2026-08-26T07:00:00-07:00"})
        self.assertEqual(jobs["weekly"]["next_run_at"],
                         "2026-08-31T07:00:00-07:00")

    def test_a_failed_email_run_sets_the_flag(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "finance-daily",
             "last_run_at": "2026-08-25T07:00:14-07:00",
             "last_status": "failed",
             "next_run_at": "2026-08-26T07:00:00-07:00"}]}))
        jobs = finance_report.state()["jobs"]
        self.assertTrue(jobs["daily"]["last_failed"])
        # the weekly job is not in the file: never run, not failed
        self.assertEqual(jobs["weekly"], {"last_run_at": None,
                                          "last_failed": False,
                                          "next_run_at": None})

    def test_carries_the_caches_and_the_stamps(self):
        finance_report._building_since = "2026-08-18T09:00:00Z"
        finance_report._building_section = "outliers"
        state = finance_report.state()
        self.assertEqual(state["building_since"], "2026-08-18T09:00:00Z")
        self.assertEqual(state["building_section"], "outliers")
        finance_report._building_since = None
        finance_report._building_section = None
        self.build()
        self.build("links")
        state = finance_report.state()
        self.assertIsNone(state["building_since"])
        self.assertIsNone(state["building_section"])
        self.assertEqual(sorted(state["reports"]), ["full", "links"])
        self.assertEqual(state["reports"]["full"]["text"], "the report\n")
        self.assertIn("built_at", state["reports"]["full"])


if __name__ == "__main__":
    unittest.main()
