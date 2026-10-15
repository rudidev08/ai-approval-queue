#!/usr/bin/env python3
"""Tests for cash_flow.py — stdlib unittest, no live data.

The compute and render functions run on a context built from an in-memory
SQLite copy of the api-cache schema (v_transactions is a view in the budget app; a
same-shaped table reads identically). Constants (FIRST_MONTH, FIXED_GROUPS,
...) are overridden per test and restored. Run:
python3 -m pytest test_cash_flow.py -q (from this directory), or
python3 services/mcp/budget/test_cash_flow.py from the repo root.
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
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
    """Fixture: Income/Salary (the monthly source category), Income/Bonus
    (the periodic one), Fixed/Rent, Variable/Groceries; FIRST_MONTH
    2026-01."""

    def setUp(self):
        for name in ("FIRST_MONTH", "EXCLUDED_GROUPS", "FIXED_GROUPS",
                     "ONE_OFF_GROUPS", "ONE_OFF_INCOME",
                     "PERSONAL_CATEGORIES",
                     "MONTHLY_CATEGORY", "PERIODIC_CATEGORY", "CASH_ACCOUNTS"):
            saved = getattr(cf, name)
            self.addCleanup(setattr, cf, name, saved)
        cf.FIRST_MONTH = "2026-01"
        cf.EXCLUDED_GROUPS = []
        cf.FIXED_GROUPS = ["Fixed"]
        cf.ONE_OFF_GROUPS = []
        cf.ONE_OFF_INCOME = "Windfall"
        cf.MONTHLY_CATEGORY = "Salary"
        cf.PERIODIC_CATEGORY = "Bonus"
        cf.CASH_ACCOUNTS = ["Checking"]
        # an empty finance folder: no cash-flow.md until write_sources()
        # writes one, and no Assets and Debt section
        self._finance = tempfile.TemporaryDirectory()
        self.addCleanup(self._finance.cleanup)
        env = mock.patch.dict(os.environ, {"ASSISTANT_FINANCE_NOTES": self._finance.name})
        env.start()
        self.addCleanup(env.stop)
        self.conn = make_conn()
        group(self.conn, "gi", "Income", is_income=1, sort_order=0)
        group(self.conn, "gf", "Fixed", sort_order=1)
        group(self.conn, "gv", "Variable", sort_order=2)
        category(self.conn, "salary", "Salary", "gi")
        category(self.conn, "bonus", "Bonus", "gi")
        category(self.conn, "rent", "Rent", "gf")
        category(self.conn, "groc", "Groceries", "gv")

    def write_sources(self, body, upcoming="", periodic=""):
        """Write cash-flow.md into the temp finance folder, holding `body`
        under the monthly category's heading and `periodic` under the
        periodic one; `upcoming` fills its own section."""
        path = os.path.join(self._finance.name, "cash-flow.md")
        with open(path, "w") as f:
            f.write(f"notes\n\n- a prose bullet: ignored\n\n"
                    f"{cf._heading(cf.MONTHLY_CATEGORY)}\n\n{body}\n\n"
                    f"{cf._heading(cf.PERIODIC_CATEGORY)}\n\n{periodic}\n")
            if upcoming:
                f.write(f"\n{cf.UPCOMING_HEADING}\n\n{upcoming}\n")
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
        rows = [{"idx": 1, "cat": None, "inc": 500, "spend": -100},
                {"idx": 1, "cat": None, "inc": 300, "spend": -50}]
        d = cf._month_data(rows, 1, {})
        self.assertEqual(d["uncat_inc"], 800)
        self.assertEqual(d["uncat_spend"], 150)

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

    def test_header(self):
        out = self.april()
        self.assertIn("for Apr 2026, day 10 of 30 "
                      "| averages calculated with data from Jan 2026 (3 months)", out)
        # the income estimate is the source estimate, although $0 arrived
        # this month
        self.assertIn("Income estimate: $5,000 monthly", out)
        self.assertNotIn("Projected", out)
        self.assertNotIn("streak", out.casefold())

    def test_window_label_full_year(self):
        self.assertEqual(cf._window_label(list(range(100, 112))),
                         "averages calculated with data from "
                         f"{cf.debts.month_name(100)} (1 year)")

    def test_estimated_and_actual_grouping(self):
        out = self.april()
        self.assertLess(out.index("### Income and spending so far"), out.index("Income: $"))
        self.assertLess(out.index("Income: $"), out.index("Expenses: $"))
        self.assertLess(out.index("Expenses: $"), out.index("### Estimated"))
        self.assertLess(out.index("### Estimated"),
                        out.index("Income estimate"))
        self.assertLess(out.index("Income estimate"),
                        out.index("Expense estimate"))
        self.assertLess(out.index("Expense estimate"), out.index("### Forecast"))
        self.assertLess(out.index("### Forecast"),
                        out.index("How long that cash lasts"))

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
        actual = out[out.index("### Income and spending so far"):]
        self.assertIn("- Salary: $0\n"
                      "  - Mar $5,000 (-100%) · Feb $5,000 (-100%)\n",
                      actual + "\n")
        self.assertLess(actual.index("- Salary: $0"),
                        actual.index("- Fixed:"))
        self.assertIn("- Fixed: $2,000 ($2,000 month)\n"
                      "  - Mar $2,000 (+0%) · Feb $2,000 (+0%)", out)
        self.assertIn("- Variable: $400 ($333 est, $1,000 month)\n"
                      "  - Mar $1,000 (-60%) · Feb $1,000 (-60%)", out)
        # no finance-file problems, so no Check section at all
        self.assertNotIn("### Check", out)

    def test_forecast_never_runs_out_when_the_estimate_covers_itself(self):
        out = self.april()
        self.assertIn("Cash today: $3,600 in Checking.\n\n"
                      "How long that cash lasts, with monthly income "
                      "only:\n"
                      "- never runs out — $2,000 extra per month\n\n"
                      "Same, with periodic income added:\n"
                      "- never runs out — $2,000 extra per month\n",
                      out + "\n")

    def test_forecast_burns_when_expenses_win(self):
        # $50,000 starting balance counts in the balance but in no average;
        # $6,000 average spending against the $5,000 income estimate
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        for ym in (202601, 202602, 202603):
            self.steady_month(ym, groc=400000)
        out = cf._status(self.ctx(date(2026, 4, 10)))
        self.assertIn("Expense estimate: $6,000 monthly", out)
        # 47,000 balance / 1,000 burn
        self.assertIn("Cash today: $47,000 in Checking.", out)
        self.assertIn("- net spending $1,000 monthly, lasts about 47.0 "
                      "months — runs out around Mar 2030", out)

    def test_categories_nest_under_the_group_row(self):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "g4", 20260405, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)), detailed=True,
                         categories=True)
        self.assertIn("- Variable: $400 ($333 est, $1,000 month)\n"
                      "  - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n"
                      "  - Groceries: $400 ($333 est, $1,000 month)\n"
                      "    - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n",
                      out + "\n")
        # the fixed group's categories share its wording
        self.assertIn("- Fixed: $0 ($2,000 month)\n"
                      "  - Mar $2,000 (-100%) · Feb $2,000 (-100%)\n"
                      "  - Rent: $0 ($2,000 month)\n", out)

    def test_categories_regular_size_has_no_history(self):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "g4", 20260405, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 4, 10)), categories=True)
        self.assertIn("- Variable: $400 ($333 est, $1,000 month)\n"
                      "  - Groceries: $400 ($333 est, $1,000 month)\n",
                      out + "\n")
        self.assertNotIn("Mar $1,000", out)

    def test_no_categories_by_default(self):
        out = self.april()
        self.assertNotIn("Groceries", out)
        self.assertNotIn("Rent", out)

    def personal_month(self):
        """The Variable group gains five personal categories, spent in
        2026-02 only: Personal (combined) $240, Recurring (combined) $60."""
        for cid, name in (("personal", "Personal"),
                          ("alex", "Personal (Alex)"),
                          ("sam", "Personal (Sam)"),
                          ("alexr", "Recurring (Alex)"),
                          ("samr", "Recurring (Sam)")):
            category(self.conn, cid, name, "gv")
        for ym in (202601, 202602):
            self.steady_month(ym)
        for i, (cid, cents) in enumerate(
                (("personal", -1000), ("alex", -20000),
                 ("sam", -3000), ("alexr", -5000), ("samr", -1000))):
            tx(self.conn, f"p{i}", 20260205, cents, cid)

    def test_combine_personal_joins_the_per_person_rows(self):
        self.personal_month()
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         categories=True, combine_personal=True)
        # the group total is untouched, so its rows still add up to it
        self.assertIn("- Variable: $1,300 ($1,000 avg, +30%)\n"
                      "  - Groceries: $1,000 ($1,000 avg, +0%)\n"
                      "  - Personal (combined): $240 ($0 avg)\n"
                      "  - Recurring (combined): $60 ($0 avg)\n",
                      out + "\n")
        for name in ("Personal:", "(Alex)", "(Sam)"):
            self.assertNotIn(name, out)

    def test_per_person_rows_print_by_name_without_the_setting(self):
        self.personal_month()
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         categories=True)
        self.assertIn("  - Personal: $10 ($0 avg)\n", out + "\n")
        self.assertIn("  - Personal (Sam): $30 ($0 avg)\n", out + "\n")
        self.assertIn("  - Recurring (Alex): $50 ($0 avg)\n", out + "\n")
        self.assertNotIn("combined", out)

    def test_combined_row_history_sums_its_categories(self):
        self.personal_month()
        tx(self.conn, "p9", 20260105, -10000, "alex")     # Jan: $100
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         detailed=True, categories=True, combine_personal=True)
        self.assertIn("  - Personal (combined): $240 ($100 avg, +140%)\n"
                      "    - Jan $100 (+140%) · Dec -\n", out + "\n")

    def test_categories_past_month_use_the_average_column(self):
        for ym in (202601, 202602):
            self.steady_month(ym)
        out = cf._status(self.ctx(date(2026, 3, 5)), cf._idx(2026, 2),
                         categories=True)
        self.assertTrue(out.startswith("for Feb 2026 | averages calculated with data from Jan 2026 (1 month)"))
        self.assertIn("- Variable: $1,000 ($1,000 avg, +0%)\n"
                      "  - Groceries: $1,000 ($1,000 avg, +0%)\n", out + "\n")


class TestHist(Base):
    """The Income and spending rows' two-month history row: each month with
    the percent the current figure changed against it, '-' before
    FIRST_MONTH."""

    def test_percent_compares_to_the_newest_previous_month(self):
        for ym in (202601, 202602):
            self.steady_month(ym)
        self.steady_month(202603, groc=500000)
        out = cf._status(self.ctx(date(2026, 4, 10)), detailed=True)
        self.assertIn("- Variable: $0 ($778 est, $2,333 month)\n"
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
        self.assertIn("- Variable: $400 ($222 est, $667 month)\n"
                      "  - Mar $0 · Feb $1,000 (-60%)", out)

    def test_months_before_first_month_print_a_dash(self):
        self.steady_month(202601)
        tx(self.conn, "g2", 20260205, -40000, "groc")
        out = cf._status(self.ctx(date(2026, 2, 10)), detailed=True)
        self.assertIn("- Variable: $400 ($357 est, $1,000 month)\n"
                      "  - Jan $1,000 (-60%) · Dec -", out)


class TestOneOff(Base):
    """ONE_OFF_GROUPS: counted in every month's totals — the leftover and
    history tell the truth — but in no average: rows print plain, and the
    runs-out divisor never sees it. 'Variable' plays the
    one-off group here."""

    def april(self, detailed=False):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "r4", 20260401, -200000, "rent")
        tx(self.conn, "g4", 20260405, -40000, "groc")
        return cf._status(self.ctx(date(2026, 4, 10)), detailed=detailed)

    def test_so_far_totals_count_the_one_off(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        out = self.april(detailed=True)
        self.assertIn("Expenses: $2,400\n"
                      "  - Mar $3,000 (-20%) · Feb $3,000 (-20%)\n",
                      out + "\n")

    def test_status_row_is_plain(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        out = self.april(detailed=True)
        self.assertIn("- Variable: $400\n"
                      "  - Mar $1,000 (-60%) · Feb $1,000 (-60%)\n",
                      out + "\n")
        self.assertNotIn("- Variable: $400 (", out)

    def test_expense_estimate_skips_the_one_off(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        out = self.april()
        self.assertIn("Expense estimate: $2,000 monthly", out)
        # and so does the forecast's burn: $5,000 income against $2,000
        self.assertIn("- never runs out — $3,000 extra per month", out)
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
        self.assertIn("Expenses: $3,000 ($2,000 avg, +50%)\n"
                      "  - Jan $3,000 (+0%) · Dec -", out)
        self.assertIn("Leftover: +$2,000\n"
                      "  - Jan $2,000 (+0%) · Dec -", out)
        self.assertIn("- Variable: $1,000\n"
                      "  - Jan $1,000 (+0%) · Dec -\n",
                      out + "\n")
        self.assertNotIn("- Variable: $1,000 (", out)

    def test_one_off_income_counts_in_the_month_in_no_average(self):
        category(self.conn, "wind", "Windfall", "gi")
        self.steady_month(202601)
        tx(self.conn, "w1", 20260120, 300000, "wind")
        self.steady_month(202602)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"))
        self.assertIn("Income: $5,000 ($5,000 avg, +0%)\n", out)
        self.assertNotIn("Windfall", out)   # no row: $0 now, and no average
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-01"))
        self.assertIn("- Windfall: $3,000\n", out + "\n")

    def test_history_lines_count_it_the_average_line_does_not(self):
        cf.ONE_OFF_GROUPS = ["Variable"]
        self.steady_month(202601)
        self.steady_month(202602, groc=600000)
        out = cf._history(self.ctx(date(2026, 3, 5)), 6)
        self.assertIn("- Feb 2026: income $5,000 | spending $8,000 | "
                      "leftover -$3,000", out)
        self.assertIn("- average: 2 months (Jan 2026 to Feb 2026): "
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
        self.assertNotIn("### Check", out)
        # the income estimate stands on the sources alone and still prints
        self.assertIn("- (no payee): not estimable", out)


# ---------------------------------------------------------------- forecast

class TestForecast(Base):
    """The how-long-money-lasts block: the CASH_ACCOUNTS balance against
    the burn (expense estimate minus income estimate), first with the
    monthly income alone, then with the periodic income included."""

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
        self.assertIn("Cash today: $46,500 in Checking.", out)
        self.assertIn("lasts about 46.5 months", out)

    def test_balance_sums_every_listed_account(self):
        cf.CASH_ACCOUNTS = ["Checking", "Savings"]
        self.burn_months()
        self.conn.execute(
            "INSERT INTO accounts (id, name) VALUES ('a2', 'Savings')")
        tx(self.conn, "other", 20260406, 1000000, account="a2")
        out = cf._status(self.ctx(date(2026, 4, 10)))
        # 3 x -1,000 + 10,000 in Savings
        self.assertIn("Cash today: $7,000 in Checking and Savings.", out)
        self.assertIn("lasts about 7.0 months", out)

    def test_periodic_income_counts_in_the_second_line_only(self):
        self.burn_months()
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        payee(self.conn, "p1", "Client")
        tx(self.conn, "v1", 20260320, 600000, "bonus", payee="p1")
        self.write_sources("", periodic="- Client\n  - frequency: annual")
        out = cf._status(self.ctx(date(2026, 4, 10)))
        # $6,000 a year is $500 a month: the burn drops from $1,000 to
        # $500 against the $53,000 balance
        self.assertIn("How long that cash lasts, with monthly income "
                      "only:\n"
                      "- net spending $1,000 monthly, lasts about 53.0 "
                      "months — runs out around Sep 2030\n\n"
                      "Same, with periodic income added:\n"
                      "- net spending $500 monthly, lasts about 106.0 "
                      "months — runs out around Feb 2035", out)

    def test_first_month_burns_on_the_so_far_estimate(self):
        tx(self.conn, "s1", 20260101, 310000, "salary")
        tx(self.conn, "g1", 20260105, -30000, "groc")
        out = cf._status(self.ctx(date(2026, 1, 10)))
        # income not estimable yet; expenses so far $300 against a $2,800
        # balance
        self.assertIn("Cash today: $2,800 in Checking.", out)
        self.assertIn("- net spending $300 monthly, lasts about 9.3 months "
                      "— runs out around Oct 2026", out)

    def test_missing_account_is_flagged(self):
        cf.CASH_ACCOUNTS = ["Checking", "Nope"]
        out = self.april()
        self.assertIn("- account 'Nope' not found — fix CASH_ACCOUNTS "
                      "in cash_flow.py", out)
        self.assertNotIn("Cash today:", out)

    def test_closed_account_does_not_match(self):
        self.conn.execute("UPDATE accounts SET closed = 1 WHERE id = 'a1'")
        out = self.april()
        self.assertIn("- account 'Checking' not found — fix CASH_ACCOUNTS "
                      "in cash_flow.py", out)

    def test_negative_balance_is_not_enough(self):
        self.burn_months()
        tx(self.conn, "drain", 20260406, -400000)
        out = cf._status(self.ctx(date(2026, 4, 10)))
        self.assertIn("- not enough to cover expenses", out)


class TestUpcoming(Base):
    """The Upcoming section: hand-listed one-time expenses ahead. They print
    in the Estimated block, and the forecast takes the total off the
    balance."""

    def test_block(self):
        self.write_sources("", upcoming="- New laptop\n  - amount: $1600\n"
                                        "  - month: 2026-06\n"
                                        "- Fence\n  - amount: 500")
        out = self.april()
        self.assertIn("Upcoming one-time expenses: $2,100", out)
        self.assertIn("- New laptop: $1,600 (Jun 2026)", out)
        self.assertIn("- Fence: $500", out)

    def test_no_section_leaves_the_report_alone(self):
        self.write_sources("")
        out = self.april()
        self.assertNotIn("Upcoming", out)

    def test_forecast_subtracts_upcoming(self):
        self.write_sources("", upcoming="- New laptop\n  - amount: $5000")
        out = self.april()
        self.assertIn("Cash today: $3,600 in Checking. Minus $5,000 listed "
                      "upcoming expenses = -$1,400 left.", out)
        # the income still covers the expenses, so the months line stays
        self.assertIn("- never runs out — $2,000 extra per month", out)

    def test_upcoming_can_leave_not_enough(self):
        for ym in (202601, 202602, 202603):
            self.steady_month(ym, groc=400000)      # burn $1,000
        self.write_sources("", upcoming="- New laptop\n  - amount: $5000")
        out = cf._status(self.ctx(date(2026, 4, 10)))
        self.assertIn("- not enough to cover expenses", out)

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
        self.write_sources("", upcoming="- New laptop\n  - amount: $1600")
        out = self.april()
        self.assertNotIn("New laptop: no deposit yet", out)


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
        rows = cf._source_rows({"Acme": [(date(2025, 1, 1), 100000)]},
                               {"Acme": {"frequency": "annual"}},
                               date(2025, 1, 1) + timedelta(days=457))
        self.assertTrue(rows[0]["stopped"])


# ---------------------------------------------------------------- income sources

class TestIncomeSources(Base):
    """The per-payee income estimate. MONTHLY_CATEGORY is 'Salary' and
    PERIODIC_CATEGORY 'Bonus' in this fixture, so Income/Salary and
    Income/Bonus rows are the sources."""

    def source(self, pid, name, dates, amounts, cat="salary"):
        payee(self.conn, pid, name)
        if not isinstance(amounts, list):
            amounts = [amounts] * len(dates)
        for i, (d, a) in enumerate(zip(dates, amounts)):
            tx(self.conn, f"{pid}-{i}", d, a, cat, payee=pid)

    def report(self, today=date(2026, 3, 20)):
        # a week after the last BIWEEKLY payment: no fixture source reads
        # as stopped (every 2 weeks tolerates ~17 silent days)
        totals, lines = cf._income_section(self.ctx(today))
        return sum(totals.values()), "\n".join(lines)

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
        # one payee, two payments of different size, small-large-small so far:
        # the last round holds one of each
        dates = [20260115, 20260415, 20260715]
        amounts = [300000, 1200000, 300000]
        self.source("p1", "Dividends", dates, amounts)
        self.write_sources("- Dividends\n  - frequency: 4 per year\n"
                           "  - incomes: 2")
        total, out = self.report(date(2026, 8, 10))
        self.assertEqual(round(total), round((1200000 + 300000) / 2 * 4 / 12))
        self.assertIn("- Dividends (4 per year): $2,500 monthly", out)
        self.assertIn("2 payments averaged, average $7,500.00", out)

    def test_without_incomes_the_same_three_payments_read_as_outlier(self):
        # no incomes field, so the picker sees small-large-small and treats
        # the large middle payment as the outlier
        dates = [20260115, 20260415, 20260715]
        amounts = [300000, 1200000, 300000]
        self.source("p1", "Dividends", dates, amounts)
        self.write_sources("- Dividends\n  - frequency: 4 per year")
        total, out = self.report(date(2026, 8, 10))
        self.assertEqual(round(total), round(300000 * 4 / 12))
        self.assertIn("- Dividends (4 per year): $1,000 monthly", out)

    def test_one_payment_is_never_rounded_away(self):
        self.source("p1", "Dividends", [20260715], 612000)
        self.write_sources("- Dividends\n  - frequency: 4 per year\n"
                           "  - incomes: 2")
        total, out = self.report(date(2026, 8, 10))
        self.assertEqual(round(total), round(612000 * 4 / 12))
        self.assertIn("1 of 2 incomes averaged, average $6,120.00", out)

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
        self.source("p1", "Dividends", [20260101, 20260401, 20260701], 300000)
        self.write_sources("- Dividends\n  - frequency: 4 per year")
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
        self.source("p1", "Brokerage Transfer", self.BIWEEKLY, 100000)
        self.write_sources("- Brokerage Transfer\n  - label: Acme Dividends")
        _, out = self.report()
        self.assertIn("- Acme Dividends (every 2 weeks):", out)
        self.assertNotIn("Brokerage", out)

    def test_entry_with_no_deposit_yet_counts_nothing(self):
        self.write_sources("- Ghost\n  - frequency: monthly")
        total, out = self.report()
        self.assertEqual(total, 0)
        self.assertIn("- Ghost (monthly): no deposit yet", out)

    def test_first_deposit_gives_the_entry_a_figure(self):
        self.source("p1", "Ghost", [20260310], 70000)
        self.write_sources("- Ghost\n  - frequency: monthly")
        total, out = self.report()
        self.assertEqual(round(total), 70000)
        self.assertIn("- Ghost (monthly): $700 monthly", out)

    def test_regular_size_is_one_line_per_source(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme\n  - frequency: every 2 weeks\n"
                           "- Ghost\n  - frequency: monthly")
        _, lines = cf._income_section(self.ctx(date(2026, 3, 20)),
                                      detailed=False)
        out = "\n".join(lines)
        self.assertIn("- Acme: $2,167 monthly", out)
        self.assertIn("- Ghost: no deposit yet", out)
        self.assertNotIn("(every 2 weeks)", out)
        self.assertNotIn("(monthly)", out)
        self.assertNotIn("payment $1,000.00", out)

    def test_estimate_field_is_unknown(self):
        # the report reads no estimate field: a stand-in value never counts
        self.write_sources("- Ghost\n  - frequency: monthly\n"
                           "  - estimate: $500")
        total, out = self.report()
        self.assertEqual(total, 0)
        self.assertIn("- Ghost (monthly): no deposit yet", out)

    def test_periodic_sources_get_their_own_subtotal(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        payee(self.conn, "p2", "Client")
        tx(self.conn, "v1", 20260120, 600000, "bonus", payee="p2")
        self.write_sources("", periodic="- Client\n  - frequency: annual\n"
                                        "- Grant\n  - frequency: annual")
        totals, lines = cf._income_section(self.ctx(date(2026, 3, 20)))
        self.assertEqual(round(totals["Salary"]), round(100000 * 26 / 12))
        self.assertEqual(round(totals["Bonus"]), 50000)
        self.assertIn("Income estimate: $2,667 monthly\n\n"
                      "Salary income: $2,167\n"
                      "- Acme (every 2 weeks): $2,167 monthly\n"
                      "    payment $1,000.00, latest 2026-03-13\n"
                      "Bonus income, spread per month: $500\n"
                      "- Client (annual): $500 monthly\n"
                      "    payment $6,000.00, latest 2026-01-20\n"
                      "- Grant (annual): no deposit yet\n",
                      "\n".join(lines) + "\n")

    def test_empty_category_says_so(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        _, out = self.report()
        self.assertIn("Bonus income, spread per month: $0\n"
                      "- no payments yet in the Bonus income category", out)

    def test_prose_bullets_above_the_heading_are_not_sources(self):
        # write_sources always puts "- a prose bullet: ignored" above it
        self.write_sources("- Ghost\n  - frequency: monthly")
        _, out = self.report()
        self.assertNotIn("a prose bullet", out)

    def test_file_without_the_sources_heading_is_reported(self):
        with open(os.path.join(self._finance.name, "cash-flow.md"), "w") as f:
            f.write("- Acme\n  - frequency: monthly\n")
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        _, out = self.report()
        self.assertIn("has no '## Salary' heading, so no Salary entries "
                      "were read", out)
        self.assertIn("has no '## Bonus' heading, so no Bonus entries "
                      "were read", out)
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
        # two twice-yearly payments a month apart: gaps of 1 and 5 months
        # describe neither grant, so no clash may be raised against the entry
        self.source("p1", "Dividends",
                    [20260115, 20260215, 20260715, 20260815],
                    [300000, 1200000, 300000, 1200000])
        self.write_sources("- Dividends\n  - frequency: 4 per year\n"
                           "  - incomes: 2")
        _, out = self.report(date(2026, 9, 10))
        self.assertNotIn("check:", out)
        self.assertIn("- Dividends (4 per year):", out)

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
        self.source("p1", "Dividends", [20260115, 20260415],
                    [300000, 1200000])
        self.write_sources("- Dividends\n  - frequency: 4 per year\n"
                           "  - incomes: 3")
        total, out = self.report(date(2026, 5, 10))
        self.assertEqual(round(total), round((300000 + 1200000) / 2 * 4 / 12))
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
        self.write_sources("- Acme Dividends\n  - frequency: monthly\n"
                           "  - amount: 2000")
        total, out = self.report(date(2026, 3, 1))
        self.assertEqual(round(total), 200000)
        self.assertIn("- Acme Dividends (monthly): $2,000 monthly", out)
        self.assertIn("- Transfer: not estimable", out)

    def test_amount_takes_a_list_and_dollar_forms(self):
        self.source("p1", "Transfer", [20260110, 20260210],
                    [200000, 165000])
        self.write_sources("- Acme Dividends\n  - frequency: monthly\n"
                           "  - amount: $2000 1650.00")
        total, out = self.report(date(2026, 3, 1))
        self.assertEqual(round(total), 165000)      # latest payment alone
        self.assertIn("- Acme Dividends (monthly):", out)
        self.assertNotIn("Transfer", out)           # payee fully claimed

    def test_amount_entry_with_no_match_yet(self):
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme Dividends\n  - amount: 2000")
        _, out = self.report()
        self.assertIn("- Acme Dividends: no deposit yet", out)

    def test_thousands_separator_in_amount_is_reported(self):
        # '2,000' splits into '2' and '000'; the note on '000' surfaces it
        self.source("p1", "Acme", self.BIWEEKLY, 100000)
        self.write_sources("- Acme Dividends\n  - amount: 2,000")
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
        self.assertIn("- Second: no deposit yet", out)
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
        _, out = self.report()               # no cash-flow.md in the folder
        self.assertIn("- Acme (every 2 weeks): $2,167 monthly", out)
        self.assertNotIn("check:", out)


# ---------------------------------------------------------------- month view

class TestPastMonth(Base):
    """A finished month: the Income and spending rows with their average
    and the percent off it, and the leftover; no Estimated or Forecast
    section."""

    def test_month_vs_average(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=140000)   # Feb: spending 3,400
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"), detailed=True)
        self.assertTrue(out.startswith(
            "for Feb 2026 | averages calculated with data from Jan 2026 (1 month)\n\n### Income and spending\n"))
        self.assertIn("Income: $5,000 ($5,000 avg, +0%)\n"
                      "  - Jan $5,000 (+0%) · Dec -\n"
                      "- Salary: $5,000 ($5,000 avg, +0%)\n"
                      "  - Jan $5,000 (+0%) · Dec -\n", out)
        self.assertIn("Expenses: $3,400 ($3,000 avg, +13%)\n"
                      "  - Jan $3,000 (+13%) · Dec -\n"
                      "- Fixed: $2,000 ($2,000 avg, +0%)\n"
                      "  - Jan $2,000 (+0%) · Dec -\n"
                      "- Variable: $1,400 ($1,000 avg, +40%)\n"
                      "  - Jan $1,000 (+40%) · Dec -\n"
                      "Leftover: +$1,600\n"
                      "  - Jan $2,000 (-20%) · Dec -\n", out + "\n")
        self.assertNotIn("Streak", out)
        self.assertNotIn("### Estimated", out)
        self.assertNotIn("### Forecast", out)
        self.assertNotIn(" est)", out)

    def test_regular_size_has_no_history_rows(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=140000)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"))
        self.assertIn("Expenses: $3,400 ($3,000 avg, +13%)\n"
                      "- Fixed: $2,000 ($2,000 avg, +0%)\n", out)
        self.assertNotIn("Jan $", out)

    def test_first_month_has_no_average_columns(self):
        self.steady_month(202601)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-01"), detailed=True)
        self.assertTrue(out.startswith("for Jan 2026 | first month: no average"))
        self.assertNotIn(" avg", out)
        self.assertIn("Income: $5,000\n"
                      "  - Dec - · Nov -\n", out + "\n")
        self.assertIn("Leftover: +$2,000\n"
                      "  - Dec - · Nov -\n", out + "\n")

    def test_overspent_month_numbers(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=600000)
        out = cf._status(self.ctx(date(2026, 3, 5)),
                         cf._parse_month("2026-02"))
        self.assertIn("Income: $5,000", out)
        self.assertIn("Expenses: $8,000", out)
        self.assertIn("Leftover: -$3,000", out)

    def test_current_month_is_the_status_view(self):
        self.steady_month(202601)
        out = cf._status(self.ctx(date(2026, 2, 5)),
                         cf._parse_month("2026-02"))
        self.assertTrue(out.startswith("for Feb 2026, day 5"))
        self.assertIn("### Estimated", out)

    def test_future_and_pre_first_rejected(self):
        self.steady_month(202601)
        ctx = self.ctx(date(2026, 2, 5))
        with self.assertRaises(ValueError):
            cf._status(ctx, cf._parse_month("2026-03"))
        with self.assertRaises(ValueError):
            cf._status(ctx, cf._parse_month("2025-12"))

# ---------------------------------------------------------------- history view

class TestHistory(Base):

    def test_lines_newest_first_with_average(self):
        self.steady_month(202601)
        self.steady_month(202602, groc=600000)
        out = cf._history(self.ctx(date(2026, 3, 5)), 6)
        lines = out.splitlines()
        self.assertEqual(lines[0], "History — last 2 complete months, "
                                   "newest first:")
        self.assertEqual(lines[1], "- Feb 2026: income $5,000 | spending "
                                   "$8,000 | leftover -$3,000")
        self.assertEqual(lines[2], "- Jan 2026: income $5,000 | spending "
                                   "$3,000 | leftover +$2,000")
        self.assertEqual(lines[3], "- average: 2 months (Jan 2026 to Feb 2026): "
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
                        .startswith("for Feb 2026, day 5"))
        self.assertTrue(cf.build_report(month="2026-01", today=today)
                        .startswith("for Jan 2026"))
        with self.assertRaises(ValueError):
            cf.build_report(month="jan", today=today)

    def test_build_report_categories(self):
        self.use_fixture_db()
        self.steady_month(202601)
        out = cf.build_report(today=date(2026, 2, 5), categories=True)
        self.assertIn("- Fixed: $0 ($2,000 month)\n"
                      "  - Rent: $0 ($2,000 month)\n", out)

    def test_history_report_validates_months(self):
        self.use_fixture_db()
        self.steady_month(202601)
        for bad in (0, 25, "6"):
            with self.assertRaises(ValueError):
                cf.history_report(bad, today=date(2026, 2, 5))
        self.assertIn("Jan 2026", cf.history_report(6, today=date(2026, 2, 5)))


class TestAssetsAndDebtSection(Base):
    """The status view carries debts.py's block under ### Assets and Debt
    when assets.md or debts.md exists, and its check items under ### Check;
    the block itself is tested in test_debts.py."""

    def test_status_carries_the_block(self):
        self.steady_month(202601)
        with open(debts.debts_file(), "w") as f:
            f.write("## low\n- House\n  - rate: 4%\n  - balances:\n"
                    "    - 2026-01: $100,000\n")
        out = cf._status(self.ctx(date(2026, 2, 5)))
        self.assertIn("### Assets and Debt", out)
        self.assertIn("Low Interest Debt:\n- House: $100,000 (Jan 2026)\n"
                      "  - 4%", out)

    def test_no_files_no_section(self):
        self.steady_month(202601)
        self.assertNotIn("Debt", cf._status(self.ctx(date(2026, 2, 5))))



class TestDebts(Base):
    """debts.md debts with a category field: the current month's estimate
    and pace rows read each debt's payment instead of its category
    average, a paid-off debt leaves them, and the forecast steps down
    when a debt's payments end."""

    def setUp(self):
        super().setUp()
        category(self.conn, "card", "Debt Card", "gf")

    def write_debts(self, body):
        with open(debts.debts_file(), "w") as f:
            f.write("## medium\n" + body)

    def months(self, card=30000, **kw):
        """Jan-Mar steady, with `card` cents paid into Debt Card each month."""
        for ym in (202601, 202602, 202603):
            self.steady_month(ym, **kw)
            tx(self.conn, f"c{ym}", ym * 100 + 20, -card, "card")

    def april(self, **kw):
        return cf._status(self.ctx(date(2026, 4, 10)), categories=True, **kw)

    def test_listed_payment_replaces_the_average(self):
        self.months()
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $10,000\n")
        out = self.april()
        self.assertIn("- Fixed: $0 ($2,500 month)\n", out)
        self.assertIn("  - Debt Card: $0 ($500 month)\n", out)
        self.assertIn("Expense estimate: $3,500 monthly", out)
        # a past month keeps the plain average
        out = cf._status(self.ctx(date(2026, 4, 10)), cf._parse_month("2026-03"),
                         categories=True)
        self.assertIn("  - Debt Card: $300 ($300 avg, +0%)", out)

    def test_category_average_stays_when_no_payment_is_listed(self):
        self.months()
        self.write_debts("- Card\n  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $10,000\n")
        out = self.april(detailed=True)
        self.assertIn("  - Debt Card: $0 ($300 month)\n", out)
        self.assertIn("pays ~$300 monthly", out)

    def test_average_starts_at_the_first_balance(self):
        # a transfer that started in March: Jan and Feb paid nothing for it
        # and must not pull its $900 payment down to $300
        for ym in (202601, 202602, 202603):
            self.steady_month(ym)
        tx(self.conn, "c3", 20260320, -90000, "card")
        self.write_debts("- Card\n  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $10,000\n")
        out = self.april(detailed=True)
        self.assertIn("  - Debt Card: $0 ($900 month)\n", out)
        self.assertIn("pays ~$900 monthly", out)

    def test_unknown_payment_keeps_the_plain_average(self):
        # first balance this month: no month to average from yet
        self.months()
        self.write_debts("- Card\n  - category: Debt Card\n  - balances:\n"
                         "    - 2026-04: $10,000\n")
        self.assertIn("  - Debt Card: $0 ($300 month)\n", self.april())

    def test_hidden_group_leaves_the_estimate(self):
        self.months()
        self.conn.execute("UPDATE category_groups SET hidden = 1 WHERE id = 'gv'")
        self.assertIn("Expense estimate: $2,300 monthly", self.april())

    def test_paid_off_debt_leaves_the_estimate(self):
        self.months()
        self.write_debts("- Card\n  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $0\n")
        self.assertIn("Expense estimate: $3,000 monthly", self.april())

    def test_payoff_before_this_month_leaves_the_estimate(self):
        # Jan and Feb payments clear $600: nothing left to pay in April
        self.months()
        self.write_debts("- Card\n  - payment: $300 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-01: $600\n")
        self.assertIn("Expense estimate: $3,000 monthly", self.april())

    def test_hidden_category_leaves_the_estimate(self):
        self.months()
        self.conn.execute("UPDATE categories SET hidden = 1 WHERE id = 'card'")
        self.assertIn("Expense estimate: $3,000 monthly", self.april())

    def test_forecast_steps_down_after_the_last_payment(self):
        # burn $1,500 = $6,500 spending - $5,000 income; payments Mar-May,
        # so from June the burn is $1,000. 45,500 cash - 1,500 x 1.67
        # months to the end of May = 43,000, then 43 months more
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        self.months(card=50000, groc=400000)
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $1,500\n")
        out = self.april()
        self.assertIn("Cash today: $45,500 in Checking.", out)
        self.assertIn("- net spending $1,500 monthly, $1,000 from Jun 2026, "
                      "lasts about 44.7 months — runs out around", out)

    def test_forecast_never_runs_out_once_the_debt_ends(self):
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        self.months(card=300000, groc=50000)
        self.write_debts("- Card\n  - payment: $3,000 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $9,000\n")
        self.assertIn("- net spending $500 monthly, $2,500 extra per month "
                      "from Jun 2026 — never runs out", self.april())

    def test_debts_ending_together_make_one_step(self):
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        category(self.conn, "loan", "Debt Loan", "gf")
        self.months(card=50000, groc=400000)
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $1,500\n"
                         "- Loan\n  - payment: $200 monthly\n"
                         "  - category: Debt Loan\n  - balances:\n"
                         "    - 2026-03: $600\n")
        out = self.april()
        self.assertIn("- net spending $1,700 monthly, $1,000 from Jun 2026, "
                      "lasts about", out)

    def test_payment_outside_the_estimate_never_steps_down(self):
        # hidden: the estimate leaves it out, so its payoff changes nothing
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        self.months(card=50000, groc=400000)
        self.conn.execute("UPDATE categories SET hidden = 1 WHERE id = 'card'")
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $1,500\n")
        out = self.april()
        self.assertIn("- net spending $1,000 monthly, lasts about", out)
        self.assertNotIn("from Jun 2026", out)

    def test_one_off_category_never_steps_down(self):
        # One-off is in no estimate, so its payoff changes nothing
        tx(self.conn, "sb", 20260101, 5000000, starting_balance_flag=1)
        cf.ONE_OFF_GROUPS = ["Once"]
        group(self.conn, "go", "Once", sort_order=3)
        category(self.conn, "old", "Old Payoff", "go")
        self.months(card=0, groc=400000)
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Old Payoff\n  - balances:\n"
                         "    - 2026-03: $1,500\n")
        out = self.april()
        self.assertIn("- net spending $1,000 monthly, lasts about", out)
        self.assertNotIn("from Jun 2026", out)

    def test_cash_runs_out_before_the_payoff(self):
        self.months(card=50000, groc=400000)
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $50,000\n")
        tx(self.conn, "top", 20260402, 1000000)
        out = self.april()
        self.assertIn("- net spending $1,500 monthly, lasts about", out)
        self.assertNotIn("from ", out[out.index("### Forecast"):
                                      out.index("### Assets")])

    def test_checks_name_a_missing_category_and_a_missed_payment(self):
        self.months()
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-01: $10,000\n"
                         "- Loan\n  - category: Debt Loan\n  - balances:\n"
                         "    - 2026-01: $1,000\n")
        out = self.april()
        self.assertIn("### Check\n"
                      "- Card: paid $300 in Feb 2026 and $300 in Mar 2026, "
                      "against $500 monthly in debts.md\n"
                      "- Loan: debts.md gives category 'Debt Loan', which "
                      "is not a budget category outside ", out)

    def test_one_month_off_or_a_new_debt_is_no_check(self):
        self.months()
        tx(self.conn, "extra", 20260325, -20000, "card")   # March: $500
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-01: $10,000\n")
        self.assertNotIn("### Check", self.april())
        # Feb is before the first balance: the debt is too new to judge
        self.write_debts("- Card\n  - payment: $900 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-03: $10,000\n")
        self.assertNotIn("### Check", self.april())

    def test_finished_debt_is_not_checked(self):
        # paid off in January: Feb and Mar paying $0 is expected
        self.months(card=0)
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-01: $500\n")
        self.assertNotIn("### Check", self.april())
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-01: $10,000\n    - 2026-02: $0\n")
        self.assertNotIn("### Check", self.april())

    def test_check_report_carries_the_debt_checks(self):
        saved = cf.api_cache.db
        cf.api_cache.db = lambda: self.conn
        self.addCleanup(setattr, cf.api_cache, "db", saved)
        self.months()
        self.write_debts("- Card\n  - payment: $500 monthly\n"
                         "  - category: Debt Card\n  - balances:\n"
                         "    - 2026-01: $10,000\n")
        self.assertEqual(cf.check_report(date(2026, 4, 10)), [
            "Card: paid $300 in Feb 2026 and $300 in Mar 2026, against "
            "$500 monthly in debts.md"])


if __name__ == "__main__":
    unittest.main()
