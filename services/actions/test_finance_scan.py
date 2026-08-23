#!/usr/bin/env python3
"""Tests for services/actions/finance_scan.py — stdlib unittest, no live data.

The pure functions (odd flags, lens numbers, category validation, card
building, email body) run against an in-memory SQLite copy of the api-cache
schema; the one main() test patches db/call_llm/post_json so no server, LLM,
or budget file is touched. Run: python3 -m pytest test_finance_scan.py -q
(from this directory), or python3 services/actions/test_finance_scan.py from
the repo root.
"""

import contextlib
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import finance_scan as fs

TODAY = date(2026, 8, 11)


def make_conn():
    """In-memory db with the tables the scan reads. v_transactions and
    v_payees are views in Actual; same-shaped tables read identically."""
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
        (tid, fs.day_int(day), -cents, payee, category))


def payee(conn, pid, name):
    conn.execute("INSERT INTO v_payees VALUES (?, ?)", (pid, name))


def tx(conn, tid, day, cents, payee=None, category=None, transfer=None,
       parent=0, starting=0, sort_order=0, account="a1"):
    """One transaction row with full control; cents signed (spend negative)."""
    conn.execute(
        "INSERT INTO v_transactions (id, date, amount, payee, category, "
        "transfer_id, is_parent, starting_balance_flag, sort_order, account) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, fs.day_int(day), cents, payee, category, transfer, parent,
         starting, sort_order, account))


def row(tid, day, cents, payee="Shop", account="Checking", cat=None,
        grp=None, sort_order=0, account_order=0, grp_order=0):
    """One new_transactions row, the shape the query returns."""
    return {"id": tid, "date": fs.day_int(day), "amount": cents,
            "sort_order": sort_order, "payee": payee, "account": account,
            "account_order": account_order, "cat": cat, "grp": grp,
            "grp_order": grp_order}


# ---------------------------------------------------------------- odd flags

class TestOddFlags(unittest.TestCase):

    def setUp(self):
        self.conn = make_conn()

    def odd(self, kind=None):
        out = fs.odd_candidates(self.conn, TODAY)
        return [o for o in out if kind is None or o["kind"] == kind]

    def test_large_absolute_threshold(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 50000)
        spend(self.conn, "t2", TODAY - timedelta(days=1), 49999)
        hits = self.odd("large")
        self.assertEqual(len(hits), 1)
        self.assertIn("$500.00", hits[0]["text"])

    def test_large_ratio_needs_history_and_floor(self):
        payee(self.conn, "p1", "Gym")
        old = TODAY - timedelta(days=60)
        for i in range(3):   # median $30
            spend(self.conn, f"h{i}", old, 3000, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 10000, payee="p1")
        hits = self.odd("large")
        self.assertEqual(len(hits), 1)
        self.assertIn("median $30.00", hits[0]["text"])

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
        self.assertIn("2x within 3 days", hits[0]["text"])

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
        self.assertEqual(len([t for t in texts if "first charge ever" in t]), 2)

    def test_known_payee_not_new(self):
        payee(self.conn, "p1", "Old Shop")
        spend(self.conn, "h1", TODAY - timedelta(days=30), 900, payee="p1")
        spend(self.conn, "t1", TODAY - timedelta(days=1), 1000, payee="p1")
        self.assertEqual(self.odd("new_payee"), [])

    def test_window_excludes_old_charges(self):
        spend(self.conn, "t1", TODAY - timedelta(days=8), 60000)
        self.assertEqual(self.odd(), [])

    def test_cap_and_larges_first(self):
        for i in range(12):
            spend(self.conn, f"t{i}", TODAY - timedelta(days=1), 50000 + i)
        payee(self.conn, "p1", "Store")
        spend(self.conn, "d1", TODAY - timedelta(days=1), 2500, payee="p1")
        spend(self.conn, "d2", TODAY - timedelta(days=2), 2500, payee="p1")
        out = fs.odd_candidates(self.conn, TODAY)
        self.assertEqual(len(out), fs.ODD_CAP)
        self.assertTrue(all(o["kind"] == "large" for o in out))


# ---------------------------------------------------------------- lens set

class TestAllLensNumbers(unittest.TestCase):

    def setUp(self):
        self.conn = make_conn()

    def test_every_lens_present_in_order(self):
        out = fs.all_lens_numbers(self.conn, TODAY)
        self.assertEqual([e["lens"] for e in out], fs.LENSES)
        self.assertEqual([e["key"] for e in out],
                         [fs.LENS_KEYS[n] for n in fs.LENSES])

    def test_category_lens_carries_no_numbers_below_the_gate(self):
        out = {e["lens"]: e["numbers"]
               for e in fs.all_lens_numbers(self.conn, TODAY)}
        self.assertIsNone(out[fs.CATEGORY_LENS])
        self.assertIsNotNone(out[fs.RECAP_LENS])

    def test_keys_are_distinct(self):
        self.assertEqual(len(set(fs.LENS_KEYS.values())), len(fs.LENSES))


class TestCategoryTrend(unittest.TestCase):
    """The gate the category lens needs before it has figures."""

    def setUp(self):
        self.conn = make_conn()
        self.conn.execute("INSERT INTO category_groups (id, name) "
                          "VALUES ('g1', 'Food')")
        self.conn.execute("INSERT INTO categories (id, name, cat_group) "
                          "VALUES ('c1', 'Groceries', 'g1')")

    def fill(self, month_first, cents_each, n=3):
        for i in range(n):
            spend(self.conn, f"{month_first}-{i}", month_first
                  + timedelta(days=i), cents_each, category="c1")

    def test_none_without_enough_data(self):
        self.fill(date(2026, 7, 1), 2000)          # July only
        self.assertIsNone(fs._category_trend(self.conn, TODAY))

    def test_reports_biggest_mover_with_gate_met(self):
        self.fill(date(2026, 7, 1), 4000)          # July: $120
        self.fill(date(2026, 6, 1), 2000)          # June: $60
        out = fs._category_trend(self.conn, TODAY)
        self.assertEqual(out["category"], "Groceries")
        self.assertEqual(out["last_month"],
                         {"month": "2026-07", "spending": "-120.00"})
        self.assertEqual(out["month_before"],
                         {"month": "2026-06", "spending": "-60.00"})


class TestLensNumbers(unittest.TestCase):
    """Every lens builds its fact dict without SQL errors, even on an
    empty budget."""

    def test_all_lenses_smoke(self):
        conn = make_conn()
        spend(conn, "t1", TODAY - timedelta(days=1), 1234, payee=None)
        for lens in fs.LENSES:
            out = fs.lens_numbers(conn, TODAY, lens)
            if lens == fs.CATEGORY_LENS:
                self.assertIsNone(out)      # gate unmet on an empty budget
                continue
            self.assertIsInstance(out, dict, lens)
            self.assertTrue({"spending", "income"} <= set(out), lens)

    def test_yesterday_lens_numbers(self):
        conn = make_conn()
        payee(conn, "p1", "Cafe")
        spend(conn, "t1", TODAY - timedelta(days=1), 700, payee="p1")
        out = fs.lens_numbers(conn, TODAY, "yesterday vs 7-day average")
        self.assertEqual(out["spending"]["yesterday"],
                         {"total": "-7.00", "transactions": 1})
        self.assertEqual(out["spending"]["largest_charge_yesterday"]["payee"],
                         "Cafe")


# ---------------------------------------------------------------- excluded groups

class TestExcludedGroups(unittest.TestCase):
    """Rows parked in cash_flow.EXCLUDED_GROUPS leave the odd checks and the
    commentary numbers; the new-transactions listing and the category list the
    model suggests from keep them."""

    def setUp(self):
        self.addCleanup(setattr, fs.cash_flow, "EXCLUDED_GROUPS",
                        fs.cash_flow.EXCLUDED_GROUPS)
        fs.cash_flow.EXCLUDED_GROUPS = ["Ignored"]
        self.conn = make_conn()
        self.conn.executescript(
            "INSERT INTO category_groups (id, name) VALUES ('g1', 'Food');"
            "INSERT INTO category_groups (id, name) VALUES ('g2', 'Ignored');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c1', 'Groceries', 'g1');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c2', 'Transfers', 'g2');")

    def test_parked_charge_is_not_odd(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c2")
        self.assertEqual(fs.odd_candidates(self.conn, TODAY), [])

    def test_same_charge_unparked_is_odd(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c1")
        self.assertEqual(len(fs.odd_candidates(self.conn, TODAY)), 1)

    def test_parked_deposit_leaves_the_commentary(self):
        payee(self.conn, "p1", "Amex Epayment Loan")
        tx(self.conn, "t1", TODAY - timedelta(days=1), 2500000, payee="p1",
           category="c2")
        out = fs.lens_numbers(self.conn, TODAY,
                              "largest charge and top payee, last 7 days")
        self.assertIsNone(out["income"]["largest_deposit"])

    def test_parked_rows_leave_the_totals(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 5000, category="c2")
        spend(self.conn, "t2", TODAY - timedelta(days=1), 700, category="c1")
        out = fs.lens_numbers(self.conn, TODAY, "yesterday vs 7-day average")
        self.assertEqual(out["spending"]["yesterday"],
                         {"total": "-7.00", "transactions": 1})

    def test_excluded_group_still_reaches_the_model(self):
        # parking is a normal choice, so the model has to see the group
        self.addCleanup(setattr, fs.cash_flow, "build_report",
                        fs.cash_flow.build_report)
        fs.cash_flow.build_report = lambda today=None: ""
        cats = fs.read_all(self.conn)["categories"]
        self.assertEqual([c["grp"] for c in cats], ["Food", "Ignored"])

    def test_new_transactions_keeps_parked_rows(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c2")
        self.assertEqual([r["id"] for r in fs.new_transactions(self.conn, TODAY)],
                         ["t1"])

    def test_empty_constant_excludes_nothing(self):
        fs.cash_flow.EXCLUDED_GROUPS = []
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c2")
        self.assertEqual(len(fs.odd_candidates(self.conn, TODAY)), 1)

    def test_deleting_the_category_counts_its_rows_again(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c2")
        self.conn.execute("UPDATE categories SET tombstone = 1 WHERE id = 'c2'")
        self.assertEqual(len(fs.odd_candidates(self.conn, TODAY)), 1)

    def test_deleting_the_group_counts_its_rows_again(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c2")
        self.conn.execute("UPDATE category_groups SET tombstone = 1 "
                          "WHERE id = 'g2'")
        self.assertEqual(len(fs.odd_candidates(self.conn, TODAY)), 1)

    def test_moving_the_category_out_counts_its_rows_again(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c2")
        self.conn.execute("UPDATE categories SET cat_group = 'g1' "
                          "WHERE id = 'c2'")
        self.assertEqual(len(fs.odd_candidates(self.conn, TODAY)), 1)


# ---------------------------------------------------------------- validate

CATS = [{"grp": "Food", "name": "Groceries"},
        {"grp": "Food", "name": "Misc"},
        {"grp": "Home", "name": "Misc"}]


class TestStripFalseCurrency(unittest.TestCase):
    """A currency sign on a count goes; money keeps its sign."""

    def test_days_lose_the_sign(self):
        self.assertEqual(
            fs.strip_false_currency("watch the remaining $16 days"),
            "watch the remaining 16 days")

    def test_other_counts(self):
        self.assertEqual(
            fs.strip_false_currency("$7 transactions over $2 weeks, up $12%"),
            "7 transactions over 2 weeks, up 12%")

    def test_percent_spelled_out(self):
        self.assertEqual(fs.strip_false_currency("groceries are up $12 percent"),
                         "groceries are up 12 percent")

    def test_money_is_untouched(self):
        for text in ("spending hit $17,825 this month",
                     "a $1,190.90 charge at the Apple store",
                     "leftover -$6,037", "$500 monthly"):
            self.assertEqual(fs.strip_false_currency(text), text)

    def test_singular_unit_is_money(self):
        # "a $250 day" is what yesterday cost, not a count of days
        for text in ("yesterday was a $250 day", "a $40 week for eating out"):
            self.assertEqual(fs.strip_false_currency(text), text)


class TestResolveCategory(unittest.TestCase):

    def test_unique_bare_name(self):
        self.assertEqual(fs.resolve_category("Groceries", CATS), "Groceries")

    def test_ambiguous_bare_name_dropped(self):
        self.assertIsNone(fs.resolve_category("Misc", CATS))

    def test_qualified_name_resolves(self):
        self.assertEqual(fs.resolve_category("Home: Misc", CATS), "Home: Misc")

    def test_case_insensitive_fallback(self):
        self.assertEqual(fs.resolve_category("groceries", CATS), "Groceries")
        self.assertEqual(fs.resolve_category("home: misc", CATS), "Home: Misc")

    def test_unknown_dropped(self):
        self.assertIsNone(fs.resolve_category("Vibes", CATS))


class TestBuildCards(unittest.TestCase):

    def data(self, queue):
        return {"queue": queue, "categories": CATS,
                "lenses": [{"lens": "week vs previous week",
                            "key": "week_vs_previous_week", "numbers": {}}],
                "odd": [{"kind": "large", "text": "odd A"},
                        {"kind": "duplicate", "text": "odd B"}]}

    def tx(self, tid, history=(), pick="latest"):
        return {"id": tid, "date": 20260810, "amount": -1500, "payee": "Shop",
                "notes": "", "account": "Checking", "account_id": "a1",
                "pick": pick,
                "history": [{"category": h, "n": 1} for h in history]}

    def test_history_beats_llm_guesses(self):
        llm = {"suggestions": [{"transaction_id": "t1",
                                "categories": ["Home: Misc"]}],
               "odd_keep": [], "summary": [], "commentary": {}}
        cards, *_ = fs.build_cards(self.data([self.tx("t1", ["Groceries"])]), llm)
        self.assertEqual(cards[0]["suggestions"],
                         [{"category": "Groceries", "basis": "history"}])

    def test_guesses_validated_deduped_capped(self):
        llm = {"suggestions": [{"transaction_id": "t1", "categories":
                                ["Vibes", "groceries", "Groceries", "Misc",
                                 "Home: Misc", "Food: Misc"]}],
               "odd_keep": [], "summary": [], "commentary": {}}
        cards, *_ = fs.build_cards(self.data([self.tx("t1")]), llm)
        self.assertEqual(cards[0]["suggestions"],
                         [{"category": "Groceries", "basis": "guess"},
                          {"category": "Home: Misc", "basis": "guess"},
                          {"category": "Food: Misc", "basis": "guess"}])

    def test_card_carries_its_pick(self):
        llm = {"suggestions": [], "odd_keep": [], "summary": [],
               "commentary": {}}
        cards, *_ = fs.build_cards(
            self.data([self.tx("t1"), self.tx("t2", pick="random")]), llm)
        self.assertEqual([c["pick"] for c in cards], ["latest", "random"])

    def test_odd_keep_filters_bad_indexes(self):
        llm = {"suggestions": [], "odd_keep": [1, 7, -1, "x"],
               "summary": [], "commentary": {}}
        _, odd_kept, *_ = fs.build_cards(self.data([]), llm)
        self.assertEqual(odd_kept, ["odd B"])

    def test_summary_and_commentary_capped(self):
        llm = {"suggestions": [], "odd_keep": [],
               "summary": [f"s{i}" for i in range(6)] + ["x" * 400],
               "commentary": {"week_vs_previous_week": ["a", "b", "c"]}}
        _, _, summary, commentary = fs.build_cards(self.data([]), llm)
        self.assertEqual(summary, ["s0", "s1", "s2", "s3"])
        self.assertEqual(commentary, {"week_vs_previous_week": ["a", "b"]})
        llm["summary"] = ["x" * 400]
        _, _, summary, _ = fs.build_cards(self.data([]), llm)
        self.assertEqual(len(summary[0]), 300)


# ---------------------------------------------------------------- new transactions

class TestNewTransactions(unittest.TestCase):

    def setUp(self):
        self.conn = make_conn()

    def ids(self):
        return {r["id"] for r in fs.new_transactions(self.conn, TODAY)}

    def test_window_bounds(self):
        tx(self.conn, "today", TODAY, -100)
        tx(self.conn, "edge", TODAY - timedelta(days=fs.NEW_WINDOW_DAYS), -100)
        tx(self.conn, "old", TODAY - timedelta(days=fs.NEW_WINDOW_DAYS + 1),
           -100)
        tx(self.conn, "future", TODAY + timedelta(days=1), -100)
        self.assertEqual(self.ids(), {"today", "edge"})

    def test_exclusions(self):
        tx(self.conn, "spend", TODAY, -100)
        tx(self.conn, "income", TODAY, 5000)
        tx(self.conn, "categorized", TODAY, -100, category="c1")
        tx(self.conn, "transfer", TODAY, -100, transfer="x")
        tx(self.conn, "parent", TODAY, -100, parent=1)
        tx(self.conn, "starting", TODAY, -100, starting=1)
        self.conn.execute("INSERT INTO accounts (id, name, offbudget) "
                          "VALUES ('a2', 'Invest', 1)")
        tx(self.conn, "offbudget", TODAY, -100, account="a2")
        self.assertEqual(self.ids(), {"spend", "income", "categorized"})

    def test_category_and_payee_joined(self):
        payee(self.conn, "p1", "Cafe")
        self.conn.execute("INSERT INTO category_groups (id, name) "
                          "VALUES ('g1', 'Food')")
        self.conn.execute("INSERT INTO categories (id, name, cat_group) "
                          "VALUES ('c1', 'Groceries', 'g1')")
        tx(self.conn, "t1", TODAY, -700, payee="p1", category="c1")
        tx(self.conn, "t2", TODAY, -300)
        rows = {r["id"]: r for r in fs.new_transactions(self.conn, TODAY)}
        self.assertEqual((rows["t1"]["grp"], rows["t1"]["cat"]),
                         ("Food", "Groceries"))
        self.assertEqual(rows["t1"]["payee"], "Cafe")
        self.assertIsNone(rows["t2"]["cat"])


class TestGroupNew(unittest.TestCase):

    def test_accounts_by_actual_order_then_name(self):
        rows = [row("t1", TODAY, -1, account="Beta", account_order=1),
                row("t2", TODAY, -2, account="Alpha", account_order=1),
                row("t3", TODAY, -3, account="First", account_order=0)]
        out = fs.group_new(rows)
        self.assertEqual([a for a, _ in out], ["First", "Alpha", "Beta"])

    def test_no_category_first_then_group_order(self):
        rows = [row("t1", TODAY, -1, cat="B", grp="G2", grp_order=2),
                row("t2", TODAY, -2),
                row("t3", TODAY, -3, cat="A", grp="G1", grp_order=1)]
        _, groups = fs.group_new(rows)[0]
        self.assertEqual([h for h, _ in groups],
                         [fs.NO_CATEGORY, "G1: A", "G2: B"])

    def test_rows_newest_first(self):
        rows = [row("old", TODAY - timedelta(days=3), -1),
                row("new", TODAY, -2),
                row("mid", TODAY - timedelta(days=1), -3)]
        _, groups = fs.group_new(rows)[0]
        self.assertEqual([r["id"] for r in groups[0][1]],
                         ["new", "mid", "old"])

    def test_same_day_sort_order_then_id(self):
        rows = [row("b", TODAY, -1, sort_order=5),
                row("a", TODAY, -2, sort_order=5),
                row("c", TODAY, -3, sort_order=9)]
        _, groups = fs.group_new(rows)[0]
        self.assertEqual([r["id"] for r in groups[0][1]], ["c", "a", "b"])


class TestReported(unittest.TestCase):
    """The reported file: load/save roundtrip, merge, purge."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._saved = fs.REPORTED_FILE
        fs.REPORTED_FILE = os.path.join(self._dir.name, "sub", "reported.json")
        self.addCleanup(self.restore)

    def restore(self):
        fs.REPORTED_FILE = self._saved

    def test_load_missing_or_junk_is_empty(self):
        self.assertEqual(fs.load_reported(), {})
        fs.save_reported({"t1": "2026-08-10"})
        pathlib.Path(fs.REPORTED_FILE).write_text("not json")
        self.assertEqual(fs.load_reported(), {})
        pathlib.Path(fs.REPORTED_FILE).write_text('["list", "not", "dict"]')
        self.assertEqual(fs.load_reported(), {})

    def test_roundtrip_creates_parent_dirs(self):
        fs.save_reported({"t1": "2026-08-10"})
        self.assertEqual(fs.load_reported(), {"t1": "2026-08-10"})

    def test_merge_adds_fresh_and_purges_aged_out(self):
        reported = {"old": (TODAY - timedelta(days=8)).isoformat(),
                    "edge": (TODAY - timedelta(days=7)).isoformat()}
        out = fs.merge_reported(reported, [row("new", TODAY, -100)], TODAY)
        self.assertEqual(out,
                         {"edge": (TODAY - timedelta(days=7)).isoformat(),
                          "new": TODAY.isoformat()})


# ---------------------------------------------------------------- ledger

class TestUpdateLedger(unittest.TestCase):
    """update_ledger against a temp file and a stubbed cash_flow.ledger_month
    — the month maths itself is cash_flow's own, tested there."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(setattr, fs, "LEDGER_FILE", fs.LEDGER_FILE)
        fs.LEDGER_FILE = os.path.join(self._dir.name, "ledger.md")
        self.addCleanup(setattr, fs.cash_flow, "FIRST_MONTH",
                        fs.cash_flow.FIRST_MONTH)
        fs.cash_flow.FIRST_MONTH = "2026-01"
        self.addCleanup(setattr, fs.cash_flow, "ledger_month",
                        fs.cash_flow.ledger_month)
        self.months = {}
        fs.cash_flow.ledger_month = lambda label, today=None: self.months[label]

    def month(self, label, estimate, recurring, one_off, spending):
        self.months[label] = {"estimate": estimate, "recurring": recurring,
                              "one_off": one_off, "spending": spending}

    def read(self):
        with open(fs.LEDGER_FILE, encoding="utf-8") as f:
            return f.read()

    def test_creates_file_and_appends_all_missing(self):
        self.month("2026-01", 216666.7, 300000, 60000, 30000)
        self.month("2026-02", 216666.7, 400000, 0, 500000)
        added = fs.update_ledger(date(2026, 3, 5))
        self.assertEqual(added, ["2026-01", "2026-02"])
        text = self.read()
        self.assertIn("# Monthly balance", text)
        self.assertIn("- 2026-01: estimate $2,167 | recurring $3,000 | "
                      "one-off $600 | spending $300 | balance +$2,467 | "
                      "total +$2,467", text)
        self.assertIn("- 2026-02: estimate $2,167 | recurring $4,000 | "
                      "one-off $0 | spending $5,000 | balance -$2,833 | "
                      "total -$366", text)

    def test_second_run_appends_nothing(self):
        self.month("2026-01", 0, 0, 0, 0)
        fs.update_ledger(date(2026, 2, 5))
        before = self.read()
        self.assertEqual(fs.update_ledger(date(2026, 2, 5)), [])
        self.assertEqual(self.read(), before)

    def test_missing_month_added_and_total_continues(self):
        self.month("2026-01", 100000, 0, 0, 0)
        fs.update_ledger(date(2026, 2, 5))
        self.month("2026-02", 0, 0, 0, 50000)
        added = fs.update_ledger(date(2026, 3, 5))
        self.assertEqual(added, ["2026-02"])
        self.assertIn("balance -$500 | total +$500", self.read())

    def test_no_complete_month_still_writes_the_header(self):
        added = fs.update_ledger(date(2026, 1, 15))
        self.assertEqual(added, [])
        self.assertIn("# Monthly balance", self.read())

    def write_months(self, *lines):
        with open(fs.LEDGER_FILE, "w", encoding="utf-8") as f:
            f.write("# Monthly balance\n\n## Months\n"
                    + "".join(line + "\n" for line in lines))

    LINE1 = ("- 2026-01: estimate $1,000 | recurring $1,000 | one-off $0 | "
             "spending $500 | balance +$500 | total +$500")
    LINE2 = ("- 2026-02: estimate $1,000 | recurring $1,000 | one-off $0 | "
             "spending $1,800 | balance -$800 | total -$300")

    def test_hand_note_shaped_like_a_month_is_refused(self):
        self.write_months(self.LINE1, "- 2026-02: check the tax refund")
        with self.assertRaises(RuntimeError) as ctx:
            fs.update_ledger(date(2026, 3, 5))
        self.assertIn("not a ledger line", str(ctx.exception))

    def test_edited_balance_is_refused(self):
        self.write_months(self.LINE1.replace("+$500 ", "+$500.50 "))
        with self.assertRaises(RuntimeError) as ctx:
            fs.update_ledger(date(2026, 2, 5))
        self.assertIn("not a ledger line", str(ctx.exception))

    def test_removed_line_breaks_the_running_sum_and_is_refused(self):
        self.write_months(self.LINE2)   # 2026-01 deleted
        with self.assertRaises(RuntimeError) as ctx:
            fs.update_ledger(date(2026, 3, 5))
        self.assertIn("not the running sum", str(ctx.exception))

    def test_reordered_lines_are_refused(self):
        self.write_months(self.LINE2.replace("total -$300", "total -$800"),
                          self.LINE1.replace("total +$500", "total -$300"))
        with self.assertRaises(RuntimeError) as ctx:
            fs.update_ledger(date(2026, 3, 5))
        self.assertIn("out of order", str(ctx.exception))

    def test_missing_months_heading_is_refused(self):
        with open(fs.LEDGER_FILE, "w", encoding="utf-8") as f:
            f.write("# Monthly balance\n\nno heading\n")
        with self.assertRaises(RuntimeError) as ctx:
            fs.update_ledger(date(2026, 2, 5))
        self.assertIn("## Months", str(ctx.exception))

    def test_intact_file_parses_and_extends(self):
        self.write_months(self.LINE1, self.LINE2)
        self.month("2026-03", 0, 0, 0, 20000)
        added = fs.update_ledger(date(2026, 4, 5))
        self.assertEqual(added, ["2026-03"])
        self.assertIn("balance -$200 | total -$500", self.read())


# ---------------------------------------------------------------- email

class TestEmailBody(unittest.TestCase):

    def setUp(self):
        # the gated lens's note names the two months it looked at, so the
        # body depends on the clock — pin it
        self.addCleanup(setattr, fs, "FAKE_TODAY", fs.FAKE_TODAY)
        fs.FAKE_TODAY = TODAY

    def test_full_body(self):
        rows = [row("t1", TODAY - timedelta(days=1), -1500, payee="Shop"),
                row("t2", TODAY - timedelta(days=2), 250000, payee="Employer",
                    cat="Fixed", grp="Income")]
        lenses = [{"lens": "week vs previous week",
                   "key": "week_vs_previous_week",
                   "numbers": {"spending": {"last_7_days":
                                            {"total": "-884.80",
                                             "transactions": 19}},
                               "income": {"last_7_days": None}}},
                  {"lens": fs.CATEGORY_LENS, "key": "category_trend",
                   "numbers": None}]
        body = fs.email_body(rows, ["odd A"], ["all fine"], lenses,
                             {"week_vs_previous_week": ["steady week"]}, 7)
        self.assertIn("Uncategorized: 7\n", body)
        self.assertNotIn("suggest:", body)
        self.assertIn("## New transactions\n\nChecking\n\n(no category)\n\n"
                      "- 2026-08-10 · Shop · -$15.00\n\n"
                      "Income: Fixed\n\n- 2026-08-09 · Employer · $2500.00\n",
                      body)
        self.assertIn("## Commentary — week vs previous week\n\n"
                      "- steady week\n\n"
                      "Spending\n- last 7 days: -$884.80, 19 transactions\n\n"
                      "Income\n- last 7 days: none\n", body)
        # the gated lens still gets a section, saying what it needs
        self.assertIn(f"## Commentary — {fs.CATEGORY_LENS}\n\n"
                      "- no category qualifies. A category needs at least 3 "
                      "categorized spending transactions in each of the last "
                      "two complete months (2026-07 and 2026-06).", body)
        self.assertIn("## Odd\n\n- odd A", body)
        # new transactions last, then the count line, then the links
        self.assertLess(body.index("## Odd"), body.index("## New transactions"))
        self.assertLess(body.index("## New transactions"),
                        body.index("Uncategorized: 7"))
        self.assertLess(body.index("Uncategorized: 7"),
                        body.index("Categorize:"))
        for url in (fs.PAGE_URL, fs.WEBUI_URL, fs.ACTUAL_URL):
            self.assertIn(url, body)

    def test_no_new_transactions(self):
        body = fs.email_body([], [], [], [], {}, 0)
        self.assertIn("Uncategorized: 0\n", body)
        self.assertIn("## New transactions\n\n- none since the last email\n", body)
        self.assertNotIn("## Odd", body)
        self.assertNotIn("Commentary", body)

    def test_cash_flow_block_included(self):
        block = "Budget status — 2026-08, day 12 of 31 | first month: no average yet"
        body = fs.email_body([], [], [], [], {}, 0, block)
        self.assertIn(block, body)


# ---------------------------------------------------------------- config

class TestEnvConfig(unittest.TestCase):

    def write(self, text):
        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".env",
                                                delete=False)
        self._tmp.write(text)
        self._tmp.close()
        self._saved = fs.ENV_FILE
        fs.ENV_FILE = self._tmp.name
        self.addCleanup(self.restore)

    def restore(self):
        fs.ENV_FILE = self._saved
        pathlib.Path(self._tmp.name).unlink()

    def test_model_required(self):
        self.write("# nothing\n")
        with self.assertRaises(RuntimeError):
            fs.env_config()

    def test_defaults_and_quote_stripping(self):
        self.write('FINANCE_MODEL="m1"\n# comment\nFINANCE_TEMPERATURE=0.7\n')
        cfg = fs.env_config()
        self.assertEqual(cfg["FINANCE_MODEL"], "m1")
        self.assertEqual(cfg["FINANCE_TEMPERATURE"], "0.7")
        self.assertEqual(cfg["FINANCE_URL"], "http://127.0.0.1:2130/v1")
        self.assertEqual(cfg["FINANCE_MAX_TOKENS"], "2048")


class TestLearnedNotes(unittest.TestCase):

    def with_skill(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
        tmp.write(text)
        tmp.close()
        saved = fs.SKILL_FILE
        fs.SKILL_FILE = tmp.name
        self.addCleanup(lambda: (setattr(fs, "SKILL_FILE", saved),
                                 pathlib.Path(tmp.name).unlink()))

    def test_extracts_section_until_next_heading(self):
        self.with_skill("# t\n\n## Learned notes\n- rule one\n- rule two\n"
                        "\n## Amazon\nother\n")
        self.assertEqual(fs.learned_notes(), "- rule one\n- rule two")

    def test_missing_file_or_section_empty(self):
        self.with_skill("# t\nno notes heading\n")
        self.assertEqual(fs.learned_notes(), "")
        fs.SKILL_FILE = "/nonexistent/skill.md"
        self.assertEqual(fs.learned_notes(), "")


# ---------------------------------------------------------------- main

class TestMainEmail(unittest.TestCase):
    """Report mode (--email) prints the week's new transactions exactly once:
    the next run finds them in the reported file and prints none. It posts
    nothing — scan mode owns the batch, and scan mode never touches the
    file."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._saved = {k: getattr(fs, k) for k in
                       ("env_config", "db", "read_all", "call_llm",
                        "post_json", "update_ledger", "REPORTED_FILE")}
        self._argv = sys.argv

        def restore():
            for k, v in self._saved.items():
                setattr(fs, k, v)
            sys.argv = self._argv
        self.addCleanup(restore)

        fs.REPORTED_FILE = os.path.join(self._dir.name, "reported.json")
        fs.env_config = lambda: {"FINANCE_MODEL": "m"}
        fs.db = lambda: None
        fs.read_all = lambda conn: {
            "queue": [], "total_uncat": 3, "categories": [],
            "cash_flow": "flow block", "odd": [],
            "lenses": [],
            "new": [row("t1", date.today(), -1500, payee="Shop")]}
        fs.call_llm = lambda cfg, data, notes: {
            "suggestions": [], "odd_keep": [], "summary": [],
            "commentary": {}}
        self.posted = []
        fs.post_json = lambda payload: self.posted.append(payload)
        # the ledger step runs in email mode; keep it off the real vault file
        self.ledger_runs = []
        fs.update_ledger = lambda today: (self.ledger_runs.append(today), [])[1]
        # same for the debts populate step: point it at a file that is not there
        self.addCleanup(setattr, fs.debts, "DEBTS_FILE", fs.debts.DEBTS_FILE)
        fs.debts.DEBTS_FILE = os.path.join(self._dir.name, "no-debts.md")

    def invoke(self, *args):
        sys.argv = ["finance_scan.py", *args]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fs.main()
        return out.getvalue()

    def test_reported_exactly_once(self):
        body = self.invoke("--email")
        self.assertEqual(self.ledger_runs, [date.today()])
        self.assertIn("- " + date.today().isoformat() + " · Shop · -$15.00",
                      body)
        self.assertEqual(json.loads(pathlib.Path(fs.REPORTED_FILE)
                                    .read_text()),
                         {"t1": date.today().isoformat()})
        body = self.invoke("--email")
        self.assertIn("## New transactions\n\n- none since the last email", body)
        self.assertNotIn("Shop", body)

    def test_scan_mode_never_touches_the_file(self):
        self.invoke()
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())

    def test_report_mode_posts_nothing(self):
        self.invoke("--email")
        self.assertEqual(self.posted, [])

    def test_scan_mode_posts_plain_cards(self):
        self.invoke()
        self.assertEqual(list(self.posted[0]), ["cards"])

    def _refuse(self, code):
        def post(payload):
            self.posted.append(payload)
            raise urllib.error.HTTPError(fs.BATCH_URL, code, "held", {}, None)
        fs.post_json = post

    def test_held_scan_save_is_not_a_failure(self):
        self._refuse(409)
        self.invoke()                            # returns, no SystemExit
        self.assertEqual(len(self.posted), 1)    # no error card followed
        self.assertIn("cards", self.posted[0])

    def test_other_http_errors_still_fail(self):
        self._refuse(500)
        with self.assertRaises(SystemExit) as ctx:
            self.invoke()
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.posted[1]["error"]["step"], "save")

    def test_report_failure_posts_no_error_card(self):
        def broken(conn):
            raise RuntimeError("boom")
        fs.read_all = broken
        with self.assertRaises(SystemExit) as ctx:
            self.invoke("--email")
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.posted, [])


class TestMainErrorPath(unittest.TestCase):
    """A failing step posts an error card naming the step and exits 1."""

    def test_read_failure_posts_error_card(self):
        posted = []
        saved = (fs.env_config, fs.db, fs.post_json, sys.argv)
        fs.env_config = lambda: {"FINANCE_MODEL": "m"}
        def broken_db():
            raise RuntimeError("expected one budget copy, found 0")
        fs.db = broken_db
        fs.post_json = lambda payload: posted.append(payload)
        sys.argv = ["finance_scan.py"]
        try:
            with self.assertRaises(SystemExit) as ctx:
                fs.main()
        finally:
            fs.env_config, fs.db, fs.post_json, sys.argv = saved
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(posted[0]["error"]["step"], "read")
        self.assertIn("expected one budget copy", posted[0]["error"]["message"])


if __name__ == "__main__":
    unittest.main()
