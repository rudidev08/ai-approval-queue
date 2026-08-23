#!/usr/bin/env python3
"""debts.py tests — temp files only, never the real vault file.
python3 -m pytest test_debts.py -q (or python3 test_debts.py)."""

import datetime
import os
import tempfile
import unittest

import debts


class Base(unittest.TestCase):

    def setUp(self):
        self.addCleanup(setattr, debts, "DEBTS_FILE", debts.DEBTS_FILE)
        debts.DEBTS_FILE = os.path.join(tempfile.gettempdir(),
                                        "no-such-debts.md")

    def write(self, body):
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(body)
        self.addCleanup(os.unlink, path)
        debts.DEBTS_FILE = path
        return path


class TestMissingFile(Base):

    def test_no_file_is_silent(self):
        self.assertEqual(debts.status_block(), "")
        self.assertEqual(debts.validate(), ["debts.md: not found"])
        self.assertEqual(debts.populate_month(datetime.date(2026, 8, 1)), [])


class TestBlock(Base):

    def test_totals_pace_and_wording(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - rate: 8.75%\n"
                   "  - balances:\n"
                   "    - 2026-06: $12,000\n"
                   "    - 2026-07: $10,000\n"
                   "## low\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-07: $100,000\n")
        out = debts.status_block()
        self.assertIn("Debt — 2026-07:", out)
        self.assertIn("- medium: $10,000 | paid down $2,000 monthly", out)
        self.assertIn("- low: $100,000\n", out)
        self.assertNotIn("low: $100,000 |", out)   # no change on low
        self.assertIn("- total: $110,000", out)
        # pace payoff: 10,000 / 2,000 = 5 months from 2026-07
        self.assertIn("- Card (8.75%): $10,000 | paid down $2,000 monthly "
                      "| paid off ~2026-12 | interest ~$73 monthly", out)
        self.assertIn("- House: $100,000", out)
        self.assertNotIn("Check:", out)

    def test_amortization_beats_pace(self):
        self.write("## medium\n"
                   "- Loan\n"
                   "  - rate: 8.99%\n"
                   "  - payment: $2,187 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $25,000\n")
        self.assertIn("paid off ~2027-07", debts.status_block())

    def test_growing_balance_reads_up(self):
        self.write("## high\n"
                   "- Card\n"
                   "  - balances:\n"
                   "    - 2026-06: $1,000\n"
                   "    - 2026-07: $1,500\n")
        out = debts.status_block()
        self.assertIn("- high: $1,500 | up $500 monthly", out)
        self.assertNotIn("paid off", out)

    def test_quarterly_gap_reads_as_monthly_pace(self):
        self.write("## medium\n"
                   "- Loan\n"
                   "  - balances:\n"
                   "    - 2026-04: $9,000\n"
                   "    - 2026-07: $6,000\n")
        self.assertIn("paid down $1,000 monthly", debts.status_block())

    def test_missing_and_stale_debts_go_to_check(self):
        self.write("## high\n"
                   "- Mystery\n"
                   "  - balances:\n"
                   "    - 2026-07: ? (no statement yet)\n"
                   "## low\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-06: $100,000\n")
        out = debts.status_block()
        self.assertIn("- high: no balance yet — Mystery", out)
        self.assertIn("- total: $100,000 (without Mystery)", out)
        self.assertIn("- Mystery: no balance yet — no statement yet", out)
        self.assertIn("- House: no 2026-07 balance — using 2026-06", out)

    def test_notes_print_under_the_debt(self):
        self.write("## medium\n"
                   "- Loan\n"
                   "  - note: ends soon\n"
                   "  - balances:\n"
                   "    - 2026-07: $5,000 (statement day 12)\n")
        out = debts.status_block()
        self.assertIn("    ends soon", out)
        self.assertIn("    statement day 12", out)

    def test_parse_problems_surface_in_check(self):
        self.write("## steep\n"
                   "- Ghost\n"
                   "  - balances:\n"
                   "    - 2026-07: $1\n")
        out = debts.status_block()
        self.assertIn("Check:", out)
        self.assertIn("not a tier", out)


class TestValidate(Base):

    def test_clean_file(self):
        self.write("## low\n"
                   "- House\n"
                   "  - rate: 3.625%\n"
                   "  - balances:\n"
                   "    - 2026-07: $100,000\n")
        self.assertEqual(debts.validate(), [])

    def test_problems_are_named(self):
        self.write("## low\n"
                   "- House\n"
                   "  - color: blue\n"
                   "  - rate: cheap\n"
                   "  - payment: sometimes\n"
                   "  - balances:\n"
                   "    - 2026-13: $1\n"
                   "    - 2026-07: one dollar\n"
                   "    - 2026-06: $2\n"
                   "- House\n"
                   "  - rate: 1%\n")
        out = "\n".join(debts.validate())
        self.assertIn("unknown field 'color'", out)
        self.assertIn("rate 'cheap' has no leading percent", out)
        self.assertIn("payment 'sometimes' does not start", out)
        self.assertIn("'2026-13' is not a YYYY-MM month", out)
        self.assertIn("not a $ amount or '?': 'one dollar'", out)
        self.assertIn("2026-06 is not after 2026-07", out)
        self.assertIn("debt 'House' appears twice", out)
        self.assertIn("House: no balances list", out)


class TestPopulate(Base):

    BODY = ("# Debts\n\nnotes\n\n"
            "## medium\n"
            "- Loan\n"
            "  - rate: 5%\n"
            "  - balances:\n"
            "    - 2026-07: $5,000\n"
            "- Fresh\n"
            "  - balances:\n"
            "    - 2026-08: $1,000\n"
            "## low\n"
            "- House\n"
            "  - balances:\n"
            "    - 2026-07: $100,000\n")

    def test_adds_missing_month_only(self):
        path = self.write(self.BODY)
        added = debts.populate_month(datetime.date(2026, 8, 17))
        self.assertEqual(added, ["Loan", "House"])   # file order
        text = open(path).read()
        self.assertIn("    - 2026-07: $5,000\n    - 2026-08: ?\n", text)
        self.assertIn("    - 2026-07: $100,000\n    - 2026-08: ?\n", text)
        self.assertEqual(text.count("- 2026-08: ?"), 2)
        # untouched apart from the two added lines
        self.assertEqual(sorted(text.splitlines()),
                         sorted(self.BODY.splitlines()
                                + ["    - 2026-08: ?"] * 2))
        # second run adds nothing
        self.assertEqual(debts.populate_month(datetime.date(2026, 8, 20)), [])
        self.assertEqual(open(path).read(), text)

    def test_populated_month_flags_every_stale_debt(self):
        self.write(self.BODY)
        debts.populate_month(datetime.date(2026, 8, 17))
        out = debts.status_block()
        self.assertIn("- Loan: no 2026-08 balance — using 2026-07", out)
        self.assertIn("- House: no 2026-08 balance — using 2026-07", out)
        self.assertNotIn("Fresh: no", out)


if __name__ == "__main__":
    unittest.main()
