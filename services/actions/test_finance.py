#!/usr/bin/env python3
"""Tests for services/actions/finance.py — stdlib unittest, no live data.

Every test runs against a temp STATE_DIR, a fake api-cache SQLite built in
the temp directory, and patched _spawn / api_cache.run_budget_helper /
subprocess — nothing
here reads or writes the real budget, state, or hermes jobs. Run:
python3 -m pytest test_finance.py -q (from this directory), or
python3 services/actions/test_finance.py from the repo root.
"""

import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import finance


def make_budget(api_cache, categories=(), transactions=()):
    """A fake api-cache copy with just the tables finance.py reads.
    categories: (id, name, group_name); transactions: (id, category,
    transfer_id). v_transactions is a real view in Actual; a table with the
    same columns reads identically."""
    budget = pathlib.Path(api_cache) / "My-Budget-abc123"
    budget.mkdir(parents=True, exist_ok=True)
    (budget / "db.sqlite").unlink(missing_ok=True)   # rebuild from scratch
    conn = sqlite3.connect(budget / "db.sqlite")
    conn.executescript(
        "CREATE TABLE category_groups (id TEXT, name TEXT, tombstone INT "
        "DEFAULT 0, hidden INT DEFAULT 0, sort_order REAL DEFAULT 0);"
        "CREATE TABLE categories (id TEXT, name TEXT, cat_group TEXT, "
        "tombstone INT DEFAULT 0, hidden INT DEFAULT 0);"
        "CREATE TABLE v_transactions (id TEXT, category TEXT, "
        "transfer_id TEXT, date INT, amount INT, payee TEXT, "
        "is_parent INT DEFAULT 0, starting_balance_flag INT DEFAULT 0, "
        "account TEXT DEFAULT 'a1');"
        "CREATE TABLE accounts (id TEXT, name TEXT, offbudget INT DEFAULT 0, "
        "tombstone INT DEFAULT 0, closed INT DEFAULT 0, "
        "sort_order REAL DEFAULT 0);"
        "CREATE TABLE v_payees (id TEXT, name TEXT);")
    conn.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Checking')")
    groups = {}
    for cid, name, grp in categories:
        if grp not in groups:
            gid = f"g-{len(groups)}"
            groups[grp] = gid
            conn.execute("INSERT INTO category_groups (id, name) VALUES (?, ?)",
                         (gid, grp))
        conn.execute("INSERT INTO categories (id, name, cat_group) "
                     "VALUES (?, ?, ?)", (cid, name, groups[grp]))
    for tid, cat, transfer in transactions:
        conn.execute("INSERT INTO v_transactions (id, category, transfer_id) "
                     "VALUES (?, ?, ?)", (tid, cat, transfer))
    conn.commit()
    conn.close()


def card(tid="t1", **over):
    c = {"transaction_id": tid, "date": "2026-08-10", "payee": "Store",
         "amount": "-12.34", "notes": "", "account": "Checking",
         "account_id": "a1", "pick": "latest", "suggestions": []}
    c.update(over)
    return c


def full_batch():
    """Every scan slot taken."""
    return [card(f"l{i}") for i in range(finance.LATEST_CAP)]


class _FakeProc:
    """A stand-in for subprocess.run's CompletedProcess — just the
    attributes run_budget_helper reads."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FinanceTest(unittest.TestCase):
    """Temp STATE_DIR + fake api-cache; _spawn recorded, never run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self._saved = (common.STATE_DIR, finance.STATE_FILE, finance.API_CACHE,
                       common.CRON_JOBS, common.CRON_EXECUTIONS,
                       finance.STATE, finance._spawn, finance.ODD_FILE,
                       finance.ODD_SEEN_FILE, finance.ODD, finance.ODD_SEEN,
                       finance._ODD_ROWS)
        common.STATE_DIR = tmp
        finance.STATE_FILE = tmp / "finance.json"
        finance.ODD_FILE = tmp / "finance-oddities.json"
        finance.ODD_SEEN_FILE = tmp / "finance-oddities-seen.json"
        finance.ODD = finance.empty_odd()
        finance.ODD_SEEN = {}
        finance._ODD_ROWS = {}
        finance.API_CACHE = tmp / "api-cache"
        common.CRON_JOBS = tmp / "jobs.json"
        common.CRON_EXECUTIONS = tmp / "executions.db"
        finance.STATE = finance.empty_state()
        finance._PAGE_SEEN = 0.0   # module state: a page_seen() call leaks it on
        finance._SCAN_REQUESTED = 0.0
        self.spawned = []
        finance._spawn = lambda *a: self.spawned.append(a)

    def tearDown(self):
        (common.STATE_DIR, finance.STATE_FILE, finance.API_CACHE,
         common.CRON_JOBS, common.CRON_EXECUTIONS,
         finance.STATE, finance._spawn, finance.ODD_FILE,
         finance.ODD_SEEN_FILE, finance.ODD, finance.ODD_SEEN,
         finance._ODD_ROWS) = self._saved
        self._tmp.cleanup()

    def budget(self, categories=(), transactions=()):
        make_budget(finance.API_CACHE, categories, transactions)

    def saved_cards(self):
        return json.loads(finance.STATE_FILE.read_text())["batch"]["cards"]


# ---------------------------------------------------------------- batch

class TestSaveBatch(FinanceTest):

    def test_valid_batch_saves_and_persists(self):
        code, out = finance._h_batch({"cards": [card(), card("t2")]})
        self.assertEqual((code, out["count"]), (200, 2))
        saved = self.saved_cards()
        self.assertEqual([c["transaction_id"] for c in saved], ["t1", "t2"])
        self.assertTrue(all(c["status"] == "pending" for c in saved))

    def test_scheduled_save_tops_up_open_slots(self):
        finance._h_batch({"cards": [card("old")]})
        code, out = finance._h_batch({"cards": [card("new")]})
        self.assertEqual((code, out["count"]), (200, 1))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["old", "new"])

    def test_scheduled_save_keeps_pending_drops_finished(self):
        finance._h_batch({"cards": [card("t1"), card("t2")]})
        finance.STATE["batch"]["cards"][1]["status"] = "done"
        code, out = finance._h_batch({"cards": [card("t3")]})
        self.assertEqual((code, out["count"]), (200, 1))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["t1", "t3"])

    def test_scheduled_save_skips_cards_already_listed(self):
        finance._h_batch({"cards": [card("t1")]})
        code, out = finance._h_batch({"cards": [card("t1"), card("t2")]})
        self.assertEqual((code, out["count"]), (200, 1))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["t1", "t2"])

    def test_scheduled_save_with_no_open_slots_changes_nothing(self):
        finance._h_batch({"cards": full_batch()})
        before = json.loads(finance.STATE_FILE.read_text())["batch"]
        code, out = finance._h_batch({"cards": [card("new")]})
        self.assertEqual((code, out["count"]), (200, 0))
        self.assertEqual(json.loads(finance.STATE_FILE.read_text())["batch"],
                         before)

    def test_scheduled_save_with_no_open_slots_short_circuits(self):
        # keep == current: the short-circuit must return without ever
        # rewriting state["batch"] (a fresh dict, even with equal content,
        # would mean the "nothing changed" branch was skipped)
        finance._h_batch({"cards": full_batch()})
        batch_before = finance.STATE["batch"]
        code, out = finance._h_batch({"cards": [card("new")]})
        self.assertEqual((code, out), (200, {"ok": True, "count": 0}))
        self.assertIs(finance.STATE["batch"], batch_before)

    def test_error_record_replaces_cards(self):
        finance._h_batch({"cards": [card()]})
        code, _ = finance._h_batch({"error": {"step": "llm", "message": "boom"}})
        self.assertEqual(code, 200)
        batch = json.loads(finance.STATE_FILE.read_text())["batch"]
        self.assertEqual(batch["cards"], [])
        self.assertEqual(batch["error"]["step"], "llm")

    def test_error_record_validated(self):
        for bad in ({"step": "llm"}, {"message": "m"}, "boom",
                    {"step": "", "message": "m"},
                    {"step": "llm", "message": "m", "extra": 1}):
            code, _ = finance._h_batch({"error": bad})
            self.assertEqual(code, 400, bad)

    def test_cards_must_be_list_capped(self):
        self.assertEqual(finance._h_batch({"cards": "no"})[0], 400)
        too_many = [card(f"t{i}") for i in range(finance.LATEST_CAP + 1)]
        self.assertEqual(finance._h_batch({"cards": too_many})[0], 400)

    def test_unknown_card_keys_rejected(self):
        code, out = finance._h_batch({"cards": [card(status="done")]})
        self.assertEqual(code, 400)
        self.assertIn("status", out["error"])

    def test_required_strings_enforced(self):
        for field in ("transaction_id", "date", "amount", "account",
                      "account_id"):
            code, _ = finance._h_batch({"cards": [card(**{field: ""})]})
            self.assertEqual(code, 400, field)
            code, _ = finance._h_batch({"cards": [card(**{field: 5})]})
            self.assertEqual(code, 400, field)

    def test_pick_validated(self):
        for bad in ("newest", "random", "email", "", 5, None):
            code, _ = finance._h_batch({"cards": [card(pick=bad)]})
            self.assertEqual(code, 400, bad)

    def test_suggestions_validated(self):
        four = [{"category": f"c{i}", "basis": "guess"} for i in range(4)]
        bad_shapes = (four, [{"category": "c", "basis": "vibes"}],
                      [{"category": "", "basis": "guess"}],
                      [{"category": "c"}], ["c"])
        for sugg in bad_shapes:
            code, _ = finance._h_batch({"cards": [card(suggestions=sugg)]})
            self.assertEqual(code, 400, sugg)
        good = [{"category": "Groceries", "basis": "history"}]
        self.assertEqual(finance._h_batch({"cards": [card(suggestions=good)]})[0],
                         200)

    def test_duplicate_transaction_ids_rejected(self):
        code, _ = finance._h_batch({"cards": [card("t1"), card("t1")]})
        self.assertEqual(code, 400)

    def test_save_blocked_while_card_executing(self):
        finance._h_batch({"cards": [card()]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        code, _ = finance._h_batch({"cards": [card("t2")]})
        self.assertEqual(code, 409)

    def test_error_record_blocked_while_card_executing(self):
        finance._h_batch({"cards": [card()]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        code, _ = finance._h_batch({"error": {"step": "llm", "message": "boom"}})
        self.assertEqual(code, 409)
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()], ["t1"])

    def test_scheduled_save_held_while_page_open(self):
        finance._h_batch({"cards": [card("old")]})
        finance._PAGE_SEEN = time.time()
        code, out = finance._h_batch({"cards": [card("new")]})
        self.assertEqual(code, 409)
        self.assertIn("page is open", out["error"])
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()], ["old"])

    def test_scheduled_save_lands_past_an_open_page_when_nothing_pending(self):
        finance._h_batch({"cards": [card("old")]})
        finance.STATE["batch"]["cards"][0]["status"] = "done"
        finance._PAGE_SEEN = time.time()
        code, out = finance._h_batch({"cards": [card("new")]})
        self.assertEqual((code, out["count"]), (200, 1))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["new"])

    def test_scheduled_save_lands_once_the_page_is_quiet(self):
        finance._h_batch({"cards": [card("old")]})
        finance._PAGE_SEEN = time.time() - finance.PAGE_ACTIVE_SECONDS - 1
        code, out = finance._h_batch({"cards": [card("new")]})
        self.assertEqual((code, out["count"]), (200, 1))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["old", "new"])

    def test_scan_key_run_replaces_the_whole_list(self):
        finance._h_batch({"cards": [card("old")]})
        finance._PAGE_SEEN = time.time()   # an open page holds no scan-key run
        finance._SCAN_REQUESTED = time.time()
        code, out = finance._h_batch({"cards": [card("new")]})
        self.assertEqual((code, out["count"]), (200, 1))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["new"])

    def test_page_seen_stamps_the_page(self):
        finance.page_seen()
        self.assertGreater(finance._PAGE_SEEN, 0.0)

    def test_state_poll_does_not_stamp_the_page(self):
        self.budget()
        finance.state()
        self.assertEqual(finance._PAGE_SEEN, 0.0)


# ---------------------------------------------------------------- scan-check

def cand(tid, pick="latest"):
    return {"transaction_id": tid, "pick": pick}


class TestScanCheck(FinanceTest):
    """The scan's pre-LLM question: would a scheduled save land anything?"""

    def test_open_slots_and_new_candidate_proceed(self):
        code, out = finance._h_scan_check({"candidates": [cand("t1")]})
        self.assertEqual((code, out["proceed"]), (200, True))

    def test_full_slots_skip(self):
        finance._h_batch({"cards": full_batch()})
        code, out = finance._h_scan_check({"candidates": [cand("new")]})
        self.assertEqual((code, out["proceed"]), (200, False))

    def test_all_candidates_already_listed_skip(self):
        finance._h_batch({"cards": [card("t1")]})
        code, out = finance._h_scan_check({"candidates": [cand("t1")]})
        self.assertEqual((code, out["proceed"]), (200, False))

    def test_no_candidates_skip(self):
        code, out = finance._h_scan_check({"candidates": []})
        self.assertEqual((code, out["proceed"]), (200, False))

    def test_rebuild_window_always_proceeds(self):
        finance._h_batch({"cards": full_batch()})
        finance._SCAN_REQUESTED = time.time()
        code, out = finance._h_scan_check({"candidates": []})
        self.assertEqual((code, out["proceed"]), (200, True))

    def test_open_page_with_pending_cards_skips(self):
        finance._h_batch({"cards": [card("t1")]})
        finance._PAGE_SEEN = time.time()
        code, out = finance._h_scan_check({"candidates": [cand("t2")]})
        self.assertEqual((code, out["proceed"]), (200, False))

    def test_open_page_without_pending_cards_proceeds(self):
        finance._PAGE_SEEN = time.time()
        code, out = finance._h_scan_check({"candidates": [cand("t1")]})
        self.assertEqual((code, out["proceed"]), (200, True))

    def test_finished_cards_free_their_slots(self):
        finance._h_batch({"cards": full_batch()})
        finance.STATE["batch"]["cards"][0]["status"] = "done"
        _, out = finance._h_scan_check({"candidates": [cand("new")]})
        self.assertTrue(out["proceed"])

    def test_executing_card_skips(self):
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        code, out = finance._h_scan_check({"candidates": [cand("t2")]})
        self.assertEqual((code, out["proceed"]), (200, False))

    def test_executing_card_skips_even_in_rebuild_window(self):
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        finance._SCAN_REQUESTED = time.time()
        _, out = finance._h_scan_check({"candidates": []})
        self.assertFalse(out["proceed"])

    def test_nothing_new_skip_drops_finished_cards(self):
        finance._h_batch({"cards": [card("t1"), card("t2")]})
        finance.STATE["batch"]["cards"][0]["status"] = "done"
        _, out = finance._h_scan_check({"candidates": [cand("t2")]})
        self.assertFalse(out["proceed"])
        self.assertEqual([c["transaction_id"]
                          for c in finance.STATE["batch"]["cards"]], ["t2"])

    def test_bad_payloads_refused(self):
        for bad in (None, "x", [{"pick": "latest"}],
                    [{"transaction_id": "", "pick": "latest"}],
                    [{"transaction_id": "t", "pick": "email"}],
                    [{"transaction_id": "t", "pick": "random"}]):
            code, _ = finance._h_scan_check({"candidates": bad})
            self.assertEqual(code, 400, bad)

    def test_check_writes_nothing(self):
        finance._h_batch({"cards": [card("t1")]})
        before = finance.STATE_FILE.read_text()
        finance._h_scan_check({"candidates": [cand("t2")]})
        self.assertEqual(finance.STATE_FILE.read_text(), before)


# ---------------------------------------------------------------- apply

class TestApply(FinanceTest):

    def setUp(self):
        super().setUp()
        self.budget(categories=[("c1", "Groceries", "Food")])
        finance._h_batch({"cards": [card("t1"), card("t2")]})

    def item(self, tid="t1", **over):
        it = {"transaction_id": tid, "category": "Groceries"}
        it.update(over)
        return it

    def test_items_must_be_list_capped(self):
        for items in (None, "no", [], [self.item(f"t{i}")
                                       for i in range(finance.LATEST_CAP
                                                      + finance.EMAIL_CAP + 1)]):
            code, _ = finance._h_apply({"items": items})
            self.assertEqual(code, 400, items)

    def test_item_must_be_object(self):
        code, _ = finance._h_apply({"items": ["t1"]})
        self.assertEqual(code, 400)

    def test_unknown_card_404(self):
        code, _ = finance._h_apply({"items": [self.item("nope")]})
        self.assertEqual(code, 404)

    def test_duplicate_transaction_ids_rejected(self):
        code, _ = finance._h_apply({"items": [self.item(), self.item()]})
        self.assertEqual(code, 400)
        self.assertEqual(self.spawned, [])

    def test_category_required(self):
        for cat in (None, "", "   ", 3):
            code, _ = finance._h_apply({"items": [self.item(category=cat)]})
            self.assertEqual(code, 400, cat)

    def test_apply_marks_in_progress_and_spawns(self):
        code, out = finance._h_apply({"items": [self.item()]})
        self.assertEqual((code, out["count"]), (202, 1))
        self.assertEqual(self.spawned, [([("t1", "c1", "Groceries", True, False)],)])
        # persisted before the thread would run
        self.assertEqual(self.saved_cards()[0]["status"], "in_progress")

    def test_apply_many_in_one_spawn(self):
        code, out = finance._h_apply({"items": [
            self.item("t1"), self.item("t2", update_rule=False)]})
        self.assertEqual((code, out["count"]), (202, 2))
        self.assertEqual(self.spawned, [([("t1", "c1", "Groceries", True, False),
                                          ("t2", "c1", "Groceries", False, False)],)])
        self.assertTrue(all(c["status"] == "in_progress"
                            for c in self.saved_cards()))

    def test_apply_without_rule_update(self):
        code, _ = finance._h_apply({"items": [self.item(update_rule=False)]})
        self.assertEqual(code, 202)
        self.assertEqual(self.spawned, [([("t1", "c1", "Groceries", False, False)],)])

    def test_apply_with_approx(self):
        code, _ = finance._h_apply({"items": [self.item(approx=True)]})
        self.assertEqual(code, 202)
        self.assertEqual(self.spawned, [([("t1", "c1", "Groceries", True, True)],)])

    def test_non_pending_card_409(self):
        finance.STATE["batch"]["cards"][0]["status"] = "done"
        code, _ = finance._h_apply({"items": [self.item()]})
        self.assertEqual(code, 409)

    def test_one_bad_item_refuses_whole_call(self):
        code, _ = finance._h_apply({"items": [self.item("t1"),
                                              self.item("t2", category="Nope")]})
        self.assertEqual(code, 400)
        self.assertEqual(self.spawned, [])
        self.assertTrue(all(c["status"] == "pending"
                            for c in self.saved_cards()))

    def test_single_flight_across_calls(self):
        finance._h_apply({"items": [self.item("t1")]})
        code, out = finance._h_apply({"items": [self.item("t2")]})
        self.assertEqual(code, 409)
        self.assertIn("another card", out["error"])
        self.assertEqual(len(self.spawned), 1)

    def test_unknown_category_400(self):
        code, out = finance._h_apply({"items": [self.item(category="Nope")]})
        self.assertEqual(code, 400)
        self.assertIn("unknown category", out["error"])
        self.assertEqual(self.saved_cards()[0]["status"], "pending")

    def test_ambiguous_category_needs_group(self):
        make_budget(finance.API_CACHE,
                    categories=[("c1", "Misc", "Food"), ("c2", "Misc", "Home")])
        code, out = finance._h_apply({"items": [self.item(category="Misc")]})
        self.assertEqual(code, 400)
        self.assertIn("more than one group", out["error"])
        code, _ = finance._h_apply({"items": [self.item(category="Home: Misc")]})
        self.assertEqual(code, 202)
        self.assertEqual(self.spawned, [([("t1", "c2", "Home: Misc", True,
                                           False)],)])

    def test_missing_api_cache_500(self):
        finance.API_CACHE = pathlib.Path(self._tmp.name) / "gone"
        code, out = finance._h_apply({"items": [self.item()]})
        self.assertEqual(code, 500)
        self.assertIn("category lookup failed", out["error"])


# ---------------------------------------------------------------- write helper

class TestRunWrite(FinanceTest):
    """api_cache.run_budget_helper itself, unpatched — _config and
    subprocess.run stubbed so no real node process runs."""

    def setUp(self):
        super().setUp()
        self._saved_config = finance.api_cache._config
        self._saved_subrun = finance.api_cache.subprocess.run
        finance.api_cache._config = lambda: {"ACTUAL_PASSWORD": "p", "ACTUAL_SYNC_ID": "s"}

    def tearDown(self):
        finance.api_cache._config = self._saved_config
        finance.api_cache.subprocess.run = self._saved_subrun
        super().tearDown()

    def run_with(self, returncode, stdout="", stderr=""):
        finance.api_cache.subprocess.run = lambda *a, **k: _FakeProc(returncode, stdout, stderr)
        return finance.api_cache.run_budget_helper({"cmd": "update", "ops": []})

    def test_nonzero_returncode_raises_with_stderr_detail(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_with(1, stdout="", stderr="boom")
        self.assertIn("write helper failed", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_nonzero_returncode_falls_back_to_stdout_when_stderr_empty(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_with(1, stdout="stdout detail", stderr="")
        self.assertIn("stdout detail", str(ctx.exception))


# ---------------------------------------------------------------- executor

class TestWorkItems(FinanceTest):
    """_work_items with run_budget_helper patched — the four outcome shapes."""

    def setUp(self):
        super().setUp()
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        self._saved_run = finance.api_cache.run_budget_helper

    def tearDown(self):
        finance.api_cache.run_budget_helper = self._saved_run
        super().tearDown()

    def run_with(self, result=None, error=None, update_rule=True, approx=False):
        def fake(command):
            self.command = command
            if error:
                raise error
            return result
        finance.api_cache.run_budget_helper = fake
        finance._work_items([("t1", "c1", "Groceries", update_rule, approx)])
        return json.loads(finance.STATE_FILE.read_text())["batch"]["cards"][0]

    def test_success_pushed(self):
        c = self.run_with({"results": [{"ok": True, "rule": "created"}],
                           "pushed": True})
        self.assertEqual(c["status"], "done")
        self.assertIn("categorized as 'Groceries'", c["status_text"])
        self.assertIn("payee rule created", c["status_text"])
        self.assertNotIn("local copy only", c["status_text"])
        self.assertEqual(self.command["ops"][0],
                         {"op": "categorize", "id": "t1", "categoryId": "c1"})

    def test_success_without_rule_update(self):
        c = self.run_with({"results": [{"ok": True, "rule": "skipped"}],
                           "pushed": True}, update_rule=False)
        self.assertEqual(c["status"], "done")
        self.assertIn("payee rule unchanged", c["status_text"])
        self.assertEqual(self.command["ops"][0],
                         {"op": "categorize", "id": "t1", "categoryId": "c1",
                          "rule": False})

    def test_success_with_approx(self):
        c = self.run_with({"results": [{"ok": True, "rule": "created"}],
                           "pushed": True}, approx=True)
        self.assertEqual(c["status"], "done")
        self.assertEqual(self.command["ops"][0],
                         {"op": "categorize", "id": "t1", "categoryId": "c1",
                          "approx": True})

    def test_approx_without_rule_update_is_dropped(self):
        c = self.run_with({"results": [{"ok": True, "rule": "skipped"}],
                           "pushed": True}, update_rule=False, approx=True)
        self.assertEqual(c["status"], "done")
        self.assertEqual(self.command["ops"][0],
                         {"op": "categorize", "id": "t1", "categoryId": "c1",
                          "rule": False})

    def test_success_not_pushed_says_so(self):
        c = self.run_with({"results": [{"ok": True, "rule": "updated"}],
                           "pushed": False})
        self.assertEqual(c["status"], "done")
        self.assertIn("local copy only", c["status_text"])

    def test_handled_elsewhere(self):
        c = self.run_with({"results": [{"ok": False, "handled": True,
                                        "error": "already categorized"}],
                           "pushed": False})
        self.assertEqual(c["status"], "already_handled")
        self.assertEqual(c["status_text"], "already categorized")

    def test_write_failure(self):
        c = self.run_with({"results": [{"ok": False, "error": "no such id"}],
                           "pushed": False})
        self.assertEqual(c["status"], "failed")
        self.assertEqual(c["status_text"], "no such id")

    def test_logged_outcome_carries_non_done_status(self):
        self.run_with({"results": [{"ok": False, "error": "no such id"}],
                       "pushed": False})
        log = common.STATE_DIR / f"decisions-{common._now()[:4]}.jsonl"
        event = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(event["outcome"], "failed")

    def test_logged_outcome_is_done_on_success(self):
        self.run_with({"results": [{"ok": True, "rule": "created"}],
                       "pushed": True})
        log = common.STATE_DIR / f"decisions-{common._now()[:4]}.jsonl"
        event = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(event["outcome"], "done")

    def test_helper_exception(self):
        c = self.run_with(error=RuntimeError("write helper failed: boom"))
        self.assertEqual(c["status"], "failed")
        self.assertIn("boom", c["status_text"])

    def test_card_gone_meanwhile_is_noop(self):
        finance.STATE["batch"]["cards"] = []
        finance.api_cache.run_budget_helper = lambda cmd: {"results": [{"ok": True}],
                                          "pushed": True}
        finance._work_items([("t1", "c1", "Groceries", True, False)])  # must not raise

    def test_many_items_one_write_results_in_order(self):
        finance.STATE["batch"]["cards"][0]["status"] = "pending"
        finance._h_batch({"cards": [card("t1"), card("t2"), card("t3")]})
        for c in finance.STATE["batch"]["cards"]:
            c["status"] = "in_progress"
        def fake(command):
            self.command = command
            return {"results": [{"ok": True, "rule": "created"},
                                {"ok": False, "handled": True,
                                 "error": "already categorized"},
                                {"ok": False, "error": "no such id"}],
                    "pushed": True}
        finance.api_cache.run_budget_helper = fake
        finance._work_items([("t1", "c1", "Groceries", True, False),
                             ("t2", "c1", "Groceries", True, False),
                             ("t3", "c1", "Groceries", False, False)])
        self.assertEqual([op["id"] for op in self.command["ops"]],
                         ["t1", "t2", "t3"])
        self.assertEqual(self.command["ops"][2].get("rule"), False)
        saved = self.saved_cards()
        self.assertEqual([c["status"] for c in saved],
                         ["done", "already_handled", "failed"])

    def test_helper_exception_fails_every_item(self):
        finance.STATE["batch"]["cards"][0]["status"] = "pending"
        finance._h_batch({"cards": [card("t1"), card("t2")]})
        for c in finance.STATE["batch"]["cards"]:
            c["status"] = "in_progress"
        def fake(command):
            raise RuntimeError("write helper failed: boom")
        finance.api_cache.run_budget_helper = fake
        finance._work_items([("t1", "c1", "Groceries", True, False),
                             ("t2", "c1", "Groceries", True, False)])
        saved = self.saved_cards()
        self.assertEqual([c["status"] for c in saved], ["failed", "failed"])
        self.assertIn("boom", saved[0]["status_text"])


# ---------------------------------------------------------------- create category

class TestCreateCategory(FinanceTest):
    """create_category with run_budget_helper patched — validation, single-flight
    with the apply path, and the outcome shapes."""

    def run_with(self, body, result=None, error=None):
        def fake(command):
            self.command = command
            if error:
                raise error
            return result
        saved = finance.api_cache.run_budget_helper
        finance.api_cache.run_budget_helper = fake
        try:
            return finance._h_create_category(body)
        finally:
            finance.api_cache.run_budget_helper = saved

    def test_name_and_group_required(self):
        ok = {"results": [{"ok": True}], "pushed": True}
        for body in ({}, {"name": "Pets"}, {"group": "Fun"},
                     {"name": " ", "group": "Fun"},
                     {"name": "Pets", "group": ""},
                     {"name": 3, "group": "Fun"},
                     {"name": "x" * (finance.NAME_CAP + 1), "group": "Fun"}):
            code, _ = self.run_with(body, result=ok)
            self.assertEqual(code, 400, body)

    def test_create_runs_write_and_returns_qualified_name(self):
        code, out = self.run_with(
            {"name": " Pets ", "group": "Fun"},
            result={"results": [{"ok": True, "id": "c9"}], "pushed": True})
        self.assertEqual((code, out["category"]), (200, "Fun: Pets"))
        self.assertEqual(self.command["ops"][0],
                         {"op": "create_category", "name": "Pets",
                          "group": "Fun"})

    def test_write_refusal_400(self):
        code, out = self.run_with(
            {"name": "Pets", "group": "Fun"},
            result={"results": [{"ok": False,
                                 "error": "category 'Pets' already exists"}],
                    "pushed": False})
        self.assertEqual(code, 400)
        self.assertIn("already exists", out["error"])

    def test_helper_exception_500_and_flag_cleared(self):
        code, out = self.run_with({"name": "Pets", "group": "Fun"},
                                  error=RuntimeError("write helper failed: boom"))
        self.assertEqual(code, 500)
        self.assertIn("boom", out["error"])
        self.assertFalse(finance._HELPER_BUSY)

    def test_blocked_while_card_executing(self):
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        code, _ = self.run_with({"name": "Pets", "group": "Fun"},
                                result={"results": [{"ok": True}],
                                        "pushed": True})
        self.assertEqual(code, 409)

    def test_apply_blocked_while_helper_busy(self):
        self.budget(categories=[("c1", "Groceries", "Food")])
        finance._h_batch({"cards": [card("t1")]})
        finance._HELPER_BUSY = True
        try:
            code, out = finance._h_apply({"items": [
                {"transaction_id": "t1", "category": "Groceries"}]})
        finally:
            finance._HELPER_BUSY = False
        self.assertEqual(code, 409)
        self.assertIn("another write", out["error"])
        self.assertEqual(self.spawned, [])


# ---------------------------------------------------------------- skip

def email_card(tid, email_id="m1", status="pending"):
    """An email-pick card as create_email_cards stores it."""
    c = card(tid, pick="email")
    c.update({"status": status, "status_text": "",
              "source": {"email_id": email_id, "subject": "s"},
              "suggestions": [{"category": "Food", "basis": "email"}]})
    return c


class TestSkip(FinanceTest):

    def setUp(self):
        super().setUp()
        finance._h_batch({"cards": [card("t1")]})

    def test_skip_drops_card(self):
        code, _ = finance._h_skip({"transaction_id": "t1", "pick": "latest"})
        self.assertEqual(code, 200)
        self.assertEqual(self.saved_cards(), [])

    def test_skip_unknown_404(self):
        self.assertEqual(
            finance._h_skip({"transaction_id": "x", "pick": "latest"})[0], 404)

    def test_skip_needs_pick(self):
        self.assertEqual(finance._h_skip({"transaction_id": "t1"})[0], 400)

    def test_skip_matches_pick_too(self):
        # the same transaction as an email card and a scan card: pick says
        # which copy goes, the other stays
        finance.STATE["batch"]["cards"].append(email_card("t1"))
        code, _ = finance._h_skip({"transaction_id": "t1", "pick": "email"})
        self.assertEqual(code, 200)
        self.assertEqual([c["pick"] for c in self.saved_cards()], ["latest"])

    def test_skip_executing_409(self):
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        self.assertEqual(
            finance._h_skip({"transaction_id": "t1", "pick": "latest"})[0], 409)

    def test_skip_resolved_card_allowed(self):
        finance.STATE["batch"]["cards"][0]["status"] = "failed"
        self.assertEqual(
            finance._h_skip({"transaction_id": "t1", "pick": "latest"})[0], 200)
        self.assertEqual(self.saved_cards(), [])

    def test_hide_all_drops_email_cards_only(self):
        finance.STATE["batch"]["cards"] += [email_card("e1"),
                                            email_card("e2", status="done")]
        code, out = finance._h_skip({"pick": "email"})
        self.assertEqual((code, out["count"]), (200, 2))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["t1"])

    def test_hide_all_leaves_executing_email_card(self):
        finance.STATE["batch"]["cards"] += [
            email_card("e1", status="in_progress"), email_card("e2")]
        code, out = finance._h_skip({"pick": "email"})
        self.assertEqual((code, out["count"]), (200, 1))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["t1", "e1"])

    def test_hide_all_email_pick_only(self):
        self.assertEqual(finance._h_skip({"pick": "latest"})[0], 400)


# ---------------------------------------------------------------- hide

class TestHide(FinanceTest):
    """Only filed transactions come off: done or already_handled."""

    def setUp(self):
        super().setUp()
        finance._h_batch({"cards": [card("t1")]})

    def settle(self, tid="t1", status="done"):
        for c in finance.STATE["batch"]["cards"]:
            if c["transaction_id"] == tid:
                c["status"] = status

    def test_hide_drops_filed_card(self):
        self.settle()
        code, out = finance._h_hide({"transaction_id": "t1"})
        self.assertEqual((code, out["hidden"]), (200, 1))
        self.assertEqual(self.saved_cards(), [])

    def test_hide_already_handled_too(self):
        self.settle(status="already_handled")
        self.assertEqual(finance._h_hide({"transaction_id": "t1"})[0], 200)
        self.assertEqual(self.saved_cards(), [])

    def test_hide_pending_409(self):
        code, out = finance._h_hide({"transaction_id": "t1"})
        self.assertEqual(code, 409)
        self.assertIn("pending", out["error"])
        self.assertEqual(len(self.saved_cards()), 1)

    def test_hide_failed_409(self):
        self.settle(status="failed")
        self.assertEqual(finance._h_hide({"transaction_id": "t1"})[0], 409)

    def test_hide_executing_409(self):
        self.settle(status="in_progress")
        self.assertEqual(finance._h_hide({"transaction_id": "t1"})[0], 409)

    def test_hide_unknown_404(self):
        self.assertEqual(finance._h_hide({"transaction_id": "x"})[0], 404)

    def test_hide_needs_an_argument(self):
        self.assertEqual(finance._h_hide({})[0], 400)

    def test_hide_takes_every_copy_of_the_transaction(self):
        # a transaction proposed from email and by the scan settles as both
        # copies at once, so hiding the visible row takes the other with it
        finance.STATE["batch"]["cards"].append(email_card("t1", status="done"))
        self.settle()
        code, out = finance._h_hide({"transaction_id": "t1"})
        self.assertEqual((code, out["hidden"]), (200, 1))
        self.assertEqual(self.saved_cards(), [])

    def test_hide_all_drops_filed_cards_only(self):
        finance.STATE["batch"]["cards"] += [
            email_card("e1", status="done"),
            email_card("e2", status="already_handled"),
            email_card("e3", status="failed")]
        code, out = finance._h_hide({"all": True})
        self.assertEqual((code, out["hidden"]), (200, 2))
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["t1", "e3"])

    def test_hide_all_with_nothing_filed_writes_nothing(self):
        code, out = finance._h_hide({"all": True})
        self.assertEqual((code, out["hidden"]), (200, 0))
        self.assertEqual(len(self.saved_cards()), 1)


# ---------------------------------------------------------------- prune

class TestPrune(FinanceTest):

    def test_pending_handled_card_dropped_on_poll(self):
        # t1 categorized in the budget copy, t2 still open, t3 a transfer now
        self.budget(transactions=[("t1", "c9", None), ("t2", None, None),
                                  ("t3", None, "tr1")])
        finance._h_batch({"cards": [card("t1"), card("t2"), card("t3")]})
        out = finance.state()
        self.assertEqual([c["transaction_id"] for c in out["cards"]], ["t2"])

    def test_resolved_cards_survive_prune(self):
        self.budget(transactions=[("t1", "c9", None)])
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = "done"
        out = finance.state()
        self.assertEqual(len(out["cards"]), 1)

    def test_unreadable_copy_prunes_nothing(self):
        finance._h_batch({"cards": [card("t1")]})   # no budget built
        out = finance.state()
        self.assertEqual(len(out["cards"]), 1)
        self.assertEqual(out["cards"][0]["status"], "pending")


# ---------------------------------------------------------------- boot settle

class TestBootSettle(FinanceTest):

    def write_state(self, status):
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = status
        finance.save_state(finance.STATE)
        finance.STATE = finance.empty_state()   # boot() reloads from disk

    def test_landed_write_settles_done(self):
        self.write_state("in_progress")
        self.budget(transactions=[("t1", "c9", None)])
        finance.boot()
        c = finance.STATE["batch"]["cards"][0]
        self.assertEqual(c["status"], "done")
        self.assertEqual(c["status_text"], "categorized (settled at boot)")

    def test_unlanded_write_back_to_pending(self):
        self.write_state("in_progress")
        self.budget(transactions=[("t1", None, None)])
        finance.boot()
        c = finance.STATE["batch"]["cards"][0]
        self.assertEqual(c["status"], "pending")
        self.assertEqual(c["status_text"], "")

    def test_unreadable_copy_stays_in_progress(self):
        self.write_state("in_progress")
        finance.boot()   # no budget built
        self.assertEqual(finance.STATE["batch"]["cards"][0]["status"],
                         "in_progress")

    def test_first_boot_creates_state_file(self):
        finance.boot()
        self.assertTrue(finance.STATE_FILE.exists())
        self.assertEqual(finance.STATE, finance.empty_state())


# ---------------------------------------------------------------- views

class TestStateView(FinanceTest):

    def test_categories_grouped_for_picker(self):
        self.budget(categories=[("c1", "Groceries", "Food"),
                                ("c2", "Dining", "Food"),
                                ("c3", "Rent", "Home")])
        out = finance.state()
        self.assertEqual(out["categories"],
                         [{"group": "Food", "categories": ["Dining", "Groceries"]},
                          {"group": "Home", "categories": ["Rent"]}])

    def test_missing_jobs_files_are_none_fields(self):
        out = finance.state()
        self.assertIsNone(out["job_last_run_at"])
        self.assertFalse(out["job_last_failed"])
        self.assertIsNone(out["job_running_since"])

    def test_job_fields_read_the_scan_job_only(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "finance-daily", "id": "j1",
             "last_run_at": "2026-08-11T10:00:00", "last_status": "ok",
             "next_run_at": "2026-08-12T07:00:00"},
            {"name": finance.SCAN_JOB, "id": "j2",
             "last_run_at": "2026-08-11T09:00:00", "last_status": "error",
             "next_run_at": "2026-08-11T13:55:00"}]}))
        fields = finance._job_fields()
        # the daily run is newer; the stamps must still be the scan job's
        self.assertEqual(fields["job_last_run_at"], "2026-08-11T09:00:00")
        self.assertEqual(fields["job_next_run_at"], "2026-08-11T13:55:00")
        self.assertTrue(fields["job_last_failed"])

    def test_job_running_since_reads_executions(self):
        conn = sqlite3.connect(common.CRON_EXECUTIONS)
        conn.execute("CREATE TABLE executions (id INTEGER PRIMARY KEY, "
                     "job_id TEXT, status TEXT, claimed_at TEXT, "
                     "started_at TEXT)")
        conn.execute("INSERT INTO executions (job_id, status, claimed_at, "
                     "started_at) VALUES ('j1', 'running', "
                     "'2026-08-11T09:00:00', '2026-08-11T09:00:05')")
        conn.commit()
        conn.close()
        self.assertEqual(
            common.job_running_since({"id": "j1"}), "2026-08-11T09:00:05")
        self.assertIsNone(common.job_running_since({"id": "other"}))


class TestFindUncategorized(FinanceTest):
    """Every test stubs run_budget_helper (the refresh) and subprocess.Popen."""

    def run_with(self, refresh_error=None, popen_error=None):
        self.commands, self.popen_calls = [], []
        def fake_write(command):
            self.commands.append(command)
            if refresh_error:
                raise refresh_error
            return {"results": [], "pushed": True}
        def fake_popen(*a, **k):
            if popen_error:
                raise popen_error
            self.popen_calls.append(a[0])
        saved_write, saved_popen = finance.api_cache.run_budget_helper, finance.subprocess.Popen
        finance.api_cache.run_budget_helper, finance.subprocess.Popen = fake_write, fake_popen
        try:
            return finance._h_uncategorized({})
        finally:
            finance.api_cache.run_budget_helper, finance.subprocess.Popen = saved_write, saved_popen

    def test_refreshes_then_starts_job_detached(self):
        code, out = self.run_with()
        self.assertEqual((code, out["started"]), (200, finance.SCAN_JOB))
        self.assertEqual(self.commands, [{"cmd": "pull"}])
        self.assertEqual(self.popen_calls,
                         [["hermes", "cron", "run", finance.SCAN_JOB]])

    def test_refresh_failure_500_flag_cleared_no_job(self):
        code, out = self.run_with(
            refresh_error=RuntimeError("write helper failed: boom"))
        self.assertEqual(code, 500)
        self.assertIn("boom", out["error"])
        self.assertFalse(finance._HELPER_BUSY)
        self.assertEqual(self.popen_calls, [])

    def test_blocked_while_card_executing(self):
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        code, _ = self.run_with()
        self.assertEqual(code, 409)
        self.assertEqual(self.commands, [])

    def test_spawn_failure_500(self):
        code, _ = self.run_with(popen_error=OSError("no hermes"))
        self.assertEqual(code, 500)


# ---------------------------------------------------------------- ask

def make_queue_budget(api_cache, rows, categories=()):
    """A fake api-cache copy with the full schema the ask reads use.
    rows: (id, date_yyyymmdd, amount_cents, payee_name, notes, account_name,
    category, transfer_id) — None category and None transfer_id make a queue
    row. categories: (id, name, group_name) as in make_budget."""
    budget = pathlib.Path(api_cache) / "My-Budget-abc123"
    budget.mkdir(parents=True, exist_ok=True)
    (budget / "db.sqlite").unlink(missing_ok=True)
    conn = sqlite3.connect(budget / "db.sqlite")
    conn.executescript(
        "CREATE TABLE category_groups (id TEXT, name TEXT, tombstone INT "
        "DEFAULT 0, hidden INT DEFAULT 0, sort_order REAL DEFAULT 0);"
        "CREATE TABLE categories (id TEXT, name TEXT, cat_group TEXT, "
        "tombstone INT DEFAULT 0, hidden INT DEFAULT 0);"
        "CREATE TABLE accounts (id TEXT, name TEXT, offbudget INT DEFAULT 0, "
        "sort_order REAL DEFAULT 0);"
        "CREATE TABLE v_payees (id TEXT, name TEXT);"
        "CREATE TABLE v_transactions (id TEXT, date INT, amount INT, "
        "payee TEXT, notes TEXT, account TEXT, is_parent INT DEFAULT 0, "
        "category TEXT, transfer_id TEXT, starting_balance_flag INT DEFAULT 0);")
    groups = {}
    for cid, name, grp in categories:
        if grp not in groups:
            gid = f"g-{len(groups)}"
            groups[grp] = gid
            conn.execute("INSERT INTO category_groups (id, name) VALUES (?, ?)",
                         (gid, grp))
        conn.execute("INSERT INTO categories (id, name, cat_group) "
                     "VALUES (?, ?, ?)", (cid, name, groups[grp]))
    accounts, payees = {}, {}
    for tid, date, amount, payee, notes, account, cat, transfer in rows:
        if account not in accounts:
            accounts[account] = f"a-{len(accounts)}"
            conn.execute("INSERT INTO accounts (id, name) VALUES (?, ?)",
                         (accounts[account], account))
        pid = None
        if payee:
            if payee not in payees:
                payees[payee] = f"p-{len(payees)}"
                conn.execute("INSERT INTO v_payees (id, name) VALUES (?, ?)",
                             (payees[payee], payee))
            pid = payees[payee]
        conn.execute("INSERT INTO v_transactions VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0)",
                     (tid, date, amount, pid, notes, accounts[account], cat, transfer))
    conn.commit()
    conn.close()


def qrow(tid, date=20260812, amount=-5210, payee="Store", notes="",
         account="Checking", cat=None, transfer=None):
    return (tid, date, amount, payee, notes, account, cat, transfer)


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


class TestAsk(FinanceTest):
    """The ask store, /api/finance/ask, open_asks_payload,
    open_ask_for_sender — fake queue budget, nothing live."""

    def setUp(self):
        super().setUp()
        self._saved_ask = (finance.ASK_FILE, finance.ASKS,
                           finance._HELPER_BUSY)
        finance.ASK_FILE = pathlib.Path(self._tmp.name) / "finance-ask.json"
        finance.ASKS = finance.empty_asks()
        finance._HELPER_BUSY = False

    def tearDown(self):
        finance.ASK_FILE, finance.ASKS, finance._HELPER_BUSY = self._saved_ask
        super().tearDown()

    def queue_budget(self, rows, categories=()):
        make_queue_budget(finance.API_CACHE, rows, categories)

    def stored_asks(self):
        return json.loads(finance.ASK_FILE.read_text())["asks"]

    def log_events(self):
        log = common.STATE_DIR / f"decisions-{common._now()[:4]}.jsonl"
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines()]

    def make_ask(self, ask_id="ask_x", to="riley@example.org", ids=("t1", "t2"),
                 created=ago(1), state="open"):
        return {"ask_id": ask_id, "to_addr": to, "created_at": created,
                "subject": finance.ASK_SUBJECT, "state": state,
                "items": [{"n": n, "transaction_id": i, "date": "2026-08-12",
                           "payee": "Store", "amount": "-52.10",
                           "account": "Checking", "notes": ""}
                          for n, i in enumerate(ids, 1)]}

    def seed(self, asks):
        finance.ASKS = {"version": 1, "asks": asks}

    # ---- store + boot

    def test_boot_loads_ask_store(self):
        finance.save_asks({"version": 1, "asks": {"ask_x": self.make_ask()}})
        finance.boot()
        self.assertIn("ask_x", finance.ASKS["asks"])

    # ---- create_ask

    def ask(self, body):
        return finance.HANDLERS["/api/finance/ask"](body)

    def test_create_ask_happy(self):
        self.queue_budget([qrow("t1", payee="Target"),
                           qrow("t2", date=20260813, amount=-4000,
                                payee="Shell", notes="fill-up")])
        code, out = self.ask({"to_addr": "Riley@EXAMPLE.org",
                              "transaction_ids": ["t1", "t2"]})
        self.assertEqual(code, 200)
        self.assertEqual(out["subject"], "categorizing help")
        self.assertEqual(out["lines"], ["1) Aug 12 · Target · -$52.10",
                                        "2) Aug 13 · Shell · -$40.00"])
        ask = self.stored_asks()[out["ask_id"]]
        self.assertEqual(ask["to_addr"], "riley@example.org")
        self.assertEqual([it["transaction_id"] for it in ask["items"]],
                         ["t1", "t2"])
        self.assertEqual(ask["items"][0]["amount"], "-52.10")
        self.assertEqual(ask["items"][1]["notes"], "fill-up")

    def test_create_ask_rejects_non_queue_ids(self):
        self.queue_budget([qrow("t1"), qrow("t2", cat="c1"),
                           qrow("t3", transfer="x")])
        code, out = self.ask({"to_addr": "riley@example.org",
                              "transaction_ids": ["t1", "t2", "t3"]})
        self.assertEqual(code, 400)
        self.assertIn("t2", out["error"])
        self.assertIn("t3", out["error"])
        self.assertNotIn("t1", out["error"])
        self.assertFalse(finance.ASK_FILE.exists())

    def test_create_ask_shape_validation(self):
        for body in ({}, {"to_addr": "riley-at-x.com", "transaction_ids": ["t1"]},
                     {"to_addr": "riley@example.org"},
                     {"to_addr": "riley@example.org", "transaction_ids": "t1"},
                     {"to_addr": "riley@example.org", "transaction_ids": []},
                     {"to_addr": "riley@example.org", "transaction_ids": ["t1", "t1"]},
                     {"to_addr": "riley@example.org", "transaction_ids": ["t1", 5]},
                     {"to_addr": "riley@example.org",
                      "transaction_ids": [f"t{i}" for i in range(finance.ASK_CAP + 1)]}):
            code, _ = self.ask(body)
            self.assertEqual(code, 400, body)

    def test_create_ask_rejects_double_coverage(self):
        self.queue_budget([qrow("t1"), qrow("t2")])
        self.assertEqual(self.ask({"to_addr": "riley@example.org",
                                   "transaction_ids": ["t1"]})[0], 200)
        code, out = self.ask({"to_addr": "riley@example.org",
                              "transaction_ids": ["t1", "t2"]})
        self.assertEqual(code, 400)
        self.assertIn("t1", out["error"])
        self.assertEqual(len(self.stored_asks()), 1)

    def test_create_ask_one_open_ask_per_recipient(self):
        self.queue_budget([qrow("t1"), qrow("t2")])
        self.assertEqual(self.ask({"to_addr": "riley@example.org",
                                   "transaction_ids": ["t1"]})[0], 200)
        code, out = self.ask({"to_addr": "riley@example.org",
                              "transaction_ids": ["t2"]})
        self.assertEqual(code, 400)
        self.assertIn("already exists", out["error"])
        self.assertEqual(len(self.stored_asks()), 1)

    def test_create_ask_allowed_after_recipient_ask_expires(self):
        self.queue_budget([qrow("t1"), qrow("t2")])
        self.seed({"ask_old": self.make_ask("ask_old", ids=("t1",),
                                            created=ago(finance.ASK_DAYS + 1))})
        code, _ = self.ask({"to_addr": "riley@example.org", "transaction_ids": ["t2"]})
        self.assertEqual(code, 200)

    # ---- get_open_ask

    def test_get_open_ask(self):
        self.seed({"ask_open": self.make_ask("ask_open"),
                   "ask_old": self.make_ask("ask_old",
                                            created=ago(finance.ASK_DAYS + 1)),
                   "ask_done": self.make_ask("ask_done", state="answered")})
        self.assertIsNotNone(finance.get_open_ask("ask_open"))
        self.assertIsNone(finance.get_open_ask("ask_old"))
        self.assertIsNone(finance.get_open_ask("ask_done"))
        self.assertIsNone(finance.get_open_ask("ask_missing"))

    # ---- open_ask_for_sender

    def test_open_ask_for_sender(self):
        self.seed({"ask_x": self.make_ask("ask_x", to="riley@example.org"),
                   "ask_old": self.make_ask("ask_old", to="sam@example.org",
                                            created=ago(finance.ASK_DAYS + 1))})
        self.assertEqual(finance.open_ask_for_sender("Riley <RILEY@example.org>"),
                         "ask_x")
        self.assertIsNone(finance.open_ask_for_sender("Sam <sam@example.org>"))
        self.assertIsNone(finance.open_ask_for_sender("Other <o@example.org>"))
        self.assertIsNone(finance.open_ask_for_sender(""))

    # ---- open_asks_payload

    def test_payload_marks_still_open_and_lists_categories(self):
        self.queue_budget([qrow("t1"), qrow("t2"), qrow("t3", cat="c1")],
                          categories=[("c1", "Groceries", "Food")])
        self.seed({"ask_x": self.make_ask("ask_x", ids=("t1", "t2", "t3"))})
        out = finance.open_asks_payload(set())
        items = out["asks"][0]["items"]
        self.assertEqual([it["still_open"] for it in items],
                         [True, True, False])
        self.assertEqual(out["categories"],
                         [{"group": "Food", "categories": ["Groceries"]}])

    def test_payload_excludes_referenced_asks(self):
        self.queue_budget([qrow("t1"), qrow("t2")])
        self.seed({"ask_x": self.make_ask("ask_x", ids=("t1",)),
                   "ask_y": self.make_ask("ask_y", ids=("t2",))})
        out = finance.open_asks_payload({"ask_x"})
        self.assertEqual([a["ask_id"] for a in out["asks"]], ["ask_y"])

    def test_payload_prunes_handled_and_expired(self):
        self.queue_budget([qrow("t1", cat="c1"), qrow("t2")])
        self.seed({"ask_done": self.make_ask("ask_done", ids=("t1",)),
                   "ask_old": self.make_ask("ask_old", ids=("t2",),
                                            created=ago(finance.ASK_DAYS + 1)),
                   "ask_live": self.make_ask("ask_live", ids=("t2",))})
        out = finance.open_asks_payload(set())
        self.assertEqual([a["ask_id"] for a in out["asks"]], ["ask_live"])
        self.assertEqual(list(self.stored_asks()), ["ask_live"])
        reasons = {e["args"]["ask_id"]: e["result"]
                   for e in self.log_events()
                   if e["event"] == "finance_ask_pruned"}
        self.assertEqual(reasons, {"ask_done": "all items handled",
                                   "ask_old": "expired"})

    # ---- _open_asks_summary

    def test_open_asks_summary_lists_only_open(self):
        created = ago(1)
        self.seed({"ask_open": self.make_ask("ask_open", to="one@example.org",
                                             created=created),
                   "ask_answered": self.make_ask(
                       "ask_answered", to="two@example.org", created=created,
                       state="answered")})
        self.assertEqual(finance._open_asks_summary(),
                         [{"ask_id": "ask_open", "to_addr": "one@example.org",
                           "created_at": created, "items": 2}])


class TestCategorizeOne(TestAsk):
    """categorize_one with run_budget_helper patched — every outcome shape."""

    def categorize(self, results=None, write_error=None, update_rule=True):
        calls = []

        def fake(cmd):
            calls.append(cmd)
            if write_error:
                raise write_error
            return {"results": results, "pushed": True}

        saved = finance.api_cache.run_budget_helper
        finance.api_cache.run_budget_helper = fake
        try:
            return finance.categorize_one("t1", "Groceries", update_rule), calls
        finally:
            finance.api_cache.run_budget_helper = saved

    def test_ok(self):
        self.queue_budget([], categories=[("c1", "Groceries", "Food")])
        (outcome, text), calls = self.categorize(
            results=[{"ok": True, "rule": "created"}])
        self.assertEqual(outcome, "ok")
        self.assertIn("categorized as 'Groceries'", text)
        self.assertIn("payee rule created", text)
        self.assertEqual(calls[0]["ops"][0],
                         {"op": "categorize", "id": "t1", "categoryId": "c1"})
        self.assertFalse(finance._HELPER_BUSY)

    def test_no_rule_passed_through(self):
        self.queue_budget([], categories=[("c1", "Groceries", "Food")])
        (outcome, _), calls = self.categorize(
            results=[{"ok": True, "rule": "skipped"}], update_rule=False)
        self.assertEqual(outcome, "ok")
        self.assertEqual(calls[0]["ops"][0]["rule"], False)

    def test_handled(self):
        self.queue_budget([], categories=[("c1", "Groceries", "Food")])
        (outcome, text), _ = self.categorize(
            results=[{"handled": True, "error": "already categorized"}])
        self.assertEqual((outcome, text), ("handled", "already categorized"))

    def test_error_result(self):
        self.queue_budget([], categories=[("c1", "Groceries", "Food")])
        (outcome, text), _ = self.categorize(results=[{"error": "boom"}])
        self.assertEqual((outcome, text), ("error", "boom"))

    def test_write_raises(self):
        self.queue_budget([], categories=[("c1", "Groceries", "Food")])
        (outcome, text), _ = self.categorize(
            write_error=RuntimeError("kaput"))
        self.assertEqual(outcome, "error")
        self.assertIn("kaput", text)
        self.assertFalse(finance._HELPER_BUSY)

    def test_unknown_category(self):
        self.queue_budget([], categories=[("c1", "Groceries", "Food")])
        (outcome, text), calls = self.categorize(
            results=[{"ok": True}], update_rule=True)
        self.assertEqual(outcome, "ok")
        outcome, text = finance.categorize_one("t1", "Nope", True)
        self.assertEqual(outcome, "error")
        self.assertIn("unknown category", text)

    def test_busy_flag_held(self):
        finance._HELPER_BUSY = True
        (outcome, _), calls = self.categorize(results=[{"ok": True}])
        self.assertEqual(outcome, "busy")
        self.assertEqual(calls, [])

    def test_busy_card_executing(self):
        finance._h_batch({"cards": [card("t1")]})
        finance.STATE["batch"]["cards"][0]["status"] = "in_progress"
        (outcome, _), calls = self.categorize(results=[{"ok": True}])
        self.assertEqual(outcome, "busy")
        self.assertEqual(calls, [])


# ---------------------------------------------------------------- email cards

class TestEmailCards(TestAsk):
    """POST /api/finance/email-cards — the email channel's finance action:
    cards from one ping mail, facts stamped from the budget copy."""

    CATS = [("c1", "Groceries", "Food"), ("c2", "Fuel", "Car")]

    def propose(self, body):
        return finance.HANDLERS["/api/finance/email-cards"](body)

    def body(self, pairs, email_id="m1", subject="Fwd: answers"):
        return {"email_id": email_id, "email_subject": subject,
                "suggestions": [{"transaction_id": t, "category": c}
                                for t, c in pairs]}

    def test_happy_stores_stamped_cards(self):
        self.queue_budget([qrow("t1", payee="Target"),
                           qrow("t2", date=20260813, amount=-4000,
                                payee="Shell", notes="fill-up")],
                          categories=self.CATS)
        code, out = self.propose(self.body([("t1", "Groceries"),
                                            ("t2", "Fuel")]))
        self.assertEqual(code, 200)
        self.assertEqual((out["stored"], out["rejected"]), (2, []))
        cards = self.saved_cards()
        self.assertEqual([c["transaction_id"] for c in cards], ["t1", "t2"])
        c1, c2 = cards
        self.assertEqual(c1["pick"], "email")
        self.assertEqual(c1["source"], {"email_id": "m1",
                                        "subject": "Fwd: answers"})
        self.assertEqual((c1["payee"], c1["amount"], c1["date"]),
                         ("Target", "-52.10", "2026-08-12"))
        self.assertEqual(c1["suggestions"],
                         [{"category": "Groceries", "basis": "email"}])
        self.assertEqual((c2["account"], c2["account_id"], c2["notes"]),
                         ("Checking", "a-0", "fill-up"))
        self.assertTrue(all(c["status"] == "pending" for c in cards))

    def test_shape_validation(self):
        self.queue_budget([qrow("t1")], categories=self.CATS)
        bad = ({}, {"email_id": 3},
               {"email_id": "m1", "suggestions": "x"},
               {"email_id": "m1", "suggestions": []},
               {"email_id": "m1", "suggestions": ["t1"]},
               {"email_id": "m1",
                "suggestions": [{"transaction_id": "t1"}]},
               {"email_id": "m1",
                "suggestions": [{"transaction_id": "t1", "category": " "}]},
               {"email_id": "m1",
                "suggestions": [{"transaction_id": "t1", "category": "x"},
                                {"transaction_id": "t1", "category": "y"}]})
        for body in bad:
            code, _ = self.propose(body)
            self.assertEqual(code, 400, body)
        self.assertEqual(finance.STATE["batch"]["cards"], [])

    def test_non_queue_ids_rejected_per_item(self):
        self.queue_budget([qrow("t1"), qrow("t2", cat="c1"),
                           qrow("t3", transfer="x")], categories=self.CATS)
        code, out = self.propose(self.body([("t1", "Groceries"),
                                            ("t2", "Groceries"),
                                            ("t3", "Groceries"),
                                            ("t4", "Groceries")]))
        self.assertEqual(code, 200)
        self.assertEqual(out["stored"], 1)
        self.assertEqual([r["transaction_id"] for r in out["rejected"]],
                         ["t2", "t3", "t4"])
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["t1"])

    def test_unknown_category_rejected_per_item(self):
        self.queue_budget([qrow("t1"), qrow("t2")], categories=self.CATS)
        code, out = self.propose(self.body([("t1", "Nope"), ("t2", "Fuel")]))
        self.assertEqual(code, 200)
        self.assertEqual(out["stored"], 1)
        self.assertEqual(out["rejected"][0]["transaction_id"], "t1")
        self.assertIn("unknown category", out["rejected"][0]["reason"])

    def test_all_bad_is_400_and_stores_nothing(self):
        self.queue_budget([qrow("t1", cat="c1")], categories=self.CATS)
        code, out = self.propose(self.body([("t1", "Groceries")]))
        self.assertEqual(code, 400)
        self.assertEqual(out["stored"], 0)
        self.assertEqual(finance.STATE["batch"]["cards"], [])

    def test_retry_same_mail_rejected_other_mail_allowed(self):
        self.queue_budget([qrow("t1")], categories=self.CATS)
        code, _ = self.propose(self.body([("t1", "Groceries")]))
        self.assertEqual(code, 200)
        code, out = self.propose(self.body([("t1", "Groceries")]))
        self.assertEqual(code, 400)
        self.assertIn("already proposed", out["rejected"][0]["reason"])
        code, out = self.propose(self.body([("t1", "Fuel")], email_id="m2"))
        self.assertEqual((code, out["stored"]), (200, 1))

    def test_cap(self):
        self.queue_budget([qrow(f"t{i}") for i in range(finance.EMAIL_CAP + 1)],
                          categories=self.CATS)
        code, out = self.propose(
            self.body([(f"t{i}", "Groceries")
                       for i in range(finance.EMAIL_CAP)]))
        self.assertEqual((code, out["stored"]), (200, finance.EMAIL_CAP))
        code, out = self.propose(
            self.body([(f"t{finance.EMAIL_CAP}", "Groceries")], email_id="m2"))
        self.assertEqual(code, 400)
        self.assertEqual(out["stored"], 0)
        self.assertIn("slots are full", out["rejected"][0]["reason"])

    def test_scan_rebuild_keeps_pending_email_cards(self):
        self.queue_budget([qrow("t1"), qrow("t2")], categories=self.CATS)
        self.propose(self.body([("t1", "Groceries"), ("t2", "Fuel")]))
        finance.STATE["batch"]["cards"][1]["status"] = "done"
        finance._SCAN_REQUESTED = finance.time.time()
        finance._h_batch({"cards": [card("new1")]})
        self.assertEqual([c["transaction_id"] for c in self.saved_cards()],
                         ["new1", "t1"])

    def test_top_up_not_suppressed_by_email_card(self):
        self.queue_budget([qrow("t1")], categories=self.CATS)
        self.propose(self.body([("t1", "Groceries")]))
        finance._h_batch({"cards": [card("t1")]})
        self.assertEqual([c["pick"] for c in self.saved_cards()],
                         ["email", "latest"])

    def test_apply_settles_every_copy(self):
        self.queue_budget([qrow("t1")], categories=self.CATS)
        self.propose(self.body([("t1", "Groceries")]))
        finance._h_batch({"cards": [card("t1")]})
        saved = finance.api_cache.run_budget_helper
        finance.api_cache.run_budget_helper = lambda cmd: {"results": [{"ok": True,
                                                       "rule": "created"}],
                                          "pushed": True}
        try:
            finance._work_items([("t1", "c1", "Groceries", True, False)])
        finally:
            finance.api_cache.run_budget_helper = saved
        self.assertEqual([c["status"] for c in self.saved_cards()],
                         ["done", "done"])


# ---------------------------------------------------------------- oddities

class TestOddities(FinanceTest):
    """The oddities queue: rules over the api-cache copy, queue and seen
    files, the seen endpoint. Charges are dated relative to today because
    state() reads the real clock."""

    def charge(self, tid, days_ago, cents, payee="p1", category=None):
        """One spending row in the fake copy; the payee row exists once."""
        path = finance.API_CACHE / "My-Budget-abc123" / "db.sqlite"
        conn = sqlite3.connect(path)
        day = date.today() - timedelta(days=days_ago)
        conn.execute("INSERT OR IGNORE INTO v_payees VALUES (?, ?)",
                     (payee, "Shop " + payee))
        conn.execute("INSERT INTO v_transactions (id, category, transfer_id, "
                     "date, amount, payee) VALUES (?, ?, NULL, ?, ?, ?)",
                     (tid, category, int(day.strftime("%Y%m%d")), -cents, payee))
        conn.commit()
        conn.close()

    def odd_file(self):
        return json.loads(finance.ODD_FILE.read_text())

    def seen_file(self):
        return json.loads(finance.ODD_SEEN_FILE.read_text())

    def test_flagged_charge_is_queued_and_shown(self):
        self.budget()
        self.charge("t1", 1, 60000)
        rows = finance.state()["oddities"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transaction_id"], "t1")
        self.assertEqual(rows[0]["payee"], "Shop p1")
        self.assertEqual(rows[0]["amount"], "-600.00")
        self.assertEqual(rows[0]["account"], "Checking")
        self.assertEqual(rows[0]["date"],
                         (date.today() - timedelta(days=1)).isoformat())
        self.assertEqual(rows[0]["reasons"],
                         ["unusually large charge", "new payee"])
        self.assertEqual(self.odd_file(), {"version": 1, "ids": ["t1"]})

    def test_nothing_flagged_is_an_empty_list(self):
        self.budget()
        self.charge("t1", 1, 1000)
        self.charge("h1", 40, 1000)
        self.assertEqual(finance.state()["oddities"], [])
        self.assertFalse(finance.ODD_FILE.exists())   # nothing to write yet

    def test_seen_takes_it_off_for_good(self):
        self.budget()
        self.charge("t1", 1, 60000)
        finance.state()
        code, out = finance.odd_seen({"transaction_id": "t1"})
        self.assertEqual((code, out), (200, {"seen": ["t1"]}))
        self.assertEqual(self.odd_file()["ids"], [])
        self.assertEqual(self.seen_file(),
                         {"t1": (date.today() - timedelta(days=1)).isoformat()})
        self.assertEqual(finance.state()["oddities"], [])
        self.assertEqual(self.odd_file()["ids"], [])

    def test_seen_wins_over_a_stale_queue(self):
        # a poll that was mid-rules when the seen tap landed rebuilds from
        # its old snapshot: the seen file still takes the id off
        self.budget()
        self.charge("t1", 1, 60000)
        finance.ODD["ids"] = ["t1"]
        finance.ODD_SEEN = {"t1": (date.today() - timedelta(days=1)).isoformat()}
        self.assertEqual(finance.state()["oddities"], [])
        self.assertEqual(self.odd_file()["ids"], [])

    def test_seen_unknown_or_missing_id(self):
        self.assertEqual(finance.odd_seen({"transaction_id": "nope"})[0], 404)
        self.assertEqual(finance.odd_seen({})[0], 400)

    def test_seen_all(self):
        self.budget()
        self.charge("t1", 3, 60000, payee="p1")
        self.charge("t2", 1, 60000, payee="p2")
        finance.state()
        code, out = finance.odd_seen({"all": True})
        self.assertEqual((code, out), (200, {"seen": ["t2", "t1"]}))
        self.assertEqual(self.odd_file()["ids"], [])
        self.assertEqual(set(self.seen_file()), {"t1", "t2"})
        self.assertEqual(finance.state()["oddities"], [])
        self.assertEqual(finance.odd_seen({"all": True}), (200, {"seen": []}))

    def test_seen_entries_outside_the_window_are_purged(self):
        self.budget()
        old = (date.today() - timedelta(days=8)).isoformat()
        finance.ODD_SEEN = {"old": old}
        self.charge("t1", 1, 60000)
        finance.state()
        finance.odd_seen({"transaction_id": "t1"})
        self.assertNotIn("old", self.seen_file())
        self.assertIn("t1", self.seen_file())

    def test_queued_charge_outlives_the_window(self):
        self.budget()
        self.charge("t1", 8, 60000)
        self.assertEqual(finance.state()["oddities"], [])   # outside the window
        finance.ODD["ids"] = ["t1"]
        rows = finance.state()["oddities"]
        self.assertEqual([r["transaction_id"] for r in rows], ["t1"])

    def test_queued_charge_no_longer_flagged_drops(self):
        self.budget(categories=[("c1", "Transfers", "Ignored")])
        self.charge("t1", 1, 60000)
        finance.state()
        self.assertEqual(self.odd_file()["ids"], ["t1"])
        path = finance.API_CACHE / "My-Budget-abc123" / "db.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("UPDATE v_transactions SET category = 'c1' WHERE id = 't1'")
        conn.commit()
        conn.close()
        self.assertEqual(finance.state()["oddities"], [])
        self.assertEqual(self.odd_file()["ids"], [])

    def test_unreadable_copy_keeps_the_last_rows(self):
        self.budget()
        self.charge("t1", 1, 60000)
        rows = finance.state()["oddities"]
        import shutil
        shutil.rmtree(finance.API_CACHE)
        self.assertEqual(finance.state()["oddities"], rows)
        self.assertEqual(self.odd_file()["ids"], ["t1"])

    def test_newest_first(self):
        self.budget()
        self.charge("t1", 3, 60000, payee="p1")
        self.charge("t2", 1, 60000, payee="p2")
        rows = finance.state()["oddities"]
        self.assertEqual([r["transaction_id"] for r in rows], ["t2", "t1"])

    def test_boot_creates_both_files(self):
        finance.boot()
        self.assertEqual(self.odd_file(), {"version": 1, "ids": []})
        self.assertEqual(self.seen_file(), {})


if __name__ == "__main__":
    unittest.main()
