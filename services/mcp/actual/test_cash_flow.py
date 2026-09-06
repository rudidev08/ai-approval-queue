#!/usr/bin/env python3
"""Tests for cash_flow.py — stdlib unittest, no live data.

The compute and render functions run on a context built from an in-memory
SQLite copy of the api-cache schema (v_transactions is a view in Actual; a
same-shaped table reads identically). Constants (FIRST_MONTH, FIXED_GROUPS,
...) are overridden per test and restored. Run:
python3 -m pytest test_cash_flow.py -q (from this directory), or
python3 services/mcp/actual/test_cash_flow.py from the repo root.
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cash_flow as cf
import debts


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE v_transactions (id TEXT, date INT, amount INT, "
        "category TEXT, transfer_id TEXT, is_parent INT DEFAULT 0, "
        "starting_balance_flag INT DEFAULT 0, account TEXT DEFAULT 'a1', "
        "payee TEXT);"
        "CREATE TABLE v_payees (id TEXT, name TEXT);"
        "CREATE TABLE accounts (id TEXT, name TEXT, offbudget INT DEFAULT 0, "
        "tombstone INT DEFAULT 0, closed INT DEFAULT 0);"
        "CREATE TABLE category_groups (id TEXT, name TEXT, "
        "is_income INT DEFAULT 0, tombstone INT DEFAULT 0, "
        "hidden INT DEFAULT 0, sort_order REAL DEFAULT 0);"
        "CREATE TABLE categories (id TEXT, name TEXT, cat_group TEXT, "
        "tombstone INT DEFAULT 0, hidden INT DEFAULT 0);")
    conn.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Checking')")
    return conn


def group(conn, gid, name, is_income=0, sort_order=0):
    conn.execute("INSERT INTO category_groups (id, name, is_income, sort_order) "
                 "VALUES (?, ?, ?, ?)", (gid, name, is_income, sort_order))


def category(conn, cid, name, gid):
    conn.execute("INSERT INTO categories (id, name, cat_group) VALUES (?, ?, ?)",
                 (cid, name, gid))


def payee(conn, pid, name):
    conn.execute("INSERT INTO v_payees (id, name) VALUES (?, ?)", (pid, name))


def tx(conn, tid, yyyymmdd, cents, cat=None, **cols):
    fields = {"id": tid, "date": yyyymmdd, "amount": cents, "category": cat}
    fields.update(cols)
    keys = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    conn.execute(f"INSERT INTO v_transactions ({keys}) VALUES ({marks})",
                 list(fields.values()))


class Base(unittest.TestCase):
    """Fixture: Income/Salary, Fixed/Rent, Variable/Groceries; FIRST_MONTH
    2026-01. Constants restored after each test."""

    def setUp(self):
        for name in ("FIRST_MONTH", "EXCLUDED_GROUPS", "FIXED_GROUPS",
                     "ONE_OFF_GROUPS",
                     "PERSONAL_CATEGORIES", "CASH_FLOW_FILE",
                     "INCOME_SOURCE_CATEGORY", "CHECKING_ACCOUNT"):
            saved = getattr(cf, name)
            self.addCleanup(setattr, cf, name, saved)
        cf.FIRST_MONTH = "2026-01"
        cf.EXCLUDED_GROUPS = []
        cf.FIXED_GROUPS = ["Fixed"]
        cf.ONE_OFF_GROUPS = []
        # never the real vault file; write_sources() points this at a temp one
        cf.CASH_FLOW_FILE = os.path.join(tempfile.gettempdir(), "no-such-file.md")
        cf.INCOME_SOURCE_CATEGORY = "Salary"
        cf.CHECKING_ACCOUNT = "Checking"
        # same for the debts/assets files: no files, so no Assets and Debt
        # section
        self.addCleanup(setattr, debts, "DEBTS_FILE", debts.DEBTS_FILE)
        self.addCleanup(setattr, debts, "ASSETS_FILE", debts.ASSETS_FILE)
        debts.DEBTS_FILE = os.path.join(tempfile.gettempdir(),
                                        "no-such-debts.md")
        debts.ASSETS_FILE = os.path.join(tempfile.gettempdir(),
                                         "no-such-assets.md")
        self.conn = make_conn()
        group(self.conn, "gi", "Income", is_income=1, sort_order=0)
        group(self.conn, "gf", "Fixed", sort_order=1)
        group(self.conn, "gv", "Variable", sort_order=2)
        category(self.conn, "salary", "Salary", "gi")
        category(self.conn, "rent", "Rent", "gf")
        category(self.conn, "groc", "Groceries", "gv")

    def write_sources(self, body, upcoming=""):
        """Point CASH_FLOW_FILE at a temp file holding `body` under the
        heading the parser looks for; `upcoming` fills its own section."""
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(f"notes\n\n- a prose bullet: ignored\n\n"
                    f"{cf.SOURCES_HEADING}\n\n{body}\n")
            if upcoming:
                f.write(f"\n{cf.UPCOMING_HEADING}\n\n{upcoming}\n")
        self.addCleanup(os.unlink, path)
        cf.CASH_FLOW_FILE = path
        return path

    def steady_month(self, ym, salary=500000, rent=200000, groc=100000):
        """One month of fixture data: income 5,000, spending 3,000. Salary
        on the 15th, so a status view early the next month is still inside
        the source's ~38-day stopped threshold."""
        tx(self.conn, f"s{ym}", ym * 100 + 15, salary, "salary")
        tx(self.conn, f"r{ym}", ym * 100 + 1, -rent, "rent")
        tx(self.conn, f"g{ym}", ym * 100 + 10, -groc, "groc")

    def ctx(self, today):
        return cf._ctx(self.conn, today)

    def april(self):
        """Jan-Mar steady; April: rent paid, groceries $400. Balance in
        'Checking' = 3 x $2,000 leftover - $2,400 = $3,600; average
        spending $3,000."""
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "r4", 20260401, -200000, "rent")
        tx(self.conn, "g4", 20260405, -40000, "groc")
        return cf._status(self.ctx(date(2026, 4, 10)))


# ---------------------------------------------------------------- helpers

class TestMonthMath(unittest.TestCase):

    def test_parse_and_label_round_trip(self):
        self.assertEqual(cf._label(cf._parse_month("2026-08")), "2026-08")
        self.assertEqual(cf._label(cf._parse_month("2025-12") + 1), "2026-01")

    def test_parse_rejects_bad_forms(self):
        for bad in ("2026-8", "2026/08", "202608", "2026-13", "aug"):
            with self.assertRaises(ValueError):
                cf._parse_month(bad)

    def test_bounds(self):
        self.assertEqual(cf._bounds(cf._parse_month("2026-02")),
                         (20260201, 20260231))

    def test_days_in(self):
        self.assertEqual(cf._days_in(cf._parse_month("2026-02")), 28)
        self.assertEqual(cf._days_in(cf._parse_month("2028-02")), 29)
        # idx // 12 must give the year: idx 49 is year 4 (leap, Feb 29) by
        # // 12, but year 3 (not leap, Feb 28) by // 13
        self.assertEqual(cf._days_in(cf._idx(4, 2)), 29)

    def test_to_date_reads_month_from_the_right_slice(self):
        self.assertEqual(cf._to_date(20261215), date(2026, 12, 15))


class TestMoney(unittest.TestCase):

    def test_money(self):
        self.assertEqual(cf._money(123456), "$1,235")
        self.assertEqual(cf._money(-123456), "-$1,235")
        self.assertEqual(cf._money(0), "$0")

    def test_signed(self):
        self.assertEqual(cf._signed(123456), "+$1,235")
        self.assertEqual(cf._signed(-123456), "-$1,235")
        self.assertEqual(cf._signed(20), "$0")


# ---------------------------------------------------------------- SQL pinning

class TestFetchFilters(Base):
    """The one query's filters, pinned: is_parent, transfer, starting
    balance, off-budget account, date range."""

    def test_excluded_rows_never_fetched(self):
        self.conn.execute("INSERT INTO accounts (id, offbudget) VALUES ('a2', 1)")
        tx(self.conn, "keep", 20260115, -1000, "groc")
        tx(self.conn, "child", 20260116, -1000, "groc")  # split child counts
        tx(self.conn, "parent", 20260115, -1000, "groc", is_parent=1)
        tx(self.conn, "xfer", 20260115, -1000, "groc", transfer_id="t2")
        tx(self.conn, "start", 20260115, -1000, "groc", starting_balance_flag=1)
        tx(self.conn, "off", 20260115, -1000, "groc", account="a2")
        tx(self.conn, "early", 20251231, -1000, "groc")
        lo = cf._parse_month("2026-01")
        rows = cf._fetch(self.conn, lo, lo)
        self.assertEqual(sum(r["n"] for r in rows), 2)
        self.assertEqual(sum(r["spend"] for r in rows), -2000)

    def test_tombstoned_category_counts_as_uncategorized(self):
        self.conn.execute("UPDATE categories SET tombstone = 1 WHERE id = 'groc'")
        tx(self.conn, "t1", 20260115, -1000, "groc")
        ctx = self.ctx(date(2026, 2, 10))
        d = ctx["months"][cf._parse_month("2026-01")]
        self.assertEqual(d["uncat_spend"], 1000)
        self.assertEqual(d["cats"], {})


# ---------------------------------------------------------------- compute

class TestComputeLayer(Base):

    def test_totals_are_net_for_categorized(self):
        tx(self.conn, "t1", 20260110, -5000, "groc")
        tx(self.conn, "t2", 20260112, 2000, "groc")   # refund reduces spending
        tx(self.conn, "t3", 20260101, 500000, "salary")
        tx(self.conn, "t4", 20260120, 700)            # uncategorized income
        ctx = self.ctx(date(2026, 2, 1))
        income, spending = cf._totals(ctx["months"][cf._parse_month("2026-01")],
                                      ctx["cats"], ctx["income_groups"])
        self.assertEqual(income, 500700)
        self.assertEqual(spending, 3000)

    def test_month_data_accumulates_multiple_uncategorized_rows(self):
        rows = [{"idx": 1, "cat": None, "inc": 500, "spend": -100, "n": 1},
                {"idx": 1, "cat": None, "inc": 300, "spend": -50, "n": 1}]
        d = cf._month_data(rows, 1, {})
        self.assertEqual(d["uncat_inc"], 800)
        self.assertEqual(d["uncat_spend"], 150)
        self.assertEqual(d["n_uncat"], 2)

    def test_average_counts_empty_months_as_zero(self):
        tx(self.conn, "t1", 20260110, -30000, "groc")   # January only
        ctx = self.ctx(date(2026, 4, 1))                # Jan, Feb, Mar complete
        avg = cf._average([ctx["months"][i] for i in cf._window(ctx, ctx["cur"])])
        self.assertAlmostEqual(avg["cats"]["groc"], -10000)

    def test_average_includes_uncategorized_totals(self):
        tx(self.conn, "u1", 20260110, -30000)   # January: uncategorized spend
        tx(self.conn, "u2", 20260115, 5000)     # January: uncategorized income
        tx(self.conn, "u3", 20260210, -10000)   # February: uncategorized spend
        ctx = self.ctx(date(2026, 4, 1))
        avg = cf._average([ctx["months"][i] for i in cf._window(ctx, ctx["cur"])])
        self.assertAlmostEqual(avg["uncat_spend"], (30000 + 10000) / 3)
        self.assertAlmostEqual(avg["uncat_inc"], 5000 / 3)

    def test_window_caps_at_12_and_crosses_years(self):
        cf.FIRST_MONTH = "2025-06"
        ctx = self.ctx(date(2026, 9, 1))
        win = cf._window(ctx, ctx["cur"])
        self.assertEqual(len(win), 12)
        self.assertEqual(cf._label(win[0]), "2025-09")
        self.assertEqual(cf._label(win[-1]), "2026-08")

    def test_streak(self):
        self.assertEqual(cf._streak([100, -1, 50, 0]), 2)
        self.assertEqual(cf._streak([-1]), 0)
        self.assertEqual(cf._streak([]), 0)

    def test_excluded_group_vanishes_entirely(self):
        cf.EXCLUDED_GROUPS = ["Variable"]
        tx(self.conn, "t1", 20260110, -5000, "groc")
        tx(self.conn, "t2", 20260101, 500000, "salary")
        ctx = self.ctx(date(2026, 2, 1))
        d = ctx["months"][cf._parse_month("2026-01")]
        income, spending = cf._totals(d, ctx["cats"], ctx["income_groups"])
        self.assertEqual((income, spending), (500000, 0))
        self.assertEqual(d["uncat_spend"], 0)     # dropped, not uncategorized
        self.assertEqual(d["cats"], {"salary": 500000})


# ---------------------------------------------------------------- status view

class TestStatus(Base):

    def april(self, detailed=False):
        """Jan-Mar steady; April 10: rent paid, groceries $400, no salary yet."""
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "r4", 20260401, -200000, "rent")
        tx(self.conn, "g4", 20260405, -40000, "groc")
        return cf._status(self.ctx(date(2026, 4, 10)), detailed=detailed)

    def test_header_and_projection(self):
        out = self.april()
        self.assertIn("2026-04, day 10 of 30 "
                      "| average: 3 months (2026-01..2026-03)", out)
        self.assertNotIn("Budget status", out)
        self.assertIn("Projected month end (income from sources, Fixed at "
                      "average, rest at pace):", out)
        # income is the source estimate, although $0 arrived this month
        self.assertIn("- income: $5,000", out)
        # fixed $2,000 flat + groceries 400 + 1000 * 20/30 = $3,067
        self.assertIn("- spending: $3,067 | average $3,000", out)
        self.assertIn("- leftover: +$1,933 | average +$2,000", out)
        self.assertIn("- streak: 3 green months in a row; "
                      "this month projected green", out)

    def test_estimated_and_actual_grouping(self):
        out = self.april()
        self.assertLess(out.index("### Estimated"),
                        out.index("Projected month end"))
        self.assertLess(out.index("Income estimate"),
                        out.index("Expense estimate"))
        self.assertLess(out.index("Expense estimate"),
                        out.index("### Actual"))
        self.assertLess(out.index("### Actual"), out.index("Income: $"))
        self.assertLess(out.index("Income: $"), out.index("Expenses: $"))
        self.assertLess(out.index("Expenses: $"), out.index("### Forecast"))
        self.assertLess(out.index("### Forecast"),
                        out.index("How long money lasts"))

    def test_expense_estimate(self):
        out = self.april()
        self.assertIn("Expense estimate: $3,000 monthly", out)
        self.assertIn("- Fixed: $2,000 monthly", out)
        self.assertIn("- Variable: $1,000 monthly", out)

    def test_expense_estimate_first_month_uses_so_far(self):
        self.steady_month(202601)
        out = cf._status(self.ctx(date(2026, 1, 20)))
        self.assertIn("Expense estimate: $3,000 — so far this month, "
                      "no average yet", out)
        self.assertIn("- Fixed: $2,000\n", out + "\n")

    def test_actual_rows(self):
        out = self.april(detailed=True)
        self.assertIn("\n"
                      "Income: $0\n"
                      "  - Mar $5,000 (-100%) · Feb $5,000 (-100%)\n",
                      out + "\n")
        self.assertIn("Expenses: $2,400\n"
                      "  - Mar $3,000 (-20%) · Feb $3,000 (-20%)\n",
                      out + "\n")
        self.assertNotIn("income - spending", out)
        # income categories print plain, before the expense groups
        actual = out[out.index("### Actual"):]
        self.assertIn("- Salary: $0\n"
                      "  - Mar $5,000 (-100%) · Feb $5,000 (-100%)\n",
                      actual + "\n")
        self.assertLess(actual.index("- Salary: $0"),
                        actual.index("- Fixed:"))
        self.assertIn("- Fixed: $2,000 | month $2,000\n"
                      "  - Mar $2,000 (+0%) · Feb $2,000 (+0%)", out)
        self.assertIn("- Variable: $400 | expected by now $333 month $1,000\n"
                      "  - Mar $1,000 (-60%) · Feb $1,000 (-60%)", out)
        # nothing to categorize, so no Check section at all
        self.assertNotIn("uncategorized", out)
        self.assertNotIn("### Check", out)

    def test_forecast_never_runs_out_when_the_estimate_covers_itself(self):
        out = self.april()
        self.assertIn("How long money lasts with current balance and "
                      "estimated income and expenses:", out)
        self.assertIn("- never runs out — $2,000 extra per month", out)

    def test_forecast_burns_when_expenses_win(self):
        # $50,000 starting balance counts in the balance but in no average;
        # $6,000 average spending against the $5,000 income estimate
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        for ym in (202601, 202602, 202603):
            self.steady_month(ym, groc=400000)
        out = cf._status(self.ctx(date(2026, 4, 10)))
        self.assertIn("Expense estimate: $6,000 monthly", out)
        # 47,000 balance / 1,000 burn
        self.assertIn("- net spending $1,000 monthly, balance $47,000 "
                      "lasts about 47.0 months — runs out around 2030-", out)

    def test_uncategorized_goes_to_check(self):
        self.steady_month(202601)
        tx(self.conn, "u1", 20260106, -10000)
        out = cf._status(self.ctx(date(2026, 1, 20)))
        self.assertIn("### Check", out)
        self.assertIn("- uncategorized: 1 transaction ($100 spending)", out)
        self.assertLess(out.index("### Actual"), out.index("### Check"))

    def test_projected_red(self):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "big", 20260402, -600000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)))
        self.assertIn("this month projected red", out)

    def test_categories_nest_under_the_group_row(self):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "g4", 20260405, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)), detailed=True,
                         categories=True)
        self.assertIn("- Variable: $400 | expected by now $333 month $1,000\n"
                      "  - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n"
                      "  - Groceries: $400 | expected by now $333 month $1,000\n"
                      "    - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n",
                      out + "\n")
        # the fixed group's categories share its wording
        self.assertIn("- Fixed: $0 | month $2,000\n"
                      "  - Mar $2,000 (-100%) · Feb $2,000 (-100%)\n"
                      "  - Rent: $0 | month $2,000\n", out)

    def test_categories_regular_size_has_no_history(self):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "g4", 20260405, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)), categories=True)
        self.assertIn("- Variable: $400 | expected by now $333 month $1,000\n"
                      "  - Groceries: $400 | expected by now $333 month $1,000\n",
                      out + "\n")
        self.assertNotIn("Mar $1,000", out)

    def test_no_categories_by_default(self):
        out = self.april()
        self.assertNotIn("Groceries", out)
        self.assertNotIn("Rent", out)

    def personal_month(self):
        """The Variable group gains the five per-person categories, spent in
        2026-02 only: Personal $240, Personal Subscription $60."""
        for cid, name in (("alex", "Alex"), ("morgan", "Morgan"),
                          ("riley", "Riley"), ("morganr", "Morgan Recurring"),
                          ("rileyr", "Riley Recurring")):
            category(self.conn, cid, name, "gv")
        for ym in (202601, 202602):
            self.steady_month(ym)
        for i, (cid, cents) in enumerate(
                (("alex", -1000), ("morgan", -20000), ("riley", -3000),
                 ("morganr", -5000), ("rileyr", -1000))):
            tx(self.conn, f"p{i}", 20260205, cents, cid)

    def test_combine_personal_joins_the_per_person_rows(self):
        self.personal_month()
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         categories=True, combine_personal=True)
        # the group total is untouched, so its rows still add up to it
        self.assertIn("- Variable: $1,300 | average $1,000\n"
                      "  - Groceries: $1,000 | average $1,000\n"
                      "  - Personal: $240 | average $0\n"
                      "  - Personal Subscription: $60 | average $0\n",
                      out + "\n")
        for name in ("Alex", "Morgan", "Riley"):
            self.assertNotIn(f"- {name}:", out)

    def test_per_person_rows_print_by_name_without_the_setting(self):
        self.personal_month()
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         categories=True)
        self.assertIn("  - Alex: $10 | average $0\n", out + "\n")
        self.assertIn("  - Morgan Recurring: $50 | average $0\n", out + "\n")
        self.assertNotIn("Personal", out)

    def test_combined_row_history_sums_its_categories(self):
        self.personal_month()
        tx(self.conn, "p9", 20260105, -10000, "morgan")     # Jan: $100
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         detailed=True, categories=True, combine_personal=True)
        self.assertIn("  - Personal: $240 | average $100\n"
                      "    - Jan $100 (+140%) · Dec -\n", out + "\n")

    def test_combine_personal_reaches_the_outliers_without_categories(self):
        """The outlier lines print whatever the categories setting is, so
        the merge has to reach them on their own."""
        self.personal_month()
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         combine_personal=True)
        self.assertIn("Outliers vs average:\n"
                      "- Personal: $240 over\n"
                      "- Personal Subscription: $60 over\n", out + "\n")
        self.assertNotIn("  - ", out)          # no category rows either way

    def test_categories_past_month_use_the_average_column(self):
        for ym in (202601, 202602):
            self.steady_month(ym)
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         categories=True)
        self.assertTrue(out.startswith("Month — 2026-02 | average: 1 month"))
        self.assertIn("- Variable: $1,000 | average $1,000\n"
                      "  - Groceries: $1,000 | average $1,000\n", out + "\n")


class TestHist(Base):
    """The Actual rows' two-month history suffix: percent against the
    newest previous month, '-' placeholders before FIRST_MONTH."""

    def test_percent_compares_to_the_newest_previous_month(self):
        for ym in (202601, 202602):
            self.steady_month(ym)
        self.steady_month(202603, groc=500000)
        out = cf._status(self.ctx(date(2026, 4, 10)), detailed=True)
        # February's $1,000 plays no role in the percent
        self.assertIn("- Variable: $0 | expected by now $778 month $2,333\n"
                      "  - Mar $5,000 (-100%) · Feb $1,000 (-100%)", out)

    def test_regular_size_has_no_history_rows(self):
        for ym in (202601, 202602):
            self.steady_month(ym)
        out = cf._status(self.ctx(date(2026, 3, 10)))
        self.assertNotIn("  - Feb", out)
        self.assertNotIn("  - Jan", out)

    def test_no_percent_when_the_previous_month_is_zero(self):
        for ym in (202601, 202602):
            self.steady_month(ym)
        tx(self.conn, "r3", 20260301, -200000, "rent")   # March: no groceries
        tx(self.conn, "g4", 20260405, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)), detailed=True)
        self.assertIn("- Variable: $400 | expected by now $222 month $667\n"
                      "  - Mar $0 · Feb $1,000 (-60%)", out)

    def test_months_before_first_month_print_a_dash(self):
        self.steady_month(202601)
        tx(self.conn, "g2", 20260205, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 2, 10)), detailed=True)
        self.assertIn("- Variable: $400 | expected by now $357 month $1,000\n"
                      "  - Jan $1,000 (-60%) · Dec -", out)


class TestOneOff(Base):
    """ONE_OFF_GROUPS: counted in every month's totals — green/red and
    history tell the truth — but in no average: the projection takes the
    group at actual spent, rows print plain, the runs-out divisor and the
    outliers never see it. 'Variable' plays the one-off group here."""

    def april(self, detailed=False):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "r4", 20260401, -200000, "rent")
        tx(self.conn, "g4", 20260405, -40000, "groc")
        return cf._status(self.ctx(date(2026, 4, 10)), detailed=detailed)

    def test_projection_counts_only_actual_and_average_excludes(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        out = self.april()
        self.assertIn("Projected month end (income from sources, Fixed at "
                      "average, Variable as spent, rest at pace):", out)
        # fixed $2,000 flat + one-off $400 as spent, no pace remainder;
        # the average column drops the $1,000 one-off groceries
        self.assertIn("- spending: $2,400 | average $2,000", out)
        self.assertIn("- leftover: +$2,600 | average +$3,000", out)

    def test_so_far_totals_count_the_one_off(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        out = self.april(detailed=True)
        self.assertIn("Expenses: $2,400\n"
                      "  - Mar $3,000 (-20%) · Feb $3,000 (-20%)\n",
                      out + "\n")

    def test_big_one_off_turns_the_projection_red(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "big", 20260402, -600000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)))
        self.assertIn("this month projected red", out)

    def test_status_row_is_plain(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        out = self.april(detailed=True)
        self.assertIn("- Variable: $400\n"
                      "  - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n",
                      out + "\n")
        self.assertNotIn("- Variable: $400 |", out)

    def test_expense_estimate_skips_the_one_off(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        out = self.april()
        self.assertIn("Expense estimate: $2,000 monthly", out)
        self.assertIn("- Fixed: $2,000 monthly", out)
        self.assertNotIn("- Variable: $1,000 monthly", out)

    def test_category_rows_are_plain(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "r4", 20260401, -200000, "rent")
        tx(self.conn, "g4", 20260405, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)), detailed=True,
                         categories=True)
        self.assertIn("- Variable: $400\n"
                      "  - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n"
                      "  - Groceries: $400\n"
                      "    - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n",
                      out + "\n")

    def test_month_view_totals_count_it_averages_do_not(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        for ym in (202601, 202602):
            self.steady_month(ym)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"), detailed=True)
        self.assertIn("Expenses: $3,000 | average $2,000\n"
                      "  - Jan $3,000 (+0%) · Dec -", out)
        self.assertIn("Leftover: +$2,000 | average +$3,000\n"
                      "  - Jan $2,000 (+0%) · Dec -", out)
        self.assertIn("- Variable: $1,000\n"
                      "  - Jan $1,000 (+0%) · Dec -\n",
                      out + "\n")
        self.assertNotIn("- Variable: $1,000 |", out)

    def test_one_off_never_lists_as_an_outlier(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        self.steady_month(202601)
        self.steady_month(202602, groc=140000)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"))
        self.assertNotIn("- Groceries: $400 over", out)

    def test_history_lines_count_it_the_average_line_does_not(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        self.steady_month(202601)
        self.steady_month(202602, groc=600000)
        out = cf._history(self.ctx(date(2026, 3, 5)), 6)
        self.assertIn("- 2026-02: income $5,000 | spending $8,000 | "
                      "leftover -$3,000 | red", out)
        self.assertIn("- average: 2 months (2026-01..2026-02): "
                      "income $5,000 | spending $2,000 | leftover +$3,000",
                      out)


class TestStatusFirstMonth(Base):

    def test_so_far_only_without_a_complete_month(self):
        tx(self.conn, "s1", 20260101, 310000, "salary")
        tx(self.conn, "g1", 20260105, -20000, "groc")
        tx(self.conn, "u1", 20260106, -10000)
        tx(self.conn, "u2", 20260107, 5000)
        out = cf._status(self.ctx(date(2026, 1, 10)), detailed=True)
        self.assertIn("| first month: no average yet", out)
        self.assertNotIn("average $", out)
        self.assertIn("Income: $3,150\n"
                      "  - Dec - · Nov -\n", out + "\n")
        self.assertIn("Expenses: $300\n"
                      "  - Dec - · Nov -\n", out + "\n")
        self.assertNotIn("income - spending", out)
        self.assertIn("- Variable: $200\n"
                      "  - Dec - · Nov -\n", out + "\n")
        self.assertIn("### Check", out)
        self.assertIn("- uncategorized: 2 transactions "
                      "($100 spending, $50 income)", out)
        # both rest on the average, so neither may appear
        self.assertNotIn("Projected month end", out)
        self.assertNotIn("streak", out.casefold())
        # the income estimate stands on the sources alone and still prints
        self.assertIn("- (no payee): not estimable", out)


# ---------------------------------------------------------------- forecast

class TestForecast(Base):
    """The how-long-money-lasts block: CHECKING_ACCOUNT balance against the
    burn (expense estimate minus income estimate)."""

    def burn_months(self):
        """Three steady months with $6,000 spending: burn $1,000 against
        the $5,000 income estimate, balance -$3,000."""
        for ym in (202601, 202602, 202603):
            self.steady_month(ym, groc=400000)

    def test_balance_counts_transfers_but_not_other_accounts(self):
        self.burn_months()
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        self.conn.execute(
            "INSERT INTO accounts (id, name) VALUES ('a2', 'Savings')")
        tx(self.conn, "move", 20260406, -50000, transfer_id="move2")
        tx(self.conn, "other", 20260406, 999900, account="a2")
        out = cf._status(self.ctx(date(2026, 4, 10)))
        # 50,000 + 3 x -1,000 - 500 transfer
        self.assertIn("balance $46,500 lasts about 46.5 months", out)

    def test_first_month_burns_on_the_so_far_estimate(self):
        tx(self.conn, "s1", 20260101, 310000, "salary")
        tx(self.conn, "g1", 20260105, -30000, "groc")
        out = cf._status(self.ctx(date(2026, 1, 10)))
        # income not estimable yet; expenses so far $300 against a $2,800
        # balance
        self.assertIn("- net spending $300 monthly, balance $2,800 lasts "
                      "about 9.3 months — runs out around 2026-10", out)

    def test_missing_account_is_flagged(self):
        cf.CHECKING_ACCOUNT = "Nope"
        out = self.april()
        self.assertIn("- account 'Nope' not found — fix CHECKING_ACCOUNT "
                      "in cash_flow.py", out)

    def test_closed_account_does_not_match(self):
        self.conn.execute("UPDATE accounts SET closed = 1 WHERE id = 'a1'")
        out = self.april()
        self.assertIn("- account 'Checking' not found — fix CHECKING_ACCOUNT "
                      "in cash_flow.py", out)

    def test_negative_balance_is_already_out(self):
        self.burn_months()
        tx(self.conn, "drain", 20260406, -400000)
        out = cf._status(self.ctx(date(2026, 4, 10)))
        self.assertIn("- already out", out)


class TestUpcoming(Base):
    """The Upcoming section: hand-listed one-time expenses ahead. They print
    in the Estimated block only; the forecast ignores them."""

    def test_block(self):
        self.write_sources("", upcoming="- New roof\n  - amount: $1600\n"
                                        "  - month: 2026-06\n"
                                        "- Fence\n  - amount: 500")
        out = self.april()
        self.assertIn("Upcoming one-time expenses: $2,100", out)
        self.assertIn("- New roof: $1,600 (2026-06)", out)
        self.assertIn("- Fence: $500", out)

    def test_no_section_leaves_the_report_alone(self):
        self.write_sources("")
        out = self.april()
        self.assertNotIn("Upcoming", out)

    def test_forecast_ignores_upcoming(self):
        self.write_sources("", upcoming="- New roof\n  - amount: $5000")
        out = self.april()
        # larger than the $3,600 balance; the forecast does not subtract it
        self.assertIn("- never runs out — $2,000 extra per month", out)

    def test_bad_amount_and_month_are_flagged(self):
        self.write_sources("", upcoming="- Roof\n  - amount: lots\n"
                                        "- Fence\n  - amount: $500\n"
                                        "  - month: June")
        out = self.april()
        self.assertIn("- Roof: no amount", out)
        self.assertIn("check: cash-flow.md gives amount 'lots', which is "
                      "not a dollar amount", out)
        self.assertIn("check: cash-flow.md gives month 'June', not YYYY-MM",
                      out)
        # the bad amount stays out of the total; the good one counts
        self.assertIn("Upcoming one-time expenses: $500", out)

    def test_upcoming_entries_are_not_income_sources(self):
        self.write_sources("", upcoming="- New roof\n  - amount: $1600")
        out = self.april()
        self.assertNotIn("New roof: not found in the budget", out)


# ---------------------------------------------------------------- income maths (pure)

class TestIncomeMaths(unittest.TestCase):
    """Boundary edges in the income source maths — no fixture needed."""

    def test_per_year_boundaries(self):
        self.assertEqual(cf._per_year("1 per year"), 1)
        self.assertEqual(cf._per_year("366 per year"), 366)
        self.assertIsNone(cf._per_year("367 per year"))

    def test_detect_bucket_edges(self):
        # median gap 5 = weekly's lower bound; median gap 9 = its upper bound
        lo = [date(2026, 1, 1), date(2026, 1, 6), date(2026, 1, 11)]
        hi = [date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 19)]
        self.assertEqual(cf._detect(lo), "weekly")
        self.assertEqual(cf._detect(hi), "weekly")

    def test_detect_twice_a_month_thresholds(self):
        # exactly 4 dates, 2 distinct days of month -> reclassified
        two_days = [date(2026, 1, 1), date(2026, 1, 15),
                   date(2026, 2, 1), date(2026, 2, 15)]
        self.assertEqual(cf._detect(two_days), "twice a month")
        # exactly 4 dates, 3 distinct days -> stays fortnightly
        three_days = [date(2026, 1, 1), date(2026, 1, 15),
                     date(2026, 2, 1), date(2026, 2, 20)]
        self.assertEqual(cf._detect(three_days), "every 2 weeks")

    def test_same_tolerance_boundary(self):
        self.assertTrue(cf._same(100, 90))    # diff 10 == 10% of 100
        self.assertFalse(cf._same(100, 89))   # diff 11 > 10% of 100

    def test_pick_payment_exactly_three_judges(self):
        self.assertEqual(cf._pick_payment([100, 100, 50]),
                         (100, "outlier, using previous"))

    def test_source_rows_stopped_threshold_boundary(self):
        # STOPPED_INTERVALS * 365.25 / per_year, per_year=1 (annual):
        # 456.5625 days; 457 days silent must already read as stopped
        ctx = {"source_file": {"Acme": {"frequency": "annual"}},
              "payments": {"Acme": [(date(2025, 1, 1), 100000)]},
              "today": date(2025, 1, 1) + timedelta(days=457)}
        rows, _ = cf._source_rows(ctx)
        self.assertTrue(rows[0]["stopped"])


# ---------------------------------------------------------------- income sources

class TestIncomeSources(Base):
    """The per-payee income estimate. INCOME_SOURCE_CATEGORY is 'Salary' in
    this fixture, so Income/Salary rows are the sources."""

    def source(self, pid, name, dates, amounts, cat="salary"):
        payee(self.conn, pid, name)
        if not isinstance(amounts, list):
            amounts = [amounts] * len(dates)
        for i, (d, a) in enumerate(zip(dates, amounts)):
            tx(self.conn, f"{pid}-{i}", d, a, cat, payee=pid)

    def report(self, today=date(2026, 3, 20)):
        # a week after the last BIWEEKLY payment: no fixture source reads
        # as stopped (every 2 weeks tolerates ~17 silent days)
        total, lines = cf._income_section(self.ctx(today))
        return total, "\n".join(lines)

    # every 14 days: 6 payments spanning 2026-01-02 .. 2026-03-13
    BIWEEKLY = [20260102, 20260116, 20260130, 20260213, 20260227, 20260313]

    def test_rate_is_per_year_not_per_month(self):
        # the whole point: 26 paychecks a year is 2.167 a month, so $1,000
        # every 2 weeks is $2,167 a month and never $2,000
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme\n  - frequency: every 2 weeks")
        total, out = self.report()
        self.assertEqual(round(total), round(100000 * 26 / 12))
        self.assertIn("- Acme (every 2 weeks): $2,167 monthly", out)
        self.assertIn("payment $1,000.00, latest 2026-03-13", out)

    def test_frequency_detected_from_dates_without_a_file(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        _, out = self.report()
        self.assertIn("- Acme (every 2 weeks): $2,167 monthly", out)

    def test_fewer_than_three_payments_names_no_frequency(self):
        self.source("p1", "Acme", self.BIWEEKLY[:2], 100000)
        total, out = self.report()
        self.assertEqual(total, 0)
        self.assertIn("- Acme: not estimable", out)
        self.assertIn("2 payments, latest 2026-01-16 — set a frequency, "
                      "or wait until it has 3", out)

    def test_twice_a_month_split_from_fortnightly_by_day_of_month(self):
        # same 14-to-17 day gaps, but the payments keep to the 1st and 15th
        self.source("p1", "Acme",
                    [20260101, 20260115, 20260201, 20260215, 20260301,
                     20260315], 100000)
        _, out = self.report()
        self.assertIn("- Acme (twice a month): $2,000 monthly", out)

    def test_incomes_uses_the_last_whole_round(self):
        # one payee, two grants of different size, small-large-small so far:
        # the last round holds one of each
        dates = [20260115, 20260415, 20260715]
        amounts = [300000, 1145000, 300000]
        self.source("p1", "Stocks", dates, amounts)
        self.write_sources("- Stocks\n  - frequency: 4 per year\n"
                           "  - incomes: 2")
        total, out = self.report(date(2026, 8, 10))
        self.assertEqual(round(total), round((1145000 + 300000) / 2 * 4 / 12))
        self.assertIn("- Stocks (4 per year): $2,408 monthly", out)
        self.assertIn("2 payments averaged, average $7,225.00", out)

    def test_without_incomes_the_same_three_payments_read_as_outlier(self):
        # no incomes field, so the picker sees small-large-small and treats
        # the large middle payment as the outlier
        dates = [20260115, 20260415, 20260715]
        amounts = [300000, 1145000, 300000]
        self.source("p1", "Stocks", dates, amounts)
        self.write_sources("- Stocks\n  - frequency: 4 per year")
        total, out = self.report(date(2026, 8, 10))
        self.assertEqual(round(total), round(300000 * 4 / 12))
        self.assertIn("- Stocks (4 per year): $1,000 monthly", out)

    def test_one_payment_is_never_rounded_away(self):
        self.source("p1", "Stocks", [20260715], 670502)
        self.write_sources("- Stocks\n  - frequency: 4 per year\n"
                           "  - incomes: 2")
        total, out = self.report(date(2026, 8, 10))
        self.assertEqual(round(total), round(670502 * 4 / 12))
        self.assertIn("1 of 2 incomes averaged, average $6,705.02", out)

    def test_old_payments_never_reach_the_estimate(self):
        dates = [20260102, 20260116, 20260130, 20260213, 20260227,
                 20260313, 20260327, 20260410]
        # the older half-size payments must not reach the estimate
        self.source("p1", "Acme", dates, [50000, 50000] + [100000] * 6)
        self.write_sources("- Acme\n  - frequency: every 2 weeks")
        _, out = self.report(date(2026, 4, 14))
        self.assertIn("- Acme (every 2 weeks): $2,167 monthly", out)
        self.assertIn("payment $1,000.00, latest 2026-04-10", out)

    def test_file_frequency_wins_and_the_clash_is_flagged(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme\n  - frequency: monthly")
        total, out = self.report()
        self.assertEqual(round(total), 100000)      # monthly, not fortnightly
        self.assertIn("- Acme (monthly): $1,000 monthly", out)
        self.assertIn("check: cash-flow.md says monthly, the dates say "
                      "every 2 weeks", out)

    def test_no_clash_when_the_words_differ_but_the_rate_agrees(self):
        self.source("p1", "Stocks", [20260101, 20260401, 20260701], 300000)
        self.write_sources("- Stocks\n  - frequency: 4 per year")
        _, out = self.report(date(2026, 8, 10))
        self.assertNotIn("check:", out)

    def test_silent_source_leaves_the_total_and_keeps_its_line(self):
        self.source("p1", "Gone", [20260105, 20260205, 20260305], 100000)
        self.source("p2", "Here", [20260510, 20260610, 20260710], 200000)
        # Gone has been silent 142 days, far past 1.25 x its own monthly gap
        total, out = self.report(date(2026, 7, 25))
        self.assertEqual(round(total), 200000)        # only Here counts
        self.assertIn("- Gone (monthly): stopped", out)
        self.assertIn("last payment 2026-03-05, 5 months ago — not counted",
                      out)
        self.assertIn("- Here (monthly): $2,000 monthly", out)

    def test_label_replaces_the_payee_name(self):
        self.source("p1", "Zeta Transfer", self.BIWEEKLY, 100000)
        self.write_sources("- Zeta Transfer\n  - label: Acme Stocks")
        _, out = self.report()
        self.assertIn("- Acme Stocks (every 2 weeks):", out)
        self.assertNotIn("Zeta", out)

    def test_entry_for_a_payee_that_does_not_exist(self):
        self.write_sources("- Ghost\n  - frequency: monthly")
        _, out = self.report()
        self.assertIn("- Ghost: not found in the budget — fix "
                      "cash-flow.md", out)

    def test_estimate_stands_in_until_the_first_deposit(self):
        self.write_sources("- Vest\n  - frequency: monthly\n"
                           "  - estimate: $500")
        total, out = self.report()
        self.assertEqual(round(total), 50000)
        self.assertIn("- Vest (monthly): $500 monthly (placeholder)", out)
        self.assertIn("no payments yet — the estimate field stands in "
                      "until the first deposit", out)

    def test_regular_size_is_one_line_per_source(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme\n  - frequency: every 2 weeks\n"
                           "- Vest\n  - frequency: monthly\n"
                           "  - estimate: $500")
        _, lines = cf._income_section(self.ctx(date(2026, 3, 20)),
                                      detailed=False)
        out = "\n".join(lines)
        self.assertIn("- Acme: $2,167 monthly", out)
        self.assertIn("- Vest: $500 monthly (est)", out)
        self.assertNotIn("(every 2 weeks)", out)
        self.assertNotIn("payment $1,000.00", out)
        self.assertNotIn("placeholder", out)

    def test_first_deposit_replaces_the_estimate(self):
        self.source("p1", "Vest", [20260310], 70000)
        self.write_sources("- Vest\n  - frequency: monthly\n"
                           "  - estimate: $500")
        total, out = self.report()
        self.assertEqual(round(total), 70000)
        self.assertNotIn("placeholder", out)

    def test_estimate_without_frequency_is_flagged(self):
        self.write_sources("- Vest\n  - estimate: $500")
        total, out = self.report()
        self.assertEqual(total, 0)
        self.assertIn("- Vest: not estimable", out)
        self.assertIn("check: estimate set but no frequency — set one in "
                      "cash-flow.md", out)

    def test_bad_estimate_value_is_flagged(self):
        self.write_sources("- Vest\n  - frequency: monthly\n"
                           "  - estimate: five hundred")
        total, out = self.report()
        self.assertEqual(total, 0)
        self.assertIn("- Vest: not estimable", out)
        self.assertIn("check: cash-flow.md gives estimate "
                      "'five hundred', which is not a dollar amount", out)

    def test_prose_bullets_above_the_heading_are_not_sources(self):
        # write_sources always puts "- a prose bullet: ignored" above it
        self.write_sources("- Ghost\n  - frequency: monthly")
        _, out = self.report()
        self.assertNotIn("a prose bullet", out)

    def test_file_without_the_sources_heading_is_reported(self):
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write("- Acme\n  - frequency: monthly\n")
        self.addCleanup(os.unlink, path)
        cf.CASH_FLOW_FILE = path
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        _, out = self.report()
        self.assertIn("has no '## Sources' heading", out)
        self.assertIn("- Acme (every 2 weeks):", out)   # dates still work

    def test_unknown_frequency_value_is_reported(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme\n  - frequency: now and then")
        _, out = self.report()
        self.assertIn("check: cash-flow.md gives frequency "
                      "'now and then', which is not a frequency I know", out)
        self.assertIn("- Acme (every 2 weeks):", out)   # falls back to dates

    def test_payments_before_first_month_never_reach_the_maths(self):
        cf.FIRST_MONTH = "2026-02"
        # 2 payments in the window; the one before FIRST_MONTH must not
        # become the third, which would let the dates be read
        self.source("p1", "Acme", [20260102] + self.BIWEEKLY[4:], 100000)
        _, out = self.report()
        self.assertIn("- Acme: not estimable", out)
        self.assertIn("2 payments, latest 2026-03-13", out)

    def test_only_the_source_category_counts(self):
        category(self.conn, "oneoff", "One-off", "gi")
        self.source("p1", "Windfall", self.BIWEEKLY, 100000, cat="oneoff")
        total, out = self.report()
        self.assertEqual(total, 0)
        self.assertNotIn("Windfall", out)

    def test_multi_source_payee_is_never_read_from_its_gaps(self):
        # two twice-yearly grants a month apart: gaps of 1 and 5 months
        # describe neither grant, so no clash may be raised against the entry
        self.source("p1", "Stocks",
                    [20260115, 20260215, 20260715, 20260815],
                    [300000, 1145000, 300000, 1145000])
        self.write_sources("- Stocks\n  - frequency: 4 per year\n"
                           "  - incomes: 2")
        _, out = self.report(date(2026, 9, 10))
        self.assertNotIn("check:", out)
        self.assertIn("- Stocks (4 per year):", out)

    def test_bad_incomes_value_is_reported(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme\n  - incomes: two")
        _, out = self.report()
        self.assertIn("check: cash-flow.md gives incomes 'two', which "
                      "is not a count", out)

    def test_two_problems_with_one_entry_both_show(self):
        # bad incomes falls back to 1, which lets the dates be read, which
        # then clashes with the frequency — neither note may hide the other
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme\n  - frequency: monthly\n  - incomes: two")
        _, out = self.report()
        self.assertIn("which is not a count", out)
        self.assertIn("says monthly, the dates say every 2 weeks", out)

    def test_fewer_payments_than_incomes_keeps_the_whole_window(self):
        # no whole round exists yet, so the average leans and the line says so
        self.source("p1", "Stocks", [20260115, 20260415],
                    [300000, 1145000])
        self.write_sources("- Stocks\n  - frequency: 4 per year\n"
                           "  - incomes: 3")
        total, out = self.report(date(2026, 5, 10))
        self.assertEqual(round(total), round((300000 + 1145000) / 2 * 4 / 12))
        self.assertIn("2 of 3 incomes averaged", out)

    def test_bonus_paycheck_is_an_outlier(self):
        self.source("p1", "Acme", self.BIWEEKLY,
                    [100000, 100000, 100000, 100000, 100000, 290000])
        self.write_sources("- Acme\n  - frequency: every 2 weeks")
        total, out = self.report()
        self.assertEqual(round(total), round(100000 * 26 / 12))
        self.assertIn("- Acme (every 2 weeks): $2,167 monthly "
                      "(outlier, using previous)", out)
        self.assertIn("payment $1,000.00, latest 2026-03-13", out)

    def test_two_agreeing_payments_confirm_a_raise(self):
        self.source("p1", "Acme", self.BIWEEKLY,
                    [100000] * 4 + [120000, 120000])
        self.write_sources("- Acme\n  - frequency: every 2 weeks")
        total, out = self.report()
        self.assertEqual(round(total), round(120000 * 26 / 12))
        self.assertIn("- Acme (every 2 weeks): $2,600 monthly "
                      "(last 2 consistent raise, using new)", out)

    def test_two_agreeing_payments_confirm_a_drop(self):
        self.source("p1", "Acme", self.BIWEEKLY,
                    [100000] * 4 + [80000, 80000])
        total, out = self.report()
        self.assertEqual(round(total), round(80000 * 26 / 12))
        self.assertIn("(last 2 consistent drop, using new)", out)

    def test_outlier_in_the_middle_leaves_the_latest_standing(self):
        self.source("p1", "Acme", self.BIWEEKLY,
                    [100000] * 4 + [290000, 100000])
        total, out = self.report()
        self.assertEqual(round(total), round(100000 * 26 / 12))
        self.assertNotIn("(outlier", out)

    def test_amount_claims_deposits_from_any_payee(self):
        # $2,000 transfers and $550 deposits share one bank payee; the
        # amount entry pulls the transfers into its own source
        self.source("p1", "Transfer", [20260110, 20260125, 20260210,
                                       20260225],
                    [200000, 55000, 200000, 55000])
        self.write_sources("- Acme Stocks\n  - frequency: monthly\n"
                           "  - amount: 2000")
        total, out = self.report(date(2026, 3, 1))
        self.assertEqual(round(total), 200000)
        self.assertIn("- Acme Stocks (monthly): $2,000 monthly", out)
        self.assertIn("- Transfer: not estimable", out)

    def test_amount_takes_a_list_and_dollar_forms(self):
        self.source("p1", "Transfer", [20260110, 20260210],
                    [200000, 170050])
        self.write_sources("- Acme Stocks\n  - frequency: monthly\n"
                           "  - amount: $2000 1700.50")
        total, out = self.report(date(2026, 3, 1))
        self.assertEqual(round(total), 170050)      # latest payment alone
        self.assertIn("- Acme Stocks (monthly):", out)
        self.assertNotIn("Transfer", out)           # payee fully claimed

    def test_amount_entry_with_no_match_yet(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme Stocks\n  - amount: 2000")
        _, out = self.report()
        self.assertIn("- Acme Stocks: no deposit matching its amount yet",
                      out)

    def test_thousands_separator_in_amount_is_reported(self):
        # '2,000' splits into '2' and '000'; the note on '000' surfaces it
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme Stocks\n  - amount: 2,000")
        _, out = self.report()
        self.assertIn("check: cash-flow.md gives amount '000', which "
                      "is not a dollar amount", out)

    def test_same_amount_on_two_entries_goes_to_the_first(self):
        self.source("p1", "Transfer", [20260110, 20260210], 200000)
        self.write_sources("- First\n  - frequency: monthly\n"
                           "  - amount: 2000\n"
                           "- Second\n  - amount: 2000")
        _, out = self.report(date(2026, 3, 1))
        self.assertIn("- First (monthly): $2,000 monthly", out)
        self.assertIn("- Second: no deposit matching its amount yet", out)
        self.assertIn("check: amount 2000 is also on First — matched there",
                      out)

    def test_two_payments_on_one_day_do_not_upset_detection(self):
        # a deposit split into two rows leaves a zero gap; the median holds
        self.source("p1", "Acme", [20260105, 20260205, 20260205, 20260305],
                    100000)
        _, out = self.report(date(2026, 4, 10))
        self.assertIn("- Acme (monthly):", out)

    def test_missing_file_leaves_every_source_on_its_dates(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        _, out = self.report()               # CASH_FLOW_FILE points at nothing
        self.assertIn("- Acme (every 2 weeks): $2,167 monthly", out)
        self.assertNotIn("check:", out)


# ---------------------------------------------------------------- month view

class TestPastMonth(Base):
    """A finished month: the Actual rows with the average column, the
    leftover, the outliers vs average and the streak; no Estimated or
    Forecast section."""

    def test_month_vs_average(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=140000)   # Feb: spending 3,400
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"), detailed=True)
        self.assertTrue(out.startswith(
            "Month — 2026-02 | average: 1 month (2026-01)\n\n### Actual\n"))
        self.assertIn("Income: $5,000 | average $5,000\n"
                      "  - Jan $5,000 (+0%) · Dec -\n"
                      "- Salary: $5,000 | average $5,000\n"
                      "  - Jan $5,000 (+0%) · Dec -\n", out)
        self.assertIn("Expenses: $3,400 | average $3,000\n"
                      "  - Jan $3,000 (+13%) · Dec -\n"
                      "- Fixed: $2,000 | average $2,000\n"
                      "  - Jan $2,000 (+0%) · Dec -\n"
                      "- Variable: $1,400 | average $1,000\n"
                      "  - Jan $1,000 (+40%) · Dec -\n"
                      "Leftover: +$1,600 | average +$2,000\n"
                      "  - Jan $2,000 (-20%) · Dec -\n", out)
        self.assertIn("Outliers vs average:\n- Groceries: $400 over", out)
        self.assertIn("Streak: green month — 2 in a row; "
                      "green: 2 of last 2 months.", out)
        self.assertNotIn("### Estimated", out)
        self.assertNotIn("### Forecast", out)
        self.assertNotIn("expected by now", out)

    def test_regular_size_has_no_history_rows(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=140000)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"))
        self.assertIn("Expenses: $3,400 | average $3,000\n"
                      "- Fixed: $2,000 | average $2,000\n", out)
        self.assertNotIn("Jan $", out)

    def test_first_month_has_no_average_columns(self):
        self.steady_month(202601)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-01"), detailed=True)
        self.assertTrue(out.startswith("Month — 2026-01 | first month: no average"))
        self.assertNotIn("| average", out)
        self.assertIn("Income: $5,000\n"
                      "  - Dec - · Nov -\n", out + "\n")
        self.assertIn("Leftover: +$2,000\n"
                      "  - Dec - · Nov -\n", out + "\n")
        self.assertNotIn("Outliers vs average", out)

    def test_red_month_wording(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=600000)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"))
        self.assertIn("Streak: red month; green: 1 of last 2 months.", out)
        self.assertIn("Income: $5,000", out)
        self.assertIn("Expenses: $8,000", out)
        self.assertIn("Leftover: -$3,000", out)

    def test_uncategorized_goes_to_check(self):
        self.steady_month(202601)
        tx(self.conn, "u1", 20260106, -10000)
        out = cf._status(self.ctx(date(2026, 2, 5)),
                         cf._parse_month("2026-01"))
        self.assertIn("Expenses: $3,100", out)
        self.assertIn("### Check\n- uncategorized: 1 transaction "
                      "($100 spending)", out)

    def test_current_month_is_the_status_view(self):
        self.steady_month(202601)
        out = cf._status(self.ctx(date(2026, 2, 5)),
                         cf._parse_month("2026-02"))
        self.assertTrue(out.startswith("2026-02, day 5"))
        self.assertIn("### Estimated", out)

    def test_future_and_pre_first_rejected(self):
        self.steady_month(202601)
        ctx = self.ctx(date(2026, 2, 5))
        with self.assertRaises(ValueError):
            cf._status(ctx, cf._parse_month("2026-03"))
        with self.assertRaises(ValueError):
            cf._status(ctx, cf._parse_month("2025-12"))

    def test_outliers_capped_and_ranked(self):
        category(self.conn, "din", "Dining", "gv")
        category(self.conn, "gas", "Gas", "gv")
        self.steady_month(202601)
        tx(self.conn, "d1", 20260110, -10000, "din")
        tx(self.conn, "x1", 20260110, -10000, "gas")
        # February moves: groceries +400, dining +200, gas +100, rent +50
        self.steady_month(202602, groc=140000, rent=205000)
        tx(self.conn, "d2", 20260210, -30000, "din")
        tx(self.conn, "x2", 20260210, -20000, "gas")
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"))
        lines = [l for l in out.splitlines() if l.startswith("- ")
                 and (" over" in l or " under" in l)]
        self.assertEqual(lines, ["- Groceries: $400 over",
                                 "- Dining: $200 over",
                                 "- Gas: $100 over"])


# ---------------------------------------------------------------- ledger

class TestLedgerMonth(Base):
    """ledger_month: one complete month's cents for the monthly-balance
    file — estimate as of the month's end, income split by the source
    category, spending."""

    def use_fixture_db(self):
        saved = cf.api_cache.db
        cf.api_cache.db = lambda: self.conn
        self.addCleanup(setattr, cf.api_cache, "db", saved)

    def january(self):
        """Biweekly $1,000 salary source, $500 one-off income, $100
        uncategorized deposit, $300 groceries — all in January."""
        payee(self.conn, "p1", "Acme")
        for i, d in enumerate((20260102, 20260116, 20260130)):
            tx(self.conn, f"a{i}", d, 100000, "salary", payee="p1")
        category(self.conn, "oneoff", "One-off", "gi")
        tx(self.conn, "w1", 20260120, 50000, "oneoff")
        tx(self.conn, "u1", 20260125, 10000)
        tx(self.conn, "g1", 20260110, -30000, "groc")

    def test_numbers(self):
        self.use_fixture_db()
        self.january()
        m = cf.ledger_month("2026-01", today=date(2026, 4, 10))
        self.assertEqual(round(m["estimate"]), round(100000 * 26 / 12))
        self.assertEqual(m["recurring"], 300000)
        self.assertEqual(m["one_off"], 60000)
        self.assertEqual(m["spending"], 30000)

    def test_estimate_ignores_later_payments(self):
        self.use_fixture_db()
        self.january()
        # a raise in February must not reach January's line
        tx(self.conn, "a9", 20260213, 900000, "salary", payee="p1")
        m = cf.ledger_month("2026-01", today=date(2026, 4, 10))
        self.assertEqual(round(m["estimate"]), round(100000 * 26 / 12))

    def test_one_off_group_spending_is_counted(self):
        self.use_fixture_db()
        self.january()
        cf.ONE_OFF_GROUPS = ["Variable"]
        m = cf.ledger_month("2026-01", today=date(2026, 4, 10))
        self.assertEqual(m["spending"], 30000)

    def test_incomplete_and_early_months_rejected(self):
        self.use_fixture_db()
        self.january()
        for month in ("2026-04", "2026-05", "2025-12"):
            with self.assertRaises(ValueError):
                cf.ledger_month(month, today=date(2026, 4, 10))


# ---------------------------------------------------------------- history view

class TestHistory(Base):

    def test_lines_newest_first_with_average(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=600000)
        out = cf._history(self.ctx(date(2026, 3, 5)), 6)
        lines = out.splitlines()
        self.assertEqual(lines[0], "History — last 2 complete months, "
                                   "newest first:")
        self.assertIn("- 2026-02: income $5,000 | spending $8,000 | "
                      "leftover -$3,000 | red", lines[1])
        self.assertIn("- 2026-01: income $5,000 | spending $3,000 | "
                      "leftover +$2,000 | green", lines[2])
        self.assertEqual(lines[3], "- average: 2 months (2026-01..2026-02): "
                         "income $5,000 | spending $5,500 | leftover -$500")

    def test_clamps_to_available(self):
        self.steady_month(202601)
        out = cf._history(self.ctx(date(2026, 2, 5)), 24)
        self.assertIn("last 1 complete month,", out)

    def test_no_complete_months(self):
        out = cf._history(self.ctx(date(2026, 1, 20)), 6)
        self.assertIn("no complete months yet", out)


# ---------------------------------------------------------------- entry points

class TestEntryPoints(Base):

    def use_fixture_db(self):
        saved = cf.api_cache.db
        cf.api_cache.db = lambda: self.conn
        self.addCleanup(setattr, cf.api_cache, "db", saved)

    def test_build_report_status_and_month(self):
        self.use_fixture_db()
        self.steady_month(202601)
        today = date(2026, 2, 5)
        self.assertTrue(cf.build_report(today=today)
                        .startswith("2026-02, day 5"))
        self.assertTrue(cf.build_report(month="2026-01", today=today)
                        .startswith("Month — 2026-01"))
        with self.assertRaises(ValueError):
            cf.build_report(month="jan", today=today)

    def test_build_report_categories(self):
        self.use_fixture_db()
        self.steady_month(202601)
        out = cf.build_report(today=date(2026, 2, 5), categories=True)
        self.assertIn("- Fixed: $0 | month $2,000\n"
                      "  - Rent: $0 | month $2,000\n", out)

    def test_history_report_validates_months(self):
        self.use_fixture_db()
        self.steady_month(202601)
        for bad in (0, 25, "6"):
            with self.assertRaises(ValueError):
                cf.history_report(bad, today=date(2026, 2, 5))
        self.assertIn("2026-01", cf.history_report(6, today=date(2026, 2, 5)))


class TestAssetsAndDebtSection(Base):
    """The status view carries debts.py's block under ### Assets and Debt
    when assets.md or debts.md exists, and its check items under ### Check;
    the block itself is tested in test_debts.py."""

    def test_status_carries_the_block(self):
        self.steady_month(202601)
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write("## low\n- House\n  - rate: 3.625%\n  - balances:\n"
                    "    - 2026-01: $100,000\n")
        self.addCleanup(os.unlink, path)
        debts.DEBTS_FILE = path
        out = cf._status(self.ctx(date(2026, 2, 5)))
        self.assertIn("### Assets and Debt", out)
        self.assertIn("Low Interest Debt:\n- House (3.625%): $100,000", out)

    def test_no_files_no_section(self):
        self.steady_month(202601)
        self.assertNotIn("Debt", cf._status(self.ctx(date(2026, 2, 5))))


if __name__ == "__main__":
    unittest.main()
