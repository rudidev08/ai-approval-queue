#!/usr/bin/env python3
"""Tests for oddities.py — stdlib unittest, in-memory SQLite (v_transactions
and v_payees are views in the budget app; same-shaped tables read identically).
Run: python3 -m pytest test_oddities.py -q (from this directory), or
python3 services/mcp/budget/test_oddities.py from the repo root.
"""

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import oddities as od

TODAY = date(2026, 8, 11)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE v_transactions (id TEXT, date INT, amount INT, "
        "payee TEXT, imported_payee TEXT, notes TEXT, category TEXT, transfer_id TEXT, "
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
        "tombstone INT DEFAULT 0, hidden INT DEFAULT 0);"
        "CREATE TABLE rules (id TEXT, stage TEXT, conditions TEXT, "
        "actions TEXT, tombstone INT DEFAULT 0, conditions_op TEXT "
        "DEFAULT 'and');")
    conn.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Checking')")
    return conn


def rule(conn, payee, amount_op, cents, repeat=None, start=None,
         conditions_op="and", extra=(), amount_options=None, end_mode="never"):
    """One budget app rule as the copy stores it: payee is (when payee is given)
    + amount is/isapprox (cents positive here, stored negative;
    amount_options is the budget app's inflow/outflow flag), optionally date isapprox
    every month/year from start; extra adds raw conditions."""
    conds = [{"field": "description", "op": "is", "value": payee, "type": "id"}] \
        if payee else []
    if amount_op:
        conds.append({"field": "amount", "op": amount_op, "value": -cents})
        if amount_options:
            conds[-1]["options"] = amount_options
    if repeat:
        conds.append({"field": "date", "op": "isapprox", "value": {
            "frequency": repeat, "start": start.isoformat(), "interval": 1,
            "patterns": [], "skipWeekend": False, "weekendSolveMode": "after",
            "endMode": end_mode}})
    conds += list(extra)
    conn.execute("INSERT INTO rules VALUES (?, NULL, ?, ?, 0, ?)",
                 (f"r{conn.execute('SELECT COUNT(*) FROM rules').fetchone()[0]}",
                  json.dumps(conds),
                  json.dumps([{"field": "category", "op": "set", "value": "c1",
                               "type": "id"}]),
                  conditions_op))


def spend(conn, tid, day, cents, payee=None, category=None, imported=None,
          notes=None):
    """One charge; day is a date, cents positive (stored negative)."""
    conn.execute(
        "INSERT INTO v_transactions (id, date, amount, payee, category, "
        "imported_payee, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, od.cash_flow._day_int(day), -cents, payee, category, imported, notes))


def payee(conn, pid, name):
    conn.execute("INSERT INTO v_payees VALUES (?, ?)", (pid, name))


class TestOddFlags(unittest.TestCase):

    def setUp(self):
        self.conn = make_conn()

    def odd(self, kind=None, ids=()):
        out = od.odd_candidates(self.conn, TODAY, ids=ids)
        return [o for o in out if kind is None or o["kind"] == kind]

    def amounts(self, ids=()):
        return self.odd("amount", ids=ids)

    def steady(self, *cents):
        """Gym: prior charges, oldest first, ending 60 days ago."""
        self.conn.execute("DELETE FROM v_transactions WHERE id LIKE 'h%'")
        self.conn.execute("INSERT OR IGNORE INTO v_payees VALUES ('p1', 'Gym')")
        for i, c in enumerate(cents):
            spend(self.conn, f"h{i}", TODAY - timedelta(days=60 - i),
                  c, payee="p1")

    def test_no_payee_never_flags(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 500000)
        self.assertEqual(self.amounts(), [])

    def test_steady_flags_a_rise(self):
        self.steady(6000, 6000, 6000)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 6500, payee="p1")
        hits = self.amounts()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["reason"], "changed amount (was $60.00)")
        self.assertEqual(hits[0]["text"],
                         "changed amount: Gym $65.00 on 2026-08-10 (was $60.00)")
        self.assertEqual(hits[0]["transaction_id"], "t1")
        self.assertEqual(hits[0]["account"], "Checking")

    def test_steady_flags_a_drop(self):
        self.steady(6000, 6000, 6000)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 4000, payee="p1")
        self.assertEqual([h["reason"] for h in self.amounts()],
                         ["changed amount (was $60.00)"])

    def test_steady_within_slack_is_quiet(self):
        self.steady(6000, 6000, 6000)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 6300, payee="p1")
        self.assertEqual(self.amounts(), [])

    def test_steady_agrees_within_slack(self):
        # $551, $541, $531 (under 2% apart) still read as one level
        self.steady(55100, 54100, 53100)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 60000, payee="p1")
        self.assertEqual([h["reason"] for h in self.amounts()],
                         ["changed amount (was $531.00)"])

    def test_new_level_flags_once(self):
        # $60 x5 then $40: the next $40 is judged against the newest past
        # charge, $40, and one odd step does not make the payee varied
        self.steady(6000, 6000, 6000, 6000, 6000, 4000)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 4000, payee="p1")
        self.assertEqual(self.amounts(), [])
        spend(self.conn, "t2", TODAY, 6000, payee="p1")
        self.assertEqual([h["reason"] for h in self.amounts()],
                         ["changed amount (was $40.00)"])

    def test_same_day_charges_are_judged_against_the_day_before(self):
        # two $15 charges on one day after six $10s: neither is the other's
        # past, so both read as a changed amount
        self.steady(1000, 1000, 1000, 1000, 1000, 1000)
        spend(self.conn, "t1", TODAY, 1500, payee="p1")
        spend(self.conn, "t2", TODAY, 1500, payee="p1")
        self.assertEqual([h["reason"] for h in self.amounts()],
                         ["changed amount (was $10.00)"] * 2)

    def test_half_the_steps_agreeing_is_steady(self):
        # rent: one increase, then level
        self.steady(81200, 84385, 84385)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 100000, payee="p1")
        self.assertEqual([h["reason"] for h in self.amounts()],
                         ["changed amount (was $843.85)"])

    def test_long_varied_history_is_not_steady(self):
        # two close runs at the end of a varied year do not make it steady
        self.steady(5764, 15289, 9570, 1208, 25711, 19801, 13238, 12700)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 14130, payee="p1")
        self.assertEqual(self.amounts(), [])

    def test_mode_reads_the_window_only(self):
        # a steady year that ended 400 days ago says nothing about today
        payee(self.conn, "p1", "Gym")
        for i in range(6):
            spend(self.conn, f"o{i}", TODAY - timedelta(days=420 - i),
                  6000, payee="p1")
        spend(self.conn, "h1", TODAY - timedelta(days=60), 5000, payee="p1")
        spend(self.conn, "h2", TODAY - timedelta(days=30), 20000, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 19000, payee="p1")
        self.assertEqual(self.amounts(), [])

    def test_varied_flags_above_the_high(self):
        self.steady(5000, 20000)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 25000, payee="p1")
        hits = self.amounts()
        self.assertEqual([h["reason"] for h in hits],
                         ["above its usual range (high $200.00 in the last year)"])
        self.assertEqual(hits[0]["text"], "above its usual range: Gym $250.00 "
                         "on 2026-08-10 (high $200.00 in the last year)")

    def test_varied_slack_and_drops_are_quiet(self):
        self.steady(5000, 20000)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 21000, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=1), 19000, payee="p1")
        spend(self.conn, "t3", TODAY - timedelta(days=1), 1000, payee="p1")
        self.assertEqual(self.amounts(), [])

    def test_varied_just_past_the_slack_flags(self):
        self.steady(5000, 20000)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 21001, payee="p1")
        self.assertEqual([h["transaction_id"] for h in self.amounts()], ["t1"])

    def test_one_prior_charge_is_the_bar(self):
        self.steady(1078)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 115390, payee="p1")
        self.assertEqual([h["reason"] for h in self.amounts()],
                         ["above its usual range (high $10.78 in the last year)"])

    def test_varied_window_drops_old_highs(self):
        payee(self.conn, "p1", "Gym")
        spend(self.conn, "old", TODAY - timedelta(days=400), 50000, payee="p1")
        spend(self.conn, "h1", TODAY - timedelta(days=60), 5000, payee="p1")
        spend(self.conn, "h2", TODAY - timedelta(days=30), 20000, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 25000, payee="p1")
        self.assertEqual([h["reason"] for h in self.amounts()],
                         ["above its usual range (high $200.00 in the last year)"])

    def test_only_an_old_high_outside_the_window_is_no_bar(self):
        payee(self.conn, "p1", "Gym")
        spend(self.conn, "old", TODAY - timedelta(days=400), 5000, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 25000, payee="p1")
        self.assertEqual(self.amounts(), [])

    def test_reread_by_id_ignores_later_charges(self):
        # queued 10 days ago above the high; a bigger charge since does
        # not lift the bar it is judged against
        self.steady(5000, 20000)
        spend(self.conn, "t1", TODAY - timedelta(days=10), 25000, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=1), 30000, payee="p1")
        self.assertEqual([h["transaction_id"] for h in self.amounts(ids=["t1"])],
                         ["t2", "t1"])

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
        payee(self.conn, "p1", "Fresh Shop")
        spend(self.conn, "t1", TODAY - timedelta(days=8), 1000, payee="p1")
        hits = self.odd(ids=["t1", "no-such-id"])
        self.assertEqual([h["transaction_id"] for h in hits], ["t1"])

    def test_ids_never_double_a_window_charge(self):
        payee(self.conn, "p1", "Fresh Shop")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 1000, payee="p1")
        self.assertEqual(len(self.odd(ids=["t1"])), 1)

    def test_new_payee_reads_the_charge_date_not_today(self):
        # queued 10 days ago as a new payee; still new when re-read by id
        payee(self.conn, "p1", "Fresh Shop")
        spend(self.conn, "t1", TODAY - timedelta(days=10), 1000, payee="p1")
        self.assertEqual(self.odd(), [])
        hits = self.odd("new_payee", ids=["t1"])
        self.assertEqual([h["transaction_id"] for h in hits], ["t1"])

    def test_all_candidates_kept_amounts_first(self):
        old = TODAY - timedelta(days=60)
        for i in range(12):   # 12 payees, each above its one $100 charge
            payee(self.conn, f"lp{i}", f"Big {i}")
            spend(self.conn, f"lh{i}", old, 10000, payee=f"lp{i}")
            spend(self.conn, f"t{i}", TODAY - timedelta(days=1), 30000 + i,
                  payee=f"lp{i}")
        payee(self.conn, "p1", "Store")
        spend(self.conn, "d1", TODAY - timedelta(days=1), 2500, payee="p1")
        spend(self.conn, "d2", TODAY - timedelta(days=2), 2500, payee="p1")
        out = od.odd_candidates(self.conn, TODAY)
        self.assertEqual(len(out), 15)
        self.assertTrue(all(o["kind"] == "amount" for o in out[:12]))
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


class TestRules(unittest.TestCase):
    """The budget app's rules make charges expected: payee + amount (+ date)."""

    def setUp(self):
        self.conn = make_conn()
        payee(self.conn, "p1", "Online Shop")
        # a varied history, high 10.00, so a 36.00 charge is above range
        for i, c in enumerate((800, 1000, 600)):
            spend(self.conn, f"h{i}", TODAY - timedelta(days=60 - i), c,
                  payee="p1")

    def kinds(self, ids=()):
        return [o["kind"] for o in od.odd_candidates(self.conn, TODAY, ids=ids)]

    def reasons(self, ids=()):
        return [o["reason"] for o in od.odd_candidates(self.conn, TODAY, ids=ids)]

    def test_no_rule_flags_as_before(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        self.assertEqual(self.kinds(), ["amount"])

    def test_approx_amount_rule_makes_it_expected(self):
        rule(self.conn, "p1", "isapprox", 3600)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        self.assertEqual(self.kinds(), [])

    def test_approx_window_is_the_apps_7_5_percent(self):
        rule(self.conn, "p1", "isapprox", 3600)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3870, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=2), 3871, payee="p1")
        self.assertEqual([o["transaction_id"] for o in
                          od.odd_candidates(self.conn, TODAY)], ["t2"])

    def test_exact_rule_needs_the_exact_amount(self):
        rule(self.conn, "p1", "is", 3600)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=2), 3601, payee="p1")
        self.assertEqual([o["transaction_id"] for o in
                          od.odd_candidates(self.conn, TODAY)], ["t2"])

    def test_yearly_rule_on_its_date_is_expected(self):
        d = TODAY - timedelta(days=1)
        rule(self.conn, "p1", "isapprox", 3600, "yearly",
             d.replace(year=d.year - 1) - timedelta(days=2))
        spend(self.conn, "t1", d, 3600, payee="p1")
        self.assertEqual(self.kinds(), [])

    def test_yearly_rule_off_its_date_flags_and_names_the_rhythm(self):
        d = TODAY - timedelta(days=1)
        rule(self.conn, "p1", "isapprox", 3600, "yearly", d - timedelta(days=100))
        spend(self.conn, "t1", d, 3600, payee="p1")
        self.assertEqual(self.reasons(), [
            "off its expected date (yearly, usually around "
            + (d - timedelta(days=100)).strftime("%b ")
            + str((d - timedelta(days=100)).day) + ")"])

    def test_off_date_skips_the_amount_and_new_payee_rules(self):
        payee(self.conn, "p2", "New")
        rule(self.conn, "p2", "isapprox", 3600, "yearly", TODAY - timedelta(days=100))
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p2")
        self.assertEqual(self.kinds(), ["off_schedule"])

    def test_monthly_rule_window_is_two_days(self):
        d = TODAY - timedelta(days=1)
        rule(self.conn, "p1", "isapprox", 3600, "monthly", d - timedelta(days=2))
        spend(self.conn, "t1", d, 3600, payee="p1")
        self.assertEqual(self.kinds(), [])
        rule(self.conn, "p1", "isapprox", 5000, "monthly", d - timedelta(days=3))
        spend(self.conn, "t2", d, 5000, payee="p1")
        self.assertEqual(self.reasons(), [
            "off its expected date (monthly, usually the "
            + od._ordinal((d - timedelta(days=3)).day) + ")"])

    def test_monthly_rule_matches_next_month(self):
        d = TODAY - timedelta(days=1)
        rule(self.conn, "p1", "isapprox", 3600, "monthly",
             date(d.year, d.month - 1, min(d.day, 28)) if d.month > 1
             else date(d.year - 1, 12, min(d.day, 28)))
        spend(self.conn, "t1", d.replace(day=min(d.day, 28)), 3600, payee="p1")
        self.assertEqual(self.kinds(), [])

    def test_a_month_without_the_start_day_has_no_occurrence(self):
        self.assertFalse(od._on_repeat(("monthly", date(2026, 1, 31)),
                                       date(2026, 2, 28)))
        self.assertTrue(od._on_repeat(("monthly", date(2026, 1, 31)),
                                      date(2026, 3, 30)))

    def test_before_the_start_is_off_date(self):
        self.assertFalse(od._on_repeat(("yearly", date(2026, 9, 9)),
                                       date(2025, 9, 9)))
        self.assertTrue(od._on_repeat(("yearly", date(2026, 9, 9)),
                                      date(2026, 9, 8)))

    def test_window_is_the_apps_one_day_early_to_two_days_late(self):
        repeat = ("monthly", date(2026, 9, 9))
        self.assertEqual([od._on_repeat(repeat, date(2026, 10, 9) + timedelta(days=k))
                          for k in (-3, -2, -1, 0, 1, 2, 3)],
                         [False, False, True, True, True, True, False])

    def test_an_ended_recurrence_says_nothing(self):
        d = TODAY - timedelta(days=1)
        rule(self.conn, "p1", "isapprox", 3600, "monthly", d, end_mode="on_date")
        spend(self.conn, "t1", d, 3600, payee="p1")
        self.assertEqual(self.kinds(), ["amount"])

    def test_duplicate_still_flags_an_expected_charge(self):
        rule(self.conn, "p1", "isapprox", 3600)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        spend(self.conn, "t2", TODAY - timedelta(days=2), 3600, payee="p1")
        self.assertEqual(self.kinds(), ["duplicate"])

    def test_expected_skips_new_payee(self):
        payee(self.conn, "p2", "New")
        rule(self.conn, "p2", "isapprox", 3600)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p2")
        self.assertEqual(self.kinds(), [])

    def test_rule_without_amount_says_nothing(self):
        rule(self.conn, "p1", None, 0)
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        self.assertEqual(self.kinds(), ["amount"])

    def test_or_rules_and_other_shapes_are_ignored(self):
        rule(self.conn, "p1", "isapprox", 3600, conditions_op="or")
        rule(self.conn, "p1", "isapprox", 3600,
             extra=[{"field": "notes", "op": "matches", "value": "x"}])
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1",
              notes="x")
        self.assertEqual(self.kinds(), ["amount"])

    def test_notes_rule_needs_its_notes_text(self):
        rule(self.conn, "p1", "isapprox", 3600,
             extra=[{"field": "notes", "op": "contains", "value": "PAYROLL"}])
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1",
              notes="acme payroll 0928")
        spend(self.conn, "t2", TODAY - timedelta(days=2), 3610, payee="p1",
              notes="acme bill")
        self.assertEqual([o["transaction_id"] for o in
                          od.odd_candidates(self.conn, TODAY)], ["t2"])

    def test_imported_payee_rule_covers_every_payee_name(self):
        payee(self.conn, "p2", "Monthly Payment Ref")
        rule(self.conn, None, "isapprox", 50000, extra=[
            {"field": "imported_description", "op": "contains",
             "value": "Monthly Payment"}])
        spend(self.conn, "t1", TODAY - timedelta(days=1), 50000, payee="p2",
              imported="Monthly Payment Ref")
        self.assertEqual(self.kinds(), [])

    def test_deleted_rule_is_ignored(self):
        rule(self.conn, "p1", "isapprox", 3600)
        self.conn.execute("UPDATE rules SET tombstone = 1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        self.assertEqual(self.kinds(), ["amount"])

    def test_outflow_option_compares_the_magnitude(self):
        rule(self.conn, "p1", "isapprox", -3600, amount_options={"outflow": True})   # stored positive
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        self.assertEqual(self.kinds(), [])

    def test_a_positive_amount_without_the_option_is_a_deposit_rule(self):
        rule(self.conn, "p1", "isapprox", -3600)   # stored positive, no option
        rule(self.conn, "p1", "is", -3600, amount_options={"inflow": True})
        spend(self.conn, "t1", TODAY - timedelta(days=1), 3600, payee="p1")
        self.assertEqual(self.kinds(), ["amount"])


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
        payee(self.conn, "p1", "Fresh Shop")

    def big(self, category):
        # a new payee: the one flag that needs no history
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              payee="p1", category=category)

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


class TestIncomeMarks(unittest.TestCase):
    """income_marks: deposits in the source categories judged by the
    cash-flow report's source maths. The schema grows is_income, and
    ASSISTANT_FINANCE_NOTES names a temp folder holding the test's cash-flow.md."""

    def setUp(self):
        self.conn = make_conn()
        self.conn.executescript(
            "ALTER TABLE category_groups ADD COLUMN is_income INT DEFAULT 0;"
            "INSERT INTO category_groups (id, name, is_income) "
            "VALUES ('gi', 'Income', 1);"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('cm', 'Monthly', 'gi'), ('cp', 'Periodic', 'gi');")
        payee(self.conn, "p1", "Acme")
        saved = od.cash_flow.FIRST_MONTH
        self.addCleanup(lambda: setattr(od.cash_flow, "FIRST_MONTH", saved))
        od.cash_flow.FIRST_MONTH = "2026-01"
        self.finance = tempfile.TemporaryDirectory()
        self.addCleanup(self.finance.cleanup)
        env = mock.patch.dict(os.environ, {"ASSISTANT_FINANCE_NOTES": self.finance.name})
        env.start()
        self.addCleanup(env.stop)
        self.sources("")

    def sources(self, body):
        with open(os.path.join(self.finance.name, "cash-flow.md"), "w") as f:
            f.write(f"{od.cash_flow._heading(od.cash_flow.MONTHLY_CATEGORY)}\n\n"
                    f"{body}\n\n"
                    f"{od.cash_flow._heading(od.cash_flow.PERIODIC_CATEGORY)}\n")

    def deposit(self, tid, days_ago, cents, category="cm", pid="p1"):
        self.conn.execute(
            "INSERT INTO v_transactions (id, date, amount, payee, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, od.cash_flow._day_int(TODAY - timedelta(days=days_ago)),
             cents, pid, category))

    def paychecks(self, *cents):
        """Deposits every 14 days, oldest first, the last one 2 days ago."""
        for i, c in enumerate(reversed(cents)):
            self.deposit(f"d{i}", 2 + 14 * i, c)

    def test_usual_paycheck_is_routine(self):
        self.paychecks(123450, 123451, 123449)
        self.assertEqual(od.income_marks(self.conn, TODAY),
                         {"d0": "routine: Acme, every 2 weeks, usual amount"})

    def test_label_and_set_frequency_from_the_file(self):
        self.sources("- Acme\n  - frequency: every 2 weeks\n  - label: Acme Paycheck")
        self.paychecks(123450)
        self.assertEqual(od.income_marks(self.conn, TODAY),
                         {"d0": "routine: Acme Paycheck, every 2 weeks, usual amount"})

    def test_off_amount_flags(self):
        self.paychecks(123450, 123451, 100000)
        self.assertEqual(od.income_marks(self.conn, TODAY),
                         {"d0": "flag: Acme deposit off its usual $1234.51"})

    def test_two_raises_move_the_usual(self):
        self.paychecks(150000, 123451, 123449)
        self.assertEqual(od.income_marks(self.conn, TODAY),
                         {"d0": "routine: Acme, every 2 weeks, usual amount"})

    def test_only_deposit_without_frequency_is_new(self):
        self.paychecks(123450)
        self.assertEqual(od.income_marks(self.conn, TODAY), {"d0": "flag: new payee"})

    def test_two_deposits_without_frequency_count(self):
        self.paychecks(123450, 123451)
        self.assertEqual(od.income_marks(self.conn, TODAY),
                         {"d0": "routine: Acme, 2 deposits so far"})

    def test_several_incomes_get_no_amount_judgment(self):
        self.sources("- Acme\n  - frequency: monthly\n  - incomes: 2")
        self.paychecks(100000, 500000, 100000)
        self.assertEqual(od.income_marks(self.conn, TODAY),
                         {"d0": "routine: Acme, monthly"})

    def test_amount_entry_claims_the_deposit(self):
        payee(self.conn, "p2", "Bank")
        self.sources("- Grant\n  - frequency: monthly\n  - amount: 250")
        self.deposit("g1", 3, 25000, pid="p2")
        self.assertEqual(od.income_marks(self.conn, TODAY),
                         {"g1": "routine: Grant, monthly, usual amount"})

    def test_other_income_categories_and_old_deposits_get_nothing(self):
        self.conn.execute("INSERT INTO categories (id, name, cat_group) "
                          "VALUES ('co', 'One-off', 'gi')")
        self.deposit("o1", 1, 81000, category="co")
        self.deposit("old", 20, 123450)
        self.assertEqual(od.income_marks(self.conn, TODAY), {})


if __name__ == "__main__":
    unittest.main()
