#!/usr/bin/env python3
"""Tests for oddities.py — stdlib unittest, in-memory SQLite (v_transactions
and v_payees are views in Actual; same-shaped tables read identically).
Run: python3 -m pytest test_oddities.py -q (from this directory), or
python3 services/mcp/actual/test_oddities.py from the repo root.
"""

import pathlib
import sqlite3
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import oddities as od

TODAY = date(2026, 8, 11)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE v_transactions (id TEXT, date INT, amount INT, "
        "payee TEXT, notes TEXT, category TEXT, transfer_id TEXT, "
        "is_parent INT DEFAULT 0, starting_balance_flag INT DEFAULT 0, "
        "sort_order INT DEFAULT 0, account TEXT DEFAULT 'a1');"
        "CREATE TABLE accounts (id TEXT, name TEXT, offbudget INT DEFAULT 0, "
        "tombstone INT DEFAULT 0, closed INT DEFAULT 0, "
        "sort_order REAL DEFAULT 0);"
        "CREATE TABLE v_payees (id TEXT, name TEXT);"
        "CREATE TABLE category_groups (id TEXT, name TEXT, "
        "tombstone INT DEFAULT 0, hidden INT DEFAULT 0, "
        "sort_order REAL DEFAULT 0);"
        "CREATE TABLE categories (id TEXT, name TEXT, cat_group TEXT, "
        "tombstone INT DEFAULT 0, hidden INT DEFAULT 0);")
    conn.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Checking')")
    return conn


def spend(conn, tid, day, cents, payee=None, category=None):
    """One charge; day is a date, cents positive (stored negative)."""
    conn.execute(
        "INSERT INTO v_transactions (id, date, amount, payee, category) "
        "VALUES (?, ?, ?, ?, ?)",
        (tid, od.cash_flow._day_int(day), -cents, payee, category))


def payee(conn, pid, name):
    conn.execute("INSERT INTO v_payees VALUES (?, ?)", (pid, name))


class TestOddFlags(unittest.TestCase):

    def setUp(self):
        self.conn = make_conn()

    def odd(self, kind=None, ids=()):
        out = od.odd_candidates(self.conn, TODAY, ids=ids)
        return [o for o in out if kind is None or o["kind"] == kind]

    def test_large_absolute_threshold(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 50000)
        spend(self.conn, "t2", TODAY - timedelta(days=1), 49999)
        hits = self.odd("large")
        self.assertEqual(len(hits), 1)
        self.assertIn("$500.00", hits[0]["text"])
        self.assertEqual(hits[0]["reason"], "unusually large charge")
        self.assertEqual(hits[0]["transaction_id"], "t1")
        self.assertEqual(hits[0]["account"], "Checking")

    def test_large_ratio_needs_history_and_floor(self):
        payee(self.conn, "p1", "Gym")
        old = TODAY - timedelta(days=60)
        for i in range(3):   # median $30
            spend(self.conn, f"h{i}", old, 3000, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 10000, payee="p1")
        hits = self.odd("large")
        self.assertEqual(len(hits), 1)
        self.assertIn("median $30.00", hits[0]["text"])
        self.assertIn("median $30.00", hits[0]["reason"])

    def test_large_ratio_threshold_direction(self):
        # median $100 (3 priors of $100) -> threshold LARGE_RATIO * median
        # = $300 exactly. A charge just under must NOT flag (rules out the
        # threshold reading as LARGE_RATIO / median, which is ~$0 and would
        # flag anything); one at the threshold, and one just over, must.
        payee(self.conn, "p1", "At")
        payee(self.conn, "p2", "Over")
        payee(self.conn, "p3", "Under")
        old = TODAY - timedelta(days=60)
        for i in range(3):
            spend(self.conn, f"a{i}", old, 10000, payee="p1")
            spend(self.conn, f"o{i}", old, 10000, payee="p2")
            spend(self.conn, f"u{i}", old, 10000, payee="p3")
        spend(self.conn, "at", TODAY - timedelta(days=1), 30000, payee="p1")
        spend(self.conn, "over", TODAY - timedelta(days=1), 30001, payee="p2")
        spend(self.conn, "under", TODAY - timedelta(days=1), 29999, payee="p3")
        texts = " ".join(h["text"] for h in self.odd("large"))
        self.assertIn("At $300.00", texts)
        self.assertIn("Over $300.01", texts)
        self.assertNotIn("Under", texts)

    def test_ratio_skipped_under_100_dollars(self):
        payee(self.conn, "p1", "Gym")
        old = TODAY - timedelta(days=60)
        for i in range(3):
            spend(self.conn, f"h{i}", old, 3000, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 9999, payee="p1")
        self.assertEqual(self.odd("large"), [])

    def test_ratio_skipped_with_short_history(self):
        payee(self.conn, "p1", "Gym")
        old = TODAY - timedelta(days=60)
        for i in range(2):   # only 2 priors
            spend(self.conn, f"h{i}", old, 3000, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 10000, payee="p1")
        self.assertEqual(self.odd("large"), [])

    def test_duplicate_within_three_days_once(self):
        payee(self.conn, "p1", "Store")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=3), 2500, payee="p1")
        hits = self.odd("duplicate")
        self.assertEqual(len(hits), 1)   # the pair reported once, not twice
        self.assertEqual(hits[0]["transaction_id"], "t1")   # on the newest
        self.assertIn("2x within 3 days", hits[0]["text"])
        self.assertEqual(hits[0]["reason"],
                         "possible duplicate charge: appears 2x within 3 days")

    def test_no_duplicate_when_far_apart(self):
        payee(self.conn, "p1", "Store")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=6), 2500, payee="p1")
        self.assertEqual(self.odd("duplicate"), [])

    def test_new_payee_flagged_once(self):
        payee(self.conn, "p1", "Fresh Shop")
        spend(self.conn, "t1", TODAY - timedelta(days=2), 1000, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=1), 1200, payee="p1")
        hits = self.odd("new_payee")
        self.assertEqual(len(hits), 2)   # two charges, but one line per text
        texts = {h["text"] for h in hits}
        self.assertEqual(len([t for t in texts if "new payee:" in t]), 2)

    def test_known_payee_not_new(self):
        payee(self.conn, "p1", "Old Shop")
        spend(self.conn, "h1", TODAY - timedelta(days=30), 900, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 1000, payee="p1")
        self.assertEqual(self.odd("new_payee"), [])

    def test_window_excludes_old_charges(self):
        spend(self.conn, "t1", TODAY - timedelta(days=8), 60000)
        self.assertEqual(self.odd(), [])

    def test_ids_add_charges_outside_the_window(self):
        spend(self.conn, "t1", TODAY - timedelta(days=8), 60000)
        hits = self.odd(ids=["t1", "no-such-id"])
        self.assertEqual([h["transaction_id"] for h in hits], ["t1"])

    def test_ids_never_double_a_window_charge(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 60000)
        self.assertEqual(len(self.odd(ids=["t1"])), 1)

    def test_new_payee_reads_the_charge_date_not_today(self):
        # queued 10 days ago as a new payee; still new when re-read by id
        payee(self.conn, "p1", "Fresh Shop")
        spend(self.conn, "t1", TODAY - timedelta(days=10), 1000, payee="p1")
        self.assertEqual(self.odd(), [])
        hits = self.odd("new_payee", ids=["t1"])
        self.assertEqual([h["transaction_id"] for h in hits], ["t1"])

    def test_all_candidates_kept_larges_first(self):
        for i in range(12):
            spend(self.conn, f"t{i}", TODAY - timedelta(days=1), 50000 + i)
        payee(self.conn, "p1", "Store")
        spend(self.conn, "d1", TODAY - timedelta(days=1), 2500, payee="p1")
        spend(self.conn, "d2", TODAY - timedelta(days=2), 2500, payee="p1")
        out = od.odd_candidates(self.conn, TODAY)
        self.assertEqual(len(out), 15)
        self.assertTrue(all(o["kind"] == "large" for o in out[:12]))
        self.assertEqual(out[12]["kind"], "duplicate")
        self.assertTrue(all(o["kind"] == "new_payee" for o in out[13:]))


class TestOffSchedule(unittest.TestCase):
    """A monthly payee — two prior charges on the same day of the month —
    charged too soon after its last charge, or off its usual day."""

    def setUp(self):
        self.conn = make_conn()
        payee(self.conn, "p1", "Gym")
        spend(self.conn, "h1", date(2026, 6, 18), 5900, payee="p1")
        spend(self.conn, "h2", date(2026, 7, 18), 9900, payee="p1")

    def off(self):
        return [o for o in od.odd_candidates(self.conn, TODAY)
                if o["kind"] == "off_schedule"]

    def test_too_soon(self):
        spend(self.conn, "t1", date(2026, 8, 1), 4620, payee="p1")
        hits = [o for o in od.odd_candidates(self.conn, date(2026, 8, 3))
                if o["kind"] == "off_schedule"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["reason"],
                         "too soon: 14 days after the last charge "
                         "(monthly, usually the 18th)")
        self.assertEqual(hits[0]["text"],
                         "too soon: Gym $46.20 on 2026-08-01, 14 days after "
                         "the last charge (monthly, usually the 18th)")

    def test_bunched_history_is_not_monthly(self):
        # two charges days apart say nothing about a day of the month,
        # whether in one month or across a month end
        spend(self.conn, "b0", date(2026, 7, 31), 2000, payee="p9")
        spend(self.conn, "b1", date(2026, 8, 2), 3500, payee="p9")
        spend(self.conn, "b2", date(2026, 8, 9), 3000, payee="p9")
        hits = [o for o in od.odd_candidates(self.conn, date(2026, 8, 10))
                if o["kind"] == "off_schedule" and o["transaction_id"] == "b2"]
        self.assertEqual(hits, [])

    def test_off_its_usual_day(self):
        spend(self.conn, "t1", date(2026, 8, 10), 9900, payee="p1")   # 23 days: too soon
        spend(self.conn, "h0", date(2026, 5, 18), 5900, payee="p1")
        self.conn.execute("DELETE FROM v_transactions WHERE id = 'h2'")
        # priors 5/18 and 6/18; 8/10 is 53 days after the last, day 10 vs 18
        hits = self.off()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["reason"],
                         "off its usual day (monthly, usually the 18th)")

    def test_on_its_day_is_fine(self):
        for day in (16, 18, 20, 21):
            self.conn.execute("DELETE FROM v_transactions WHERE id = 't1'")
            spend(self.conn, "t1", date(2026, 8, day), 9900, payee="p1")
            self.assertEqual(self.off(), [], day)

    def test_one_prior_is_not_a_rhythm(self):
        self.conn.execute("DELETE FROM v_transactions WHERE id = 'h1'")
        spend(self.conn, "t1", date(2026, 8, 5), 9900, payee="p1")
        self.assertEqual(self.off(), [])

    def test_scattered_priors_are_not_a_rhythm(self):
        spend(self.conn, "h3", date(2026, 7, 3), 9900, payee="p1")
        spend(self.conn, "t1", date(2026, 8, 5), 9900, payee="p1")
        self.assertEqual(self.off(), [])

    def test_month_end_wraps(self):
        self.conn.execute("DELETE FROM v_transactions WHERE id IN ('h1', 'h2')")
        spend(self.conn, "h1", date(2026, 5, 31), 900, payee="p1")
        spend(self.conn, "h2", date(2026, 7, 1), 900, payee="p1")
        spend(self.conn, "t1", date(2026, 8, 2), 900, payee="p1")
        self.assertEqual(self.off(), [])

    def test_skipped_when_a_duplicate(self):
        spend(self.conn, "t1", date(2026, 8, 9), 9900, payee="p1")
        spend(self.conn, "t2", date(2026, 8, 10), 9900, payee="p1")
        out = od.odd_candidates(self.conn, TODAY)
        self.assertEqual([o["kind"] for o in out], ["duplicate"])


class TestExcludedGroups(unittest.TestCase):
    """Rows parked in cash_flow.EXCLUDED_GROUPS or ONE_OFF_GROUPS leave the
    checks; deleting or moving the category or group brings them back."""

    def setUp(self):
        self.addCleanup(setattr, od.cash_flow, "EXCLUDED_GROUPS",
                        od.cash_flow.EXCLUDED_GROUPS)
        self.addCleanup(setattr, od.cash_flow, "ONE_OFF_GROUPS",
                        od.cash_flow.ONE_OFF_GROUPS)
        od.cash_flow.EXCLUDED_GROUPS = ["Ignored"]
        od.cash_flow.ONE_OFF_GROUPS = ["One-off"]
        self.conn = make_conn()
        self.conn.executescript(
            "INSERT INTO category_groups (id, name) VALUES ('g1', 'Food');"
            "INSERT INTO category_groups (id, name) VALUES ('g2', 'Ignored');"
            "INSERT INTO category_groups (id, name) VALUES ('g3', 'One-off');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c1', 'Groceries', 'g1');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c2', 'Transfers', 'g2');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c3', 'Computer', 'g3');")

    def big(self, category):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category=category)

    def test_parked_charge_is_not_odd(self):
        self.big("c2")
        self.assertEqual(od.odd_candidates(self.conn, TODAY), [])

    def test_one_off_charge_is_not_odd(self):
        self.big("c3")
        self.assertEqual(od.odd_candidates(self.conn, TODAY), [])

    def test_same_charge_unparked_is_odd(self):
        self.big("c1")
        self.assertEqual(len(od.odd_candidates(self.conn, TODAY)), 1)

    def test_parked_queued_charge_is_dropped_too(self):
        self.big("c2")
        self.assertEqual(od.odd_candidates(self.conn, TODAY, ids=["t1"]), [])

    def test_empty_constant_excludes_nothing(self):
        od.cash_flow.EXCLUDED_GROUPS = []
        self.big("c2")
        self.assertEqual(len(od.odd_candidates(self.conn, TODAY)), 1)

    def test_deleting_the_category_counts_its_rows_again(self):
        self.big("c2")
        self.conn.execute("UPDATE categories SET tombstone = 1 WHERE id = 'c2'")
        self.assertEqual(len(od.odd_candidates(self.conn, TODAY)), 1)

    def test_deleting_the_group_counts_its_rows_again(self):
        self.big("c2")
        self.conn.execute("UPDATE category_groups SET tombstone = 1 "
                          "WHERE id = 'g2'")
        self.assertEqual(len(od.odd_candidates(self.conn, TODAY)), 1)

    def test_moving_the_category_out_counts_its_rows_again(self):
        self.big("c2")
        self.conn.execute("UPDATE categories SET cat_group = 'g1' "
                          "WHERE id = 'c2'")
        self.assertEqual(len(od.odd_candidates(self.conn, TODAY)), 1)


if __name__ == "__main__":
    unittest.main()
