"""Finance email builds, with budget reads and model calls stubbed."""
import copy
import unittest
from datetime import date
from unittest import mock

import finance_email as content


class ContentTest(unittest.TestCase):
    def setUp(self):
        self.request = {"kind": "daily", "day": "2026-09-14", "reported": {},
                        "settings": {"sections": ["New transactions"], "detailed": False,
                                     "combine": False, "show_new_payees": True}}
        self.data = {"new": [{"id": "old", "date": 20260913}, {"id": "fresh", "date": 20260914}], "odd": [],
                     "check": [], "cash_flow": "Cash flow"}
        self.db = mock.patch.object(content.finance, "db").start()
        self.read = mock.patch.object(content.finance, "read_all", return_value=self.data).start()
        self.llm = mock.patch.object(content.finance, "llm_report_sentences", return_value={"summary": []}).start()
        mock.patch.object(content.finance, "learned_notes", return_value="").start()
        self.body = mock.patch.object(content.finance, "email_body", return_value="Selected content").start()
        self.addCleanup(mock.patch.stopall)

    def test_preview_omits_summary_and_filters_reported_transactions(self):
        self.request["reported"] = {"old": "2026-09-13"}
        result = content.build(self.request)
        section, data = self.body.call_args.args[:2]
        self.assertEqual(section, "new")
        self.assertEqual([r["id"] for r in data["new"]], ["fresh"])
        self.assertEqual(result["reported"], {"old": "2026-09-13", "fresh": "2026-09-14"})
        self.llm.assert_not_called()
        self.db.return_value.close.assert_called_once()

    def test_monthly_report_reads_the_month_it_is_given(self):
        self.request.update(kind="monthly", day="2026-01-01", month="2025-12")
        self.request["settings"]["sections"] = ["Cash flow"]
        result = content.build(self.request)
        self.assertEqual(result["subject"], "Finance monthly — Dec 2025")
        self.assertEqual(self.read.call_args.args[2], "2025-12")
        self.assertEqual(result["reported"], {})

    def test_summary_and_budget_read_run_once_for_multiple_sections(self):
        self.request["settings"]["sections"] = ["Summary", "New transactions"]
        content.build(self.request)
        self.read.assert_called_once()
        self.llm.assert_called_once()

    def test_fresh_rows_are_marked_reported_whichever_sections_print(self):
        # the summary comments on them too, so a daily without the
        # transaction section must not see the same rows for a week
        self.request["settings"]["sections"] = ["Summary"]
        self.request["reported"] = {"old": "2026-09-13"}
        before = copy.deepcopy(self.request)
        result = content.build(self.request)
        self.assertEqual(result["reported"], {"old": "2026-09-13", "fresh": "2026-09-14"})
        self.assertEqual(self.request, before)

    def test_sample_follows_show_new_payees(self):
        day = date(2026, 9, 14)
        on = content.sample_data(day, "daily", True)
        self.assertIn("new_payee", {o["kind"] for o in on["odd"]})
        off = content.sample_data(day, "daily", False)
        self.assertNotIn("new_payee", {o["kind"] for o in off["odd"]})
        # the charge itself still lists, without its flag
        self.assertEqual([r["mark"] for r in off["new"] if r["payee"] == "Hillside Bakery"], [""])

    def test_sample_reads_nothing_and_fills_every_section(self):
        self.request["settings"]["sections"] = ["Summary", "Items to check", "Unusual charges", "New transactions"]
        self.request.update(reported={"old": "2026-09-13"}, sample=True)
        result = content.build(self.request)
        self.db.assert_not_called()
        self.read.assert_not_called()
        self.llm.assert_not_called()
        self.assertEqual(result["subject"], "Finance daily — 2026-09-14 (sample)")
        self.assertEqual(result["reported"], {"old": "2026-09-13"})
        _, data, summary = self.body.call_args.args[:3]
        self.assertTrue(data["new"] and data["odd"] and summary and data["cash_flow"] and data["check"])
        self.assertEqual(data["new"][0]["date"], 20260913)

    def test_full_window_reads_every_row_and_records_nothing(self):
        # a finance reports build: no seen list, no printed ids, and the
        # note under the daily's list headings
        self.request.update(reported={"old": "2026-09-13"}, full_window=True)
        result = content.build(self.request)
        self.assertTrue(self.read.call_args.kwargs["full_window"])
        section, data, _, _, full_window = self.body.call_args.args
        self.assertEqual([r["id"] for r in data["new"]], ["old", "fresh"])
        self.assertTrue(full_window)
        self.assertEqual(result["reported"], {})

    def test_part_builds_alone_with_no_title(self):
        self.request.update(part="Links")
        result = content.build(self.request)
        self.assertEqual(self.body.call_args.args[0], "links")
        self.assertEqual(result["text"], "Selected content\n")
        self.llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
