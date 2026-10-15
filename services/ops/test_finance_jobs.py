#!/usr/bin/env python3
"""Tests for services/ops/finance_jobs.py — stdlib unittest, no live data.

Read tests use an in-memory SQLite copy of the api-cache schema. The main()
tests patch db, read_all, llm_category_guess, post_json and check_scan, so no
server, model, or budget file is touched. Run:
python3 -m pytest test_finance_jobs.py -q (from this directory), or
python3 services/ops/test_finance_jobs.py from the repo root.
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
from unittest import mock
import urllib.error
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import finance_jobs as fs

TODAY = date(2026, 8, 11)


def make_conn():
    """In-memory db with the tables the scan reads. v_transactions and
    v_payees are views in the budget app; same-shaped tables read identically."""
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
        "CREATE TABLE rules (id TEXT, stage TEXT, conditions TEXT, actions TEXT, "
        "tombstone INT DEFAULT 0, conditions_op TEXT DEFAULT 'and');"
        "CREATE TABLE category_groups (id TEXT, name TEXT, "
        "tombstone INT DEFAULT 0, hidden INT DEFAULT 0, is_income INT DEFAULT 0, "
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
    """db()'s exactly-one-copy guard — the api-cache variable points at a temp
    dir, pull_api_cache_if_stale stubbed so nothing touches the network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"ASSISTANT_BUDGET_API_CACHE": self._tmp.name})
        self._env.start()
        self._saved_pull = fs.api_cache.pull_api_cache_if_stale
        fs.api_cache.pull_api_cache_if_stale = lambda: None

    def tearDown(self):
        self._env.stop()
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
        # read_all reads the seen file from the state folder: an empty one
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ASSISTANT_OPS_STATE": tmp.name})
        env.start()
        self.addCleanup(env.stop)
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
        for text in ("spending hit $1,000 this month",
                     "a $250.00 charge at the hardware store",
                     "leftover -$500", "$500 monthly"):
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
                "odd": [{"kind": "amount", "date": 20260810, "text": "odd A"},
                        {"kind": "duplicate", "date": 20260805,
                         "text": "odd B"}]}

    def tx(self, tid, history=()):
        return {"id": tid, "date": 20260810, "amount": -1500, "payee": "Shop",
                "notes": "", "account": "Checking", "account_id": "a1",
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

    def test_summary_capped(self):
        llm = {"summary": [f"s{i}" for i in range(6)] + ["x" * 400]}
        summary = fs.build_sentences(llm)
        self.assertEqual(summary, ["s0", "s1", "s2"])
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
                           "leaked_snake_case_key:",
                           "flag: above its usual range",
                           "Routine: Acme, every 2 weeks"]}
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

    def test_accounts_by_app_order_then_name(self):
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
    """The printed ids a family emails item keeps: merge and purge."""

    def test_merge_adds_fresh_and_purges_aged_out(self):
        reported = {"old": (TODAY - timedelta(days=8)).isoformat(),
                    "edge": (TODAY - timedelta(days=7)).isoformat()}
        out = fs.merge_reported(reported, [row("new", TODAY, -100)], TODAY)
        self.assertEqual(out,
                         {"edge": (TODAY - timedelta(days=7)).isoformat(),
                          "new": TODAY.isoformat()})


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
        # two charges of $600 from a payee first seen this week: a
        # duplicate pair (reported on big1) and two new-payee flags
        spend(self.conn, "big1", TODAY - timedelta(days=1), 60000, payee="p1",
              category="c1")
        spend(self.conn, "big2", TODAY - timedelta(days=2), 60000, payee="p1",
              category="c1")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ASSISTANT_OPS_STATE": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
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
        fs.odd_seen_file().write_text(
            json.dumps({"big2": (TODAY - timedelta(days=2)).isoformat()}))
        data = fs.read_all(self.conn, TODAY)
        # every flag of the seen charge goes, every flag of the other stays
        self.assertEqual({o["transaction_id"] for o in data["odd"]}, {"big1"})
        self.assertEqual(data["check"], ["item"])
        self.assertEqual(data["cash_flow"], "")
        self.assertEqual(self.builds, [])
        self.assertEqual([t["id"] for t in data["new"]], ["big1", "big2"])

    def test_full_window_keeps_seen_outliers(self):
        fs.odd_seen_file().write_text(
            json.dumps({"big2": (TODAY - timedelta(days=2)).isoformat()}))
        data = fs.read_all(self.conn, TODAY, full_window=True)
        self.assertEqual({o["transaction_id"] for o in data["odd"]},
                         {"big1", "big2"})

    def test_missing_or_junk_seen_file_drops_nothing(self):
        both = {"big1", "big2"}
        data = fs.read_all(self.conn, TODAY)
        self.assertEqual({o["transaction_id"] for o in data["odd"]}, both)
        fs.odd_seen_file().write_text("junk")
        data = fs.read_all(self.conn, TODAY)
        self.assertEqual({o["transaction_id"] for o in data["odd"]}, both)

    def test_show_new_payees_off_drops_their_flags_and_marks(self):
        data = fs.read_all(self.conn, TODAY)
        self.assertIn("new_payee", {o["kind"] for o in data["odd"]})
        self.assertTrue(any("new payee" in r["mark"] for r in data["new"]))
        for month in ("", "2026-08"):
            data = fs.read_all(self.conn, TODAY, month=month, show_new_payees=False)
            self.assertEqual({o["kind"] for o in data["odd"]}, {"duplicate"})
        data = fs.read_all(self.conn, TODAY, show_new_payees=False)
        self.assertFalse(any("new payee" in r["mark"] for r in data["new"]))

    def test_weekly_reads_the_block_with_categories_and_no_lists(self):
        data = fs.read_all(self.conn, TODAY, weekly=True)
        self.assertEqual(data["cash_flow"], "block")
        self.assertEqual(self.builds, [("", True)])
        self.assertEqual((data["odd"], data["new"], data["check"]),
                         ([], [], []))

    def test_month_reads_its_block_and_outliers_only(self):
        data = fs.read_all(self.conn, TODAY, month="2026-08")
        self.assertEqual(self.builds, [("2026-08", True)])
        self.assertEqual({o["transaction_id"] for o in data["odd"]},
                         {"big1", "big2"})
        self.assertEqual((data["new"], data["check"]), ([], []))


# ---------------------------------------------------------------- email

def parts(new=(), odd=(), check=(), cash_flow=""):
    """The rows email_body reads, in read_all's shape."""
    return {"new": list(new), "odd": list(odd), "check": list(check), "cash_flow": cash_flow}


class TestEmailBody(unittest.TestCase):

    def test_check_section(self):
        body = fs.email_body("check", parts(check=["House: no Jul 2026 balance — using Apr 2026"]))
        self.assertEqual(body, "## Check\n\n- House: no Jul 2026 balance — using Apr 2026\n\n")
        # nothing to check prints nothing
        self.assertEqual(fs.email_body("check", parts()).strip(), "")

    def test_summary_and_cash_flow_sections(self):
        block = "for Aug 2026, day 12 of 31 | first month: no average yet"
        self.assertEqual(fs.email_body("summary", parts(), ["fine"]), "## Summary\n\n- fine\n\n")
        self.assertEqual(fs.email_body("cashflow", parts(odd=["odd A"], cash_flow=block), ["fine"]),
                         f"## Cash flow\n\n{block}\n\n")

    def test_new_section(self):
        rows = [row("t1", TODAY - timedelta(days=1), -1500, payee="Shop"),
                row("t2", TODAY - timedelta(days=2), 250000, payee="Employer",
                    cat="Fixed", grp="Income")]
        body = fs.email_body("new", parts(new=rows))
        self.assertNotIn("suggest:", body)
        self.assertIn("## Transactions since the last email\n\n### Checking\n\n(no category)\n\n"
                      "- 2026-08-10 · Shop · -$15.00\n\n"
                      "Income: Fixed\n\n- 2026-08-09 · Employer · $2500.00\n",
                      body)
        # a marked row prints its mark after the amount
        fs.mark_rows(rows, [{"transaction_id": "t1", "reason": "new payee"},
                            {"transaction_id": "t1", "reason": "possible duplicate"}],
                     {"t2": "routine: Employer, monthly, usual amount"})
        marked = fs.email_body("new", parts(new=rows))
        self.assertIn("- 2026-08-10 · Shop · -$15.00 — flag: new payee; possible duplicate\n", marked)
        self.assertIn("- 2026-08-09 · Employer · $2500.00 — routine: Employer, monthly, usual amount\n", marked)
        fs.mark_rows(rows, [], {})
        self.assertIn("- 2026-08-10 · Shop · -$15.00\n", fs.email_body("new", parts(new=rows)))
        # the uncategorized count belongs to the cash-flow block's Check
        self.assertNotIn("Uncategorized", body)

    def test_full_window_note_under_the_daily_lists_only(self):
        odd = [{"kind": "amount", "date": 20260810, "text": "odd A"}]
        rows = [row("t1", TODAY - timedelta(days=1), -1500, payee="Shop")]
        self.assertNotIn(fs.FULL_WINDOW_NOTE, fs.email_body("outliers", parts(odd=odd)))
        self.assertIn("## Outliers in last week\n\n" + fs.FULL_WINDOW_NOTE + "\n\n2026-08-10:\n",
                      fs.email_body("outliers", parts(odd=odd), full_window=True))
        self.assertIn("## Transactions since the last email\n\n" + fs.FULL_WINDOW_NOTE
                      + "\n\n### Checking\n", fs.email_body("new", parts(new=rows), full_window=True))
        self.assertEqual(fs.email_body("check", parts(check=["item"]), full_window=True),
                         "## Check\n\n- item\n\n")
        # a month's outliers cover the whole month, so no note
        self.assertNotIn(fs.FULL_WINDOW_NOTE,
                         fs.email_body("outliers", parts(odd=odd), month="2026-08", full_window=True))

    def test_outliers_grouped_by_date(self):
        odd = [{"kind": "new_payee", "date": 20260806,
                "text": "new payee: Shop $10.00 on 2026-08-06"},
               {"kind": "amount", "date": 20260811,
                "text": "above its usual range: Store $500.00 on 2026-08-11 (high $200.00 in the last year)"},
               {"kind": "duplicate", "date": 20260811,
                "text": "possible duplicate charge: Cafe $5.00 on 2026-08-11 "
                        "appears 2x within 3 days"}]
        body = fs.email_body("outliers", parts(odd=odd))
        self.assertIn("## Outliers in last week\n\n2026-08-11:\n"
                      "- above its usual range: Store $500.00 (high $200.00 in the last year)\n"
                      "- possible duplicate charge: Cafe $5.00 "
                      "appears 2x within 3 days\n\n"
                      "2026-08-06:\n"
                      "- new payee: Shop $10.00\n", body)
        # none flagged prints nothing
        self.assertEqual(fs.email_body("outliers", parts()).strip(), "")

    def test_no_new_transactions(self):
        self.assertIn("## Transactions since the last email\n\n- none since the last email\n",
                      fs.email_body("new", parts()))

    def test_links_section(self):
        body = fs.email_body("links", parts())
        self.assertNotIn("# Finance", body)
        self.assertIn(f"Categorize: {fs.PAGE_URL}", body)
        self.assertIn(f"Budget: {fs.BUDGET_URL}", body)

    def test_month_outliers_heading(self):
        odd = [{"kind": "amount", "date": 20260811,
                "text": "above its usual range: Store $500.00 on 2026-08-11 (high $200.00 in the last year)"}]
        body = fs.email_body("outliers", parts(odd=odd), month="2026-08")
        self.assertIn("## Outliers in Aug 2026\n\n2026-08-11:\n"
                      "- above its usual range: Store $500.00 (high $200.00 in the last year)\n", body)


# ---------------------------------------------------------------- config

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


class TestReportPrompt(unittest.TestCase):
    """The summary prompt names no person: any household member may have
    made a purchase, and the model once wrote "Alex was billed twice"."""

    def prompts(self, **kw):
        got = {}
        saved = fs.ask_llm
        fs.ask_llm = lambda system, user, schema: got.update(
            system=system, user=user) or {"summary": []}
        self.addCleanup(setattr, fs, "ask_llm", saved)
        fs.llm_report_sentences("report text", "", **kw)
        return got

    def test_every_kind_forbids_person_names(self):
        for kw in ({}, {"weekly": True}, {"month": "2026-07"}):
            got = self.prompts(**kw)
            self.assertIn("Never name a person", got["system"], kw)
            self.assertNotIn("Alex", got["system"] + got["user"], kw)


# ---------------------------------------------------------------- main

class TestMain(unittest.TestCase):
    """The scan: the quarter month's balance lines, the pre-check, the
    model call for history-less payees, and the batch it posts."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._saved = {k: getattr(fs, k) for k in
                       ("db", "read_all", "llm_category_guess",
                        "post_json", "check_scan")}
        self._argv = sys.argv

        def restore():
            for k, v in self._saved.items():
                setattr(fs, k, v)
            sys.argv = self._argv
        self.addCleanup(restore)

        env = mock.patch.dict(os.environ, {
            "ASSISTANT_OPS_STATE": self._dir.name,
            "ASSISTANT_FINANCE_NOTES": self._dir.name,
            "ASSISTANT_LLM_PAUSE": os.path.join(self._dir.name, "assistant-llm-pause.json")})
        env.start()
        self.addCleanup(env.stop)
        fs.db = lambda: None
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, combine_personal=False, full_window=False: {
            "queue": [], "total_uncat": 3, "categories": [],
            "cash_flow": "flow block", "check": [], "odd": [],
            "new": [row("t1", date.today(), -1500, payee="Shop")]}
        fs.llm_category_guess = lambda data, notes: {"suggestions": []}
        self.posted = []
        fs.post_json = lambda url, payload: self.posted.append(payload)
        self.checked = []
        fs.check_scan = lambda candidates: (
            self.checked.append(candidates),
            {"proceed": True, "reason": "stub"})[1]

    def test_a_paused_scan_does_nothing(self):
        fs.llm_pause.start(30, "other")
        self.invoke()
        self.assertEqual(self.checked, [])
        self.assertEqual(self.posted, [])

    def invoke(self, *args):
        sys.argv = ["finance_jobs.py", *args]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fs.main()
        return out.getvalue()

    def test_scan_writes_the_quarter_months_balance_lines(self):
        with mock.patch.object(fs.debts, "populate_month", return_value=["House"]) as debts:
            self.invoke()
            debts.assert_called_once_with(date.today())

    def test_scan_mode_posts_plain_cards(self):
        self.invoke()
        self.assertEqual(list(self.posted[0]), ["cards"])

    def test_scan_mode_checks_with_the_queues_ids(self):
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, combine_personal=False, full_window=False: {
            "queue": [{"id": "q1"}, {"id": "q2"}],
            "total_uncat": 3, "categories": [], "cash_flow": "flow block", "check": [],
            "odd": [], "new": []}
        fs.check_scan = lambda candidates: (
            self.checked.append(candidates),
            {"proceed": False, "reason": "stub"})[1]
        self.invoke()
        self.assertEqual(self.checked,
                         [[{"transaction_id": "q1"}, {"transaction_id": "q2"}]])

    def test_scan_skip_ends_before_the_llm(self):
        llm_calls = []
        fs.llm_category_guess = lambda data, notes, month="", weekly=False: llm_calls.append(1)
        fs.check_scan = lambda candidates: {"proceed": False, "reason": "full"}
        self.invoke()                            # returns, no SystemExit
        self.assertEqual(llm_calls, [])
        self.assertEqual(self.posted, [])

    def queue_row(self, history):
        return {"id": "t1", "date": fs.cash_flow._day_int(date.today()), "amount": -500,
                "payee": "Shop", "notes": "", "account": "Checking",
                "account_id": "a1", "history": history}

    def test_scan_all_history_skips_the_llm(self):
        llm_calls = []
        fs.llm_category_guess = lambda data, notes, month="", weekly=False: llm_calls.append(1)
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, combine_personal=False, full_window=False: {
            "queue": [self.queue_row([{"category": "Food", "n": 4}])],
            "total_uncat": 1, "categories": [], "cash_flow": "flow block", "check": [],
            "odd": [], "new": []}
        self.invoke()
        self.assertEqual(llm_calls, [])
        self.assertEqual(self.posted[0]["cards"][0]["suggestions"],
                         [{"category": "Food", "basis": "history"}])

    def test_scan_history_less_payee_still_calls_the_llm(self):
        llm_calls = []

        def llm(data, notes):
            llm_calls.append(1)
            return {"suggestions": []}
        fs.llm_category_guess = llm
        fs.read_all = lambda conn, today, month="", weekly=False, detailed=False, combine_personal=False, full_window=False: {
            "queue": [self.queue_row([])],
            "total_uncat": 1, "categories": [], "cash_flow": "flow block", "check": [],
            "odd": [], "new": []}
        self.invoke()
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

class TestMainErrorPath(unittest.TestCase):
    """A failing step posts an error card naming the step and exits 1."""

    def test_read_failure_posts_error_card(self):
        posted = []
        saved = (fs.db, fs.post_json, sys.argv)
        def broken_db():
            raise RuntimeError("expected one budget copy, found 0")
        fs.db = broken_db
        fs.post_json = lambda url, payload: posted.append(payload)
        sys.argv = ["finance_jobs.py"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        try:
            with mock.patch.dict(os.environ, {"ASSISTANT_LLM_PAUSE": os.path.join(
                    tmp.name, "assistant-llm-pause.json")}):
                with self.assertRaises(SystemExit) as ctx:
                    fs.main()
        finally:
            fs.db, fs.post_json, sys.argv = saved
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(posted[0]["error"]["step"], "read")
        self.assertIn("expected one budget copy", posted[0]["error"]["message"])


class TestModelCall(unittest.TestCase):

    def test_call_goes_through_model_fallback(self):
        answer = {"choices": [{"message": {"content": '{"summary": ["ok"]}'}}]}
        with mock.patch.object(fs.model_fallback, "chain", return_value=answer) as chain:
            self.assertEqual(fs.ask_llm("s", "u", fs.REPORT_SCHEMA), {"summary": ["ok"]})
        body, timeout, backup = chain.call_args.args
        self.assertEqual(timeout, fs.LLM_TIMEOUT)
        self.assertIs(backup, fs.model_fallback.LOCAL)
        self.assertNotIn("model", body)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], fs.REPORT_SCHEMA)


if __name__ == "__main__":
    unittest.main()
