#!/usr/bin/env python3
"""Tests for services/actions/finance_jobs.py — stdlib unittest, no live data.

The pure functions (category validation, card
building, email body) run against an in-memory SQLite copy of the api-cache
schema; the main() tests patch db, the two llm functions, and post_json so
no server, LLM, or budget file is touched. Run:
python3 -m pytest test_finance_jobs.py -q (from this directory), or
python3 services/actions/test_finance_jobs.py from the repo root.
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
import finance_jobs as fs

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
        (tid, fs.cash_flow._day_int(day), -cents, payee, category))


def payee(conn, pid, name):
    conn.execute("INSERT INTO v_payees VALUES (?, ?)", (pid, name))


def tx(conn, tid, day, cents, payee=None, category=None, transfer=None,
       parent=0, starting=0, sort_order=0, account="a1"):
    """One transaction row with full control; cents signed (spend negative)."""
    conn.execute(
        "INSERT INTO v_transactions (id, date, amount, payee, category, "
        "transfer_id, is_parent, starting_balance_flag, sort_order, account) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, fs.cash_flow._day_int(day), cents, payee, category, transfer, parent,
         starting, sort_order, account))


def row(tid, day, cents, payee="Shop", account="Checking", cat=None,
        grp=None, sort_order=0, account_order=0, grp_order=0):
    """One new_transactions row, the shape the query returns."""
    return {"id": tid, "date": fs.cash_flow._day_int(day), "amount": cents,
            "sort_order": sort_order, "payee": payee, "account": account,
            "account_order": account_order, "cat": cat, "grp": grp,
            "grp_order": grp_order}


# ---------------------------------------------------------------- db

class TestDb(unittest.TestCase):
    """db()'s exactly-one-copy guard — api_cache.API_CACHE points at a temp
    dir, pull_api_cache_if_stale stubbed so nothing touches the network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_cache = fs.api_cache.API_CACHE
        self._saved_pull = fs.api_cache.pull_api_cache_if_stale
        fs.api_cache.API_CACHE = self._tmp.name
        fs.api_cache.pull_api_cache_if_stale = lambda: None

    def tearDown(self):
        fs.api_cache.API_CACHE = self._saved_cache
        fs.api_cache.pull_api_cache_if_stale = self._saved_pull
        self._tmp.cleanup()

    def make_copy(self, name):
        d = pathlib.Path(self._tmp.name) / name
        d.mkdir()
        (d / "db.sqlite").write_text("")

    def test_zero_copies_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            fs.db()
        self.assertIn("found 0", str(ctx.exception))

    def test_two_copies_rejected(self):
        self.make_copy("Budget-a")
        self.make_copy("Budget-b")
        with self.assertRaises(RuntimeError) as ctx:
            fs.db()
        self.assertIn("found 2", str(ctx.exception))


class TestExcludedGroups(unittest.TestCase):
    """Rows parked in cash_flow.EXCLUDED_GROUPS leave the odd checks
    (test_oddities.py); the new-transactions listing and the category list
    the model suggests from keep them."""

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

    def test_excluded_group_still_reaches_the_model(self):
        # parking is a normal choice, so the model has to see the group
        self.addCleanup(setattr, fs.cash_flow, "build_report",
                        fs.cash_flow.build_report)
        fs.cash_flow.build_report = lambda month="", today=None, detailed=False, \
            categories=False, combine_personal=False: ""
        self.addCleanup(setattr, fs.cash_flow, "check_report",
                        fs.cash_flow.check_report)
        fs.cash_flow.check_report = lambda today=None: []
        cats = fs.read_all(self.conn, TODAY)["categories"]
        self.assertEqual([c["grp"] for c in cats], ["Food", "Ignored"])

    def test_new_transactions_keeps_parked_rows(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c2")
        self.assertEqual([r["id"] for r in fs.new_transactions(self.conn, TODAY)],
                         ["t1"])


class TestOneOffGroups(unittest.TestCase):
    """Rows in cash_flow.ONE_OFF_GROUPS leave the odd checks the same way
    EXCLUDED_GROUPS rows do (test_oddities.py); the new-transactions
    listing keeps them."""

    def setUp(self):
        self.addCleanup(setattr, fs.cash_flow, "ONE_OFF_GROUPS",
                        fs.cash_flow.ONE_OFF_GROUPS)
        fs.cash_flow.ONE_OFF_GROUPS = ["One-off"]
        self.conn = make_conn()
        self.conn.executescript(
            "INSERT INTO category_groups (id, name) VALUES ('g1', 'Food');"
            "INSERT INTO category_groups (id, name) VALUES ('g3', 'One-off');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c1', 'Groceries', 'g1');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c3', 'Computer', 'g3');")

    def test_new_transactions_keeps_one_off_rows(self):
        spend(self.conn, "t1", TODAY - timedelta(days=1), 2500000,
              category="c3")
        self.assertEqual([r["id"] for r in fs.new_transactions(self.conn, TODAY)],
                         ["t1"])


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
                "odd": [{"kind": "large", "date": 20260810, "text": "odd A"},
                        {"kind": "duplicate", "date": 20260805,
                         "text": "odd B"}]}

    def tx(self, tid, history=(), pick="latest"):
        return {"id": tid, "date": 20260810, "amount": -1500, "payee": "Shop",
                "notes": "", "account": "Checking", "account_id": "a1",
                "pick": pick,
                "history": [{"category": h, "n": 1} for h in history]}

    def test_history_beats_llm_guesses(self):
        llm = {"suggestions": [{"transaction_id": "t1",
                                "categories": ["Home: Misc"]}]}
        cards = fs.build_cards(self.data([self.tx("t1", ["Groceries"])]), llm)
        self.assertEqual(cards[0]["suggestions"],
                         [{"category": "Groceries", "basis": "history"}])

    def test_guesses_validated_deduped_capped(self):
        llm = {"suggestions": [{"transaction_id": "t1", "categories":
                                ["Vibes", "groceries", "Groceries", "Misc",
                                 "Home: Misc", "Food: Misc"]}]}
        cards = fs.build_cards(self.data([self.tx("t1")]), llm)
        self.assertEqual(cards[0]["suggestions"],
                         [{"category": "Groceries", "basis": "guess"},
                          {"category": "Home: Misc", "basis": "guess"},
                          {"category": "Food: Misc", "basis": "guess"}])

    def test_card_carries_its_pick(self):
        cards = fs.build_cards(self.data([self.tx("t1")]), {"suggestions": []})
        self.assertEqual(cards[0]["pick"], "latest")

    def test_summary_capped(self):
        llm = {"summary": [f"s{i}" for i in range(6)] + ["x" * 400]}
        summary = fs.build_sentences(llm)
        self.assertEqual(summary, ["s0", "s1", "s2", "s3"])
        llm["summary"] = ["x" * 400]
        summary = fs.build_sentences(llm)
        self.assertEqual(len(summary[0]), 300)

    def test_summary_cap_keeps_whole_sentences(self):
        long = ("one. " + "x" * 250 + ". " + "y" * 100)   # boundary at 256
        summary = fs.build_sentences({"summary": [long]})
        self.assertEqual(summary[0], long[:256])

    def test_summary_drops_leaked_json_keys(self):
        llm = {"summary": ["real sentence",
                           "summary: [{",
                           "suggestions: [{",
                           "odd_keep: [1, 2]",
                           "leaked_snake_case_key:"]}
        summary = fs.build_sentences(llm)
        self.assertEqual(summary, ["real sentence"])


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


class TestReadAllKinds(unittest.TestCase):
    """read_all's per-kind reads: the daily's outliers leave out the ids the
    actions page's seen key wrote; the weekly reads the cash-flow block with
    category rows and no lists; a month reads its block and its outliers."""

    def setUp(self):
        self.conn = make_conn()
        self.conn.executescript(
            "INSERT INTO category_groups (id, name) VALUES ('g1', 'Food');"
            "INSERT INTO categories (id, name, cat_group) "
            "VALUES ('c1', 'Groceries', 'g1');"
            "INSERT INTO v_payees (id, name) VALUES ('p1', 'Store');")
        # two charges of $600: both flagged large
        spend(self.conn, "big1", TODAY - timedelta(days=1), 60000, payee="p1",
              category="c1")
        spend(self.conn, "big2", TODAY - timedelta(days=2), 60000, payee="p1",
              category="c1")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, fs, "ODD_SEEN_FILE", fs.ODD_SEEN_FILE)
        fs.ODD_SEEN_FILE = os.path.join(self._tmp.name, "seen.json")
        self.builds = []
        for name, stub in (("build_report", self.build_report),
                           ("check_report", lambda today=None: ["item"])):
            self.addCleanup(setattr, fs.cash_flow, name,
                            getattr(fs.cash_flow, name))
            setattr(fs.cash_flow, name, stub)

    def build_report(self, month="", today=None, detailed=False,
                     categories=False, combine_personal=False):
        self.builds.append((month, categories))
        return "block"

    def test_daily_drops_seen_outliers_and_reads_the_check(self):
        pathlib.Path(fs.ODD_SEEN_FILE).write_text(
            json.dumps({"big2": (TODAY - timedelta(days=2)).isoformat()}))
        data = fs.read_all(self.conn, TODAY)
        # every flag of the seen charge goes, every flag of the other stays
        self.assertEqual({o["transaction_id"] for o in data["odd"]}, {"big1"})
        self.assertEqual(data["check"], ["item"])
        self.assertEqual(data["cash_flow"], "")
        self.assertEqual(self.builds, [])
        self.assertEqual([t["id"] for t in data["new"]], ["big1", "big2"])

    def test_missing_or_junk_seen_file_drops_nothing(self):
        both = {"big1", "big2"}
        data = fs.read_all(self.conn, TODAY)
        self.assertEqual({o["transaction_id"] for o in data["odd"]}, both)
        pathlib.Path(fs.ODD_SEEN_FILE).write_text("junk")
        data = fs.read_all(self.conn, TODAY)
        self.assertEqual({o["transaction_id"] for o in data["odd"]}, both)

    def test_weekly_reads_the_block_with_categories_and_no_lists(self):
        data = fs.read_all(self.conn, TODAY, weekly=True)
        self.assertEqual(data["cash_flow"], "block")
        self.assertEqual(self.builds, [("", True)])
        self.assertEqual((data["odd"], data["new"], data["check"]),
                         ([], [], []))

    def test_month_reads_its_block_and_outliers_only(self):
        data = fs.read_all(self.conn, TODAY, month="2026-08")
        self.assertEqual(self.builds, [("2026-08", False)])
        self.assertEqual({o["transaction_id"] for o in data["odd"]},
                         {"big1", "big2"})
        self.assertEqual((data["new"], data["check"]), ([], []))


# ---------------------------------------------------------------- email

class TestEmailBody(unittest.TestCase):

    def test_daily_check_section(self):
        body = fs.email_body([], [], ["quiet day"], "", ["uncategorized: 2 "
                             "transactions ($141 out)"], TODAY)
        self.assertEqual(body, f"# Finance daily — {TODAY}\n\n"
                         "## Summary\n\n- quiet day\n\n"
                         "## Check\n\n- uncategorized: 2 transactions "
                         "($141 out)\n\n"
                         "## New transactions\n\n- none since the last email\n")
        # a daily with nothing to check has no Check section
        self.assertNotIn("Check", fs.email_body([], [], [], "", [], TODAY))

    def test_weekly_body(self):
        block = "2026-08, day 12 of 31 | first month: no average yet"
        body = fs.email_body([row("t1", TODAY, -1500)], ["odd A"], ["fine"],
                             block, ["item"], TODAY, weekly=True)
        self.assertEqual(body, f"# Finance weekly — {TODAY}\n\n"
                         "## Summary\n\n- fine\n\n"
                         f"## Cash flow\n\n{block}\n\n")

    def test_month_has_no_check(self):
        body = fs.email_body([], [], [], "block", ["item"], TODAY,
                             month="2026-08")
        self.assertNotIn("Check", body)

    def test_full_body(self):
        rows = [row("t1", TODAY - timedelta(days=1), -1500, payee="Shop"),
                row("t2", TODAY - timedelta(days=2), 250000, payee="Employer",
                    cat="Fixed", grp="Income")]
        body = fs.email_body(rows, [{"kind": "large", "date": 20260810,
                                     "text": "odd A"}], ["all fine"], "",
                             [], TODAY)
        self.assertTrue(body.startswith(f"# Finance daily — {TODAY}\n"))
        self.assertNotIn("suggest:", body)
        self.assertIn("## New transactions\n\nChecking\n\n(no category)\n\n"
                      "- 2026-08-10 · Shop · -$15.00\n\n"
                      "Income: Fixed\n\n- 2026-08-09 · Employer · $2500.00\n",
                      body)
        self.assertIn("## Outliers in last week\n\n2026-08-10:\n"
                      "- odd A", body)
        # the emailed body ends at the new-transactions section: no links —
        # that part is an actions-page preview only
        self.assertNotIn("## Links", body)
        for url in (fs.PAGE_URL, fs.WEBUI_URL, fs.ACTUAL_URL):
            self.assertNotIn(url, body)
        # outliers before new transactions
        self.assertLess(body.index("## Outliers"),
                        body.index("## New transactions"))

    def test_outliers_grouped_by_date(self):
        odd = [{"kind": "new_payee", "date": 20260806,
                "text": "new payee: Shop $10.00 on 2026-08-06"},
               {"kind": "large", "date": 20260811,
                "text": "unusually large charge: Store $500.00 on 2026-08-11"},
               {"kind": "duplicate", "date": 20260811,
                "text": "possible duplicate charge: Cafe $5.00 on 2026-08-11 "
                        "appears 2x within 3 days"}]
        body = fs.email_body([], odd, [], "", [], TODAY)
        self.assertIn("## Outliers in last week\n\n2026-08-11:\n"
                      "- unusually large charge: Store $500.00\n"
                      "- possible duplicate charge: Cafe $5.00 "
                      "appears 2x within 3 days\n\n"
                      "2026-08-06:\n"
                      "- new payee: Shop $10.00\n", body)

    def test_no_new_transactions(self):
        body = fs.email_body([], [], [], "", [], TODAY)
        self.assertIn("## New transactions\n\n- none since the last email\n",
                      body)
        self.assertNotIn("Outliers", body)

    def test_daily_has_no_cash_flow_block(self):
        block = "Budget status — 2026-08, day 12 of 31 | first month: no average yet"
        body = fs.email_body([], [], [], block, [], TODAY)
        self.assertNotIn(block, body)
        self.assertNotIn("Cash flow", body)

    def test_section_prints_that_component_alone(self):
        block = "Budget status — 2026-08"
        body = fs.email_body([], ["odd A"], ["all fine"], block, [], TODAY,
                             section="cashflow")
        self.assertEqual(body, f"## Cash flow\n\n{block}\n\n")

    def test_section_has_no_title(self):
        body = fs.email_body([], [], [], "", [], TODAY, section="links")
        self.assertNotIn("# Finance daily", body)
        self.assertIn("Categorize:", body)

    def test_past_month_body(self):
        odd = [{"kind": "large", "date": 20260811,
                "text": "unusually large charge: Store $500.00 on 2026-08-11"}]
        body = fs.email_body([row("t1", TODAY, -1500)], odd, ["fine"],
                             "block", [], TODAY, month="2026-08")
        self.assertTrue(body.startswith("# Finance — 2026-08\n"))
        self.assertIn("## Outliers in 2026-08\n\n2026-08-11:\n"
                      "- unusually large charge: Store $500.00\n", body)
        # the month has no new-transactions section, even with rows given
        self.assertNotIn("New transactions", body)
        self.assertNotIn("Shop", body)
        self.assertIn("## Summary\n\n- fine\n", body)
        self.assertIn("## Cash flow\n\nblock\n", body)

    def test_new_section_prints_alone(self):
        body = fs.email_body([row("t1", TODAY, -1500)], [], [], "", [], TODAY,
                             section="new")
        self.assertIn("## New transactions", body)
        # the count line is gone: the cash-flow block's Check section
        # carries the uncategorized count
        self.assertNotIn("Uncategorized", body)
        self.assertNotIn("## Links", body)


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
        self.assertEqual(cfg["FINANCE_URL"], "https://openrouter.ai/api/v1")
        self.assertEqual(cfg["FINANCE_KEY_ENV"], "OPENROUTER_API_KEY")
        self.assertEqual(cfg["FINANCE_MAX_TOKENS"], "4096")


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
                       ("env_config", "db", "read_all", "llm_category_guess",
                        "llm_report_sentences", "post_json", "check_scan",
                        "update_ledger", "REPORTED_FILE")}
        self._argv = sys.argv

        def restore():
            for k, v in self._saved.items():
                setattr(fs, k, v)
            sys.argv = self._argv
        self.addCleanup(restore)

        fs.REPORTED_FILE = os.path.join(self._dir.name, "reported.json")
        fs.env_config = lambda: {"FINANCE_MODEL": "m"}
        fs.db = lambda: None
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [], "total_uncat": 3, "categories": [],
            "cash_flow": "flow block", "check": [], "odd": [],
            "new": [row("t1", date.today(), -1500, payee="Shop")]}
        fs.llm_category_guess = lambda cfg, data, notes: {"suggestions": []}
        fs.llm_report_sentences = lambda cfg, data, notes, month="", weekly=False: {"summary": []}
        self.posted = []
        fs.post_json = lambda url, payload: self.posted.append(payload)
        self.checked = []
        fs.check_scan = lambda candidates: (
            self.checked.append(candidates),
            {"proceed": True, "reason": "stub"})[1]
        # the ledger step runs in email mode; keep it off the real vault file
        self.ledger_runs = []
        fs.update_ledger = lambda today: (self.ledger_runs.append(today), [])[1]
        # same for the debts populate step: point it at files that are not
        # there
        self.addCleanup(setattr, fs.debts, "DEBTS_FILE", fs.debts.DEBTS_FILE)
        self.addCleanup(setattr, fs.debts, "ASSETS_FILE", fs.debts.ASSETS_FILE)
        fs.debts.DEBTS_FILE = os.path.join(self._dir.name, "no-debts.md")
        fs.debts.ASSETS_FILE = os.path.join(self._dir.name, "no-assets.md")

    def invoke(self, *args):
        sys.argv = ["finance_jobs.py", *args]
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

    def test_no_llm_section_skips_llm_and_writes_nothing(self):
        llm_calls = []
        fs.llm_report_sentences = lambda cfg, data, notes, month="", weekly=False: llm_calls.append(1)
        body = self.invoke("--email", "--weekly", "--section", "cashflow")
        self.assertEqual(body, "## Cash flow\n\nflow block\n\n")
        self.assertEqual(llm_calls, [])
        self.assertEqual(self.ledger_runs, [])
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())

    def test_weekly_body_writes_nothing(self):
        body = self.invoke("--email", "--weekly")
        self.assertTrue(body.startswith(f"# Finance weekly — {date.today()}\n"))
        self.assertIn("## Cash flow\n\nflow block\n", body)
        self.assertNotIn("New transactions", body)
        self.assertNotIn("Shop", body)
        self.assertEqual(self.ledger_runs, [])
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())

    def test_weekly_with_a_past_month_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            self.invoke("--email", "--weekly", "--month", "2000-01")
        self.assertNotEqual(ctx.exception.code, 0)
        # the current month is today's report, so weekly is fine with it
        body = self.invoke("--email", "--weekly", "--month",
                           date.today().strftime("%Y-%m"))
        self.assertIn("# Finance weekly", body)

    def test_section_outside_its_kind_is_refused(self):
        for args in (("--email", "--section", "cashflow"),
                     ("--email", "--weekly", "--section", "new"),
                     ("--email", "--weekly", "--section", "outliers"),
                     ("--email", "--month", "2000-01", "--section", "check")):
            with self.assertRaises(SystemExit) as ctx:
                self.invoke(*args)
            self.assertNotEqual(ctx.exception.code, 0, args)
        # links is a page-only part of every kind
        for args in (("--email", "--section", "links"),
                     ("--email", "--weekly", "--section", "links")):
            self.assertIn("Categorize:", self.invoke(*args))

    def test_daily_llm_reads_the_lists_weekly_the_block(self):
        got = []
        fs.llm_report_sentences = lambda cfg, report, notes, month="", weekly=False: (
            got.append((report, month, weekly)), {"summary": []})[1]
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [], "total_uncat": 3, "categories": [],
            "cash_flow": "flow block", "check": ["uncategorized: 1"],
            "odd": [{"kind": "large", "date": 20260810, "text": "odd A"}],
            "new": [row("t1", date.today(), -1500, payee="Shop")]}
        self.invoke("--email")
        report, month, weekly = got[0]
        self.assertEqual((month, weekly), ("", False))
        self.assertIn("## Check\n\n- uncategorized: 1\n", report)
        self.assertIn("- odd A", report)
        self.assertIn("Shop", report)
        self.assertNotIn("flow block", report)
        self.invoke("--email", "--weekly")
        self.assertEqual(got[1], ("flow block", "", True))

    def test_outliers_section_is_data_only(self):
        llm_calls = []
        fs.llm_report_sentences = lambda cfg, data, notes, month="", weekly=False: llm_calls.append(1)
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [], "total_uncat": 3, "categories": [],
            "cash_flow": "flow block", "check": [],
            "odd": [{"kind": "large", "date": 20260810, "text": "odd A"}],
            "new": []}
        body = self.invoke("--email", "--section", "outliers")
        self.assertIn("odd A", body)
        self.assertEqual(llm_calls, [])

    def test_llm_section_calls_llm_and_writes_nothing(self):
        fs.llm_report_sentences = lambda cfg, data, notes, month="", weekly=False: {"summary": ["fine"]}
        body = self.invoke("--email", "--section", "summary")
        self.assertEqual(body, "## Summary\n\n- fine\n\n")
        self.assertEqual(self.ledger_runs, [])
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())

    def test_new_section_never_consumes_the_rows(self):
        body = self.invoke("--email", "--section", "new")
        self.assertIn("Shop", body)
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())
        body = self.invoke("--email", "--section", "new")
        self.assertIn("Shop", body)

    def test_bad_or_email_less_section_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            self.invoke("--email", "--section", "junk")
        self.assertNotEqual(ctx.exception.code, 0)
        with self.assertRaises(SystemExit):
            self.invoke("--section", "cashflow")

    def test_flags_reach_read_all(self):
        seen = []
        base = fs.read_all

        def read_all(conn, today, month="", weekly=False, detailed=False,
                     categories=False, combine_personal=False):
            seen.append((today, month, weekly, detailed, categories,
                         combine_personal))
            return base(conn, today)
        fs.read_all = read_all
        self.invoke("--email", "--weekly", "--detailed", "--categories",
                    "--combine-personal", "--section", "cashflow")
        self.assertEqual(seen, [(date.today(), "", True, True, True, True)])
        for flag in ("--weekly", "--detailed", "--categories",
                     "--combine-personal", "--preview"):
            with self.assertRaises(SystemExit):
                self.invoke(flag)               # scan mode has no report

    def test_preview_prints_the_body_and_writes_nothing(self):
        body = self.invoke("--email", "--preview")
        self.assertTrue(body.startswith(f"# Finance daily — {date.today()}\n"))
        self.assertNotIn("test data", body)
        self.assertIn("Shop", body)
        self.assertEqual(self.ledger_runs, [])
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())
        # nothing was marked printed, so the row is still new
        self.assertIn("Shop", self.invoke("--email", "--preview"))

    def test_past_month_reaches_read_all_and_writes_nothing(self):
        seen = []
        base = fs.read_all

        def read_all(conn, today, month="", weekly=False, detailed=False,
                     categories=False, combine_personal=False):
            seen.append(month)
            return base(conn, today)
        fs.read_all = read_all
        asked = []
        fs.llm_report_sentences = lambda cfg, data, notes, month="", weekly=False: (
            asked.append(month), {"summary": ["a fine month"]})[1]
        body = self.invoke("--email", "--month", "2026-08")
        self.assertEqual(seen, ["2026-08"])
        self.assertEqual(asked, ["2026-08"])
        self.assertTrue(body.startswith("# Finance — 2026-08\n"))
        self.assertIn("- a fine month", body)
        self.assertNotIn("New transactions", body)
        self.assertEqual(self.ledger_runs, [])
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())

    def test_current_month_is_todays_report(self):
        seen = []
        base = fs.read_all

        def read_all(conn, today, month="", weekly=False, detailed=False,
                     categories=False, combine_personal=False):
            seen.append(month)
            return base(conn, today)
        fs.read_all = read_all
        body = self.invoke("--email", "--preview", "--month",
                           date.today().strftime("%Y-%m"))
        self.assertEqual(seen, [""])
        self.assertIn("# Finance daily", body)

    def test_bad_month_and_new_section_of_a_past_month_are_refused(self):
        for args in (("--email", "--month", "2026-8"),
                     ("--month", "2026-08"),
                     ("--email", "--month", "2000-01", "--section", "new")):
            with self.assertRaises(SystemExit) as ctx:
                self.invoke(*args)
            self.assertNotEqual(ctx.exception.code, 0, args)

    def test_report_mode_posts_nothing(self):
        self.invoke("--email")
        self.assertEqual(self.posted, [])

    def test_scan_mode_posts_plain_cards(self):
        self.invoke()
        self.assertEqual(list(self.posted[0]), ["cards"])

    def test_scan_mode_checks_with_the_queues_ids(self):
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [{"id": "q1", "pick": "latest"},
                      {"id": "q2", "pick": "latest"}],
            "total_uncat": 3, "categories": [], "cash_flow": "flow block", "check": [],
            "odd": [], "new": []}
        fs.check_scan = lambda candidates: (
            self.checked.append(candidates),
            {"proceed": False, "reason": "stub"})[1]
        self.invoke()
        self.assertEqual(self.checked,
                         [[{"transaction_id": "q1", "pick": "latest"},
                           {"transaction_id": "q2", "pick": "latest"}]])

    def test_scan_skip_ends_before_the_llm(self):
        llm_calls = []
        fs.llm_category_guess = lambda cfg, data, notes, month="", weekly=False: llm_calls.append(1)
        fs.check_scan = lambda candidates: {"proceed": False, "reason": "full"}
        self.invoke()                            # returns, no SystemExit
        self.assertEqual(llm_calls, [])
        self.assertEqual(self.posted, [])

    def test_report_mode_never_checks(self):
        self.invoke("--email")
        self.assertEqual(self.checked, [])

    def queue_row(self, history):
        return {"id": "t1", "date": fs.cash_flow._day_int(date.today()), "amount": -500,
                "payee": "Shop", "notes": "", "account": "Checking",
                "account_id": "a1", "pick": "latest", "history": history}

    def test_scan_all_history_skips_the_llm(self):
        llm_calls = []
        fs.llm_category_guess = lambda cfg, data, notes, month="", weekly=False: llm_calls.append(1)
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [self.queue_row([{"category": "Food", "n": 4}])],
            "total_uncat": 1, "categories": [], "cash_flow": "flow block", "check": [],
            "odd": [], "new": []}
        self.invoke()
        self.assertEqual(llm_calls, [])
        self.assertEqual(self.posted[0]["cards"][0]["suggestions"],
                         [{"category": "Food", "basis": "history"}])

    def test_scan_history_less_payee_still_calls_the_llm(self):
        llm_calls = []

        def llm(cfg, data, notes):
            llm_calls.append(1)
            return {"suggestions": []}
        fs.llm_category_guess = llm
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [self.queue_row([])],
            "total_uncat": 1, "categories": [], "cash_flow": "flow block", "check": [],
            "odd": [], "new": []}
        self.invoke()
        self.assertEqual(llm_calls, [1])

    def test_email_mode_calls_the_llm_with_all_history(self):
        llm_calls = []

        def llm(cfg, data, notes, month="", weekly=False):
            llm_calls.append(1)
            return {"summary": []}
        fs.llm_report_sentences = llm
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [self.queue_row([{"category": "Food", "n": 4}])],
            "total_uncat": 1, "categories": [], "cash_flow": "flow block", "check": [],
            "odd": [], "new": []}
        self.invoke("--email")
        self.assertEqual(llm_calls, [1])

    def _refuse(self, code):
        def post(url, payload):
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
        def broken(conn, detailed=False):
            raise RuntimeError("boom")
        fs.read_all = broken
        with self.assertRaises(SystemExit) as ctx:
            self.invoke("--email")
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.posted, [])


class TestMainSend(unittest.TestCase):
    """Report mode --send mails the body through the send_mail helper instead
    of printing it: stdout stays empty, the helper gets the subject as argv
    and the body on stdin, and the reported file updates only after the send
    succeeded."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._saved = {k: getattr(fs, k) for k in
                       ("env_config", "db", "read_all",
                        "llm_report_sentences", "post_json", "update_ledger",
                        "REPORTED_FILE")}
        self._argv = sys.argv
        self._run = fs.subprocess.run

        def restore():
            for k, v in self._saved.items():
                setattr(fs, k, v)
            sys.argv = self._argv
            fs.subprocess.run = self._run
        self.addCleanup(restore)

        fs.REPORTED_FILE = os.path.join(self._dir.name, "reported.json")
        fs.env_config = lambda: {"FINANCE_MODEL": "m"}
        fs.db = lambda: None
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, categories=False, combine_personal=False: {
            "queue": [], "total_uncat": 3, "categories": [],
            "cash_flow": "flow block", "check": [], "odd": [],
            "new": [row("t1", date.today(), -1500, payee="Shop")]}
        fs.llm_report_sentences = lambda cfg, data, notes, month="", weekly=False: {"summary": []}
        fs.post_json = lambda url, payload: None
        self.ledger_runs = []
        fs.update_ledger = lambda today: (self.ledger_runs.append(today), [])[1]
        self.addCleanup(setattr, fs.debts, "DEBTS_FILE", fs.debts.DEBTS_FILE)
        self.addCleanup(setattr, fs.debts, "ASSETS_FILE", fs.debts.ASSETS_FILE)
        fs.debts.DEBTS_FILE = os.path.join(self._dir.name, "no-debts.md")
        fs.debts.ASSETS_FILE = os.path.join(self._dir.name, "no-assets.md")
        # the helper subprocess, stubbed: records argv + stdin, exits 0
        self.exit_code = 0
        self.calls = []

        def fake_run(argv, input=None, **kw):
            self.calls.append({"argv": argv, "input": input})
            return fs.subprocess.CompletedProcess(
                argv, self.exit_code, "", "boom" if self.exit_code else "")
        fs.subprocess.run = fake_run

    def invoke(self, *args):
        sys.argv = ["finance_jobs.py", *args]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fs.main()
        return out.getvalue(), err.getvalue()

    def test_success_prints_nothing_and_saves_reported(self):
        out, _ = self.invoke("--send")
        self.assertEqual(out, "")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["argv"],
                         [fs.JMAP_PYTHON, fs.SEND_MAIL,
                          f"Finance — {date.today().isoformat()}",
                          fs.DAILY_RECIPIENT])
        self.assertIn("# Finance daily", call["input"])
        self.assertIn("- " + date.today().isoformat() + " · Shop · -$15.00",
                      call["input"])
        self.assertEqual(json.loads(pathlib.Path(fs.REPORTED_FILE)
                                    .read_text()),
                         {"t1": date.today().isoformat()})
        self.assertEqual(self.ledger_runs, [date.today()])

    def test_helper_failure_exits_nonzero_and_marks_nothing(self):
        self.exit_code = 1
        with self.assertRaises(SystemExit) as ctx:
            self.invoke("--send")
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())

    def test_preview_send_still_sends_but_writes_nothing(self):
        out, _ = self.invoke("--send", "--preview")
        self.assertEqual(out, "")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["argv"][2],
                         f"Finance — {date.today().isoformat()}")
        self.assertIn("# Finance daily", self.calls[0]["input"])
        self.assertFalse(pathlib.Path(fs.REPORTED_FILE).exists())
        self.assertEqual(self.ledger_runs, [])


class TestMainErrorPath(unittest.TestCase):
    """A failing step posts an error card naming the step and exits 1."""

    def test_read_failure_posts_error_card(self):
        posted = []
        saved = (fs.env_config, fs.db, fs.post_json, sys.argv)
        fs.env_config = lambda: {"FINANCE_MODEL": "m"}
        def broken_db():
            raise RuntimeError("expected one budget copy, found 0")
        fs.db = broken_db
        fs.post_json = lambda url, payload: posted.append(payload)
        sys.argv = ["finance_jobs.py"]
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
