#!/usr/bin/env python3
"""Tests for reminders.py: the grouping, the column order, the ignored
lists and the due-today count, from canned server data. No host app, no
Reminders access: _call is stubbed, and nothing boots the refresh thread.
Run: python3 -m pytest test_reminders.py -q (from this directory), or
python3 services/actions/test_reminders.py from the repo root.
"""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import reminders

LISTS = [
    {"name": "Alex NEXT", "open": 1, "color": "#83d754"},
    {"name": "Alex READY", "open": 4, "color": "#83d754"},
    {"name": "Alex WAITING", "open": 1, "color": "#83d754"},
    {"name": "Later", "open": 2, "color": "#ff8d28"},
    {"name": "Next", "open": 3, "color": "#ff8d28"},
    {"name": "Riley", "open": 2, "color": "#ac7f5e"},
    {"name": "Riley Shopping", "open": 0, "color": "#ac7f5e"},
    {"name": "Sam DONE", "open": 0, "color": "#5b626a"},
    {"name": "Sam WAITING", "open": 1, "color": "#5b626a"},
]


def rem(name, lst, due=""):
    return {"name": name, "list": lst, "due": due, "priority": "", "notes": ""}


REMINDERS = [
    rem("Call the school office", "Alex NEXT"),
    rem("A", "Alex READY", "2026-09-01"), rem("B", "Alex READY"),
    rem("C", "Alex READY"), rem("D", "Alex READY"),
    rem("Hear back from the plumber", "Alex WAITING", "2026-09-08"),
    rem("Take the bins out", "Riley", "2026-09-09"), rem("Change the water filter", "Riley"),
    rem("Pick up the parcel", "Sam WAITING"),
    rem("Pay the gas bill", "Next", "2026-09-08"), rem("Fix the gate", "Next"),
    rem("Book the dentist", "Next"), rem("Read the manual", "Later"), rem("Tidy the shed", "Later"),
]
TODAY = "2026-09-08"


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.s = reminders.build(LISTS, REMINDERS, TODAY)

    def test_groups_by_first_word_most_open_first_and_ignores_own_lists(self):
        self.assertEqual([g["label"] for g in self.s["groups"]], ["Alex", "Riley", "Sam"])
        self.assertEqual([g["open"] for g in self.s["groups"]], [6, 2, 1])
        self.assertEqual(self.s["open"], 9)

    def test_shared_stage_columns_come_first_then_workflow_order(self):
        alex = self.s["groups"][0]["lists"]
        # WAITING is shared by two groups, NEXT and READY by one each
        self.assertEqual([l["label"] for l in alex], ["waiting", "next", "ready"])

    def test_plain_lists_label_and_order(self):
        riley = self.s["groups"][1]["lists"]
        self.assertEqual([l["label"] for l in riley], ["", "Shopping"])

    def test_counts_come_from_the_lists_colors_and_first_three_items(self):
        # the count is get_lists' open count, not the capped search's size
        ready = self.s["groups"][0]["lists"][2]
        self.assertEqual((ready["count"], ready["color"]), (4, "#83d754"))
        self.assertEqual(reminders.build(LISTS, [], TODAY)["open"], 9)
        self.assertEqual([i["name"] for i in ready["items"]], ["A", "B", "C"])
        self.assertEqual(ready["items"][0]["due"], "2026-09-01")

    def test_columns_is_the_biggest_group(self):
        self.assertEqual(self.s["columns"], 3)

    def test_due_today_counts_today_and_overdue_in_shown_lists(self):
        # Alex READY A (overdue), Alex WAITING (today); Next's is ignored,
        # Riley's is tomorrow
        self.assertEqual(self.s["due_today"], 2)


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self._saved = reminders.STATE

    def tearDown(self):
        reminders.STATE = self._saved

    def test_before_the_first_read_the_state_is_empty_with_no_error(self):
        s = reminders.state()
        self.assertEqual((s["error"], s["groups"], s["columns"], s["open"], s["due_today"]),
                         (None, [], 1, 0, 0))

    def test_a_failed_call_reads_as_an_error_with_empty_groups(self):
        with mock.patch.object(reminders, "_call", lambda tool, args: (False, "FAILED: host down")):
            reminders.refresh()
        s = reminders.state()
        self.assertEqual((s["error"], s["groups"], s["due_today"]), ("FAILED: host down", [], 0))

    def test_a_malformed_answer_reads_as_an_error(self):
        with mock.patch.object(reminders, "_call", lambda tool, args: (True, "{}")):
            reminders.refresh()
        self.assertIn("answered badly", reminders.state()["error"])

    def test_one_refresh_is_the_lists_then_the_search(self):
        calls = []
        def stub(tool, args):
            calls.append(tool)
            return (True, '{"default": "x", "lists": []}' if tool == "get_lists"
                    else '{"total": 0, "reminders": []}')
        with mock.patch.object(reminders, "_call", stub):
            reminders.refresh()
        self.assertEqual(calls, ["get_lists", "search_reminders"])
        self.assertIsNone(reminders.state()["error"])


if __name__ == "__main__":
    unittest.main()
