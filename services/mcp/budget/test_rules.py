#!/usr/bin/env python3
"""Tests for rules.py — stdlib unittest, in-memory SQLite shaped like the
api-cache copy. Run: python3 services/mcp/budget/test_rules.py from the repo
root."""

import json
import pathlib
import sqlite3
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import rules  # noqa: E402

PAYEES = {"p-acme": "Acme", "p-sched": "Monthly Payment", "p-mobile": "Mobile"}
CATS = {"c-acme": ("Acme", False), "c-shop": ("Shopping", False),
        "c-car": ("Loan", False), "c-monthly": ("Monthly", True)}


def make_conn(*stored):
    """A copy holding the given rules: (id, stage, conditions, category)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE rules (id TEXT, stage TEXT, conditions TEXT, actions TEXT, "
        "tombstone INT DEFAULT 0, conditions_op TEXT DEFAULT 'and');"
        "CREATE TABLE v_payees (id TEXT, name TEXT);"
        "CREATE TABLE v_transactions (payee TEXT, imported_payee TEXT);"
        "CREATE TABLE categories (id TEXT, name TEXT, is_income INT, "
        "tombstone INT DEFAULT 0);")
    conn.executemany("INSERT INTO v_payees VALUES (?, ?)", PAYEES.items())
    conn.executemany("INSERT INTO categories (id, name, is_income) VALUES (?, ?, ?)",
                     [(k, n, int(i)) for k, (n, i) in CATS.items()])
    for rid, stage, conds, cat in stored:
        conn.execute("INSERT INTO rules (id, stage, conditions, actions) VALUES (?, ?, ?, ?)",
                     (rid, stage, json.dumps(conds), json.dumps(
                         [{"field": "category", "op": "set", "value": cat}])))
    return conn


def payee_is(pid):
    return {"field": "description", "op": "is", "value": pid, "type": "id"}


def notes(text):
    return {"field": "notes", "op": "contains", "value": text}


def refusal(rule, conn, exclude=None):
    return rules.check(rule, rules.load(conn), PAYEES, CATS, rules.texts(conn), exclude)


class TestParse(unittest.TestCase):

    def test_built_rule_reads_back_the_same(self):
        rule = rules.build("c-car", imported="Monthly Payment",
                           amount=("isapprox", -50000),
                           repeat=("monthly", "2026-08-28"), notes="Monthly Pay")
        self.assertEqual(rules.parse(rules.entry_of(rule)), rule)
        self.assertEqual(rule["stage"], "post")
        self.assertIsNone(rules.build("c-car", payee="p-sched")["stage"])

    def test_other_shapes_are_web_interface_only(self):
        conn = make_conn(
            ("r1", None, [{"field": "imported_description", "op": "contains",
                           "value": "Example Bank"},
                          {"field": "notes", "op": "matches", "value": "^x$"}], "c-shop"),
            ("r2", "pre", [payee_is("p-acme")], "c-acme"))
        payees, cats = rules.names(conn)
        lines = [rules.line(e, payees, cats) for e in rules.load(conn)]
        self.assertEqual(lines, [
            "imported payee contains 'Example Bank' and notes matches \"^x$\" "
            "→ Shopping (web interface only)",
            "payee is Acme → Acme (web interface only)"])

    def test_approx_window_rounds_halves_up_like_the_app(self):
        self.assertEqual([rules.approx_slack(c) for c in (140, -140, 60, 50000)],
                         [11, 11, 5, 3750])

    def test_describe_reads_like_the_rule(self):
        rule = rules.build("c-car", payee="p-sched", amount=("isapprox", -50000),
                           repeat=("monthly", "2026-08-28"), notes="Monthly Pay")
        self.assertEqual(
            rules.describe(rules.entry_of(rule), PAYEES, CATS),
            "payee is Monthly Payment and amount is about -$500.00 and repeats "
            "monthly from 2026-08-28 and notes contain 'Monthly Pay' → Loan")


class TestPriority(unittest.TestCase):

    def test_highest_priority_first(self):
        conn = make_conn(
            ("plain", None, [payee_is("p-acme")], "c-shop"),
            ("amount", None, [payee_is("p-acme"),
                              {"field": "amount", "op": "is", "value": -500}], "c-shop"),
            ("notes", "post", [payee_is("p-acme"), notes("PAYROLL")], "c-monthly"))
        self.assertEqual([e["id"] for e in rules.by_priority(rules.load(conn))],
                         ["notes", "amount", "plain"])


class TestCheck(unittest.TestCase):

    def test_plain_rule_beside_normal_stage_notes_rule_is_refused(self):
        conn = make_conn(("n1", None, [payee_is("p-acme"), notes("PAYROLL")], "c-monthly"))
        text, _ = refusal(rules.build("c-shop", payee="p-acme"), conn)
        self.assertIn("rule n1", text)
        self.assertIn("would stop applying", text)

    def test_plain_rule_beside_post_stage_notes_rule_is_fine(self):
        conn = make_conn(("n1", "post", [payee_is("p-acme"), notes("PAYROLL")], "c-monthly"))
        self.assertEqual(refusal(rules.build("c-shop", payee="p-acme"), conn), (None, None))

    def test_notes_rule_beside_plain_rule_is_fine_in_post(self):
        conn = make_conn(("p1", None, [payee_is("p-acme")], "c-acme"))
        new = rules.build("c-monthly", payee="p-acme", notes="PAYROLL")
        self.assertEqual(refusal(new, conn), (None, None))

    def test_same_conditions_other_category_is_refused(self):
        conn = make_conn(("p1", None, [payee_is("p-acme")], "c-acme"))
        text, _ = refusal(rules.build("c-shop", payee="p-acme"), conn)
        self.assertIn("same conditions", text)
        self.assertIn("Update rule p1", text)

    def test_same_rule_already_there_is_covered(self):
        conn = make_conn(("p1", None, [payee_is("p-acme")], "c-acme"))
        text, covered = refusal(rules.build("c-acme", payee="p-acme"), conn)
        self.assertIsNone(text)
        self.assertEqual(covered["id"], "p1")

    def test_same_rule_already_there_is_not_refused_over_other_rules(self):
        conn = make_conn(("p1", None, [payee_is("p-acme")], "c-acme"),
                         ("w1", None, [payee_is("p-acme"),
                                       {"field": "notes", "op": "matches", "value": "^x$"}], "c-shop"))
        text, covered = refusal(rules.build("c-acme", payee="p-acme"), conn)
        self.assertIsNone(text)
        self.assertEqual(covered["id"], "p1")

    def test_narrower_rule_of_the_same_category_is_written(self):
        conn = make_conn(("p1", None, [payee_is("p-acme")], "c-acme"))
        self.assertEqual(refusal(rules.build("c-acme", payee="p-acme",
                                             amount=("isapprox", -1549)), conn), (None, None))

    def test_equal_rank_overlap_is_a_tie(self):
        conn = make_conn(("a1", None, [payee_is("p-sched"),
                                       {"field": "amount", "op": "isapprox", "value": -50000}],
                          "c-car"))
        new = rules.build("c-shop", payee="p-sched", repeat=("monthly", "2026-08-01"))
        text, _ = refusal(new, conn)
        self.assertIn("ranks both rules the same", text)

    def test_narrower_higher_rule_is_fine(self):
        conn = make_conn(("p1", None, [payee_is("p-sched")], "c-shop"))
        new = rules.build("c-car", payee="p-sched", amount=("isapprox", -50000))
        self.assertEqual(refusal(new, conn), (None, None))

    def test_imported_rule_against_payee_named_like_it(self):
        conn = make_conn(("i1", "post", [
            {"field": "imported_description", "op": "contains", "value": "monthly payment"},
            {"field": "amount", "op": "isapprox", "value": -50000}], "c-car"))
        new = rules.build("c-shop", payee="p-sched", amount=("is", -50000))
        text, _ = refusal(new, conn)
        self.assertIn("rule i1", text)
        self.assertIn("never apply", text)

    def test_imported_rule_against_a_payee_the_bank_names_differently(self):
        conn = make_conn(("p1", None, [payee_is("p-acme")], "c-acme"))
        conn.execute("INSERT INTO v_transactions VALUES ('p-acme', 'Brokerage Transfer')")
        text, _ = refusal(rules.build("c-shop", imported="brokerage"), conn)
        self.assertIn("rule p1", text)
        self.assertIn("would stop applying", text)

    def test_imported_rule_covers_a_payee_only_when_every_bank_text_holds_it(self):
        conn = make_conn(("i1", "post", [{"field": "imported_description", "op": "contains",
                                          "value": "AMZN"}], "c-shop"))
        conn.executemany("INSERT INTO v_transactions VALUES ('p-acme', ?)",
                         [("AMZN MKTP",), ("AMAZON.COM",)])
        self.assertEqual(refusal(rules.build("c-shop", payee="p-acme"), conn), (None, None))
        self.assertEqual(refusal(rules.build("c-acme", payee="p-acme"), conn), (None, None))

    def test_the_replaced_rule_is_left_out(self):
        conn = make_conn(("p1", None, [payee_is("p-acme")], "c-acme"))
        self.assertEqual(refusal(rules.build("c-shop", payee="p-acme"), conn,
                                 exclude="p1"), (None, None))

    def test_web_interface_rule_on_the_same_payee_is_refused(self):
        conn = make_conn(("w1", None, [
            {"field": "imported_description", "op": "contains", "value": "Monthly"},
            {"field": "notes", "op": "matches", "value": "^x$"}], "c-car"))
        text, _ = refusal(rules.build("c-shop", payee="p-sched"), conn)
        self.assertIn("rule w1", text)
        self.assertIn("web interface", text)
        self.assertEqual(refusal(rules.build("c-car", payee="p-sched"), conn), (None, None))
        self.assertEqual(refusal(rules.build("c-shop", payee="p-acme"), conn), (None, None))

    def test_repeat_start_day_29_and_feb_29(self):
        conn = make_conn()
        text, _ = refusal(rules.build("c-car", payee="p-sched",
                                      repeat=("monthly", "2026-07-30")), conn)
        self.assertIn("day 30", text)
        text, _ = refusal(rules.build("c-car", payee="p-sched",
                                      repeat=("yearly", "2028-02-29")), conn)
        self.assertIn("Feb 29", text)

    def test_spending_never_goes_to_income(self):
        conn = make_conn()
        text, _ = refusal(rules.build("c-monthly", payee="p-acme",
                                      amount=("is", -500)), conn)
        self.assertIn("income category", text)
        self.assertEqual(refusal(rules.build("c-monthly", payee="p-acme",
                                             amount=("is", 500)), conn), (None, None))
        self.assertIsNone(rules.income_refusal(CATS, "c-shop", 500))


if __name__ == "__main__":
    unittest.main()
