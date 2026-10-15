#!/usr/bin/env python3
"""Tests for services/ops/finance_reports.py — stdlib unittest, no live data.

The builds, the draft process and the threads are stubbed, so nothing
reads the budget and no draft is made. Run with
~/.venvs/mail_tools/bin/python services/ops/test_finance_reports.py
(family_emails, which it imports, reads the host app through httpx).
"""

import os
import pathlib
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import family_emails
import finance_reports as reports

DAILY = {"sections": ["Summary", "New transactions"], "detailed": False, "combine": False,
         "show_new_payees": True}
WEEKLY = {"sections": ["Cash flow"], "detailed": True, "combine": True, "show_new_payees": False}


class ReportsTest(unittest.TestCase):
    def setUp(self):
        reports._building = reports._drafting = None
        reports._builds = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ASSISTANT_FINANCE_NOTES": self.tmp.name,
                                           "ASSISTANT_LLM_PAUSE": os.path.join(self.tmp.name, "pause.json")})
        env.start()
        self.addCleanup(env.stop)
        self.old = family_emails.DATA
        self.addCleanup(setattr, family_emails, "DATA", self.old)
        family_emails.DATA = family_emails.empty()
        family_emails.DATA["addresses"]["Sam"] = "sam@example.com"
        self.threads = mock.patch.object(reports.threading, "Thread").start()
        self.addCleanup(mock.patch.stopall)

    def built(self, kind="daily", part="email", data="now", spec=DAILY, **kw):
        """A row's last build, as _build leaves it."""
        rep = {"built_at": "2026-09-14T17:00:00Z", "data": data, "label": "2026-09-14",
               "settings": dict(spec), "subject": "Finance daily — 2026-09-14", "text": "the report\n", **kw}
        reports._builds[f"{kind}/{part}"] = rep
        return rep

    # ---- build

    def test_build_refuses_a_bad_kind_part_or_data(self):
        for body in ({"kind": "yearly", "part": "email", "data": "now", "settings": DAILY},
                     {"kind": "daily", "part": "Cash flow", "data": "now", "settings": DAILY},
                     {"kind": "daily", "part": "email", "data": "2026-9-1", "settings": DAILY},
                     {"kind": "daily", "part": "email", "data": "2999-01-01", "settings": DAILY},
                     {"kind": "monthly", "part": "email", "data": "2026-09-01", "settings": DAILY},
                     {"kind": "monthly", "part": "email", "data": "2000-01",
                      "settings": {"sections": ["Summary"]}},
                     {"kind": "daily", "part": "email", "data": "now", "settings": {"sections": []}}):
            self.assertEqual(reports.build(body)[0], 400, body)
        self.threads.assert_not_called()

    def test_build_maps_the_data_to_the_request(self):
        today = date.today()
        req, label = reports.request("daily", "email", "now", DAILY)
        self.assertEqual((req["day"], req["sample"], req["full_window"], label),
                         (today.isoformat(), False, True, today.isoformat()))
        self.assertNotIn("part", req)
        req, label = reports.request("weekly", "Cash flow", "2026-09-07", WEEKLY)
        self.assertEqual((req["day"], req["part"], label), ("2026-09-07", "Cash flow", "2026-09-07"))
        req, label = reports.request("daily", "Links", "demo", DAILY)
        self.assertEqual((req["sample"], req["full_window"], req["part"]), (True, False, "Links"))
        req, label = reports.request("monthly", "email", "now", DAILY)
        self.assertEqual((req["month"], label), (today.strftime("%Y-%m"),) * 2)
        req, _ = reports.request("monthly", "email", "demo", DAILY)
        self.assertEqual(req["month"], family_emails.last_month(today))
        req, _ = reports.request("monthly", "email", reports.cash_flow.FIRST_MONTH, DAILY)
        self.assertEqual(req["month"], reports.cash_flow.FIRST_MONTH)

    def test_one_build_or_draft_at_a_time(self):
        body = {"kind": "weekly", "part": "email", "data": "now", "settings": WEEKLY}
        self.assertEqual(reports.build(body), (200, {"started": True}))
        key, req, label, data, spec = self.threads.call_args.kwargs["args"]
        self.assertEqual((key, data, spec["sections"]), ("weekly/email", "now", ["Cash flow"]))
        self.assertEqual(reports.build(body)[0], 409)
        self.assertEqual(reports.state()["building"]["part"], "email")
        with mock.patch.object(family_emails, "finance_build",
                               return_value={"subject": "Finance weekly — 2026-09-14", "text": "body\n"}):
            reports._build(key, req, label, data, spec)
        self.assertIsNone(reports.state()["building"])
        self.assertEqual(reports.state()["builds"]["weekly/email"]["text"], "body\n")
        reports._drafting = {"kind": "weekly", "part": "email"}
        self.assertEqual(reports.build(body)[0], 409)

    def test_a_pause_refuses_real_builds_with_the_summary(self):
        family_emails.llm_pause.start(30, "other")
        whole = {"kind": "daily", "part": "email", "data": "now", "settings": DAILY}
        for body in (whole, {**whole, "part": "Summary"}):
            code, out = reports.build(body)
            self.assertEqual(code, 409)
            self.assertIn("paused until", out["error"])
        # demo data, or a build without the summary, calls no model
        self.assertEqual(reports.build({**whole, "data": "demo"})[0], 200)
        reports._building = None
        self.assertEqual(reports.build({**whole, "part": "New transactions"})[0], 200)
        reports._building = None
        self.assertEqual(reports.build({**whole, "settings": {**DAILY, "sections": ["New transactions"]}})[0], 200)

    def test_a_failed_build_keeps_its_error_and_a_part_names_itself(self):
        req, label = reports.request("daily", "Summary", "now", DAILY)
        with mock.patch.object(family_emails, "finance_build", side_effect=ValueError("budget copy is 2 days old")):
            reports._build("daily/Summary", req, label, "now", DAILY)
        self.assertEqual(reports._builds["daily/Summary"]["error"], "budget copy is 2 days old")
        with mock.patch.object(family_emails, "finance_build",
                               return_value={"subject": "Finance daily — 2026-09-14", "text": "## Summary\n"}):
            reports._build("daily/Summary", req, label, "now", DAILY)
        self.assertEqual(reports._builds["daily/Summary"]["subject"], "Finance daily — 2026-09-14 · Summary")
        self.assertNotIn("error", reports._builds["daily/Summary"])

    # ---- draft and save

    def test_draft_needs_a_real_build_and_a_saved_address(self):
        body = {"kind": "daily", "part": "email", "person": "Sam"}
        self.assertEqual(reports.draft(body)[0], 400)            # nothing built
        self.built(data="demo")
        self.assertEqual(reports.draft(body)[0], 400)            # demo data is never sent
        self.built()
        self.assertEqual(reports.draft({**body, "person": "Riley"})[0], 400)   # no address
        self.assertEqual(reports.draft(body), (200, {"started": True}))
        rep, to = self.threads.call_args.kwargs["args"]
        self.assertEqual(to, "sam@example.com")
        self.assertEqual(reports.draft(body)[0], 409)
        run = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(reports.subprocess, "run", return_value=run) as process:
            reports._draft(rep, to)
        argv = process.call_args.args[0]
        self.assertEqual(argv[1:], [str(reports.DRAFT_MAIL), "Finance daily — 2026-09-14", "sam@example.com"])
        self.assertEqual(process.call_args.kwargs["input"], "the report\n")
        self.assertEqual(rep["drafted_to"], "sam@example.com")
        self.assertIsNone(reports._drafting)
        with mock.patch.object(reports.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stderr="mail API failed")):
            reports._draft(rep, to)
        self.assertEqual(rep["draft_error"], "mail API failed")
        self.assertNotIn("drafted_at", rep)

    def test_save_writes_a_real_build_under_its_settings_name(self):
        body = {"kind": "weekly", "part": "Cash flow"}
        self.assertEqual(reports.save(body)[0], 400)
        self.built("weekly", "Cash flow", data="demo", spec=WEEKLY)
        self.assertEqual(reports.save(body)[0], 400)
        rep = self.built("weekly", "Cash flow", spec=WEEKLY)
        code, out = reports.save(body)
        self.assertEqual(code, 200)
        path = pathlib.Path(out["path"])
        self.assertEqual(path, pathlib.Path(self.tmp.name) / "reports"
                         / "2026-09-14-finance-weekly-details-personal-cashflow.md")
        self.assertEqual(path.read_text(), "the report\n")
        self.assertEqual(rep["saved_name"], path.name)
        self.built()
        self.assertTrue(reports.save({"kind": "daily", "part": "email"})[1]["path"]
                        .endswith("2026-09-14-finance-daily.md"))


if __name__ == "__main__":
    unittest.main()
