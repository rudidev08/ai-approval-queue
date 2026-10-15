#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1"]
# ///
"""Tests for reminders.py: the grouping, the column order, the ignored
lists and the due-today count, from canned server data. No host app, no
Reminders access: _call is stubbed, and no read thread ever starts."""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import reminders

LISTS = [
    {"name": "Robin DONE", "open": 0, "color": "#5b626a"},
    {"name": "Robin WAITING", "open": 1, "color": "#5b626a"},
    {"name": "Taylor", "open": 2, "color": "#ac7f5e"},
    {"name": "Taylor Shopping", "open": 0, "color": "#ac7f5e"},
    {"name": "Casey NEXT", "open": 1, "color": "#83d754"},
    {"name": "Casey READY", "open": 4, "color": "#83d754"},
    {"name": "Casey WAITING", "open": 1, "color": "#83d754"},
    {"name": "Alex", "open": 3, "color": "#ff8d28"},
    {"name": "Sam WAITING", "open": 1, "color": "#cb30e0"},
]


def rem(name, lst, due=""):
    return {"name": name, "list": lst, "due": due, "priority": "", "notes": ""}


REMINDERS = [
    rem("Call back", "Casey NEXT"),
    rem("A", "Casey READY", "2026-09-01"), rem("B", "Casey READY"),
    rem("C", "Casey READY"), rem("D", "Casey READY"),
    rem("Follow up", "Casey WAITING", "2026-09-08"),
    rem("Bins", "Taylor", "2026-09-09"), rem("Filter", "Taylor"),
    rem("Paperwork", "Robin WAITING"),
    rem("Cables", "Sam WAITING"),
    rem("Water plants", "Alex", "2026-09-08"), rem("Library books", "Alex"), rem("Bike tune-up", "Alex"),
]
TODAY = "2026-09-08"


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.s = reminders.build(LISTS, REMINDERS, TODAY)

    def test_groups_by_first_word_most_open_first_and_ignores_alex(self):
        self.assertEqual([g["label"] for g in self.s["groups"]],
                         ["Casey", "Taylor", "Robin", "Sam"])
        self.assertEqual([g["open"] for g in self.s["groups"]], [6, 2, 1, 1])
        self.assertEqual(self.s["open"], 10)

    def test_shared_stage_columns_come_first_then_workflow_order(self):
        casey = self.s["groups"][0]["lists"]
        # WAITING is shared by three groups, READY and NEXT by one each
        self.assertEqual([l["label"] for l in casey], ["waiting", "next", "ready"])

    def test_plain_lists_label_and_order(self):
        taylor = self.s["groups"][1]["lists"]
        self.assertEqual([l["label"] for l in taylor], ["", "Shopping"])

    def test_counts_come_from_the_lists_colors_and_first_three_items(self):
        # the count is get_lists' open count, not the capped search's size
        ready = self.s["groups"][0]["lists"][2]
        self.assertEqual((ready["count"], ready["color"]), (4, "#83d754"))
        self.assertEqual(reminders.build(LISTS, [], TODAY)["open"], 10)
        self.assertEqual([i["name"] for i in ready["items"]], ["A", "B", "C"])
        self.assertEqual(ready["items"][0]["due"], "2026-09-01")

    def test_columns_is_the_biggest_group(self):
        self.assertEqual(self.s["columns"], 3)

    def test_each_list_counts_its_due_today_and_overdue(self):
        casey = self.s["groups"][0]["lists"]
        # WAITING (today), NEXT (none), READY A (overdue)
        self.assertEqual([l["due"] for l in casey], [1, 0, 1])

    def test_due_today_counts_only_alexs_lists_the_ignored_one_included(self):
        # Casey READY A (overdue), Casey WAITING (today), Alex Joy (today);
        # Taylor's is tomorrow; Robin and Sam are not Alex's
        self.assertEqual(self.s["due_today"], 3)
        robin = [rem("Late", "Robin WAITING", "2026-09-01")]
        self.assertEqual(reminders.build(LISTS, REMINDERS + robin, TODAY)["due_today"], 3)


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self._saved = (reminders.STATE, reminders._NEXT_READ)
        reminders._NEXT_READ = 0.0

    def tearDown(self):
        reminders.STATE, reminders._NEXT_READ = self._saved

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

    # ---- the page's polls are the only trigger ----

    def _polls(self, n, clock):
        """n page polls at the clock's times, each read run inline."""
        reads = []
        times = iter(clock)
        with mock.patch.object(reminders.time, "monotonic", lambda: next(times)), \
             mock.patch.object(reminders.threading, "Thread",
                               lambda target, daemon: mock.Mock(start=lambda: reads.append(target))):
            for _ in range(n):
                reminders.page_poll()
        return reads

    def test_the_first_page_poll_starts_one_read(self):
        self.assertEqual(self._polls(1, [100.0, 100.0]), [reminders.refresh])

    def test_polls_inside_the_refresh_window_start_no_read(self):
        # the first poll at 100 reads; 130 and 159 are inside REFRESH_S
        self.assertEqual(len(self._polls(3, [100.0, 100.0, 130.0, 159.0])), 1)

    def test_a_poll_past_the_window_reads_again(self):
        self.assertEqual(len(self._polls(2, [100.0, 100.0, 160.0, 160.0])), 2)

    def test_boot_starts_no_read(self):
        with mock.patch.object(reminders.threading, "Thread") as thread:
            reminders.boot()
        thread.assert_not_called()

    def test_clear_empties_the_state_and_lets_the_next_poll_read(self):
        with mock.patch.object(reminders, "_call", lambda tool, args: (False, "FAILED: host down")):
            reminders.refresh()
        self._polls(1, [100.0, 100.0])   # the window now runs to 160
        reminders.clear()
        self.assertEqual(reminders.state(), reminders._empty(None))
        self.assertEqual(len(self._polls(1, [120.0, 120.0])), 1)


if __name__ == "__main__":
    unittest.main()
