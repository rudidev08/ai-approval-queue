"""Finance area — Actual Budget categorization queue: batch store and the
apply path.

Served by server.py (one process, one page). The finance-uncategorized cron
job (finance_scan.py; finance-daily only emails the report) builds the
card batch — the 5 newest plus 5 random uncategorized transactions with
category suggestions — and saves it here. Each card carries the pick it came
from ('latest' or 'random'), which the page shows as two groups. A third
pick, 'email', holds cards proposed from a ping mail through the email
channel's run_action — stored here so prune, settle, skip and apply cover
them, but never added or rebuilt by the scan. A run fired
by the page's scan key
replaces the whole batch, so a transaction can never show as two cards; a
scheduled run only tops up open slots (of LATEST_CAP and RANDOM_CAP, counted
per pick): cards still pending
stay, finished cards leave, and new cards fill the freed slots, newest
candidates first. A failed run saves an error record
instead (which step, error text); the page renders it as a card with no
buttons.

A scheduled save waits while the page is open (PAGE_ACTIVE_SECONDS since
the last /api/state poll), so a top-up never swaps the cards out from under
a review. A page left open therefore holds the batch until it closes; the
page shows the batch's age so that reads as a held rebuild, not a stall. The
page's own scan key never waits and always rebuilds — it stamps
_SCAN_REQUESTED when it fires the job, which marks that run's save as asked
for.

State lives in common.STATE_DIR (same tmp+fsync+replace write as the emails
area, under this module's own LOCK): finance.json — one batch object:
cards, error, saved_at; finance-ask.json — the categorize asks: numbered
questions about uncategorized transactions, emailed to a helper (created
through /api/finance/ask, answered by reply mail, paired back to
transactions by the inbox scan — the emails area's categorize_transaction
rows execute through categorize_one below). An ask is open while any of its
items is still uncategorized and it is younger than ASK_DAYS; the scan's
payload build (open_asks_payload) prunes dead ones. The shared decisions
log gets one line per event: finance_batch_saved, finance_batch_error,
finance_card_resolved, finance_card_skipped, finance_cards_hidden,
finance_category_created, finance_ask_created, finance_ask_pruned.

Execution: POST /api/finance/apply takes a list of items — the page's
per-row check key sends one, its submit-all key sends every valid row —
and runs write.mjs (the actual MCP server's helper, run in place with node;
it resolves @actual-app/api from ~/Iris/services/actual/node_modules via
createRequire) once, with one categorize op per item — category plus payee
rule (skipped when the item
says update_rule=false) in one budget download and sync, no MCP layer in
the loop. Each op checks its transaction
in the freshly downloaded budget: gone, already categorized, split, or
turned transfer means no write, and that card resolves as already handled.
One apply call runs at a time (409 while any card is in_progress); cards
left in_progress by a crash are settled at boot against the api-cache copy
(write.mjs downloads the budget into it before applying, so a landed write
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
                            each pick's open slots and is held (409) while the
                            page is
                            open. 409 also while a card is executing — a
                            refusal, not a failure, and the scan treats it as
                            one
- POST /api/finance/apply   {items: [{transaction_id, category,
                            update_rule?, approx?}]}: resolve every category
                            name against the api-cache copy, then run one
                            categorize op per item in a single write-helper
                            call (409 while another apply runs); any invalid
                            item refuses the whole call; update_rule=false
                            sets that item's category only, leaving the
                            payee's rules alone; approx=true anchors the
                            payee rule on this transaction's amount with
                            Actual's "is approximately" operator
- POST /api/finance/skip    {transaction_id, pick}: drop that one card — pick
                            names which copy of a duplicated transaction;
                            {pick: "email"} alone drops every email card not
                            executing. No skip memory — the next scan may
                            bring the transaction back
- POST /api/finance/hide    {transaction_id} takes one settled card off the
                            page, {all: true} every settled card. Settled is
                            done or already_handled; a card in any other
                            status still has a decision waiting. The
                            transaction is out of the uncategorized queue,
                            so no scan can propose it again
- POST /api/finance/ask     {to_addr, transaction_ids}: store a categorize
                            ask (every id must be an uncategorized queue
                            transaction, not covered by an open ask, at most
                            ASK_CAP) and return the numbered question lines
                            for the draft mail
- POST /api/finance/email-cards  {email_id, email_subject, suggestions}:
                            the email channel's finance action (run_action on
                            the actions_inbox MCP server) — cards from one
                            ping mail's content, pick "email". Every id must
                            be an uncategorized queue transaction, every
                            category a real one; the card's facts come from
                            the budget copy. Scan rebuilds and top-ups leave
                            pending email cards alone; per-item rejections
                            come back in 'rejected'
- POST /api/finance/create-category  {name, group}: create the category in
                            Actual through the write helper (fresh budget
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
import os
import pathlib
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

import common
from common import _log, _now

APP = pathlib.Path(__file__).resolve().parent
IRIS = APP.parent.parent
STATE_FILE = common.STATE_DIR / "finance.json"
ASK_FILE = common.STATE_DIR / "finance-ask.json"

# the write helper and the pinned node_modules it must run beside — same
# paths and pattern as services/mcp/actual/server.py
ACTUAL_APP = IRIS / "services" / "actual"
API_CACHE = ACTUAL_APP / "api-cache"
WRITE_MJS = IRIS / "services" / "mcp" / "actual" / "write.mjs"
ACTUAL_CONFIG = pathlib.Path.home() / ".config/actual/env"
NODE = "/Users/me/.local/bin/node"
SERVER_URL = "http://127.0.0.1:60195"
WRITE_TIMEOUT = 300

# SCAN_JOB builds the batch (the page's scan key starts it detached);
# DAILY_JOB only emails the report. The page's status line reads both
DAILY_JOB = "finance-daily"
SCAN_JOB = "finance-uncategorized"
CRON_JOBS = pathlib.Path.home() / ".hermes/cron/jobs.json"
CRON_EXECUTIONS = pathlib.Path.home() / ".hermes/cron/executions.db"

# the two picks the scan builds, each with its own slot count — the page
# shows them as two groups
LATEST_CAP = 5
RANDOM_CAP = 5
PICK_CAPS = {"latest": LATEST_CAP, "random": RANDOM_CAP}
BATCH_CAP = LATEST_CAP + RANDOM_CAP
# email cards (pick "email") come from the email channel's run_action, not
# the scan — they ride the same batch list but outside the scan's caps
EMAIL_CAP = 20
SUGGESTION_CAP = 3
STATUS_CAP = 300
BASES = {"history", "guess", "email"}
CARD_KEYS = {"transaction_id", "date", "payee", "amount", "notes",
             "account", "account_id", "pick", "suggestions"}

# categorize asks: numbered questions about uncategorized transactions, emailed
# to a helper (the finance-buddy skill); the reply comes back through the
# inbox scan as categorize_transaction rows in the emails area. An ask is open
# while any of its items is still uncategorized and it is younger than
# ASK_DAYS; the scan's payload build does the pruning, so expiry always has a
# trigger. Amounts and dates are stored in the exact forms the page's
# formatters parse (dollars "-52.10", iso "2026-08-12").
ASK_CAP = 20
ASK_DAYS = 14
# the ask mail's subject — pairing rides on the sender address alone (one
# open ask per recipient), never on a subject tag
ASK_SUBJECT = "categorizing help"

# the page's poll of /api/state stamps this; a scheduled save is held while
# it is fresh, so a top-up never swaps the cards out mid-review. In memory
# only — a restart means "never polled", so the next save lands.
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


def empty_asks():
    return {"version": 1, "asks": {}}


# boot() replaces these with the loaded files; the defaults keep state() safe
# in tests that never boot the area
STATE = empty_state()
ASKS = empty_asks()


def _write_json(path, obj):
    """tmp file + fsync + os.replace + dir fsync, mode 0600. Caller holds LOCK."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(common.STATE_DIR, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def save_state(state):
    """Caller holds LOCK."""
    _write_json(STATE_FILE, state)


def save_asks(asks):
    """Caller holds LOCK."""
    _write_json(ASK_FILE, asks)


def _find_card(state, transaction_id):
    return next((c for c in state["batch"]["cards"]
                 if c["transaction_id"] == transaction_id), None)


# ---------------------------------------------------------------- api-cache reads

def _db():
    """The api-cache SQLite copy read-only — the same read the actual MCP
    server does. Raises when the copy is missing."""
    paths = glob.glob(str(API_CACHE / "*" / "db.sqlite"))
    if len(paths) != 1:
        raise RuntimeError(f"expected one budget copy under {API_CACHE}, "
                           f"found {len(paths)}")
    conn = sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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
    rules as the actual MCP server. Raises ValueError."""
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


# ---------------------------------------------------------------- ask reads

def _dollars(cents):
    return f"{(cents or 0) / 100:.2f}"


def _iso(yyyymmdd):
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _money(cents):
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:.2f}"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _human_date(iso_date):
    """"2026-08-12" -> "Aug 12", for the mail lines."""
    _, m, d = iso_date.split("-")
    return f"{_MONTHS[int(m) - 1]} {int(d)}"


def _ask_expired(ask):
    created = datetime.fromisoformat(
        ask["created_at"].replace("Z", "+00:00")).timestamp()
    return created < time.time() - ASK_DAYS * 86400


def _queue_rows(conn, ids):
    """{id: row} for the given transaction ids that match the uncategorized
    queue filter — the same WHERE the scan's queue query uses."""
    qmarks = ",".join("?" * len(ids))
    return {r["id"]: dict(r) for r in conn.execute(
        "SELECT t.id, t.date, t.amount, COALESCE(p.name, '') AS payee, "
        "COALESCE(t.notes, '') AS notes, a.name AS account, "
        "t.account AS account_id "
        "FROM v_transactions t "
        "JOIN accounts a ON a.id = t.account "
        "LEFT JOIN v_payees p ON p.id = t.payee "
        "WHERE t.is_parent = 0 AND t.category IS NULL "
        "AND t.transfer_id IS NULL AND t.starting_balance_flag = 0 "
        "AND a.offbudget = 0 "
        f"AND t.id IN ({qmarks})", list(ids))}


def open_ask_for_sender(from_text):
    """The ask_id when from_text (a From: value, any case) names an open,
    unexpired ask's recipient address — the scan's deterministic pairing of
    a reply to its ask. None when nothing matches."""
    lowered = (from_text or "").lower()
    with LOCK:
        for a in ASKS["asks"].values():
            if a["state"] == "open" and not _ask_expired(a) \
                    and a["to_addr"] in lowered:
                return a["ask_id"]
    return None


def get_open_ask(ask_id):
    """The ask record when it exists, is open, and is not expired; else None.
    Expired asks are dead here even before the payload build prunes them."""
    with LOCK:
        ask = ASKS["asks"].get(ask_id)
        if ask is None or ask["state"] != "open" or _ask_expired(ask):
            return None
        return dict(ask)


def open_asks_payload(referenced):
    """pending_asks for the inbox scan: open asks not referenced by a pending
    set, items annotated still_open, plus the category list. Prunes expired
    asks and asks whose every item was handled (a write only when something
    changed). An unreadable budget copy prunes nothing and marks every item
    still_open."""
    with LOCK:
        open_asks = {k: a for k, a in ASKS["asks"].items()
                     if a["state"] == "open"}
    if open_asks:
        ids = [it["transaction_id"] for a in open_asks.values()
               for it in a["items"]]
        states = _transaction_states(ids)
        dead = []
        for k, a in open_asks.items():
            if _ask_expired(a):
                dead.append((k, "expired"))
            elif states is not None and all(
                    states.get(it["transaction_id"]) == "handled"
                    for it in a["items"]):
                dead.append((k, "all items handled"))
        if dead:
            with LOCK:
                for k, why in dead:
                    if ASKS["asks"].pop(k, None) is not None:
                        _log({"event": "finance_ask_pruned",
                              "args": {"ask_id": k},
                              "outcome": "done", "result": why})
                save_asks(ASKS)
            open_asks = {k: a for k, a in open_asks.items()
                         if k not in dict(dead)}
    out = []
    for k, a in open_asks.items():
        if k in referenced:
            continue
        items = [dict(it, still_open=(states is None or states.get(
            it["transaction_id"]) != "handled")) for it in a["items"]]
        out.append({"ask_id": k, "to_addr": a["to_addr"],
                    "created_at": a["created_at"], "subject": a["subject"],
                    "items": items})
    return {"asks": out, "categories": _categories()}


# ---------------------------------------------------------------- the write helper

def _config():
    """KEY=VALUE pairs from ACTUAL_CONFIG; raises when required keys are
    missing. Same file the actual MCP server and hermes/actual/driver.py read."""
    values = {}
    try:
        with open(ACTUAL_CONFIG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    missing = [k for k in ("ACTUAL_PASSWORD", "ACTUAL_SYNC_ID") if not values.get(k)]
    if missing:
        raise RuntimeError(f"missing {', '.join(missing)} in {ACTUAL_CONFIG}")
    return values


def _run_write(command):
    """Run write.mjs in a fresh Node process; returns {results, pushed}."""
    cfg = _config()
    env = dict(os.environ)
    env.update({
        "ACTUAL_SERVER_URL": cfg.get("ACTUAL_SERVER_URL", SERVER_URL),
        "ACTUAL_PASSWORD": cfg["ACTUAL_PASSWORD"],
        "ACTUAL_SYNC_ID": cfg["ACTUAL_SYNC_ID"],
        "ACTUAL_CACHE_DIR": str(API_CACHE),
    })
    try:
        result = subprocess.run([NODE, str(WRITE_MJS), json.dumps(command)],
                                cwd=ACTUAL_APP, env=env, capture_output=True,
                                text=True, timeout=WRITE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"write helper timed out after {WRITE_TIMEOUT}s") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"write helper failed: {detail[-1000:]}")
    for line in result.stdout.splitlines():
        if line.startswith("WRITE_RESULT "):
            return json.loads(line[len("WRITE_RESULT "):])
    raise RuntimeError(f"write helper returned no result JSON: {result.stdout[-500:]!r}")


# ---------------------------------------------------------------- executor

# the write helper's rule outcomes, spelled out for status text — shared by
# _work_items (finance cards) and categorize_one (emails-area rows)
RULE_TEXTS = {"created": "payee rule created",
              "updated": "payee rule updated",
              "no payee": "no payee, no rule",
              "skipped": "payee rule unchanged"}


def _spawn(work):
    """One approved apply call's execution thread — work is a list of
    (transaction_id, cat_id, cat_name, update_rule, approx). Module-level so
    tests can run executions synchronously."""
    threading.Thread(target=_work_items, args=(work,), daemon=True).start()


def _work_items(work):
    """Run one categorize op per work item in a single write-helper call
    (one budget download, one sync); apply persisted in_progress before
    spawning. Skip 409s an in_progress card, so nothing can void the cards
    meanwhile."""
    try:
        ops = []
        for transaction_id, cat_id, _, update_rule, approx in work:
            op = {"op": "categorize", "id": transaction_id, "categoryId": cat_id}
            if not update_rule:
                op["rule"] = False
            elif approx:
                op["approx"] = True
            ops.append(op)
        out = _run_write({"cmd": "update", "ops": ops})
        results, pushed, err = out["results"], out.get("pushed"), None
    except Exception as e:
        results, pushed, err = [], False, f"{type(e).__name__}: {e}"
    with LOCK:
        for i, (transaction_id, _, cat_name, _, _) in enumerate(work):
            if err is not None:
                status, text = "failed", err
            else:
                res = results[i] if i < len(results) else {}
                if res.get("ok"):
                    status, text = "done", \
                        f"categorized as '{cat_name}' · {RULE_TEXTS.get(res.get('rule'), '')}"
                    if not pushed:
                        text += " · local copy only, the next sync pushes it"
                elif res.get("handled"):
                    status, text = "already_handled", \
                        res.get("error") or "already handled"
                else:
                    status, text = "failed", res.get("error") or "write failed"
            # every card carrying this id settles: two routes can propose
            # the same transaction (no cross-route dedup), and the one
            # write resolves all copies at once
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
                  "outcome": status if status != "done" else "done",
                  "result": text})
        save_state(STATE)


def categorize_one(transaction_id, category, update_rule):
    """One categorize write for the emails area's categorize_transaction
    rows — same helper, same single-flight as the apply path: the busy
    check-and-set is one atomic step under LOCK, exactly as apply does it.
    Returns (outcome, text): 'ok' | 'handled' | 'busy' | 'error'."""
    global _HELPER_BUSY
    with LOCK:
        if any(c["status"] == "in_progress" for c in STATE["batch"]["cards"]):
            return "busy", "a finance card is executing"
        if _HELPER_BUSY:
            return "busy", "another write is running"
        _HELPER_BUSY = True
    try:
        try:
            conn = _db()
            cat_id = _category_id(conn, category)
        except (RuntimeError, sqlite3.Error, ValueError) as e:
            return "error", f"category lookup failed: {e}"
        op = {"op": "categorize", "id": transaction_id, "categoryId": cat_id}
        if not update_rule:
            op["rule"] = False
        try:
            out = _run_write({"cmd": "update", "ops": [op]})
        except Exception as e:
            return "error", f"{type(e).__name__}: {e}"
        res = out["results"][0] if out["results"] else {}
        if res.get("ok"):
            text = f"categorized as '{category}' · {RULE_TEXTS.get(res.get('rule'), '')}"
            if not out.get("pushed"):
                text += " · local copy only, the next sync pushes it"
            return "ok", text
        if res.get("handled"):
            return "handled", res.get("error") or "already handled"
        return "error", res.get("error") or "write failed"
    finally:
        with LOCK:
            _HELPER_BUSY = False


# ---------------------------------------------------------------- API views

def _job_record(jobs, name):
    return next((j for j in jobs if j.get("name") == name), None)


def _job_records():
    """The finance-daily and finance-uncategorized entries from hermes' own jobs
    file. Unreadable file -> two Nones."""
    try:
        jobs = json.loads(CRON_JOBS.read_text())["jobs"]
    except (OSError, ValueError, KeyError):
        return None, None
    return _job_record(jobs, DAILY_JOB), _job_record(jobs, SCAN_JOB)


def _job_running_since(job):
    """Start time of a run going right now, from hermes' executions ledger —
    same read as the emails area. Any read problem -> None."""
    if not job or not job.get("id"):
        return None
    try:
        conn = sqlite3.connect(f"file:{CRON_EXECUTIONS}?mode=ro", uri=True,
                               timeout=1)
        try:
            row = conn.execute(
                "SELECT status, coalesce(started_at, claimed_at) "
                "FROM executions WHERE job_id = ? "
                "ORDER BY claimed_at DESC, id DESC LIMIT 1",
                (job["id"],)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row and row[0] in ("claimed", "running"):
        return row[1]
    return None


def _job_fields():
    """last-run stamp (newest of the two jobs), the coming run (soonest of
    the two), whether the last run failed, and the start time of a run
    going right now."""
    daily, scan = _job_records()
    jobs = [j for j in (daily, scan) if j and j.get("last_run_at")]
    newest = max(jobs, key=lambda j: j["last_run_at"], default=None)
    nexts = [j["next_run_at"] for j in (daily, scan)
             if j and j.get("next_run_at")]
    running = _job_running_since(daily) or _job_running_since(scan)
    return {"job_last_run_at": newest["last_run_at"] if newest else None,
            "job_next_run_at": min(nexts, default=None),
            "job_last_failed": bool(newest and newest.get("last_status")
                                    not in (None, "ok")),
            "job_running_since": running}


def _card_view(c):
    return dict(c)


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
    if not isinstance(cards, list) or len(cards) > BATCH_CAP:
        return 400, {"error": f"cards must be a list of at most {BATCH_CAP}"}
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
        if c.get("pick") not in PICK_CAPS:
            return 400, {"error": "card pick must be latest or random"}
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
                       "account_id": c["account_id"], "pick": c["pick"],
                       "suggestions": sugg,
                       "status": "pending", "status_text": ""})
    ids = [c["transaction_id"] for c in stored]
    if len(set(ids)) != len(ids):
        return 400, {"error": "duplicate transaction_id in the batch"}
    for pick, cap in PICK_CAPS.items():
        if sum(1 for c in stored if c["pick"] == pick) > cap:
            return 400, {"error": f"at most {cap} {pick} cards"}
    current = state["batch"]["cards"]
    if any(c["status"] == "in_progress" for c in current):
        return 409, {"error": "a card is executing — save again when it settles"}
    if time.time() - _SCAN_REQUESTED <= SCAN_EXEMPT_SECONDS:
        # the page's scan key fired this run: rebuild the scan's picks.
        # Email cards are not the scan's to rebuild — pending ones stay.
        state["batch"] = {"cards": stored + [c for c in current
                                             if c["pick"] == "email"
                                             and c["status"] == "pending"],
                          "error": None, "saved_at": _now()}
        _log({"event": "finance_batch_saved", "rows": ids})
        save_state(state)
        return 200, {"ok": True, "count": len(stored)}
    # a scheduled run only tops up open slots — finished cards leave, pending
    # cards stay, new candidates (not already listed) fill the freed slots —
    # and it waits while the page is open so cards never swap mid-review
    if time.time() - _PAGE_SEEN < PAGE_ACTIVE_SECONDS:
        return 409, {"error": "the page is open — the batch stays as it is"}
    keep = [c for c in current if c["status"] == "pending"]
    # an email card never suppresses a scan card for the same transaction —
    # duplicate proposals across routes are accepted by choice
    have = {c["transaction_id"] for c in keep if c["pick"] != "email"}
    # each pick fills its own slots, so the two groups keep their shape
    added = []
    for pick, cap in PICK_CAPS.items():
        room = cap - sum(1 for c in keep if c["pick"] == pick)
        added += [c for c in stored if c["pick"] == pick
                  and c["transaction_id"] not in have][:room]
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
    if not isinstance(items, list) or not 1 <= len(items) <= BATCH_CAP + EMAIL_CAP:
        return 400, {"error": f"items must be a list of 1 to "
                              f"{BATCH_CAP + EMAIL_CAP}"}
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
        except sqlite3.Error as e:
            return 500, {"error": f"category lookup failed: {e}"}
        except ValueError as e:
            return 400, {"error": str(e)}
        work.append((c["transaction_id"], cat_id, category.strip(),
                     bool(item.get("update_rule", True)),
                     bool(item.get("approx", False))))
        cards.append(c)
    for c in cards:
        c["status"] = "in_progress"
        c["status_text"] = "working"
    save_state(state)
    _spawn(work)
    return 202, {"ok": True, "count": len(work)}


def skip(state, body):
    """POST /api/finance/skip: drop cards — no skip memory of any kind; the
    next scan rebuilds the batch and may bring the transaction back.
    {transaction_id, pick} drops that one card — pick names which copy of a
    duplicated transaction goes. {pick: "email"} alone is the page's
    hide-all key: every email card not executing goes. Caller holds LOCK."""
    pick = body.get("pick")
    if pick not in ("latest", "random", "email"):
        return 400, {"error": "pick must be latest, random, or email"}
    tid = body.get("transaction_id")
    if tid is None:
        if pick != "email":
            return 400, {"error": "only the email cards can be dropped whole"}
        keep, drop = [], []
        for c in state["batch"]["cards"]:
            (drop if c["pick"] == "email" and c["status"] != "in_progress"
             else keep).append(c)
        if drop:
            state["batch"]["cards"] = keep
            for c in drop:
                _log({"event": "finance_card_skipped",
                      "transaction_id": c["transaction_id"],
                      "result": f"hide all: skipped in status {c['status']}"})
            save_state(state)
        return 200, {"ok": True, "count": len(drop)}
    c = next((cc for cc in state["batch"]["cards"]
              if cc["transaction_id"] == tid and cc["pick"] == pick), None)
    if c is None:
        return 404, {"error": "unknown card"}
    if c["status"] == "in_progress":
        return 409, {"error": "card is executing"}
    state["batch"]["cards"] = [cc for cc in state["batch"]["cards"] if cc is not c]
    _log({"event": "finance_card_skipped", "transaction_id": c["transaction_id"],
          "result": f"skipped in status {c['status']}"})
    save_state(state)
    return 200, {"ok": True}


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
    Every copy of the transaction goes, since one write settles them all.
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


def create_ask(body):
    """POST /api/finance/ask: store a categorize ask and return the numbered
    question lines for the draft mail. Every id must be an uncategorized
    queue transaction (the scan's filter) and not already covered by an open
    ask. The budget read runs outside LOCK; the store mutation under it."""
    to_addr = body.get("to_addr")
    if not isinstance(to_addr, str) or "@" not in to_addr:
        return 400, {"error": "to_addr must be the recipient's email address"}
    ids = body.get("transaction_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= ASK_CAP \
            or any(not isinstance(i, str) or not i for i in ids):
        return 400, {"error": f"transaction_ids must be a list of 1 to {ASK_CAP} ids"}
    if len(set(ids)) != len(ids):
        return 400, {"error": "duplicate transaction ids"}
    try:
        conn = _db()
        rows = _queue_rows(conn, ids)
    except (RuntimeError, sqlite3.Error) as e:
        return 500, {"error": f"budget copy unreadable: {e}"}
    bad = [i for i in ids if i not in rows]
    if bad:
        return 400, {"error": "not uncategorized queue transactions: "
                     + ", ".join(bad)}
    with LOCK:
        covered = {it["transaction_id"] for a in ASKS["asks"].values()
                   if a["state"] == "open" for it in a["items"]}
        dup = [i for i in ids if i in covered]
        if dup:
            return 400, {"error": "already covered by an open ask: "
                         + ", ".join(dup)}
        # one open ask per recipient — the scan pairs a reply to its ask by
        # sender address, which two asks to the same person would make
        # ambiguous
        same_addr = [a["ask_id"] for a in ASKS["asks"].values()
                     if a["state"] == "open" and not _ask_expired(a)
                     and a["to_addr"] == to_addr.strip().lower()]
        if same_addr:
            return 400, {"error": f"an open ask to {to_addr.strip().lower()} "
                                  f"already exists ({same_addr[0]}) — wait for "
                                  "its reply or let it expire"}
        ask_id = "ask_" + uuid.uuid4().hex[:12]
        items, lines = [], []
        for n, i in enumerate(ids, 1):
            r = rows[i]
            items.append({"n": n, "transaction_id": i, "date": _iso(r["date"]),
                          "payee": r["payee"], "amount": _dollars(r["amount"]),
                          "account": r["account"], "notes": r["notes"]})
            lines.append(f"{n}) {_human_date(_iso(r['date']))} · "
                         f"{r['payee'] or '(no payee)'} · {_money(r['amount'])}")
        ASKS["asks"][ask_id] = {"ask_id": ask_id,
                                "to_addr": to_addr.strip().lower(),
                                "created_at": _now(), "subject": ASK_SUBJECT,
                                "items": items, "state": "open"}
        save_asks(ASKS)
    _log({"event": "finance_ask_created",
          "args": {"ask_id": ask_id, "to": to_addr.strip().lower()},
          "outcome": "done", "result": f"{len(items)} transactions"})
    return 200, {"ask_id": ask_id, "subject": ASK_SUBJECT, "lines": lines}


def create_email_cards(body):
    """POST /api/finance/email-cards: the email channel's finance action —
    cards proposed from one ping mail's content (run_action on the
    actions_inbox MCP server). The caller supplies (transaction_id,
    category) pairs plus the source mail's id and subject for the page.
    Every id must be an uncategorized queue transaction and every category
    a real one; the card's facts come from the budget copy here, never
    from the mail. Partial success is normal: bad items come back in
    'rejected' with per-item reasons. The budget read runs outside LOCK;
    the store mutation under it."""
    email_id = body.get("email_id")
    if not isinstance(email_id, str) or not email_id.strip():
        return 400, {"error": "email_id is required"}
    subject = body.get("email_subject", "")
    if not isinstance(subject, str):
        return 400, {"error": "email_subject must be a string"}
    email_id, subject = email_id.strip(), subject.strip()[:200]
    items = body.get("suggestions")
    if not isinstance(items, list) or not 1 <= len(items) <= EMAIL_CAP:
        return 400, {"error": f"suggestions must be a list of 1 to "
                              f"{EMAIL_CAP} items"}
    seen, pairs = set(), []
    for it in items:
        if not isinstance(it, dict) \
                or not isinstance(it.get("transaction_id"), str) \
                or not it["transaction_id"] \
                or not isinstance(it.get("category"), str) \
                or not it["category"].strip():
            return 400, {"error": "each suggestion needs transaction_id "
                                  "and category strings"}
        if it["transaction_id"] in seen:
            return 400, {"error": "duplicate transaction_id in the suggestions"}
        seen.add(it["transaction_id"])
        pairs.append((it["transaction_id"], it["category"].strip()))
    try:
        conn = _db()
        rows = _queue_rows(conn, [t for t, _ in pairs])
    except (RuntimeError, sqlite3.Error) as e:
        return 500, {"error": f"budget copy unreadable: {e}"}
    # read-phase checks: the queue filter, then the category name
    ok, rejected = [], []
    for transaction_id, category in pairs:
        if transaction_id not in rows:
            rejected.append({"transaction_id": transaction_id,
                             "reason": "not an uncategorized queue transaction "
                                       "(handled elsewhere or unknown)"})
            continue
        try:
            _category_id(conn, category)
        except ValueError as e:
            rejected.append({"transaction_id": transaction_id,
                             "reason": str(e)})
            continue
        ok.append((transaction_id, category))
    stored = []
    with LOCK:
        pending_email = [c for c in STATE["batch"]["cards"]
                         if c["pick"] == "email" and c["status"] == "pending"]
        for transaction_id, category in ok:
            r = rows[transaction_id]
            # retry guard, not cross-route dedup: the same transaction from
            # a different mail may be proposed again on purpose
            if any(c["transaction_id"] == transaction_id
                   and c.get("source", {}).get("email_id") == email_id
                   for c in pending_email):
                rejected.append({"transaction_id": transaction_id,
                                 "reason": "already proposed from this mail"})
                continue
            if len(pending_email) + len(stored) >= EMAIL_CAP:
                rejected.append({"transaction_id": transaction_id,
                                 "reason": f"the email card slots are full "
                                           f"({EMAIL_CAP}) — apply or skip some first"})
                continue
            stored.append({"transaction_id": transaction_id,
                           "date": _iso(r["date"]), "payee": r["payee"],
                           "amount": _dollars(r["amount"]), "notes": r["notes"],
                           "account": r["account"], "account_id": r["account_id"],
                           "pick": "email",
                           "source": {"email_id": email_id, "subject": subject},
                           "suggestions": [{"category": category,
                                            "basis": "email"}],
                           "status": "pending", "status_text": ""})
        if stored:
            STATE["batch"]["cards"] += stored
            save_state(STATE)
    if stored:
        _log({"event": "finance_email_cards_saved",
              "args": {"email_id": email_id, "count": len(stored)},
              "outcome": "done", "result": subject[:100]})
    return (200 if stored else 400), {"stored": len(stored),
                                      "rejected": rejected}


def create_category(body):
    """POST /api/finance/create-category: create {name} in group {group}
    through the write helper — fresh budget download, duplicate and group
    checks against it (in write.mjs), sync. Runs in the request thread (the
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
        out = _run_write({"cmd": "update", "ops": [
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
    through the write helper (categories created in Actual's web UI reach
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
        _run_write({"cmd": "refresh"})
    except Exception as e:
        return 500, {"error": f"budget refresh failed — {type(e).__name__}: {e}"}
    finally:
        with LOCK:
            _HELPER_BUSY = False
    # marks the coming save as user-asked: it rebuilds the whole list instead
    # of topping up, and the page-open hold lets it through
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
    api-cache copy is current after any landed write (write.mjs downloads
    the budget into it before applying), so categorized there = the write
    landed (done), still open = it did not (back to pending). An unreadable
    copy leaves the card in_progress for the next boot. Also loads the ask
    store (no settle needed — the payload build prunes it)."""
    global STATE, ASKS
    STATE = load_state()
    if STATE is None:
        STATE = empty_state()
        with LOCK:
            save_state(STATE)
    ASKS = load_asks()
    if ASKS is None:
        ASKS = empty_asks()
        with LOCK:
            save_asks(ASKS)
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
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text())


def load_asks():
    """None when no ask file exists."""
    if not ASK_FILE.exists():
        return None
    return json.loads(ASK_FILE.read_text())


def _open_asks_summary():
    """Open, unexpired categorize asks for the page's finance area —
    recipient, created date, item count. A plain read: no pruning, no
    budget access."""
    with LOCK:
        return [{"ask_id": a["ask_id"], "to_addr": a["to_addr"],
                 "created_at": a["created_at"], "items": len(a["items"])}
                for a in ASKS["asks"].values()
                if a["state"] == "open" and not _ask_expired(a)]


def state():
    """The finance part of GET /api/state: the batch (pruned against the
    api-cache copy), the uncategorized count, the picker's category list, and
    the jobs' stamps. The sqlite and jobs-file reads run outside LOCK, so a
    poll never queues an apply behind their I/O. Only the page polls this, so
    the call itself is the page-open signal a scheduled save's hold honors."""
    global _PAGE_SEEN
    _PAGE_SEEN = time.time()
    with LOCK:
        pending = [c["transaction_id"] for c in STATE["batch"]["cards"]
                   if c["status"] == "pending"]
    states = _transaction_states(pending)
    with LOCK:
        if states:
            _prune(STATE, states)
        out = {"cards": [_card_view(c) for c in STATE["batch"]["cards"]],
               "error": STATE["batch"]["error"],
               "saved_at": STATE["batch"]["saved_at"]}
    out["uncategorized"] = _uncategorized_count()
    out["categories"] = _categories()
    out["open_asks"] = _open_asks_summary()
    out.update(_job_fields())
    return out


def _h_batch(body):
    with LOCK:
        return save_batch(STATE, body)


def _h_apply(body):
    with LOCK:
        return apply(STATE, body)


def _h_skip(body):
    with LOCK:
        return skip(STATE, body)


def _h_hide(body):
    with LOCK:
        return hide(STATE, body)


def _h_create_category(body):
    return create_category(body)   # takes LOCK internally around the flag


def _h_uncategorized(body):
    return uncategorized()


HANDLERS = {"/api/finance/batch": _h_batch, "/api/finance/apply": _h_apply,
            "/api/finance/skip": _h_skip, "/api/finance/hide": _h_hide,
            "/api/finance/ask": create_ask,
            "/api/finance/email-cards": create_email_cards,
            "/api/finance/create-category": _h_create_category,
            "/api/finance/uncategorized": _h_uncategorized}
