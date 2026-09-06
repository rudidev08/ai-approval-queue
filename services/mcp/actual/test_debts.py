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
        self.addCleanup(setattr, debts, "ASSETS_FILE", debts.ASSETS_FILE)
        debts.DEBTS_FILE = os.path.join(tempfile.gettempdir(),
                                        "no-such-debts.md")
        debts.ASSETS_FILE = os.path.join(tempfile.gettempdir(),
                                         "no-such-assets.md")

    def write(self, body, assets=False):
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(body)
        self.addCleanup(os.unlink, path)
        if assets:
            debts.ASSETS_FILE = path
        else:
            debts.DEBTS_FILE = path
        return path


class TestCentsAndPayment(unittest.TestCase):
    """_cents/_payment dollar parsing — no fixture needed."""

    def test_nonzero_cents_parse_correctly(self):
        self.assertEqual(debts._cents("$100.50"), 10050)
        self.assertEqual(debts._payment({"payment": "$100.50 monthly"}),
                         10050)


class TestMissingFile(Base):

    def test_no_file_is_silent(self):
        self.assertEqual(debts.status_block(), ("", []))
        self.assertEqual(debts.validate(),
                         ["assets.md: not found", "debts.md: not found"])
        self.assertEqual(debts.populate_month(datetime.date(2026, 10, 1)),
                         [])


class TestBlock(Base):

    def test_pace_and_wording(self):
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
        out, checks = debts.status_block(datetime.date(2026, 8, 15))
        self.assertNotIn("- total:", out)
        # pace payoff: 10,000 / 2,000 = 5 months from 2026-07
        self.assertIn("Medium Interest Debt:\n- Card (8.75%): $10,000\n"
                      "  - Jul $10,000 · Apr -\n"
                      "  - paid down $2,000 monthly"
                      " · paid off ~2026-12"
                      " · interest ~$73 monthly\n", out)
        self.assertIn("Low Interest Debt:\n- House: $100,000\n"
                      "  - Jul $100,000 · Apr -", out)
        self.assertEqual(checks, [])

    def test_regular_size_keeps_balance_and_rate_only(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - rate: 8.75%\n"
                   "  - note: ends soon\n"
                   "  - balances:\n"
                   "    - 2026-06: $12,000\n"
                   "    - 2026-07: $10,000\n")
        out, checks = debts.status_block(datetime.date(2026, 8, 15),
                                         detailed=False)
        self.assertIn("Medium Interest Debt:\n- Card (8.75%): $10,000",
                      out)
        self.assertNotIn("Jul", out)          # no history row
        self.assertNotIn("paid down", out)    # no facts row
        self.assertNotIn("ends soon", out)
        self.assertEqual(checks, [])

    def test_regular_size_asset_keeps_pace_without_history(self):
        self.write("## assets\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-04: $100,000\n"
                   "    - 2026-07: $110,000\n",
                   assets=True)
        out, _ = debts.status_block(datetime.date(2026, 8, 15),
                                    detailed=False)
        self.assertIn("Assets:\n- House: $110,000\n"
                      "  - up $3,333 monthly", out)
        self.assertNotIn("Jul $110,000", out)   # no history row

    def test_history_percent_against_each_month(self):
        """Each quarter month carries the change the current balance made
        against it; the month the balance itself came from carries none."""
        self.write("## medium\n"
                   "- Loan\n"
                   "  - balances:\n"
                   "    - 2026-04: $25,000\n"
                   "    - 2026-07: $22,000\n"
                   "    - 2026-08: $20,000\n"
                   "- Quarterly\n"
                   "  - balances:\n"
                   "    - 2026-04: $25,000\n"
                   "    - 2026-07: $22,000\n")
        out, _ = debts.status_block(datetime.date(2026, 8, 15))
        self.assertIn("- Loan: $20,000\n"
                      "  - Jul $22,000 (-9%) · Apr $25,000 (-20%)", out)
        self.assertIn("- Quarterly: $22,000\n"
                      "  - Jul $22,000 · Apr $25,000 (-12%)", out)

    def test_amortization_beats_pace(self):
        self.write("## medium\n"
                   "- Loan\n"
                   "  - rate: 8.99%\n"
                   "  - payment: $2,187 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $25,000\n")
        self.assertIn("paid off ~2027-07", debts.status_block()[0])

    def test_zero_interest_payoff_uses_ceil_bal_over_payment(self):
        # no rate field: r is 0, so payoff is ceil(bal / payment) months on —
        # bal / payment lands exactly on 3, so even a sliver of interest
        # (the wrong branch) would push it to 4 and miss this month
        self.write("## medium\n"
                   "- Loan\n"
                   "  - payment: $300 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $900\n")
        self.assertIn("paid off ~2026-10", debts.status_block()[0])

    def test_growing_balance_reads_up(self):
        self.write("## high\n"
                   "- Card\n"
                   "  - balances:\n"
                   "    - 2026-06: $1,000\n"
                   "    - 2026-07: $1,500\n")
        out, _ = debts.status_block(datetime.date(2026, 8, 15))
        self.assertIn("- Card: $1,500\n"
                      "  - Jul $1,500 · Apr -\n"
                      "  - up $500 monthly", out)
        self.assertNotIn("paid off", out)

    def test_quarterly_gap_reads_as_monthly_pace(self):
        self.write("## medium\n"
                   "- Loan\n"
                   "  - balances:\n"
                   "    - 2026-04: $9,000\n"
                   "    - 2026-07: $6,000\n")
        self.assertIn("paid down $1,000 monthly", debts.status_block()[0])

    def test_balance_history_row(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - balances:\n"
                   "    - 2026-04: $12,000\n"
                   "    - 2026-07: $10,000\n"
                   "    - 2026-10: ?\n"
                   "- Fresh\n"
                   "  - balances:\n"
                   "    - 2026-10: $5,000\n"
                   "- Hand\n"
                   "  - balances:\n"
                   "    - 2026-08: $8,000\n")
        # the two most recent quarter months, newest first; a month with
        # no balance typed yet prints '-'
        out, checks = debts.status_block(datetime.date(2026, 10, 5))
        self.assertIn("- Card: $10,000\n"
                      "  - Oct - · Jul $10,000\n", out)
        self.assertIn("Card: no 2026-10 balance — using 2026-07", checks)
        self.assertIn("- Fresh: $5,000\n"
                      "  - Oct $5,000 · Jul -\n", out)
        # an off-cycle hand balance is the figure but never a slot: the
        # slots are always the quarter months
        out, _ = debts.status_block(datetime.date(2026, 8, 5))
        self.assertIn("- Hand: $8,000\n"
                      "  - Jul - · Apr -", out)

    def test_unfilled_ask_goes_to_check(self):
        self.write("## high\n"
                   "- Mystery\n"
                   "  - balances:\n"
                   "    - 2026-07: ? (no statement yet)\n"
                   "## low\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-06: $100,000\n"
                   "    - 2026-07: ?\n")
        out, checks = debts.status_block()
        self.assertIn("High Interest Debt:\n"
                      "- Mystery: no balance yet — no statement yet",
                      out)
        self.assertIn("House: no 2026-07 balance — using 2026-06", checks)
        # the placeholder line says it; no extra check item
        self.assertNotIn("no balance yet", "\n".join(checks))

    def test_stale_between_asks_is_not_flagged(self):
        # a real newest balance line is never flagged, however old — the
        # next quarter's populate asks again
        self.write("## low\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-06: $100,000\n"
                   "- Cabin\n"
                   "  - balances:\n"
                   "    - 2026-07: $50,000\n")
        self.assertEqual(debts.status_block()[1], [])

    def test_notes_print_inline(self):
        self.write("## medium\n"
                   "- Loan\n"
                   "  - note: ends soon\n"
                   "  - balances:\n"
                   "    - 2026-07: $5,000 (statement day 12)\n")
        out, _ = debts.status_block(datetime.date(2026, 8, 15))
        self.assertIn("- Loan: $5,000\n"
                      "  - Jul $5,000 · Apr -\n"
                      "  - ends soon · statement day 12", out)

    def test_parse_problems_surface_in_check(self):
        self.write("## steep\n"
                   "- Ghost\n"
                   "  - balances:\n"
                   "    - 2026-07: $1\n")
        _, checks = debts.status_block()
        self.assertTrue(any("not a tier" in c for c in checks))


class TestAssets(Base):

    def test_assets_render_first_with_change(self):
        self.write("## assets\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-04: $100,000\n"
                   "    - 2026-07: $110,000\n"
                   "- Fund\n"
                   "  - balances:\n"
                   "    - 2026-06: $50,000\n"
                   "    - 2026-07: $49,000\n",
                   assets=True)
        out, checks = debts.status_block(datetime.date(2026, 8, 15))
        # quarterly gap spread to a monthly change
        self.assertIn("Assets:\n- House: $110,000\n"
                      "  - Jul $110,000 · Apr $100,000 (+10%)\n"
                      "  - up $3,333 monthly\n", out)
        self.assertIn("- Fund: $49,000\n"
                      "  - Jul $49,000 · Apr -\n"
                      "  - down $1,000 monthly", out)
        self.assertNotIn("paid", out)
        self.assertEqual(checks, [])

    def test_assets_come_before_the_debt_tiers(self):
        self.write("## assets\n"
                   "- Fund\n"
                   "  - balances:\n"
                   "    - 2026-07: $50,000\n",
                   assets=True)
        self.write("## low\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-07: $100,000\n")
        out, _ = debts.status_block()
        self.assertLess(out.index("Assets:"), out.index("Low Interest Debt:"))

    def test_asset_placeholder_and_unfilled_ask(self):
        self.write("## assets\n"
                   "- Fund\n"
                   "  - note: TODO: get the balance\n"
                   "  - balances:\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-07: $100,000\n"
                   "    - 2026-10: ?\n",
                   assets=True)
        out, checks = debts.status_block(datetime.date(2026, 10, 17))
        self.assertIn("- Fund: no balance yet — TODO: get the balance", out)
        self.assertIn("House: no 2026-10 balance — using 2026-07", checks)

    def test_populate_covers_both_files(self):
        assets_path = self.write("## assets\n"
                                 "- Fund\n"
                                 "  - balances:\n"
                                 "    - 2026-07: $50,000\n",
                                 assets=True)
        debts_path = self.write("## low\n"
                                "- House\n"
                                "  - balances:\n"
                                "    - 2026-07: $100,000\n")
        added = debts.populate_month(datetime.date(2026, 10, 17))
        self.assertEqual(added, ["Fund", "House"])   # assets file first
        self.assertIn("    - 2026-10: ?\n", open(assets_path).read())
        self.assertIn("    - 2026-10: ?\n", open(debts_path).read())

    def test_validate_names_the_assets_file(self):
        self.write("## assets\n"
                   "- Fund\n"
                   "  - color: blue\n",
                   assets=True)
        out = "\n".join(debts.validate())
        self.assertIn("assets.md: Fund: unknown field 'color'", out)
        self.assertIn("assets.md: Fund: no balances list", out)

    def test_both_files_contribute_checks_and_problems(self):
        # a bad heading in each file; both must survive, not just the one
        # processed last
        self.write("## nope\n- Fund\n  - balances:\n    - 2026-07: $1\n",
                   assets=True)
        self.write("## alsonope\n- Ghost\n  - balances:\n    - 2026-07: $1\n")
        _, checks = debts.status_block()
        self.assertIn("heading '## nope' is not a tier (assets)", checks)
        self.assertIn("heading '## alsonope' is not a tier "
                      "(high / medium / low)", checks)
        out = debts.validate()
        self.assertIn("assets.md: heading '## nope' is not a tier (assets)",
                      out)
        self.assertIn("debts.md: heading '## alsonope' is not a tier "
                      "(high / medium / low)", out)


class TestValidate(Base):

    def test_clean_file(self):
        self.write("## assets\n"
                   "- Fund\n"
                   "  - balances:\n"
                   "    - 2026-07: $50,000\n",
                   assets=True)
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
            "    - 2026-10: $1,000\n"
            "## low\n"
            "- House\n"
            "  - balances:\n"
            "    - 2026-07: $100,000\n")

    def test_adds_missing_quarter_month_only(self):
        path = self.write(self.BODY)
        added = debts.populate_month(datetime.date(2026, 10, 17))
        self.assertEqual(added, ["Loan", "House"])   # file order
        text = open(path).read()
        self.assertIn("    - 2026-07: $5,000\n    - 2026-10: ?\n", text)
        self.assertIn("    - 2026-07: $100,000\n    - 2026-10: ?\n", text)
        self.assertEqual(text.count("- 2026-10: ?"), 2)
        # untouched apart from the two added lines
        self.assertEqual(sorted(text.splitlines()),
                         sorted(self.BODY.splitlines()
                                + ["    - 2026-10: ?"] * 2))
        # second run adds nothing
        self.assertEqual(debts.populate_month(datetime.date(2026, 10, 20)), [])
        self.assertEqual(open(path).read(), text)

    def test_off_quarter_months_add_nothing(self):
        path = self.write(self.BODY)
        self.assertEqual(debts.populate_month(datetime.date(2026, 8, 17)), [])
        self.assertEqual(open(path).read(), self.BODY)

    def test_missing_trailing_newline_is_added_before_the_new_line(self):
        # the file body has no trailing newline on its last line
        path = self.write("## low\n"
                          "- House\n"
                          "  - balances:\n"
                          "    - 2026-07: $100,000")
        added = debts.populate_month(datetime.date(2026, 10, 17))
        self.assertEqual(added, ["House"])
        text = open(path).read()
        self.assertIn("    - 2026-07: $100,000\n    - 2026-10: ?\n", text)

    def test_populated_quarter_flags_unfilled_debts(self):
        self.write(self.BODY)
        debts.populate_month(datetime.date(2026, 10, 17))
        _, checks = debts.status_block(datetime.date(2026, 10, 17))
        self.assertIn("Loan: no 2026-10 balance — using 2026-07", checks)
        self.assertIn("House: no 2026-10 balance — using 2026-07", checks)
        self.assertNotIn("Fresh: no", "\n".join(checks))


if __name__ == "__main__":
    unittest.main()
