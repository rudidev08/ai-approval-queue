"""Finance area — budget categorization queue: batch store and the
apply path.

Served by server.py (one process, one page). The finance-uncategorized cron
job (finance_jobs.py) builds the
card batch — the LATEST_CAP newest uncategorized transactions with
category suggestions — and saves it here. A run fired by the page's scan key
replaces the whole batch, so a transaction can never show as two cards; a
scheduled run only tops up the open slots (LATEST_CAP): cards still pending
stay, finished cards leave, and new cards fill the freed slots, newest
candidates first. A failed run saves an error record
instead (which step, error text); the page renders it as a card with no
buttons.

A scheduled save waits while the page is open with cards still pending
(PAGE_ACTIVE_SECONDS since the page's last flagged /api/state poll), so a
top-up never swaps the cards out from under a review; a settled-only batch
has no review to protect and the save lands. The page's own poll carries
the hold-update query flag — other /api/state pollers (the menu-bar app's
badge count) never hold a save. The page shows the batch's age so a held
rebuild reads as old, not fresh. The page's own scan key never waits and
always rebuilds — it stamps _SCAN_REQUESTED when it fires the job, which
marks that run's save as asked for.

State lives in the folder common.state_dir() names (common.write_json,
under this module's own LOCK): finance.json — one batch object:
cards, error, saved_at; finance-oddities.json — the oddities queue, a list of transaction ids;
finance-oddities-seen.json — {transaction id: its date} for the ones the
seen key took off. The shared decisions log gets one line per event:
finance_batch_saved, finance_batch_pruned, finance_batch_error,
finance_card_resolved, finance_cards_hidden, finance_category_created,
finance_oddity_seen.

Oddities: every poll runs the odd-charge rules of
services/mcp/budget/oddities.py (the same rules the daily email's Outliers
section prints) over the api-cache copy — the last ODD_WINDOW_DAYS days of
spending, plus every transaction already queued. A flagged transaction
that is neither queued nor seen joins the queue; a queued one stays until
the seen key takes it off (or until the rules stop flagging it — a twin
deleted, the row parked in an excluded group — since an id with no flag has
nothing to show). The seen file keeps the cutoff of the rules' window, so a
seen transaction can never be flagged again: by the time its entry is
purged its date is outside the window the rules read. The page gets one
row per queued transaction with its reasons; the finance tile counts them
with the pending cards.

Execution: POST /api/finance/apply takes a list of items — the page's
per-row check key sends one, its submit-all key sends every valid row —
and runs budget_helper.mjs (the budget MCP server's helper, run in place with node;
it resolves the budget app's API package from ~/Assistant/services/budget/node_modules via
createRequire) once, with one categorize op per item — category plus a
payee rule anchored on the transaction (none when the item says
rule=false) in one budget download and sync, no MCP layer in the loop.
Before that write the budget is pulled fresh and each item's rule is
checked with the budget server's rules.py, against the budget's rules plus
the ones earlier items of the same call write: a refused item writes
nothing and its card fails with the refusal text; a rule already there
(same conditions and category) is left out and the category is still set. A spending item filed into an income category refuses the whole call.
Each op checks its transaction
in the freshly downloaded budget: gone, already categorized, split, or
turned transfer means no write, and that card resolves as already handled.
One apply call runs at a time (409 while any card is in_progress); cards
left in_progress by a crash are settled at boot against the api-cache copy
(budget_helper.mjs downloads the budget into it before applying, so a landed write
shows there).

Pruning: a pending card whose transaction the api-cache copy shows
categorized, gone, or turned transfer is dropped on the next poll — page
and chat writes refresh that copy, so their transactions leave the queue at
once. Cards resolved through the page keep their status until the next
batch replaces them.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/finance/batch   {cards: [...]} from a good run, {error: {step,
                            message}} from a failed one. A scan-key run
                            replaces the whole batch; a scheduled run tops up
                            the open slots and is held (409) while the
                            page is open with cards still pending. 409 also
                            while a card is executing — a refusal, not a
                            failure, and the scan treats it as one
- POST /api/finance/scan-check  {candidates: [{transaction_id}]}: the
                            scan's pre-LLM question — would a scheduled save
                            land any of these right now? Answers {proceed,
                            reason} with save_batch's own arithmetic; a
                            page-asked rebuild proceeds unless a card is
                            executing. A "nothing new" skip drops finished
                            cards, standing in for the save's rewrite
- POST /api/finance/apply   {items: [{transaction_id, category, rule}]}:
                            resolve every category name against the
                            api-cache copy, then run one categorize op per
                            item in a single write-helper call (409 while
                            another apply runs); any invalid item refuses
                            the whole call, and so does spending filed into
                            an income category. rule is false (set the
                            category only, the rules untouched) or {amount:
                            any|approx|exact, repeat: any|monthly|yearly}:
                            the payee rule written with it, its amount and
                            date conditions anchored on this transaction
- POST /api/finance/hide    {transaction_id} takes one settled card off the
                            page, {all: true} every settled card. Settled is
                            done or already_handled; a card in any other
                            status still has a decision waiting. The
                            transaction is out of the uncategorized queue,
                            so no scan can propose it again
- POST /api/finance/odd-seen  {transaction_id} takes one oddity off the
                            page for good, {all: true} every queued one —
                            out of the queue, into the seen file
- POST /api/finance/create-category  {name, group}: create the category in
                            the budget app through the write helper (fresh budget
                            download, duplicate/group checks against it,
                            sync) and return its 'group: name' form; one
                            write at a time together with the apply path
- POST /api/finance/uncategorized  download a fresh budget copy (so the
                            picker and the scan see new categories and
                            transactions), then start the cron job of the
                            same name detached
"""

import glob
import json
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta

import common
from common import _log, _now

APP = pathlib.Path(__file__).resolve().parent
PROJECT = APP.parent.parent
sys.path.append(str(PROJECT / "services" / "mcp" / "budget"))
import api_cache  # noqa: E402
import cash_flow  # noqa: E402
import oddities  # noqa: E402
import rules  # noqa: E402



def state_file():
    return common.state_dir() / "finance.json"


def odd_file():
    return common.state_dir() / "finance-oddities.json"


def odd_seen_file():
    return common.state_dir() / "finance-oddities-seen.json"


# SCAN_JOB builds the batch (the page's scan key starts it detached);
# finance-daily only emails the report. The page's status line reads the
# scan job only, so a report run never makes a stale list look fresh
SCAN_JOB = "finance-uncategorized"

# the scan's cards: the newest uncategorized transactions
LATEST_CAP = 10
SUGGESTION_CAP = 3
STATUS_CAP = 1000   # a rule refusal names the rules in the way and the fix
BASES = {"history", "guess"}
CARD_KEYS = {"transaction_id", "date", "payee", "amount", "notes",
             "account", "account_id", "suggestions"}

# _PAGE_SEEN is stamped by page_seen(). In memory only: a restart means
# "never polled", so the next save lands.
PAGE_ACTIVE_SECONDS = 900
_PAGE_SEEN = 0.0

# the page's scan key stamps this when it fires the scan job: that run's save
# is the rebuild the user asked for, so it replaces the whole batch instead
# of topping up, and the page-open hold lets it through. Same in-memory deal
# as _PAGE_SEEN
SCAN_EXEMPT_SECONDS = 600
_SCAN_REQUESTED = 0.0

LOCK = threading.Lock()   # serializes every state mutation
# a non-apply write-helper run (create-category or the rescan refresh) is
# going — LOCK-guarded flag; the write itself runs outside LOCK, and the
# apply path refuses while it is set
_HELPER_BUSY = False
NAME_CAP = 100


# ---------------------------------------------------------------- state

def empty_state():
    return {"version": 1,
            "batch": {"cards": [], "error": None, "saved_at": None}}


def empty_odd():
    return {"version": 1, "ids": []}


# boot() replaces these with the loaded files; the defaults keep state() safe
# in tests that never boot the area
STATE = empty_state()
ODD = empty_odd()
ODD_SEEN = {}          # transaction id -> iso date of the transaction
_ODD_ROWS = {}         # transaction id -> the page row built on the last poll


def save_state(state):
    """Caller holds LOCK."""
    common.write_json(state_file(), state)


def save_odd(odd):
    """Caller holds LOCK."""
    common.write_json(odd_file(), odd)


def save_odd_seen(seen, today):
    """The seen file, entries dated before the rules' window dropped first
    — the same cutoff the window query uses, so a purged transaction is
    never read again. Caller holds LOCK."""
    cutoff = (today - timedelta(days=oddities.ODD_WINDOW_DAYS)).isoformat()
    seen = {i: d for i, d in seen.items() if d >= cutoff}
    common.write_json(odd_seen_file(), seen)
    return seen


def _find_card(state, transaction_id):
    return next((c for c in state["batch"]["cards"]
                 if c["transaction_id"] == transaction_id), None)


# ---------------------------------------------------------------- api-cache reads

def _db():
    """The api-cache SQLite copy read-only — the same read the budget MCP
    server does. Raises when the copy is missing."""
    cache = api_cache.cache_dir()
    paths = glob.glob(str(cache / "*" / "db.sqlite"))
    if len(paths) != 1:
        raise RuntimeError(f"expected one budget copy under {cache}, "
                           f"found {len(paths)}")
    conn = sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _db_error():
    """None when the api-cache copy opens, else why not. The reads below
    answer an unreadable copy with an empty list, a blank count, and the
    last poll's rows — a page that looks clean — so this is the one line
    that says the budget could not be read."""
    try:
        _db().close()
    except (RuntimeError, sqlite3.Error) as e:
        return str(e)
    return None


def _categories():
    """[{group, categories: [names]}] for the page's picker: one optgroup per
    category group, alphabetical inside each group; hidden categories and
    groups excluded, income kept (deposits need it). Empty on any read
    failure — the page then shows suggestion buttons only."""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT g.name AS grp, c.name FROM categories c "
            "JOIN category_groups g ON g.id = c.cat_group "
            "WHERE c.tombstone = 0 AND g.tombstone = 0 "
            "AND c.hidden = 0 AND g.hidden = 0 "
            "ORDER BY g.sort_order, g.name, c.name COLLATE NOCASE").fetchall()
    except (RuntimeError, sqlite3.Error):
        return []
    groups = {}
    for r in rows:
        groups.setdefault(r["grp"], []).append(r["name"])
    return [{"group": g, "categories": names} for g, names in groups.items()]


def _uncategorized_count():
    """On-budget uncategorized transactions in the api-cache copy — the same
    filter as the scan. None when the copy is unreadable (the page then
    hides its count line)."""
    try:
        conn = _db()
        return conn.execute(
            "SELECT COUNT(*) FROM v_transactions t "
            "JOIN accounts a ON a.id = t.account "
            "WHERE t.is_parent = 0 AND t.category IS NULL "
            "AND t.transfer_id IS NULL AND t.starting_balance_flag = 0 "
            "AND a.offbudget = 0").fetchone()[0]
    except (RuntimeError, sqlite3.Error):
        return None


def _category_id(conn, name):
    """Exact category name; the 'group: category' form disambiguates. Same
    rules as the budget MCP server. Raises ValueError."""
    rows = conn.execute(
        "SELECT c.id, c.name, g.name AS grp FROM categories c "
        "JOIN category_groups g ON g.id = c.cat_group "
        "WHERE c.tombstone = 0 AND g.tombstone = 0").fetchall()
    want = name.strip()
    hit = [r for r in rows if want in (r["name"], f"{r['grp']}: {r['name']}")]
    if len(hit) == 1:
        return hit[0]["id"]
    if len(hit) > 1:
        opts = ", ".join(sorted(f"'{r['grp']}: {r['name']}'" for r in hit))
        raise ValueError(f"category {want!r} is in more than one group — "
                         f"pick one of: {opts}")
    raise ValueError(f"unknown category {want!r}")


def _transaction_states(ids):
    """{id: 'open'|'handled'} from the api-cache copy for the given
    transaction ids — 'handled' when categorized, gone, or a transfer now.
    None when the copy is unreadable (then nothing is pruned or settled)."""
    if not ids:
        return {}
    try:
        conn = _db()
        qmarks = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT id, category, transfer_id FROM v_transactions "
            f"WHERE id IN ({qmarks})", list(ids)).fetchall()
    except (RuntimeError, sqlite3.Error):
        return None
    found = {r["id"]: r for r in rows}
    out = {}
    for i in ids:
        r = found.get(i)
        out[i] = "open" if r is not None and r["category"] is None \
            and r["transfer_id"] is None else "handled"
    return out


def _card_rules(ids):
    """{transaction id: [rule lines]} — the rules that can match each
    transaction, highest priority first: its payee's, and those whose
    imported payee text is in its bank payee name. Lines read as list_rules
    prints them. Empty on any read failure."""
    if not ids:
        return {}
    try:
        conn = _db()
        entries = rules.load(conn)
        payees, cats = rules.names(conn)
        qmarks = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT id, payee, imported_payee FROM v_transactions "
            f"WHERE id IN ({qmarks})", list(ids)).fetchall()
    except (RuntimeError, sqlite3.Error):
        return {}

    def fits(m, r):
        if m is None:
            return False
        if m[0] == "payee":
            return m[1] == r["payee"]
        return m[1].casefold() in (r["imported_payee"] or "").casefold()

    return {r["id"]: [rules.line(e, payees, cats) for e in rules.by_priority(entries)
                      if fits(rules.match_of(e), r)] for r in rows}


# ---------------------------------------------------------------- executor


def _spawn(work):
    """One approved apply call's execution thread — work is a list of
    (transaction_id, cat_id, cat_name, rule). Module-level so tests can run
    executions synchronously."""
    threading.Thread(target=_work_items, args=(work,), daemon=True).start()


def _work_items(work):
    """Run one categorize op per work item in a single write-helper call
    (one budget download, one sync); apply persisted in_progress before
    spawning. A fresh pull first: each item's rule is checked against the
    current rules plus those the items before it write."""
    refused, said, results, pushed, err = {}, {}, {}, False, None
    try:
        api_cache.run_budget_helper({"cmd": "pull"})
        conn = _db()
        entries = rules.load(conn)
        payees, cats = rules.names(conn)
        texts = rules.texts(conn)
        ops = []
        for transaction_id, cat_id, _, mode in work:
            op = {"op": "categorize", "id": transaction_id, "categoryId": cat_id}
            row = conn.execute("SELECT payee, amount, date, category, transfer_id, "
                               "is_parent FROM v_transactions WHERE id = ?",
                               (transaction_id,)).fetchone()
            if mode is False or row is None or row["category"] \
                    or row["transfer_id"] or row["is_parent"]:
                # gone or handled: the helper reports it, and no rule is
                # planned that a later item could count on
                said[transaction_id] = "no rule"
            elif not row["payee"]:
                refused[transaction_id] = ("nothing written — no payee, so no "
                                           "payee rule; untick rule to set only "
                                           "the category")
            else:
                rule = rules.from_transaction(row, cat_id, mode["amount"], mode["repeat"])
                refusal, covered = rules.check(rule, entries, payees, cats, texts)
                if refusal:
                    refused[transaction_id] = (f"nothing written — {refusal}. Or "
                                               "untick rule to set only the category")
                elif covered:
                    said[transaction_id] = (f"rule unchanged: rule {covered['id']} "
                                            "is the same rule")
                else:
                    op["rule"] = rules.to_api(rule)
                    entries.append(rules.entry_of(rule, "planned on another card"))
                    said[transaction_id] = "rule created"
            if transaction_id not in refused:
                ops.append(op)
        if ops:
            out = api_cache.run_budget_helper({"cmd": "update", "ops": ops})
            results = dict(zip([op["id"] for op in ops], out["results"]))
            pushed = out.get("pushed")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    with LOCK:
        for transaction_id, _, cat_name, _ in work:
            if transaction_id in refused:
                status, text = "failed", refused[transaction_id]
            elif err is not None:
                status, text = "failed", err
            else:
                res = results.get(transaction_id, {})
                if res.get("ok"):
                    status, text = "done", \
                        f"categorized as '{cat_name}' · {said[transaction_id]}"
                    if not pushed:
                        text += " · local copy only, the next sync pushes it"
                elif res.get("handled"):
                    status, text = "already_handled", \
                        res.get("error") or "already handled"
                else:
                    status, text = "failed", res.get("error") or "write failed"
            cards = [c for c in STATE["batch"]["cards"]
                     if c["transaction_id"] == transaction_id]
            if not cards:
                continue
            for c in cards:
                c["status"] = status
                c["status_text"] = text[:STATUS_CAP]
            _log({"event": "finance_card_resolved",
                  "transaction_id": transaction_id,
                  "args": {"category": cat_name},
                  "outcome": status,
                  "result": text})
        save_state(STATE)


# ---------------------------------------------------------------- API views

def _job_fields():
    """The scan job's last-run stamp, its coming run, whether that run
    failed, and the start time of a run going right now."""
    scan = common.cron_job(SCAN_JOB)
    return {"job_last_run_at": scan.get("last_run_at") if scan else None,
            "job_next_run_at": scan.get("next_run_at") if scan else None,
            "job_last_failed": bool(scan and scan.get("last_status")
                                    not in (None, "ok")),
            "job_running_since": common.job_running_since(scan)}


def _prune(state, states):
    """Drop pending cards whose transaction the api-cache copy shows handled
    elsewhere; resolved cards keep their status until the next batch. Caller
    holds LOCK; states comes from _transaction_states (None = no prune)."""
    if not states:
        return False
    keep = []
    dropped = False
    for c in state["batch"]["cards"]:
        if c["status"] == "pending" and states.get(c["transaction_id"]) == "handled":
            _log({"event": "finance_card_resolved",
                  "transaction_id": c["transaction_id"], "args": {},
                  "outcome": "pruned", "result": "handled outside the page"})
            dropped = True
        else:
            keep.append(c)
    if dropped:
        state["batch"]["cards"] = keep
        save_state(state)
    return dropped


# ---------------------------------------------------------------- handlers

def _topup(current, candidates):
    """What a scheduled save would land right now: pending cards stay, the
    open scan slots take candidates not already listed (dicts with at least
    transaction_id). Shared by save_batch and scan_check, so the scan's skip
    decision can never drift from the save's."""
    keep = [c for c in current if c["status"] == "pending"]
    have = {c["transaction_id"] for c in keep}
    room = LATEST_CAP - len(have)
    added = [c for c in candidates if c["transaction_id"] not in have][:room]
    return keep, added


def scan_check(state, body):
    """POST /api/finance/scan-check: would a scheduled save land anything
    right now? The scan asks with this run's candidate ids before its LLM
    call, so a run whose slots are all filled — or whose candidates are all
    already listed — skips the model call. Same decision path as save_batch
    (the executing-card refusal, _topup, the page-open hold); a page-asked
    rebuild proceeds unless a card is executing. A skipped run would never
    reach save_batch's rewrite, so the skip itself drops finished cards —
    the one write here. Caller holds LOCK."""
    cands = body.get("candidates")
    if not isinstance(cands, list) or not all(
            isinstance(c, dict) and isinstance(c.get("transaction_id"), str)
            and c["transaction_id"] for c in cands):
        return 400, {"error": "candidates must be a list of objects with "
                              "transaction_id (non-empty string)"}
    current = state["batch"]["cards"]
    if any(c["status"] == "in_progress" for c in current):
        return 200, {"proceed": False,
                     "reason": "a card is executing — the save would be refused"}
    if time.time() - _SCAN_REQUESTED <= SCAN_EXEMPT_SECONDS:
        return 200, {"proceed": True, "reason": "rebuild asked from the page"}
    if time.time() - _PAGE_SEEN < PAGE_ACTIVE_SECONDS \
            and any(c["status"] == "pending" for c in current):
        return 200, {"proceed": False,
                     "reason": "the page is open — the save would be held"}
    keep, added = _topup(current, cands)
    if added:
        return 200, {"proceed": True,
                     "reason": f"{len(added)} card(s) would land"}
    if len(keep) != len(current):
        state["batch"] = {"cards": keep, "error": None, "saved_at": _now()}
        _log({"event": "finance_batch_pruned",
              "rows": [c["transaction_id"] for c in keep]})
        save_state(state)
    return 200, {"proceed": False, "reason": "slots full or nothing new"}


def save_batch(state, body):
    """POST /api/finance/batch: replace the whole batch — cards from a good
    run, or an error record from a failed one. Caller holds LOCK."""
    err = body.get("error")
    if err is not None:
        if not isinstance(err, dict) or not isinstance(err.get("step"), str) \
                or not isinstance(err.get("message"), str) or not err["step"]:
            return 400, {"error": "error needs step and message strings"}
        if set(err) - {"step", "message"}:
            return 400, {"error": "error keys are step and message"}
        if any(c["status"] == "in_progress" for c in state["batch"]["cards"]):
            return 409, {"error": "a card is executing — the error would clear it"}
        state["batch"] = {"cards": [], "error": {"step": err["step"],
                          "message": err["message"][:1000]}, "saved_at": _now()}
        _log({"event": "finance_batch_error", "step": err["step"],
              "result": err["message"]})
        save_state(state)
        return 200, {"ok": True}

    cards = body.get("cards")
    if not isinstance(cards, list) or len(cards) > LATEST_CAP:
        return 400, {"error": f"cards must be a list of at most {LATEST_CAP}"}
    stored = []
    for c in cards:
        if not isinstance(c, dict):
            return 400, {"error": "each card must be an object"}
        unknown = set(c) - CARD_KEYS
        if unknown:
            return 400, {"error": f"unknown card keys: {sorted(unknown)}"}
        for k in ("transaction_id", "date", "amount", "account", "account_id"):
            if not isinstance(c.get(k), str) or not c[k]:
                return 400, {"error": f"card {k} must be a non-empty string"}
        for k in ("payee", "notes"):
            if not isinstance(c.get(k, ""), str):
                return 400, {"error": f"card {k} must be a string"}
        sugg = c.get("suggestions", [])
        if not isinstance(sugg, list) or len(sugg) > SUGGESTION_CAP:
            return 400, {"error": f"suggestions must be a list of at most {SUGGESTION_CAP}"}
        for s in sugg:
            if not isinstance(s, dict) or set(s) != {"category", "basis"} \
                    or not isinstance(s.get("category"), str) or not s["category"] \
                    or s.get("basis") not in BASES:
                return 400, {"error": "each suggestion needs category (non-empty "
                             "string) and basis (history|guess)"}
        stored.append({"transaction_id": c["transaction_id"], "date": c["date"],
                       "payee": c.get("payee", ""), "amount": c["amount"],
                       "notes": c.get("notes", ""), "account": c["account"],
                       "account_id": c["account_id"],
                       "suggestions": sugg,
                       "status": "pending", "status_text": ""})
    ids = [c["transaction_id"] for c in stored]
    if len(set(ids)) != len(ids):
        return 400, {"error": "duplicate transaction_id in the batch"}
    current = state["batch"]["cards"]
    if any(c["status"] == "in_progress" for c in current):
        return 409, {"error": "a card is executing — save again when it settles"}
    if time.time() - _SCAN_REQUESTED <= SCAN_EXEMPT_SECONDS:
        # the page's scan key fired this run: rebuild the whole batch
        state["batch"] = {"cards": stored, "error": None, "saved_at": _now()}
        _log({"event": "finance_batch_saved", "rows": ids})
        save_state(state)
        return 200, {"ok": True, "count": len(stored)}
    # a scheduled run only tops up open slots — finished cards leave, pending
    # cards stay, new candidates (not already listed) fill the freed slots —
    # and it waits while the page is open with a review still possible (at
    # least one pending card), so a card never swaps out from under a click
    if time.time() - _PAGE_SEEN < PAGE_ACTIVE_SECONDS \
            and any(c["status"] == "pending" for c in current):
        return 409, {"error": "the page is open — the batch stays as it is"}
    keep, added = _topup(current, stored)
    if not added and len(keep) == len(current):
        return 200, {"ok": True, "count": 0}   # no open slots, nothing new
    state["batch"] = {"cards": keep + added, "error": None, "saved_at": _now()}
    _log({"event": "finance_batch_saved",
          "rows": [c["transaction_id"] for c in added]})
    save_state(state)
    return 200, {"ok": True, "count": len(added)}


def apply(state, body):
    """POST /api/finance/apply: one or many items, all-or-nothing — any
    invalid item refuses the whole call before anything is marked. Caller
    holds LOCK; the write runs in its own thread after in_progress is
    persisted."""
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= LATEST_CAP:
        return 400, {"error": f"items must be a list of 1 to {LATEST_CAP}"}
    # one apply call at a time area-wide — the page disables the apply keys
    # while one works; this 409 is the real rule (second tab, curl)
    if any(cc["status"] == "in_progress" for cc in state["batch"]["cards"]):
        return 409, {"error": "another card is executing"}
    if _HELPER_BUSY:
        return 409, {"error": "another write is running — retry in a moment"}
    try:
        conn = _db()
    except (RuntimeError, sqlite3.Error) as e:
        return 500, {"error": f"category lookup failed: {e}"}
    work, cards = [], []
    for item in items:
        if not isinstance(item, dict):
            return 400, {"error": "each item must be an object"}
        c = _find_card(state, item.get("transaction_id"))
        if c is None:
            return 404, {"error": f"unknown card {item.get('transaction_id')!r}"}
        if c in cards:
            return 400, {"error": "duplicate transaction_id in the items"}
        category = item.get("category")
        if not isinstance(category, str) or not category.strip():
            return 400, {"error": "category is required"}
        if c["status"] != "pending":
            return 409, {"error": f"card {c['transaction_id']} is {c['status']}"}
        try:
            cat_id = _category_id(conn, category)
            row = conn.execute("SELECT amount FROM v_transactions WHERE id = ?",
                               (c["transaction_id"],)).fetchone()
            refusal = row and rules.income_refusal(rules.names(conn)[1], cat_id,
                                                   row["amount"])
        except sqlite3.Error as e:
            return 500, {"error": f"category lookup failed: {e}"}
        except ValueError as e:
            return 400, {"error": str(e)}
        if refusal:
            return 400, {"error": refusal}
        rule = item.get("rule")
        if rule is not False and not (
                isinstance(rule, dict) and set(rule) == {"amount", "repeat"}
                and rule["amount"] in ("any", "approx", "exact")
                and rule["repeat"] in ("any", "monthly", "yearly")):
            return 400, {"error": "rule must be false or {amount, repeat}"}
        work.append((c["transaction_id"], cat_id, category.strip(), rule))
        cards.append(c)
    for c in cards:
        c["status"] = "in_progress"
        c["status_text"] = "working"
    save_state(state)
    _spawn(work)
    return 202, {"ok": True, "count": len(work)}


def _settled(c):
    """A card whose transaction is out of the uncategorized queue for good:
    done (this page wrote the category) or already_handled (the write found
    it gone, already categorized, split, or turned transfer). Nothing else
    is settled — a failed card still has a retry waiting."""
    return c["status"] in ("done", "already_handled")


def hide(state, body):
    """POST /api/finance/hide: take settled cards off the page —
    {transaction_id} for one, {all: true} for every settled card. No scan
    can bring them back: the queue lists uncategorized transactions only.
    Caller holds LOCK."""
    if body.get("all") is True:
        drop = {c["transaction_id"] for c in state["batch"]["cards"]
                if _settled(c)}
    elif isinstance(body.get("transaction_id"), str):
        copies = [c for c in state["batch"]["cards"]
                  if c["transaction_id"] == body["transaction_id"]]
        if not copies:
            return 404, {"error": f"unknown card {body['transaction_id']!r}"}
        if not all(_settled(c) for c in copies):
            return 409, {"error": f"card is {copies[0]['status']}"}
        drop = {body["transaction_id"]}
    else:
        return 400, {"error": "pass transaction_id or all: true"}
    if drop:
        state["batch"]["cards"] = [c for c in state["batch"]["cards"]
                                   if c["transaction_id"] not in drop]
        _log({"event": "finance_cards_hidden", "rows": sorted(drop)})
        save_state(state)
    return 200, {"hidden": len(drop)}


def create_category(body):
    """POST /api/finance/create-category: create {name} in group {group}
    through the write helper — fresh budget download, duplicate and group
    checks against it (in budget_helper.mjs), sync. Runs in the request thread (the
    page's accept key stays grey until it settles); takes LOCK only around
    the single-flight flag, so a poll never queues behind the write."""
    global _HELPER_BUSY
    name = body.get("name")
    group = body.get("group")
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name is required"}
    if len(name.strip()) > NAME_CAP:
        return 400, {"error": f"name must be at most {NAME_CAP} characters"}
    if not isinstance(group, str) or not group.strip():
        return 400, {"error": "group is required"}
    name, group = name.strip(), group.strip()
    with LOCK:
        if any(c["status"] == "in_progress" for c in STATE["batch"]["cards"]):
            return 409, {"error": "a card is executing — try again when it settles"}
        if _HELPER_BUSY:
            return 409, {"error": "another write is running"}
        _HELPER_BUSY = True
    try:
        out = api_cache.run_budget_helper({"cmd": "update", "ops": [
            {"op": "create_category", "name": name, "group": group}]})
    except Exception as e:
        return 500, {"error": f"{type(e).__name__}: {e}"}
    finally:
        with LOCK:
            _HELPER_BUSY = False
    res = out["results"][0] if out["results"] else {}
    if not res.get("ok"):
        return 400, {"error": res.get("error") or "create failed"}
    qualified = f"{group}: {name}"
    _log({"event": "finance_category_created",
          "args": {"name": name, "group": group}, "outcome": "done",
          "result": qualified})
    return 200, {"ok": True, "category": qualified}


def uncategorized():
    """POST /api/finance/uncategorized: download a fresh budget copy
    through the write helper (categories created in the budget app's web UI reach
    the picker; the scan reads current transactions), then start
    `hermes cron run finance-uncategorized` detached. The run claims the
    job, so a second tap while one is going cannot fire it twice. The
    refresh runs in the request thread — the page's key stays grey while
    it works — under the same single-flight flag as create-category."""
    global _HELPER_BUSY, _SCAN_REQUESTED
    with LOCK:
        if any(c["status"] == "in_progress" for c in STATE["batch"]["cards"]):
            return 409, {"error": "a card is executing — try again when it settles"}
        if _HELPER_BUSY:
            return 409, {"error": "another write is running"}
        _HELPER_BUSY = True
    try:
        api_cache.run_budget_helper({"cmd": "pull"})
    except Exception as e:
        return 500, {"error": f"budget refresh failed — {type(e).__name__}: {e}"}
    finally:
        with LOCK:
            _HELPER_BUSY = False
    _SCAN_REQUESTED = time.time()
    try:
        subprocess.Popen(["hermes", "cron", "run", SCAN_JOB],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start hermes: {e}"}
    return 200, {"started": SCAN_JOB}


# ---------------------------------------------------------------- area interface

NAME = "finance"


def boot():
    """Load the state file and settle cards a crash left in_progress: the
    api-cache copy is current after any landed write (budget_helper.mjs downloads
    the budget into it before applying), so categorized there = the write
    landed (done), still open = it did not (back to pending). An unreadable
    copy leaves the card in_progress for the next boot."""
    global STATE, ODD, ODD_SEEN
    STATE = load_state()
    if STATE is None:
        STATE = empty_state()
        with LOCK:
            save_state(STATE)
    ODD = load_odd()
    if ODD is None:
        ODD = empty_odd()
        with LOCK:
            save_odd(ODD)
    ODD_SEEN = load_odd_seen()
    if ODD_SEEN is None:
        with LOCK:
            ODD_SEEN = save_odd_seen({}, date.today())
    stuck = [c["transaction_id"] for c in STATE["batch"]["cards"]
             if c["status"] == "in_progress"]
    if not stuck:
        return
    states = _transaction_states(stuck)
    if states is None:
        return
    with LOCK:
        for c in STATE["batch"]["cards"]:
            if c["status"] != "in_progress":
                continue
            handled = states.get(c["transaction_id"]) == "handled"
            c["status"] = "done" if handled else "pending"
            c["status_text"] = "categorized (settled at boot)" if handled else ""
            _log({"event": "finance_card_resolved",
                  "transaction_id": c["transaction_id"], "args": {},
                  "outcome": "reconciled",
                  "result": f"boot settle: {'landed' if handled else 'did not land'}"})
        save_state(STATE)


def load_state():
    """None when no state file exists."""
    path = state_file()
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_odd():
    """None when no oddities file exists."""
    path = odd_file()
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_odd_seen():
    """None when no seen file exists."""
    path = odd_seen_file()
    if not path.exists():
        return None
    return json.loads(path.read_text())


# ---------------------------------------------------------------- oddities

def _oddities(today):
    """The page's oddities rows: run the rules over the window plus the
    queue, queue what is flagged and neither queued nor seen, drop queued
    ids the rules no longer flag, and build one row per queued transaction
    (reasons in rule order). An unreadable copy leaves the queue alone and
    returns the rows of the last poll."""
    global _ODD_ROWS
    with LOCK:
        queued = list(ODD["ids"])
    try:
        flags = oddities.odd_candidates(_db(), today, ids=queued)
    except (RuntimeError, sqlite3.Error):
        return [_ODD_ROWS[i] for i in queued if i in _ODD_ROWS]
    by_id = {}
    for f in flags:
        by_id.setdefault(f["transaction_id"], []).append(f)
    with LOCK:
        ids = [i for i in queued if i in by_id and i not in ODD_SEEN]
        ids += [i for i in by_id if i not in ids and i not in ODD_SEEN]
        if ids != ODD["ids"]:
            ODD["ids"] = ids
            save_odd(ODD)
        rows = {}
        for i in ids:
            f = by_id[i][0]
            rows[i] = {"transaction_id": i, "date": cash_flow._iso(f["date"]),
                       "payee": f["payee"], "amount": cash_flow._dollars(f["amount"]),
                       "account": f["account"],
                       "reasons": [x["reason"] for x in by_id[i]]}
        _ODD_ROWS = rows
    return sorted(rows.values(), key=lambda r: r["date"], reverse=True)


def odd_seen(body):
    """POST /api/finance/odd-seen: queued oddities off the page for good —
    {transaction_id} for one, {all: true} for every queued one. A seen
    entry carries the transaction's date (today when the row is not on
    hand), which is what the purge reads."""
    global ODD_SEEN
    today = date.today()
    with LOCK:
        if body.get("all") is True:
            drop = list(ODD["ids"])
        elif isinstance(body.get("transaction_id"), str):
            if body["transaction_id"] not in ODD["ids"]:
                return 404, {"error": f"unknown oddity {body['transaction_id']!r}"}
            drop = [body["transaction_id"]]
        else:
            return 400, {"error": "pass transaction_id or all: true"}
        if not drop:
            return 200, {"seen": []}
        ODD["ids"] = [i for i in ODD["ids"] if i not in drop]
        save_odd(ODD)
        for tid in drop:
            row = _ODD_ROWS.pop(tid, None)
            ODD_SEEN[tid] = row["date"] if row else today.isoformat()
            _log({"event": "finance_oddity_seen", "transaction_id": tid,
                  "result": " · ".join(row["reasons"]) if row else ""})
        ODD_SEEN = save_odd_seen(ODD_SEEN, today)
    return 200, {"seen": drop}


def page_seen():
    """The page-open stamp: server.py calls this on GET /api/state carrying
    the hold-update flag (the page's own poll). A scheduled batch save is
    held while the stamp is fresh and at least one card is still pending."""
    global _PAGE_SEEN
    _PAGE_SEEN = time.time()


def state():
    """The finance part of GET /api/state: the batch (pruned against the
    api-cache copy), the uncategorized count, the picker's category list,
    the oddities rows, the jobs' stamps, and db_error when the copy cannot
    be read. The sqlite and jobs-file reads run outside LOCK, so a
    poll never queues an apply behind their I/O. More than the page polls
    this (the menu-bar app's badge count), so the call itself is not the
    page-open signal — page_seen() is."""
    with LOCK:
        pending = [c["transaction_id"] for c in STATE["batch"]["cards"]
                   if c["status"] == "pending"]
    states = _transaction_states(pending)
    card_rules = _card_rules(pending)
    with LOCK:
        if states:
            _prune(STATE, states)
        out = {"cards": [dict(c, rules=card_rules.get(c["transaction_id"], []))
                         for c in STATE["batch"]["cards"]],
               "error": STATE["batch"]["error"],
               "saved_at": STATE["batch"]["saved_at"]}
    out["db_error"] = _db_error()
    out["uncategorized"] = _uncategorized_count()
    out["categories"] = _categories()
    out["oddities"] = _oddities(date.today())
    out.update(_job_fields())
    return out


def _h_batch(body):
    with LOCK:
        return save_batch(STATE, body)


def _h_scan_check(body):
    with LOCK:
        return scan_check(STATE, body)


def _h_apply(body):
    with LOCK:
        return apply(STATE, body)


def _h_hide(body):
    with LOCK:
        return hide(STATE, body)


def _h_create_category(body):
    return create_category(body)   # takes LOCK internally around the flag


def _h_uncategorized(body):
    return uncategorized()


HANDLERS = {"/api/finance/batch": _h_batch,
            "/api/finance/scan-check": _h_scan_check,
            "/api/finance/apply": _h_apply,
            "/api/finance/hide": _h_hide,
            "/api/finance/odd-seen": odd_seen,
            "/api/finance/create-category": _h_create_category,
            "/api/finance/uncategorized": _h_uncategorized}
