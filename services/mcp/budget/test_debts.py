#!/usr/bin/env python3
"""debts.py tests — temp files only, never the real finance file.
python3 -m pytest test_debts.py -q (or python3 test_debts.py)."""

import datetime
import os
import tempfile
import unittest
from unittest import mock

import debts


class Base(unittest.TestCase):
    """A temp finance folder, empty until write() puts a file in it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ASSISTANT_FINANCE_NOTES": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def write(self, body, assets=False):
        path = debts.assets_file() if assets else debts.debts_file()
        with open(path, "w") as f:
            f.write(body)
        return path


class TestCentsAndPayment(unittest.TestCase):
    """_cents/_payment dollar parsing — no fixture needed."""

    def test_nonzero_cents_parse_correctly(self):
        self.assertEqual(debts._cents("$100.50"), 10050)
        self.assertEqual(debts._payment({"payment": "$100.50 monthly"}),
                         10050)


class TestPct(unittest.TestCase):

    def test_whole_tenth_and_tiny(self):
        self.assertEqual(debts.pct(124, 100), "+24%")
        self.assertEqual(debts.pct(99600, 100000), "-0.4%")
        self.assertEqual(debts.pct(412396, 412400), "+0%")   # never "-0.0%"


class TestMissingFile(Base):

    def test_no_file_is_silent(self):
        self.assertEqual(debts.status_block(), ("", []))
        self.assertEqual(debts.validate(),
                         ["assets.md: not found", "debts.md: not found"])
        self.assertEqual(debts.populate_month(datetime.date(2026, 10, 1)),
                         [])


class TestBlock(Base):

    def test_detailed_facts_row(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - rate: 8.75%\n"
                   "  - payment: $2,000 monthly\n"
                   "  - balances:\n"
                   "    - 2026-06: $12,000\n"
                   "    - 2026-07: $10,000\n"
                   "## low\n"
                   "- House\n"
                   "  - balances:\n"
                   "    - 2026-07: $100,000\n")
        out, checks = debts.status_block(datetime.date(2026, 8, 15))
        self.assertNotIn("- total:", out)
        # 6 payments from Jul: the last one in Dec
        self.assertIn("Medium Interest Debt:\n- Card: $10,000 "
                      "(Jul 2026, -17% from Jun 2026)\n"
                      "  - Jul $10,000 · Apr -\n"
                      "  - 8.75% · pays $2,000 monthly"
                      " · paid off ~Dec 2026"
                      " · interest ~$73 monthly\n", out)
        self.assertIn("Low Interest Debt:\n- House: $100,000 (Jul 2026)\n"
                      "  - Jul $100,000 · Apr -", out)
        self.assertEqual(checks, [])

    def test_regular_size_keeps_rate_and_payoff(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - rate: 8.75%\n"
                   "  - payment: $2,000 monthly\n"
                   "  - note: ends soon\n"
                   "  - balances:\n"
                   "    - 2026-06: $12,000\n"
                   "    - 2026-07: $10,000\n"
                   "## low\n"
                   "- Car\n"
                   "  - rate: 4.75%\n"
                   "  - payment: $800 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $50,000\n")
        out, checks = debts.status_block(datetime.date(2026, 8, 15),
                                         detailed=False)
        self.assertIn("Medium Interest Debt:\n- Card: $10,000 "
                      "(Jul 2026, -17% from Jun 2026)\n"
                      "  - 8.75% · paid off ~Dec 2026\n", out)
        self.assertNotIn("Jul $10,000", out)  # no history row
        self.assertNotIn("pays", out)
        self.assertNotIn("ends soon", out)
        # low debts carry the rate alone
        self.assertIn("- Car: $50,000 (Jul 2026)\n  - 4.75%", out + "\n")
        self.assertNotIn("Car: $50,000 (Jul 2026)\n  - 4.75% ·", out)
        self.assertEqual(checks, [])

    def test_regular_size_asset_keeps_notes_without_history(self):
        self.write("## assets\n"
                   "- House\n"
                   "  - note: appraised\n"
                   "  - balances:\n"
                   "    - 2026-04: $100,000\n"
                   "    - 2026-07: $110,000\n",
                   assets=True)
        out, _ = debts.status_block(datetime.date(2026, 8, 15),
                                    detailed=False)
        self.assertIn("Assets:\n- House: $110,000 (Jul 2026, +10% from Apr 2026)\n"
                      "  - appraised", out)
        self.assertNotIn("Jul $110,000", out)   # no history row

    def test_unreadable_rate_payment_and_shared_category_reach_check(self):
        # each would otherwise fall back silently: a promotion read as
        # permanent, a payment replaced by the average, an average counted
        # twice
        self.write("## medium\n"
                   "- Card\n"
                   "  - rate: 0% Until Jan 2027, then 24%\n"
                   "  - payment: about $300\n"
                   "  - category: Debt Card\n"
                   "  - balances:\n"
                   "    - 2026-07: $1,000\n"
                   "- Card two\n"
                   "  - category: Debt Card\n"
                   "  - balances:\n"
                   "    - 2026-07: $1,000\n")
        checks = debts.status_block(datetime.date(2026, 8, 15))[1]
        self.assertEqual(checks, [
            "Card: rate '0% Until Jan 2027, then 24%' is not "
            "'X% until Mon YYYY, then Y%'",
            "Card: payment 'about $300' does not start with '$N monthly'",
            "category 'Debt Card' is on Card and Card two — one debt per "
            "category"])

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
        self.assertIn("- Loan: $20,000 (Aug 2026, -9% from Jul 2026)\n"
                      "  - Jul $22,000 (-9%) · Apr $25,000 (-20%)", out)
        self.assertIn("- Quarterly: $22,000 (Jul 2026, -12% from Apr 2026)\n"
                      "  - Jul $22,000 · Apr $25,000 (-12%)", out)

    def test_history_percent_small_change_one_decimal(self):
        """A change that rounds to 0% but is not 0 shows one decimal; no
        change at all stays +0%."""
        self.write("## medium\n"
                   "- Mortgage\n"
                   "  - balances:\n"
                   "    - 2026-04: $201,000\n"
                   "    - 2026-07: $200,000\n"
                   "    - 2026-08: $200,000\n")
        out, _ = debts.status_block(datetime.date(2026, 8, 15))
        self.assertIn("  - Jul $200,000 (+0%) · Apr $201,000 (-0.5%)", out)

    def test_payoff_is_the_last_payment_month(self):
        # the July balance is before July's payment: 12 payments, Jul-Jun
        self.write("## medium\n"
                   "- Loan\n"
                   "  - rate: 9%\n"
                   "  - payment: $2,200 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $25,000\n")
        self.assertIn("paid off ~Jun 2027", debts.status_block()[0])

    def test_zero_interest_payoff_uses_ceil_bal_over_payment(self):
        # no rate field: r is 0, so three payments, Jul-Sep — bal / payment
        # lands exactly on 3, so even a sliver of interest (the wrong
        # branch) would push it to Oct
        self.write("## medium\n"
                   "- Loan\n"
                   "  - payment: $300 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $900\n")
        self.assertIn("paid off ~Sep 2026", debts.status_block()[0])

    def test_payment_short_of_the_interest_never_pays_off(self):
        self.write("## high\n"
                   "- Card\n"
                   "  - rate: 24%\n"
                   "  - payment: $10 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $1,000\n")
        today = datetime.date(2026, 8, 15)
        self.assertIn("  - 24% · never paid off at $10 monthly",
                      debts.status_block(today, detailed=False)[0])
        self.assertIn("  - 24% · pays $10 monthly · never paid off at this "
                      "pace · interest ~$20 monthly",
                      debts.status_block(today)[0])

    def test_promotional_rate_steps_then_jumps(self):
        # 0% for Oct-Dec takes $3,000 off; the $2,000 left at 24% (2% a
        # month) needs Jan, Feb and Mar
        self.write("## medium\n"
                   "- Transfer\n"
                   "  - rate: 0% until Jan 2027, then 24%\n"
                   "  - payment: $1,000 monthly\n"
                   "  - balances:\n"
                   "    - 2026-10: $5,000\n")
        out = debts.status_block(datetime.date(2026, 10, 15))[0]
        self.assertIn("  - 0% until Jan 2027, then 24% · pays $1,000 monthly"
                      " · paid off ~Mar 2027"
                      " · $1,667 monthly clears it by Jan 2027"
                      " · interest ~$0 monthly", out)
        # past the promotion the interest is at the second rate
        out = debts.status_block(datetime.date(2027, 2, 15))[0]
        self.assertIn("interest ~$100 monthly", out)
        self.assertNotIn("clears it", out)

    def test_category_average_is_the_payment(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - category: Debt Card\n"
                   "  - balances:\n"
                   "    - 2026-07: $1,500\n")
        today = datetime.date(2026, 8, 15)
        avg = lambda name, first: {"Debt Card": 50000}.get(name)
        self.assertIn("- Card: $1,500 (Jul 2026)\n"
                      "  - Jul $1,500 · Apr -\n"
                      "  - pays ~$500 monthly · paid off ~Sep 2026",
                      debts.status_block(today, average=avg)[0])
        # no average yet: no payment, no payoff
        self.assertNotIn("paid off", debts.status_block(today)[0])

    def test_never_at_an_average_payment(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - rate: 24%\n"
                   "  - category: Debt Card\n"
                   "  - balances:\n"
                   "    - 2026-07: $10,000\n")
        out, _ = debts.status_block(datetime.date(2026, 8, 15), False,
                                    lambda name, first: 13700)
        self.assertIn("  - 24% · never paid off at ~$137 monthly", out)

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
        self.assertIn("- Card: $10,000 (Jul 2026, -17% from Apr 2026)\n"
                      "  - Oct - · Jul $10,000\n", out)
        self.assertIn("Card: no Oct 2026 balance — using Jul 2026", checks)
        self.assertIn("- Fresh: $5,000 (Oct 2026)\n"
                      "  - Oct $5,000 · Jul -\n", out)
        # an off-cycle hand balance is the figure but never a slot: the
        # slots are always the quarter months
        out, _ = debts.status_block(datetime.date(2026, 8, 5))
        self.assertIn("- Hand: $8,000 (Aug 2026)\n"
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
        self.assertIn("House: no Jul 2026 balance — using Jun 2026", checks)
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
        self.assertIn("- Loan: $5,000 (Jul 2026)\n"
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
        # the change from the balance before, whatever the gap; no
        # monthly change row
        self.assertIn("Assets:\n- House: $110,000 (Jul 2026, +10% from Apr 2026)\n"
                      "  - Jul $110,000 · Apr $100,000 (+10%)\n", out)
        self.assertIn("- Fund: $49,000 (Jul 2026, -2% from Jun 2026)\n"
                      "  - Jul $49,000 · Apr -", out)
        self.assertNotIn("monthly", out)
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
        self.assertIn("House: no Oct 2026 balance — using Jul 2026", checks)

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


class TestPlans(Base):

    def test_one_plan_per_debt_with_a_category(self):
        self.write("## medium\n"
                   "- Loan\n"
                   "  - payment: $300 monthly\n"
                   "  - category: Debt Loan\n"
                   "  - balances:\n"
                   "    - 2026-07: $900\n"
                   "    - 2026-12: $500\n"
                   "- Card\n"
                   "  - category: Debt Card\n"
                   "  - balances:\n"
                   "    - 2026-06: $1,000\n"
                   "    - 2026-07: $0\n"
                   "## low\n"
                   "- House\n"
                   "  - payment: $2,000 monthly\n"
                   "  - balances:\n"
                   "    - 2026-07: $100,000\n")
        out = debts.plans(datetime.date(2026, 8, 15),
                          lambda name, first: {"Debt Card": 20000}.get(name))
        # the December balance is after today and never counts
        self.assertEqual(out, [
            {"name": "Loan", "category": "Debt Loan", "monthly": 30000,
             "average": False, "payoff": 2026 * 12 + 8, "first": 2026 * 12 + 6,
             "done": False},
            {"name": "Card", "category": "Debt Card", "monthly": 20000,
             "average": True, "payoff": None, "first": 2026 * 12 + 5,
             "done": True}])

    def test_no_file_no_plans(self):
        self.assertEqual(debts.plans(datetime.date(2026, 8, 15), None), [])


class TestValidate(Base):

    def test_clean_file(self):
        self.write("## assets\n"
                   "- Fund\n"
                   "  - balances:\n"
                   "    - 2026-07: $50,000\n",
                   assets=True)
        self.write("## low\n"
                   "- House\n"
                   "  - rate: 4%\n"
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

    def test_category_is_a_field_and_promotional_rates_parse(self):
        self.write("## medium\n"
                   "- Card\n"
                   "  - rate: 1% until Jan 2028, then 20%\n"
                   "  - category: Debt Card\n"
                   "  - balances:\n"
                   "    - 2026-07: $1,000\n"
                   "- Loan\n"
                   "  - rate: 5% until later, then 9%\n"
                   "  - balances:\n"
                   "    - 2026-07: $1,000\n")
        self.assertEqual(debts.validate(), [
            "assets.md: not found",
            "debts.md: Loan: rate '5% until later, then 9%' is not "
            "'X% until Mon YYYY, then Y%'"])


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
        self.assertIn("Loan: no Oct 2026 balance — using Jul 2026", checks)
        self.assertIn("House: no Oct 2026 balance — using Jul 2026", checks)
        self.assertNotIn("Fresh: no", "\n".join(checks))


if __name__ == "__main__":
    unittest.main()
