"""Emails area — inbox-derived action sets: state store and execution path.

Served by server.py (one process, one page); this module owns everything
email: the LLM cron job's scan/save surface, the intake surface the chat
agent's add_to_actions tool posts to, the set/row state, and the
executors that run approved rows for real — calendar create/update/delete
through the host app's macos_calendar server, reminder create through
the host app's reminders server, email archive through a long-lived
jmap_mail stdio child, mirror kick through the host,
transaction categorization through the finance area's write helper
(categorize_transaction rows, born from a helper's emailed answers to a
categorize ask — see finance.py's module docstring).

State lives in common.STATE_DIR (dir 0700, files 0600):

- state.json — seen ledger, sets with their action rows, denials, scan
  stamps, last_inbox_ids/last_iris_ids, and the intake intents (emails
  the user asked the chat agent to queue for action). Every write is a tmp
  file + fsync + os.replace + dir fsync, all under one global lock.
- the shared decisions log (common._log) gets one line per event:
  set_created (rationale, member ids, rows snapshot), set_superseded,
  set_reset, set_hidden, trim_skipped, row_resolved (set id,
  row id, kind + exact args,
  done/denied/run_failed/precheck_failed, result text capped at 500 chars,
  args blob capped at 1000), row_reconciled, email_intent_added,
  email_intent_dropped.

Execution semantics: rows store selectors, not ids. At
execute time the service re-lists the selector's day (calendar) or
re-searches the inbox (email). Listings come back as JSON (the tools'
format="json"); every parser fails closed — unparseable JSON, a count
mismatch, a warning, or a malformed entry raises ListingError and the row
fails. Every selector row carries args.snapshot (notes, location, end,
repeats — the event's exact fields), recorded by the save endpoint from
the live event, never authored by the scan. At execute time the snapshot
picks the target among same-title, same-time events and proves the event
still reads exactly as it did at save; zero matches, no unique target, or
any changed field means the row goes precheck_failed and never executes — a
selector that matches nothing is never read as "already done", because the
selector's day and time are derived from an email and can simply be wrong
(the boot reconciliation below is the only place zero matches prove a
delete landed). create_event and update_event verify by re-listing after
the write — create against the row's full snapshot (end, notes,
recurrence), update against the row's new values. PARTIAL: is
a failure marker, and delete/archive counts are verified against the
request. archive_email compares id + subject only and goes precheck_failed on any
mismatch, archiving nothing. open_email rows are display-only: the page's
open button is a plain Webmail link, approve (the done button) is a no-op
that marks the row handled. create_reminder writes one Apple Reminders
entry through the host app's reminders server: the tool's own SUCCESS line
is the proof (the id comes back from the EventKit save). One row executes at
a time area-wide:
resolve 409s an approve while another row is in_progress, and each approved
row runs in its own thread. mcp itself is imported lazily inside the
tool-call layer so the test suite can run under plain python3 with the
layer stubbed.

Boot reconciliation: a row left in_progress when the service
dies is settled against calendar/mailbox reality in a background thread at
boot (serving does not wait): delete — selector gone = success, present and
matching = back to pending, ambiguous/mismatch = unknown; create — exactly
one match = success, zero = pending, more = unknown; update — reads as the
new values = success, reads as the old snapshot = pending, else unknown;
archive — all members out of the inbox = success, all in = pending, mixed =
unknown; mirror_kick = pending (safe to re-run: the mirror prunes stale
copies and never duplicates); create_reminder — an open reminder of that
exact name on that list = success, none = pending. Unreachable servers leave the row unknown;
every outcome lands in the log as a row_reconciled line.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/emails/resolve  {set_id, row_id, decision: approve|deny, args_sha256};
                     one row executes at a time (409 while another runs)
- POST /api/emails/reset    {set_id} (any state — the curl repair path) or
                     {all: true}; 409 when a matched set has an in_progress
                     row; voids pending sets, wipes overlapping denials,
                     drops member ledger entries. {all: true} also deletes
                     every set record — a blank slate, with the decisions
                     log as the record
- POST /api/emails/hide     {set_id} or {all: true}: take a finished set's
                     card off the page before its 24 h window ends. A
                     resolved set qualifies; by set_id a stuck one does too
                     (still pending, no row waiting or running, at least
                     one row failed) — hiding it settles it as resolved,
                     accepting the failed rows as final (409 otherwise).
                     all: true takes resolved sets only. The ledger and the
                     log are untouched
- GET  /api/emails/body     ?email_id — that email's own text (header block +
                     body, get_email's rendering, uncut), read live for the
                     page's expanded row and for the agent's read_email tool.
                     The id must be one the service has seen: the scan listing
                     while the mail is in the inbox, or any set that holds it.
                     An id the webmail child no longer knows is re-registered
                     by one mailbox-wide search on the subject before the fetch
                     retries; a failed fetch is a 502 carrying the tool's first
                     line
- POST /api/emails/rescan   starts the email-scan cron job detached — the
                     agent run that scans and proposes; returns at once,
                     results arrive over the next few page polls
- POST /api/emails/inbox-scan  the cron agent's scan (single-flight, 409 while one
                     runs): provably-complete inbox listing, trim, every new
                     email with a ~400-character snippet (read_email widens
                     one), a per-uid invitation timeline over the batch,
                     pending-set summaries; failed body fetches land in
                     fetch_failed, never fail the scan. Injected mail and a
                     finance-review reply carry their ~4 KB body instead —
                     the agent must act on that content — and an invitation
                     also keeps its link list; ICS facts always ride outside
                     the cut. Mail from an
                     IGNORE_EMAIL_FROM sender, and mail addressed to an
                     IGNORE_EMAIL_TO recipient, is dropped and never handed
                     over — Iris's own address is both. A new email whose
                     sender is an open categorize ask's recipient is marked
                     finance_review (the ask id) and gets its whole thread
                     attached as thread — the agent's only job there is
                     pairing the answers to the ask's items. The scan also
                     lists the iris chat account's inbox through a second,
                     read-only webmail child (on when
                     JMAP_TOKEN_READONLY_IRIS is in ~/.hermes/.env;
                     a boot line says when it is not): only emails with a
                     recorded intake intent join the pipeline, under an
                     "iris:"-prefixed id and carrying the intent's category
                     as actions_category — chat mail never enters. Injected
                     emails skip the ignore lists, the finance-review
                     check, and thread checks. The response
                     also carries
                     pending_asks: open categorize asks not already
                     referenced by a pending set, with still-open flags and
                     the valid category list. New emails and
                     pending sets waiting on the user (a suggestion or
                     open_email row) carry thread_after: follow-up messages
                     in the same thread (list_thread), from_you flagged,
                     capped bodies — or thread_error when the check failed
- POST /api/emails/intake  {category, sender, subject, received?} — the
                     chat agent's add_to_actions path: resolves the
                     selector against the iris account's inbox (exactly one
                     match required: none is 404, several is 400 with the
                     candidates; category must be in ACTIONS_CATEGORIES)
                     and records an intent in state.json. The next scan
                     queues the email like any new mail; the intent is
                     consumed when the email enters a set and dropped when
                     it leaves the iris inbox or after INTENT_DAYS days
- POST /api/emails/sets     save a set (kind action|ignore): before the lock,
                     every update/delete selector is resolved against the
                     live calendar and the event's current fields are
                     recorded as args.snapshot (the optional args.expected
                     hint picks the target among lookalikes and must land
                     on exactly one event — otherwise 400, nothing saved);
                     every categorize_transaction row is validated against
                     its ask (ask_id required, the id one of the ask's items
                     and still uncategorized, the category a real one) and
                     its display facts are stamped as args.transaction from
                     the ask store, never authored by the scan;
                     then, under the global lock: emails is a list of id
                     strings, each resolved against the scan's listing cache
                     (state["listing"]) — subject, from and receivedAt are
                     stamped from that cache, never authored by the scan, an
                     unknown id is a 400; archive_email/open_email rows
                     likewise carry member ids only and get the same stamped
                     facts; whitelist/typed row validation (including
                     optional row-level display data: series, suggestion),
                     archive rows bound to member emails and refused on
                     iris-source members (injected mail stays as chat
                     history), intent consumption for iris-source members,
                     auto-supersede on shared members (ignore included),
                     deny guard on overlapping denials

Scan trim: only when every listing in play is provably complete (query
total == returned count, hi@'s non-empty — an empty iris inbox is the
norm and counts; otherwise nothing is dropped and a trim_skipped line
lands in the log). Iris-source ids ride through ledger, sets and trim
under their "iris:" prefix. All email-area state for an email
dies when it leaves its inbox: ledger entries, denials (except the ones
attached to pending sets), resolved/superseded set records whose members
are all gone. Pending sets and in_progress rows are never touched.
"""

import asyncio
import atexit
import concurrent.futures
import html
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import common
import finance
from common import _log, _now, _sha256

APP = pathlib.Path(__file__).resolve().parent
IRIS = APP.parent.parent
sys.path.append(str(IRIS / "services" / "mcp" / "common"))
import call_host_tool  # noqa: E402
import hermes_env  # noqa: E402

STATE_FILE = common.STATE_DIR / "state.json"

# server-side whitelist: every saved row is validated against these, and
# anything else is rejected before it can ever reach an executor
ROW_KINDS = {"create_event", "update_event", "delete_event", "archive_email",
             "mirror_kick", "open_email", "categorize_transaction",
             "create_reminder"}
CALENDARS = {"Personal", "Partner"}
# the Apple Reminders lists a row may write: the user's own two (Next for soon,
# Later for later) and the two per-person shared ones
REMINDER_LISTS = {"Next", "Later", "Alex", "Riley"}
REMINDER_NOTES_CAP = 200   # the reminders server's own cap on notes
SPANS = {"this", "future"}
REPEATS = {"daily", "weekly", "monthly", "yearly"}
EXPECTED_KEYS = {"location", "notes_contains", "end_local", "repeats_contains"}
SNAPSHOT_FIELDS = ("notes", "location", "end", "repeats")
UPDATE_FIELDS = {"new_title", "start", "end", "location", "notes"}

# row states that settle a set. Nothing further happens to a run_failed,
# precheck_failed or unknown row either, but the set stays pending so the
# page keeps showing it:
# a row that did not do what it said needs a human, and the page is the only
# place one is visible. the user's two exits there: retry (reset) or hide
# (accept the failed rows as final).
SETTLED = {"success", "denied"}

# ---- tool-call layer constants ----
CALENDAR_PORT = 8355   # host app's macos_calendar
REMINDERS_PORT = 4471   # host app's reminders
JMAP_PYTHON = "/Users/me/.venvs/jmap_tools/bin/python"
JMAP_SERVER = IRIS / "services/mcp/jmap_mail/jmap_mail.py"
JMAP_TOKEN_VARS = ("JMAP_TOKEN_READONLY", "JMAP_TOKEN_WRITE")
# the iris chat account is a separate Webmail account, read through its own
# child carrying only this read-only token — nothing ever writes there
IRIS_TOKEN_VAR = "JMAP_TOKEN_READONLY_IRIS"
# injected iris mail rides through ledger, sets and trim under this id prefix
IRIS_PREFIX = "iris:"
# the intake whitelist: add_to_actions files under one of these; the
# category is stored on the intent and shown to the scan agent as a hint —
# it changes no behavior (finance's special handling stays ask-driven)
ACTIONS_CATEGORIES = {"finance", "general"}
INTENT_DAYS = 14
TOOL_TIMEOUT = 45
FAILURE_MARKERS = ("FAILED:", "REJECTED:", "PARTIAL:")

STATUS_CAP = 300   # row status_text bound (the log bounds its result at 500)
RATIONALE_CAP = 300   # action-set rationale bound — the page shows it verbatim
SUGGESTION_CAP = 150   # row suggestion note bound — the page shows it verbatim

# the cron job whose agent run calls /api/emails/inbox-scan and posts the sets back
CRON_JOB = "actions-inbox-scan"

# /api/emails/inbox-scan: one scan at a time, newest 50 new emails max,
# snippets for ordinary mail and ~4 KB bodies for the two always-actionable
# classes
SCAN_LOCK = threading.Lock()
# ids the newest scan handed to the agent — display only, feeds the page's
# "3/12 decided" while a run is going; a restart just blanks the counter
SCAN_BATCH = []
NEW_CAP = 50
BODY_CAP = 4096
# Prose handed over per ordinary email. The whole scan must fit hermes' MCP
# result cap (50,000 characters), and past it the agent gets a 1,500-character
# preview plus a file path it has no tool to open — the run then loses every
# email it was handed. 50 emails of header + snippet is ~26,000 characters, so
# the payload cannot reach the cap; the agent widens what it needs with
# read_email.
SNIPPET_CAP = 400
THREAD_AFTER_CAP = 5   # newest follow-up messages handed over per thread
FINANCE_THREAD_CAP = 20   # full-chain bound for finance-review emails
# body-fetch budget per scan: the clock starts before the inbox listing,
# and the loop stops at BODY_BUDGET_S - 75 — one iteration can cost ~75 s
# (respawn wait 40 + fut timeout 35), so 405 + 75 = 480 < the proxy's
# 600 s read timeout, with room for the trim and the response
BODY_BUDGET_S = 480

# Addresses the scan drops, matched lowercase against the sender and the
# To: header. Iris must not propose actions on its own email: mail it sent
# (FROM) and the user's replies back to it (TO). Entries must be lowercase.
IGNORE_EMAIL_FROM = ["iris@example.org"]
IGNORE_EMAIL_TO = ["iris@example.org"]

# a body line worth keeping under the cap: dates, times, weekdays, month
# names, URLs
_INTERESTING_RE = re.compile(
    r"https?://\S+|\d{4}-\d{2}-\d{2}|\b\d{1,2}:\d{2}\b"
    r"|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b", re.I)

LOCK = threading.Lock()   # serializes every state mutation
STATE = {}


class CalendarError(RuntimeError):
    pass


class WebmailError(RuntimeError):
    pass


class ListingError(RuntimeError):
    """A listing did not parse completely — fail closed, never treat
    a partial listing as authoritative."""


def _first_line(text):
    """The tool result's first line (marker + summary), for status text."""
    text = (text or "").strip()
    return text.splitlines()[0] if text else ""


# ---------------------------------------------------------------- validation

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def _valid_date(v, field):
    """'YYYY-MM-DD' -> True (date-only); 'YYYY-MM-DD HH:MM' -> False; else reject."""
    if isinstance(v, str) and _DATE.match(v):
        datetime.strptime(v, "%Y-%m-%d")
        return True
    if isinstance(v, str) and _DATETIME.match(v):
        datetime.strptime(v, "%Y-%m-%d %H:%M")
        return False
    raise ValueError(f"{field} must be 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM', got {v!r}")


def _req_str(args, k):
    v = args.get(k)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{k} is required")
    return v


def _system_tz():
    """The Mac's IANA zone from the /etc/localtime symlink, '' when unreadable."""
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        return ""
    return link.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in link else ""


def _require_tz():
    """The system zone, always — a caller-supplied tz must not survive."""
    tz = _system_tz()
    if not tz:
        raise ValueError("cannot determine the system timezone")
    return tz


def _find_calendars(obj):
    """Every value of a "calendar" key anywhere in a row's args."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "calendar" and isinstance(v, str):
                found.append(v)
            else:
                found += _find_calendars(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _find_calendars(v)
    return found


# typed schemas: allowed arg keys per row kind
ARG_KEYS = {
    "create_event": {"calendar", "title", "start", "end", "location", "notes",
                     "repeat", "repeat_interval", "repeat_until", "tz", "all_day"},
    "update_event": {"calendar", "title", "start_local", "span", "expected",
                     "snapshot", "tz",
                     "new_title", "start", "end", "location", "notes"},
    "delete_event": {"calendar", "title", "start_local", "span", "expected",
                     "snapshot"},
    "archive_email": {"emails"},
    "mirror_kick": {"days"},
    "open_email": {"email"},
    "categorize_transaction": {"transaction_id", "category", "update_rule",
                               "transaction"},
    "create_reminder": {"name", "list", "due", "notes"},
}

_LINE_BREAK = re.compile(r"[\r\n]")


def _validate_row(r):
    """Typed arg schemas per kind; also normalizes (span/days
    defaults, tz overwrite, derived all_day). Raises ValueError on any
    problem; save_set also catches TypeError from int() coercions."""
    kind = r["kind"]
    if kind not in ROW_KINDS:
        raise ValueError(f"row kind {kind!r} not allowed")
    args = r.get("args")
    if not isinstance(args, dict):
        raise ValueError("row args must be an object")

    unknown = set(args) - ARG_KEYS[kind]
    if unknown:
        hint = ""
        if kind in ("update_event", "delete_event") and unknown & EXPECTED_KEYS:
            hint = " — hint fields belong in args.expected"
        raise ValueError(f"unknown arg keys for {kind}: {sorted(unknown)}{hint}")
    bad = [c for c in _find_calendars(args) if c not in CALENDARS]
    if bad:
        raise ValueError(f"calendar {bad[0]!r} not allowed")
    for k in ("title", "new_title", "location"):
        if k in args and isinstance(args[k], str) and _LINE_BREAK.search(args[k]):
            raise ValueError(f"{k} must not contain line breaks")
    # the calendar strips notes before storing them, so the row's own copy
    # must be stripped too — otherwise no verify or read-back can match
    if isinstance(args.get("notes"), str):
        args["notes"] = args["notes"].strip()

    if kind == "create_event":
        _req_str(args, "calendar")
        _req_str(args, "title")
        all_day = _valid_date(_req_str(args, "start"), "start")
        if "end" in args and _valid_date(args["end"], "end") != all_day:
            raise ValueError("start and end must both be dates (all-day) or both date-times")
        # all_day/tz stay in stored args as documented intent only — the
        # calendar tool derives all-day from date-only times and always uses
        # the Mac's local zone, so there is nothing to forward them to
        args["all_day"] = all_day
        if args.get("repeat"):
            if args["repeat"] not in REPEATS:
                raise ValueError("repeat must be daily, weekly, monthly, or yearly")
            # a real int only — the stored value is forwarded verbatim to
            # macos_calendar's repeat_interval: int, so anything else must
            # fail here, before approval, not at execute time
            interval = args.get("repeat_interval", 1)
            if isinstance(interval, bool) or not isinstance(interval, int) \
                    or interval < 1:
                raise ValueError("repeat_interval must be an integer, 1 or more")
            if args.get("repeat_until"):
                _valid_date(args["repeat_until"], "repeat_until")
        elif args.get("repeat_interval", 1) != 1 or args.get("repeat_until"):
            # same rule as macos_calendar.create_event, caught before approval
            raise ValueError("repeat_interval/repeat_until need repeat to be set")
        args["tz"] = _require_tz()

    elif kind in ("update_event", "delete_event"):
        _req_str(args, "calendar")
        _req_str(args, "title")
        _valid_date(_req_str(args, "start_local"), "start_local")
        args.setdefault("span", "this")
        if args["span"] not in SPANS:
            raise ValueError("span must be 'this' or 'future'")
        expected = args.get("expected")
        if expected is not None:
            if not isinstance(expected, dict) or not expected:
                raise ValueError("args.expected, when present, must be a non-empty "
                                 f"object, keys within {sorted(EXPECTED_KEYS)}")
            if set(expected) - EXPECTED_KEYS:
                raise ValueError(f"expected keys must be within {sorted(EXPECTED_KEYS)}")
            if not all(isinstance(v, str) and v for v in expected.values()):
                raise ValueError("expected values must be non-empty strings")
        snap = args.get("snapshot")
        if not (isinstance(snap, dict) and set(snap) == set(SNAPSHOT_FIELDS)
                and all(isinstance(snap[k], str)
                        for k in ("notes", "location", "repeats"))
                and (snap["end"] is None or isinstance(snap["end"], str))):
            raise ValueError("selector rows need args.snapshot — the save "
                             "endpoint records it from the live event")
        if kind == "update_event":
            if not (set(args) & UPDATE_FIELDS):
                raise ValueError("update_event needs at least one field to change: "
                                 "new_title, start, end, location, notes")
            for k in ("start", "end"):
                if k in args:
                    _valid_date(args[k], k)
            args["tz"] = _require_tz()

    elif kind == "archive_email":
        emails = args.get("emails")
        if not isinstance(emails, list) or not 1 <= len(emails) <= 50:
            raise ValueError("archive_email needs 1-50 emails")
        for m in emails:
            if not isinstance(m, dict) or not all(
                    isinstance(m.get(k), str) and m[k]
                    for k in ("id", "subject", "from", "receivedAt")):
                raise ValueError("each email needs id, subject, from, receivedAt")
        # the mailbox tool dedupes ids and reports the deduped count, which
        # would never match a row that carries one twice
        ids = [m["id"] for m in emails]
        if len(set(ids)) != len(ids):
            raise ValueError("archive_email carries the same email id twice")

    elif kind == "mirror_kick":
        args["days"] = int(args.get("days", 365))
        if args["days"] < 1:
            raise ValueError("days must be 1 or more")

    elif kind == "open_email":
        m = args.get("email")
        if not isinstance(m, dict) or not all(
                isinstance(m.get(k), str) and m[k]
                for k in ("id", "subject", "from", "receivedAt")):
            raise ValueError("open_email needs an email object with id, subject, "
                             "from, receivedAt")

    elif kind == "categorize_transaction":
        _req_str(args, "transaction_id")
        _req_str(args, "category")
        # the payee rule is never taught from a helper's wording by default;
        # the agent sets it true only when the answer plainly generalizes
        args["update_rule"] = bool(args.get("update_rule", False))
        tx = args.get("transaction")
        if not isinstance(tx, dict) or set(tx) != {"transaction_id", "date",
                "payee", "amount", "account", "notes"}:
            raise ValueError("categorize rows need args.transaction — the save "
                             "endpoint stamps it from the ask store")
        if tx["transaction_id"] != args["transaction_id"]:
            raise ValueError("args.transaction does not match transaction_id")

    elif kind == "create_reminder":
        args["name"] = _req_str(args, "name").strip()
        if _LINE_BREAK.search(args["name"]):
            raise ValueError("name must not contain line breaks")
        if _req_str(args, "list") not in REMINDER_LISTS:
            raise ValueError(f"list must be one of {sorted(REMINDER_LISTS)}")
        due = args.get("due")
        if due and not (isinstance(due, str) and _DATE.match(due)):
            raise ValueError("due must be a date, 'YYYY-MM-DD'")
        if due:
            datetime.strptime(due, "%Y-%m-%d")   # a real calendar day
        notes = args.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError("notes must be a string")
        if len(notes) > REMINDER_NOTES_CAP:
            raise ValueError(f"notes is {len(notes)} characters, the cap is "
                             f"{REMINDER_NOTES_CAP}")

    # optional row-level display data: the targeted series' repeat pattern,
    # end and occurrence count. Lives outside args, so it never enters
    # args_sha256 — it describes the target, it is not part of the action.
    series = r.get("series")
    if series is not None:
        if kind not in ("update_event", "delete_event"):
            raise ValueError("series is only allowed on update_event and "
                             "delete_event rows")
        if not isinstance(series, dict):
            raise ValueError("series must be an object")
        if set(series) - {"repeat", "repeat_until", "occurrences"}:
            raise ValueError("series keys must be within "
                             "['occurrences', 'repeat', 'repeat_until']")
        if series.get("repeat") not in REPEATS:
            raise ValueError("series.repeat must be daily, weekly, monthly, or yearly")
        if "repeat_until" in series:
            _valid_date(series["repeat_until"], "series.repeat_until")
        if "occurrences" in series and (
                not isinstance(series["occurrences"], int) or series["occurrences"] < 1):
            raise ValueError("series.occurrences must be 1 or more")

    # optional row-level display data: one short line saying why this time
    # was suggested, or the answer's own wording on a categorize row. Lives
    # outside args like series — it describes the proposal, not the action,
    # so it never enters args_sha256.
    suggestion = r.get("suggestion")
    if suggestion is not None:
        if kind not in ("create_event", "categorize_transaction"):
            raise ValueError("suggestion is only allowed on create_event and "
                             "categorize_transaction rows")
        if not isinstance(suggestion, str) or not suggestion.strip():
            raise ValueError("suggestion must be a non-empty string")
        if _LINE_BREAK.search(suggestion):
            raise ValueError("suggestion must not contain line breaks")
        if len(suggestion) > SUGGESTION_CAP:
            raise ValueError(f"suggestion is {len(suggestion)} characters, the "
                             f"cap is {SUGGESTION_CAP} — one short line")


def _validate_set(s):
    """Set-level rules: row ids unique; archive rows bound to the set's own
    member emails and kept off iris-source members (injected mail stays as
    chat history); open rows point at a member."""
    rows = s["rows"]
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate row id")
    for r in rows:
        if r["kind"] == "archive_email":
            if any(m["id"].startswith(IRIS_PREFIX)
                   for m in r["args"]["emails"]):
                raise ValueError("archive_email rows may not carry "
                                 "iris-injected emails")
            extra = {m["id"] for m in r["args"]["emails"]} - set(s["email_ids"])
            if extra:
                raise ValueError("archive_email rows may only carry the set's own "
                                 "member emails")
        if r["kind"] == "open_email":
            if r["args"]["email"]["id"] not in s["email_ids"]:
                raise ValueError("open_email rows may only point at the set's own "
                                 "member emails")


def finalize_set(s):
    """The save path every set goes through: validate the row whitelist and
    typed args, the set-level rules, then stamp args_sha256 (over
    the normalized args) and the initial status fields."""
    for r in s["rows"]:
        _validate_row(r)
        r["args_sha256"] = _sha256(r["args"])
        r.setdefault("status", "pending")
        r.setdefault("status_text", "")
    _validate_set(s)
    return s


# ---------------------------------------------------------------- state

def empty_state():
    return {"version": 1, "ledger": {}, "sets": {}, "denials": [],
            "last_scan_at": None, "last_scan_status": None,
            "last_inbox_ids": [], "last_iris_ids": None, "intents": {},
            "listing": {}}


def save_state(state):
    """Caller holds LOCK."""
    common.write_json(STATE_FILE, state)


def load_state():
    """None when no state file exists. Rows left in_progress stay so — the
    boot reconciliation thread settles them against reality."""
    if not STATE_FILE.exists():
        return None
    state = json.loads(STATE_FILE.read_text())
    state.setdefault("listing", {})
    return state


def _find_row(s, row_id):
    return next((r for r in s["rows"] if r["id"] == row_id), None)


def _has_in_progress(s):
    return any(r["status"] == "in_progress" for r in s["rows"])


def _stuck(s):
    """Pending with nothing left to decide or run. In practice that means a
    failed row (run_failed, precheck_failed, unknown) is holding it — an
    all-settled set resolves through _maybe_resolve_set before this is ever
    asked. the user's choice on a stuck set is retry (void and re-propose) or
    hide (accept the failed rows as final)."""
    return (s["state"] == "pending"
            and all(r["status"] not in ("pending", "in_progress")
                    for r in s["rows"]))


def _maybe_resolve_set(s):
    """A pending set resolves once every row succeeded or was denied. One that
    failed at run time, failed its pre-check or ended unknown keeps the set
    pending and on the page until the user retries it (reset) or hides it
    (accepting the failed rows as final)."""
    if s["state"] == "pending" and all(r["status"] in SETTLED for r in s["rows"]):
        s["state"] = "resolved"
        s["resolved_at"] = _now()


# ---------------------------------------------------------------- parsers
# All three tool surfaces are asked for format="json". Every parser fails closed:
# any violation raises ListingError, and a partial listing is never treated
# as authoritative.

def _unfenced(text):
    """The span between the ===== BEGIN/END ... ===== marker lines."""
    lines = (text or "").splitlines()
    begin = next((i for i, l in enumerate(lines) if l.startswith("===== BEGIN")), None)
    end = next((i for i, l in enumerate(lines) if l.startswith("===== END")), None)
    if begin is None or end is None or end <= begin:
        raise ListingError("listing is missing its BEGIN/END data fence")
    return "\n".join(lines[begin + 1:end])


def parse_event_listing(text):
    """[{title, start, end, all_day, calendar, location, repeats, notes, id}]
    out of a format="json" list_events result. Fails closed: the JSON must
    parse, carry no warning, and return as many events as it reports. Notes
    come flattened the way the text listing rendered them (newlines -> " / "),
    so snapshots compare the same everywhere."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ListingError("list_events result was not JSON") from None
    if not isinstance(data, dict):
        raise ListingError("list_events result was not a JSON object")
    if data.get("warning"):
        raise ListingError(f"list_events result carries a warning: {data['warning']}")
    events = data.get("events")
    if (type(data.get("total")) is not int or not isinstance(events, list)
            or data["total"] != len(events)):
        raise ListingError(
            f"list_events reported {data.get('total')!r}, returned "
            f"{len(events) if isinstance(events, list) else 'none'}")
    out = []
    for e in events:
        if (not isinstance(e, dict)
                or not all(isinstance(e.get(k), str) for k in
                           ("title", "start", "calendar", "location", "repeats",
                            "notes", "id"))
                or not (e.get("end") is None or isinstance(e["end"], str))
                or not isinstance(e.get("all_day"), bool)
                or not e["start"] or not e["id"]):
            raise ListingError(f"list_events entry did not parse: {e!r}")
        out.append({"title": e["title"], "start": e["start"], "end": e["end"],
                    "all_day": e["all_day"], "calendar": e["calendar"],
                    "location": e["location"], "repeats": e["repeats"],
                    "notes": e["notes"].replace("\n", " / "), "id": e["id"]})
    return out


def parse_reminder_listing(text):
    """[{name, list}] out of a format="json" search_reminders result.
    Fails closed: the JSON must parse and return as many reminders as it
    reports — a capped listing is never treated as complete."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ListingError("search_reminders result was not JSON") from None
    if not isinstance(data, dict):
        raise ListingError("search_reminders result was not a JSON object")
    reminders = data.get("reminders")
    if (type(data.get("total")) is not int or not isinstance(reminders, list)
            or data["total"] != len(reminders)):
        raise ListingError(
            f"search_reminders reported {data.get('total')!r}, returned "
            f"{len(reminders) if isinstance(reminders, list) else 'none'}")
    out = []
    for r in reminders:
        if (not isinstance(r, dict)
                or not all(isinstance(r.get(k), str) for k in ("name", "list"))):
            raise ListingError(f"search_reminders entry did not parse: {r!r}")
        out.append({"name": r["name"], "list": r["list"]})
    return out


def parse_inbox_listing(text):
    """(total, entries) out of a format="json" search_mail result; entries
    are [{receivedAt, from, subject, id}]. Fails closed: the JSON must parse,
    every entry must be complete, and a page may never hold more than the
    reported total."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ListingError("search_mail result was not JSON") from None
    if not isinstance(data, dict):
        raise ListingError("search_mail result was not a JSON object")
    total, emails = data.get("total"), data.get("emails")
    if type(total) is not int or not isinstance(emails, list) or len(emails) > total:
        raise ListingError("search_mail page parsed badly")
    entries = []
    for e in emails:
        if not isinstance(e, dict) or not all(
                isinstance(e.get(k), str) and e[k]
                for k in ("id", "from", "subject", "received_at")):
            raise ListingError(f"search_mail entry did not parse: {e!r}")
        entries.append({"receivedAt": e["received_at"], "from": e["from"],
                        "subject": e["subject"], "id": e["id"]})
    return total, entries


def parse_thread_listing(text):
    """[{id, from, subject, receivedAt, from_you}] out of a format="json"
    list_thread result, oldest first. Fails closed: the JSON must parse,
    every entry must be complete, and the count must match the reported
    total."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ListingError("list_thread result was not JSON") from None
    if not isinstance(data, dict):
        raise ListingError("list_thread result was not a JSON object")
    total, emails = data.get("total"), data.get("emails")
    if type(total) is not int or not isinstance(emails, list) or total != len(emails):
        raise ListingError(
            f"list_thread reported {total!r}, returned "
            f"{len(emails) if isinstance(emails, list) else 'none'}")
    out = []
    for e in emails:
        if (not isinstance(e, dict)
                or not all(isinstance(e.get(k), str) and e[k]
                           for k in ("id", "from", "subject", "received_at"))
                or not isinstance(e.get("from_you"), bool)):
            raise ListingError(f"list_thread entry did not parse: {e!r}")
        out.append({"id": e["id"], "from": e["from"], "subject": e["subject"],
                    "receivedAt": e["received_at"], "from_you": e["from_you"]})
    return out


# ---------------------------------------------------------------- tool layer
# Every actual MCP tool invocation lives behind these three functions
# (call_calendar, call_reminders, call_webmail), so tests stub exactly them.

def call_calendar(tool, args):
    """(ok, text) from one call to the host app's macos_calendar server."""
    return _call_host(CALENDAR_PORT, tool, args)


def call_reminders(tool, args):
    """(ok, text) from one call to the host app's reminders server."""
    return _call_host(REMINDERS_PORT, tool, args)


def _call_host(port, tool, args):
    """(ok, text) from one call to a server the host app runs — common's
    call_host_tool, any exception turned into a FAILED: text."""
    try:
        text, ok = asyncio.run(call_host_tool.call_host_tool(port, tool, args))
    except Exception as e:
        return False, f"FAILED: {type(e).__name__}: {e}"
    return ok, text


def _hermes_env():
    """KEY=VALUE pairs out of ~/.hermes/.env (the same semantics as
    health_check's _load_env, so a value that passes the health check works
    here too; the launchd environment carries neither). Missing file -> {}."""
    try:
        return hermes_env.read()
    except OSError:
        return {}


def _iris_token():
    """The iris account's read-only token, None when not configured."""
    return _hermes_env().get(IRIS_TOKEN_VAR) or None


def _jmap_env(token_var=None):
    """os.environ plus the webmail tokens out of ~/.hermes/.env. Default:
    the hi@ account's read-only + write pair. With token_var (the iris
    child): that token alone, passed as the child's read-only token — no
    write token, so the child can never write the iris mailbox."""
    env = dict(os.environ)
    values = _hermes_env()
    if token_var is None:
        for k in JMAP_TOKEN_VARS:
            if k in values:
                env[k] = values[k]
    elif token_var in values:
        env["JMAP_TOKEN_READONLY"] = values[token_var]
    return env


class WebmailChild:
    """One long-lived jmap_mail stdio child, owned by a dedicated
    asyncio loop in its own thread; sync callers hand requests over through
    a queue the single session task consumes.

    Email ids are valid only in the child session that search_mail issued
    them, so all JMAP traffic lives in this one process. A call gets ONE
    attempt: on timeout/EOF the session unwinds (killing the child) and
    respawns, and it is the caller's whole operation (search → resolve →
    write) that retries through the fresh session — the fresh search is
    what re-validates ids, a bare call retry would reuse dead ones.

    A child that never reaches a ready session (wrong interpreter path,
    missing token, import error) is respawned at most RESPAWN_CAP times before
    the thread exits, so a broken install cannot spawn processes forever; the
    next call starts a fresh thread.
    """

    _SHUTDOWN = (None, None, None, None)
    RESPAWN_CAP = 5
    RESPAWN_DELAY = 0.5

    def __init__(self, token_var=None):
        self._op_lock = threading.Lock()  # one JMAP operation at a time
        self._ready = threading.Event()
        self._loop = None
        self._req_q = None
        self._thread = None
        self._stopping = False
        self._started = False   # this attempt reached a ready session
        self._token_var = token_var   # alternate account token (iris child)

    # ---- sync side ----

    def call(self, tool, args, timeout=TOOL_TIMEOUT):
        with self._op_lock:
            try:
                self._ensure_running()
                return self._request(tool, args, timeout)
            except WebmailError:
                raise
            except Exception as e:
                raise WebmailError(f"{type(e).__name__}: {e}")

    def shutdown(self):
        self._stopping = True
        if self._loop is not None and self._thread is not None:
            self._loop.call_soon_threadsafe(self._req_q.put_nowait, self._SHUTDOWN)
            self._thread.join(timeout=10)

    def _ensure_running(self):
        if self._thread is None or not self._thread.is_alive():
            self._ready.clear()
            self._stopping = False
            self._thread = threading.Thread(target=self._thread_main, daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout=40):
            raise WebmailError("webmail child did not come up")

    def _request(self, tool, args, timeout):
        fut = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._req_q.put_nowait, (fut, tool, args, timeout))
        # the session task applies its own timeout and fails the future
        # first, so this only bounds a wedged loop
        return fut.result(timeout=timeout + 15)

    # ---- child side (the loop thread) ----

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        self._req_q = asyncio.Queue()
        self._loop.run_until_complete(self._run())
        self._loop.close()

    async def _run(self):
        # only attempts that never reached a ready session count against the
        # cap: a child that dies mid-call is respawned on demand, never in a loop
        never_ready = 0
        while not self._stopping and never_ready < self.RESPAWN_CAP:
            self._started = False
            try:
                await self._session_task()
                break  # clean shutdown
            except Exception as e:
                # lands in actions.error.log; the child's own stderr is
                # already inherited there — this covers parent-side failures
                # (bad interpreter path, handshake errors)
                print(f"webmail child session ended: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
            never_ready = 0 if self._started else never_ready + 1
            if not self._stopping:
                await asyncio.sleep(self.RESPAWN_DELAY)

    async def _session_task(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        try:
            params = StdioServerParameters(command=JMAP_PYTHON,
                                           args=[str(JMAP_SERVER)],
                                           env=_jmap_env(self._token_var))
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    self._started = True
                    self._ready.set()
                    while True:
                        fut, tool, args, timeout = await self._req_q.get()
                        if fut is None:  # shutdown sentinel
                            return
                        try:
                            res = await asyncio.wait_for(session.call_tool(tool, args), timeout)
                            text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
                            ok = not res.isError and not text.lstrip().startswith(FAILURE_MARKERS)
                            fut.set_result((ok, text))
                        except Exception as e:
                            if not fut.done():
                                fut.set_exception(e)
                            raise  # the session state is suspect — unwind, respawn
        finally:
            # before unwinding finishes: no caller may hand work to the dying
            # generation, and nothing queued may run in the next one
            self._ready.clear()
            while True:
                try:
                    fut = self._req_q.get_nowait()[0]
                except asyncio.QueueEmpty:
                    break
                if fut is not None and not fut.done():
                    fut.set_exception(WebmailError("webmail child restarted mid-call"))


webmail = WebmailChild()
# the iris chat account's own child — read-only token, intake and injected
# mail only; chat mail is never touched through it either
webmail_iris = WebmailChild(token_var=IRIS_TOKEN_VAR)

# set by boot() from ~/.hermes/.env; tests set it directly. When False the
# intake endpoint 503s and the scan touches nothing on the iris side
IRIS_ENABLED = False


def call_webmail(tool, args, timeout=TOOL_TIMEOUT):
    """(ok, text) through the long-lived webmail child."""
    return webmail.call(tool, args, timeout)


def call_webmail_iris(tool, args, timeout=TOOL_TIMEOUT):
    """(ok, text) through the iris account's child."""
    return webmail_iris.call(tool, args, timeout)


# ---------------------------------------------------------------- resolution

def _list_day(calendar, start_local):
    """Parsed list_events over the selector's day; matching happens here, not
    server-side — a title query would drop events whose real title carries
    HTML entities the stored copy has decoded. Raises CalendarError on a
    failed call."""
    day = start_local[:10]
    ok, text = call_calendar("list_events", {
        "start": day, "end": day, "calendars": [calendar], "format": "json"})
    if not ok:
        raise CalendarError(_first_line(text))
    return parse_event_listing(text)


def _resolve(calendar, title, start_local):
    """Entries matching title AND calendar AND start exactly. Titles compare
    after html.unescape, single pass, both sides: booking systems (the booking service)
    write titles with literal entities (&quot;) that the scan's copy carries
    decoded."""
    want = html.unescape(title)
    hits = [e for e in _list_day(calendar, start_local)
            if html.unescape(e["title"]) == want and e["calendar"] == calendar
            and e["start"] == start_local]
    for e in hits:
        if e["title"] != title:
            print(f"title matched after html.unescape: stored {title!r}, "
                  f"event {e['title']!r}", flush=True)
    return hits


def _check_expected(entry, expected):
    """None when the entry matches the containment claims, else a note naming
    the mismatch. Two users: the create verify (against _create_expected) and
    the save endpoint's args.expected hint check."""
    if not expected:
        return None
    if "notes_contains" in expected:
        # raw containment first, unescaped as the fallback: unescaping can
        # break a raw match whose needle straddles an entity, never make one
        needle, notes = expected["notes_contains"], entry["notes"]
        if (needle not in notes
                and html.unescape(needle) not in html.unescape(notes)):
            return "notes no longer contain the expected text"
    if "location" in expected and entry["location"] != expected["location"]:
        return "location changed"
    if "end_local" in expected and entry["end"] != expected["end_local"]:
        return "end changed"
    if "repeats_contains" in expected:
        if expected["repeats_contains"] not in entry["repeats"]:
            return "recurrence changed"
    return None


def _take_snapshot(entry):
    """The event's current fields, recorded at save time as args.snapshot."""
    return {k: entry[k] for k in SNAPSHOT_FIELDS}


def _check_snapshot(entry, snapshot):
    """None when every field still reads exactly as recorded at save, else a
    note naming the changed field and both values; any note ends the row as
    precheck_failed — never executed on an event that changed since save."""
    for k in SNAPSHOT_FIELDS:
        if entry[k] != snapshot[k]:
            name = "repeat" if k == "repeats" else k
            return f"{name} changed: was {snapshot[k]!r}, now {entry[k]!r}"
    return None


def _resolve_one(calendar, title, start_local, snapshot):
    """(outcome, entry, note); outcome ∈ one|gone|ambiguous|mismatch.

    The snapshot does double duty: it picks the target among same-title,
    same-time events (after creating the new coaching series, Aug 11 holds
    two identical-looking occurrences and only the notes tell them apart),
    and it proves the entry still reads exactly as it did at save."""
    matches = _resolve(calendar, title, start_local)
    if not matches:
        return "gone", None, ""
    if len(matches) > 1:
        fitting = [e for e in matches if _check_snapshot(e, snapshot) is None]
        if len(fitting) == 1:
            return "one", fitting[0], ""
        if len(fitting) > 1:
            return "ambiguous", None, \
                f"ambiguous: {len(fitting)} events match the selector and snapshot"
        return "mismatch", None, "no event matching the selector also matches the snapshot"
    note = _check_snapshot(matches[0], snapshot)
    if note is not None:
        return "mismatch", None, note
    return "one", matches[0], ""


def _search_inbox(needed_ids=None):
    """(entries, total) — inbox summaries, paged at 200. Stops only when the
    accumulated page reaches the reported total (total > 0), the needed ids
    are all seen, or the inbox is legitimately empty; a page that ends short
    of the total raises. entries == total (non-empty) means the listing is
    provably complete. Raises WebmailError on a failed call, ListingError
    on a broken one."""
    entries, offset, seen, total = [], 0, set(), 0
    while True:
        ok, text = call_webmail("search_mail",
                                 {"scope": "inbox", "limit": 200, "offset": offset,
                                  "format": "json"})
        if not ok:
            raise WebmailError(_first_line(text))
        total, page = parse_inbox_listing(text)
        entries += page
        seen.update(e["id"] for e in page)
        if total == 0 and not page:
            return entries, total  # empty inbox — not provably complete
        if not page:
            raise ListingError("inbox listing ended before its reported total")
        if offset + len(page) >= total and total > 0:
            return entries, total
        if needed_ids is not None and needed_ids <= seen:
            return entries, total
        offset += len(page)


def _search_iris_inbox():
    """(entries, total) — the iris account's inbox, same paging and
    fail-closed parsing as _search_inbox, one difference: an empty inbox is
    the norm there, so it counts as provably complete. Returning at all
    means complete. Raises WebmailError on a failed call, ListingError on
    a broken one."""
    entries, offset = [], 0
    while True:
        ok, text = call_webmail_iris("search_mail",
                                      {"scope": "inbox", "limit": 200,
                                       "offset": offset, "format": "json"})
        if not ok:
            raise WebmailError(_first_line(text))
        total, page = parse_inbox_listing(text)
        entries += page
        if not page:
            if total == 0:
                return entries, total
            raise ListingError("iris inbox listing ended before its reported total")
        if offset + len(page) >= total:
            return entries, total
        offset += len(page)


# ---------------------------------------------------------------- executor

def _count_verified(text, n):
    """The tool's first line says 'deleted X of Y' / 'archived X of Y' with
    both counts equal to what we requested."""
    m = re.search(r"(?:deleted|archived) (\d+) of (\d+)", _first_line(text), re.I)
    return bool(m) and int(m.group(1)) == n and int(m.group(2)) == n


def _repeat_text_expected(args):
    """The recurrence phrase a listing should carry for a created event —
    mirrors macos_calendar._repeat_text."""
    interval = int(args.get("repeat_interval", 1))
    if interval > 1:
        unit = {"daily": "day", "weekly": "week", "monthly": "month",
                "yearly": "year"}[args["repeat"]]
        text = f"every {interval} {unit}s"
    else:
        text = args["repeat"]
    if args.get("repeat_until"):
        text += f" until {args['repeat_until'][:10]}"
    return text


def _create_expected(args):
    """The snapshot a created event must read back as: the row's own end,
    notes and recurrence — the old-series lookalike (same title/start,
    other notes/recurrence) must not satisfy it. The notes needle is
    flattened the way listings render notes (newlines -> " / "). All-day
    rows carry no end_local: an all-day end never reads back as written
    (a one-day event shows no end, multi-day shows the last day inclusive),
    so title + start + notes/repeats carry the identity."""
    exp = {}
    if args.get("end") and not args.get("all_day"):
        exp["end_local"] = args["end"]
    if args.get("notes"):
        exp["notes_contains"] = args["notes"].replace("\n", " / ")
    if args.get("repeat"):
        exp["repeats_contains"] = _repeat_text_expected(args)
    return exp


def _exec_create(args):
    cargs = {"calendar": args["calendar"], "title": args["title"], "start": args["start"]}
    for k in ("end", "location", "notes", "repeat", "repeat_until"):
        if args.get(k):
            cargs[k] = args[k]
    if args.get("repeat"):
        cargs["repeat_interval"] = args.get("repeat_interval", 1)
    ok, text = call_calendar("create_event", cargs)
    if not ok:
        return "run_failed", _first_line(text)
    # verify the write against the full snapshot before any
    # delete depends on it. The old series can still sit at the same
    # title/start: its occurrences before the old-series delete's
    # start_local stay (delete forward only), and the user can approve the
    # create first. So the verify picks the snapshot-matching entry, not
    # the only entry.
    matches = _resolve(args["calendar"], args["title"], args["start"])
    fitting = [e for e in matches if _check_expected(e, _create_expected(args)) is None]
    if len(fitting) == 1:
        return "success", _first_line(text)
    if not fitting:
        return "run_failed", "create said SUCCESS but re-list found no match — " + _first_line(text)
    return "run_failed", "created but verify ambiguous"


def _exec_delete(args):
    outcome, e, note = _resolve_one(args["calendar"], args["title"],
                                    args["start_local"], args["snapshot"])
    if outcome == "gone":
        return "precheck_failed", "no event matches the selector — nothing deleted"
    if outcome != "one":
        return "precheck_failed", note
    # the safety title comes from the fresh listing, not the stored args:
    # the tool's check is byte-exact and the stored copy may differ by
    # entity encoding
    ok, text = call_calendar("delete_event", {
        "ids": [e["id"]], "event_titles": [e["title"]],
        "span": args.get("span", "this")})
    if not ok:
        return "run_failed", _first_line(text)
    if not _count_verified(text, 1):
        return "run_failed", "delete count not verified: " + _first_line(text)
    return "success", _first_line(text)


def _exec_update(args):
    outcome, e, note = _resolve_one(args["calendar"], args["title"],
                                    args["start_local"], args["snapshot"])
    if outcome == "gone":
        return "precheck_failed", "event is gone"
    if outcome != "one":
        return "precheck_failed", note
    fields = {("title" if k == "new_title" else k): args[k]
              for k in UPDATE_FIELDS if k in args}
    # safety title from the fresh listing — see _exec_delete
    ok, text = call_calendar("update_event", {
        "id": e["id"], "event_title": e["title"],
        "span": args.get("span", "this"), **fields})
    if not ok:
        return "run_failed", _first_line(text)
    # verify the write against a fresh listing — the same bar create meets
    outcome, e, reads = _find_updated(args)
    if outcome == "gone":
        return "run_failed", "update said SUCCESS but re-list found no event " \
            "under old or new title/start — " + _first_line(text)
    if outcome == "ambiguous":
        return "run_failed", "update said SUCCESS but re-list is ambiguous — " + _first_line(text)
    if all(reads.values()):
        return "success", _first_line(text)
    if any(reads.values()):
        return "run_failed", "update said SUCCESS but only some fields read back " \
            "as new — " + _first_line(text)
    return "run_failed", "update said SUCCESS but the event still reads as the " \
        "old values — " + _first_line(text)


def _archive_once(args):
    """One search → resolve → archive pass. The operation is the retry unit:
    after a child respawn ids only become valid again through a fresh
    search, so the whole sequence reruns, never the bare archive call."""
    members = args["emails"]
    entries, _ = _search_inbox({m["id"] for m in members})
    by_id = {e["id"]: e for e in entries}
    present = [m for m in members if m["id"] in by_id]
    # snapshot compare on id + subject only — from/receivedAt render through
    # different paths and are unstable (they stay in stored args for the page)
    mismatched = sum(1 for m in present if by_id[m["id"]]["subject"] != m["subject"])
    if mismatched:
        return "precheck_failed", f"{mismatched} member(s) no longer match their snapshot — nothing archived"
    absent = len(members) - len(present)
    if not present:
        return "success", f"already done — all {len(members)} emails already out of the inbox"
    note = f"; {absent} already out of the inbox" if absent else ""
    ok, text = call_webmail("archive_email", {
        "ids": [m["id"] for m in present],
        "subjects": [m["subject"] for m in present]})
    if not ok:
        return "run_failed", _first_line(text) + note
    if not _count_verified(text, len(present)):
        return "run_failed", "archive count not verified: " + _first_line(text) + note
    return "success", _first_line(text) + note


def _exec_archive(args):
    try:
        return _archive_once(args)
    except WebmailError:
        return _archive_once(args)  # second failure propagates as row run_failed


def _exec_mirror(args):
    ok, text = call_calendar("mirror_busy_events", {"days": args["days"]})
    return ("success" if ok else "run_failed"), _first_line(text)


def _exec_reminder(args):
    ok, text = call_reminders("create_reminder", {
        "name": args["name"], "list_name": args["list"],
        "notes": args.get("notes", ""), "due": args.get("due", "")})
    # the tool's SUCCESS line ends with the reminder id, an internal handle
    # the reminders server says never to show
    return ("success" if ok else "run_failed"), _first_line(text).rsplit(" — id:", 1)[0]


def _reminder_exists(args):
    """True when an open reminder of that exact name sits on that list (the
    list compared the way the reminders server matches it: ignoring case).
    A failed or unparseable search raises, so the caller marks the row
    unknown."""
    ok, text = call_reminders("search_reminders", {
        "query": args["name"], "list_name": args["list"], "limit": 50,
        "format": "json"})
    if not ok:
        raise RuntimeError(_first_line(text))
    return any(r["name"] == args["name"] and r["list"].lower() == args["list"].lower()
               for r in parse_reminder_listing(text))


# a busy finance write is waited out in the executor thread: one row runs at
# a time area-wide, so at most one thread ever waits on this
CATEGORIZE_BUSY_WAIT_S = 150
CATEGORIZE_BUSY_POLL_S = 5


def _exec_categorize(args):
    """One categorize write through the finance area's helper path — the
    same budget_helper.mjs op and single-flight as the finance page's apply."""
    deadline = time.monotonic() + CATEGORIZE_BUSY_WAIT_S
    while True:
        outcome, text = finance.categorize_one(
            args["transaction_id"], args["category"], args["update_rule"])
        if outcome != "busy":
            break
        if time.monotonic() >= deadline:
            return "run_failed", "finance write stayed busy — deny, or " \
                "leave it: the ask stays open and the next scan re-proposes"
        time.sleep(CATEGORIZE_BUSY_POLL_S)
    if outcome == "ok":
        return "success", text
    if outcome == "handled":
        return "success", "already handled — " + text
    return "run_failed", text


def execute_row(r):
    """(status, status_text) — the real executor; status
    success|run_failed|precheck_failed.
    Tool-transport problems raise (CalendarError/WebmailError); the worker
    turns them into run_failed."""
    kind, args = r["kind"], r["args"]
    if kind == "create_event":
        return _exec_create(args)
    if kind == "delete_event":
        return _exec_delete(args)
    if kind == "update_event":
        return _exec_update(args)
    if kind == "archive_email":
        return _exec_archive(args)
    if kind == "mirror_kick":
        return _exec_mirror(args)
    if kind == "categorize_transaction":
        return _exec_categorize(args)
    if kind == "create_reminder":
        return _exec_reminder(args)
    if kind == "open_email":
        # the open button is a plain link on the page; done (approve) only
        # marks the row handled — there is nothing to execute
        return "success", "marked done"
    raise ValueError(f"row kind {kind!r} not allowed")


# ---------------------------------------------------------------- boot reconciliation

def _field_reads(e, k, v):
    """True when the listing entry already reads as the update's new value."""
    if k == "new_title":
        return e["title"] == v
    if k == "start":
        return e["start"] == v
    if k == "end":
        if e["end"]:
            return e["end"] == v
        # a single-day all-day event lists its end as None: an all-day end
        # never reads back as written (the _create_expected rule), so a
        # date-only end counts as read — a timed event with no end does not
        return e["all_day"] and bool(_DATE.match(v))
    if k == "location":
        return e["location"] == v
    if k == "notes":
        return e["notes"] == v.replace("\n", " / ")
    return False


def _find_updated(args):
    """(outcome, entry, reads) — search the cartesian product of old/new
    title × old/new start: an update that moved the event is findable, and
    both old and new shape existing at once is itself inconclusive. reads
    maps each changed field to whether the single entry already reads as the
    new value."""
    titles = {args["title"], args.get("new_title", args["title"])}
    starts = {args["start_local"], args.get("start", args["start_local"])}
    found = []
    for t in titles:
        for st in starts:
            found += _resolve(args["calendar"], t, st)
    by_id = {e["id"]: e for e in found}
    if not by_id:
        return "gone", None, {}
    if len(by_id) > 1:
        return "ambiguous", None, {}
    e = next(iter(by_id.values()))
    return "one", e, {k: _field_reads(e, k, args[k]) for k in UPDATE_FIELDS if k in args}


def _reconcile_update(args):
    outcome, e, reads = _find_updated(args)
    if outcome == "gone":
        return "unknown", "event not found under old or new title/start"
    if outcome == "ambiguous":
        return "unknown", "ambiguous after restart"
    if all(reads.values()):
        return "success", "updated before the crash"
    if any(reads.values()):
        return "unknown", "partially updated before the crash"
    if _check_snapshot(e, args["snapshot"]) is None:
        return "pending", "update never landed — re-fire"
    return "unknown", "cannot tell whether the update landed"


def reconcile_row(r):
    """(status, text) for a row left in_progress by a crash, checked against
    calendar/mailbox reality (see the module docstring). Unreachable servers
    raise; the caller marks the row unknown."""
    args = r["args"]
    kind = r["kind"]
    if kind == "create_event":
        matches = _resolve(args["calendar"], args["title"], args["start"])
        if not matches:
            return "pending", "create never landed — re-fire"
        # same expected-filtering as the executor's verify: the old series
        # can coexist at the same title/start
        fitting = [e for e in matches
                   if _check_expected(e, _create_expected(args)) is None]
        if len(fitting) == 1:
            return "success", "created before the crash"
        if len(fitting) > 1:
            return "unknown", "ambiguous after restart"
        return "unknown", "re-list match does not look like the new event"
    if kind == "delete_event":
        outcome, e, note = _resolve_one(args["calendar"], args["title"],
                                        args["start_local"], args["snapshot"])
        if outcome == "gone":
            return "success", "deleted before the crash"
        if outcome == "one":
            return "pending", "delete never landed — re-fire"
        return "unknown", note
    if kind == "update_event":
        return _reconcile_update(args)
    if kind == "archive_email":
        entries, _ = _search_inbox({m["id"] for m in args["emails"]})
        inbox = {e["id"] for e in entries}
        present = sum(1 for m in args["emails"] if m["id"] in inbox)
        if not present:
            return "success", "archived before the crash"
        if present == len(args["emails"]):
            return "pending", "archive never landed — re-fire"
        return "unknown", "partially archived before the crash"
    if kind == "mirror_kick":
        return "pending", "safe to re-run — the mirror prunes, never duplicates"
    if kind == "categorize_transaction":
        states = finance._transaction_states([args["transaction_id"]])
        if states is None:
            return "unknown", "could not reconcile: budget copy unreadable"
        if states.get(args["transaction_id"]) == "handled":
            return "success", "categorized (settled at boot)"
        return "pending", "categorize never landed — re-fire"
    if kind == "create_reminder":
        if _reminder_exists(args):
            return "success", "added before the crash"
        return "pending", "reminder never landed — re-fire"
    if kind == "open_email":
        return "success", "marked done"  # a no-op has nothing left to land
    raise ValueError(f"row kind {kind!r} not allowed")


def reconcile_boot():
    """Settle rows left in_progress at the last shutdown, in the
    background so serving starts immediately. Every outcome is logged."""
    with LOCK:
        stuck = [(s["id"], dict(r)) for s in STATE["sets"].values()
                 for r in s["rows"] if r["status"] == "in_progress"]
    for set_id, r in stuck:
        try:
            status, text = reconcile_row(r)
        except Exception as e:
            status, text = "unknown", f"could not reconcile: {type(e).__name__}: {e}"
        with LOCK:
            s = STATE["sets"].get(set_id)
            rr = _find_row(s, r["id"]) if s else None
            if rr is None or rr["status"] != "in_progress":
                continue
            rr["status"] = status
            rr["status_text"] = text[:STATUS_CAP]
            _log({"event": "row_reconciled", "set_id": set_id, "row_id": r["id"],
                  "kind": r["kind"], "args": r["args"], "outcome": status,
                  "result": text})
            _maybe_resolve_set(s)
            save_state(STATE)


# ---------------------------------------------------------------- execution

def _spawn(set_id, row_id):
    """One approved row's execution thread. Module-level so the test suite
    can run executions synchronously."""
    threading.Thread(target=_work_item, args=(set_id, row_id), daemon=True).start()


def _work_item(set_id, row_id):
    """Execute one approved row in its own thread; resolve spawns it after
    persisting in_progress. No void path can touch the row meanwhile — reset
    and supersede both 409 on an in_progress row."""
    with LOCK:
        s = STATE["sets"].get(set_id)
        r = _find_row(s, row_id) if s else None
    if r is None:
        return
    try:
        status, text = execute_row(r)
    except Exception as e:
        status, text = "run_failed", f"{type(e).__name__}: {e}"
    outcome = "done" if status == "success" else status
    with LOCK:
        s = STATE["sets"].get(set_id)
        r = _find_row(s, row_id) if s else None
        if r is None:
            return
        r["status"] = status
        r["status_text"] = text[:STATUS_CAP]
        _log({"event": "row_resolved", "set_id": set_id, "row_id": row_id,
              "kind": r["kind"], "args": r["args"], "outcome": outcome,
              "result": text})
        _maybe_resolve_set(s)
        save_state(STATE)


# ---------------------------------------------------------------- API views

def _row_view(r):
    view = {"id": r["id"], "kind": r["kind"], "label": r["label"],
            "args": r["args"], "args_sha256": r["args_sha256"],
            "status": r["status"], "status_text": r["status_text"]}
    if r.get("series") is not None:
        view["series"] = r["series"]
    if r.get("suggestion") is not None:
        view["suggestion"] = r["suggestion"]
    return view


def _recently_resolved(s):
    """A resolved set stays on the page (collapsed) for 24 h. Sets resolved
    before the resolved_at stamp existed have none and never show."""
    if s["state"] != "resolved" or not s.get("resolved_at"):
        return False
    resolved = datetime.fromisoformat(s["resolved_at"])
    return (datetime.now(timezone.utc) - resolved).total_seconds() <= 24 * 3600


def _gone(email_id, inbox, iris):
    """A set member left its own inbox — per source, and only once that
    inbox's listing was provably complete (hi's empty list means it
    never was; the iris side uses None for that, its empty list is a real
    listing)."""
    if email_id.startswith(IRIS_PREFIX):
        return iris is not None and email_id not in iris
    return bool(inbox) and email_id not in inbox


def page_state(state):
    """GET /api/state: scan stamps plus the pending sets and the sets
    resolved in the last 24 h, minus the ones hidden by hand, page-ready.
    Caller holds LOCK; state() merges in the job fields and calendar colors
    after releasing it."""
    sets = []
    inbox = state["last_inbox_ids"]
    iris = state.get("last_iris_ids")
    for s in state["sets"].values():
        if s.get("hidden"):
            continue
        if s["state"] != "pending" and not _recently_resolved(s):
            continue
        emails = [{**e, "gone": _gone(e["id"], inbox, iris)}
                  for e in s["emails"]]
        sets.append({"id": s["id"], "title": s["title"], "rationale": s["rationale"],
                     "created_at": s["created_at"], "created_by": s["created_by"],
                     "state": s["state"], "resolved_at": s.get("resolved_at"),
                     "stuck": _stuck(s), "emails": emails,
                     "rows": [_row_view(r) for r in s["rows"]]})
    sets.sort(key=lambda s: s["created_at"])
    # 0 means "no provably-complete listing yet" (an empty inbox is never
    # stored either) — the page hides its count line then
    return {"inbox_count": len(inbox),
            "last_scan_at": state["last_scan_at"],
            "last_scan_status": state["last_scan_status"],
            "batch_total": len(SCAN_BATCH),
            "batch_decided": sum(1 for i in SCAN_BATCH
                                 if i in state["ledger"]), "sets": sets}


def resolve(state, body):
    """POST /api/emails/resolve. Returns (http status, payload). Caller holds LOCK."""
    s = state["sets"].get(body.get("set_id"))
    if s is None:
        return 404, {"error": "unknown set"}
    r = _find_row(s, body.get("row_id"))
    if r is None:
        return 404, {"error": "unknown row"}
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        return 400, {"error": "decision must be approve or deny"}
    view = lambda: _row_view(r)
    if s["state"] != "pending":
        return 409, {"error": f"set is {s['state']}", "row": view()}
    if body.get("args_sha256") != r["args_sha256"]:
        return 409, {"error": "args changed — re-render and retry", "row": view()}
    if decision == "approve" and r["status"] == "in_progress":
        return 200, {"ok": True, "row": view()}  # duplicate: never re-executes
    if r["status"] != "pending":
        return 409, {"error": f"row is {r['status']}", "row": view()}
    if decision == "deny":
        r["status"] = "denied"
        r["status_text"] = "denied by user"
        state["denials"].append({
            "email_ids": sorted(s["email_ids"]),
            "args_sha256": r["args_sha256"], "denied_at": _now()})
        _log({"event": "row_resolved", "set_id": s["id"], "row_id": r["id"],
              "kind": r["kind"], "args": r["args"], "outcome": "denied",
              "result": "denied by user"})
        _maybe_resolve_set(s)
        save_state(state)
        return 200, {"ok": True, "row": view()}
    # one row executes at a time service-wide — the page disables the other
    # approve keys while one works; this 409 is the real rule (second tab,
    # curl). The check-and-set is atomic: the caller holds LOCK.
    busy = any(rr["status"] == "in_progress"
               for ss in state["sets"].values() for rr in ss["rows"])
    if busy:
        return 409, {"error": "another row is executing", "row": view()}
    r["status"] = "in_progress"
    r["status_text"] = "working"
    save_state(state)
    _spawn(s["id"], r["id"])
    return 202, {"ok": True, "row": view()}


def reset(state, body):
    """POST /api/emails/reset: {set_id} (any state — the curl repair path for
    resolved sets) or {all: true}. 409 when a matched set has an in_progress
    row. {all: true} leaves the state file blank — every set record goes,
    not only the pending ones. Caller holds LOCK."""
    if body.get("all") is True:
        matched = list(state["sets"].values())
        scope_ids = None
    elif isinstance(body.get("set_id"), str):
        s = state["sets"].get(body["set_id"])
        if s is None:
            return 404, {"error": "unknown set"}
        matched = [s]
        scope_ids = set(s["email_ids"])
    else:
        return 400, {"error": "pass set_id or all: true"}
    for s in matched:
        if _has_in_progress(s):
            return 409, {"error": f"set {s['id']} has an in_progress row"}
    n = 0
    for s in matched:
        # every matched set is logged — also the resolved/superseded ones
        # (the curl repair path), with the state it was in
        _log({"event": "set_reset", "set_id": s["id"], "state": s["state"],
              "email_ids": s["email_ids"],
              "scope": "all" if scope_ids is None else "scoped"})
        if s["state"] == "pending":
            s["state"] = "superseded"
            s["superseded_by"] = None
            n += 1
        # every matched set, any state: no ledger entry may keep pointing
        # at it, and denials overlapping the scope die
        for i in s["email_ids"]:
            if state["ledger"].get(i, {}).get("set_id") == s["id"]:
                del state["ledger"][i]
    if scope_ids is None:
        # a full reset means a blank slate: superseded records would otherwise
        # sit here until their emails leave the inbox, piling up across runs.
        # The decisions log keeps what actually happened. The scan stamps and
        # the listing cache stay: the page reads a missing stamp as a stale
        # scan, and a run in flight still saves its sets against the listing.
        state["denials"] = []
        state["ledger"] = {}
        state["sets"] = {}
    else:
        state["denials"] = [d for d in state["denials"]
                            if not (set(d.get("email_ids", [])) & scope_ids)]
    save_state(state)
    return 200, {"reset": n}


def hide(state, body):
    """POST /api/emails/hide: {set_id} for one finished set, {all: true} for
    every resolved set on the page (the done section's clear key). The ledger
    entries and the decisions log stay as they are — the card only leaves the
    page before its 24 h window ends. A set with rows still to answer or a row
    running cannot be hidden; a stuck set (failed rows only left) can, by
    set_id only (the card's accept key) — hiding it settles it as resolved,
    accepting the failed rows as final, so the trim can clear the record like
    any resolved set. all: true never touches a stuck set: it still needs
    the user's choice. Caller holds LOCK."""
    if body.get("all") is True:
        matched = [s for s in state["sets"].values()
                   if _recently_resolved(s) and not s.get("hidden")]
    elif isinstance(body.get("set_id"), str):
        s = state["sets"].get(body["set_id"])
        if s is None:
            return 404, {"error": "unknown set"}
        if s["state"] != "resolved" and not _stuck(s):
            return 409, {"error": f"set is {s['state']}"}
        matched = [s] if not s.get("hidden") else []
    else:
        return 400, {"error": "pass set_id or all: true"}
    for s in matched:
        if s["state"] == "pending":  # stuck — settle it, failed rows stand
            s["state"] = "resolved"
            s["resolved_at"] = _now()
        s["hidden"] = True
        _log({"event": "set_hidden", "set_id": s["id"],
              "email_ids": s["email_ids"]})
    if matched:
        save_state(state)
    return 200, {"hidden": len(matched)}



def _register_id(email_id, subject, iris):
    """One search over the whole mailbox to put an id back in the child's
    session: get_email answers only for ids that session issued, and a member
    email that left the inbox (an archive row ran, or the user filed it) is in
    no inbox listing. True when the id came back."""
    call = call_webmail_iris if iris else call_webmail
    ok, text = call("search_mail", {"query": subject, "limit": 200,
                                    "format": "json"})
    if not ok:
        return False
    return any(e["id"] == email_id for e in parse_inbox_listing(text)[1])


def _known_subject(email_id):
    """The email's subject, or None when the service has never seen the id.
    Two places carry one: the scan listing while the mail is in the inbox, and
    any set that holds it — an archived member is in no listing, and the page
    still expands its row. The subject is what _register_id searches on."""
    known = STATE["listing"].get(email_id)
    if known:
        return known["subject"]
    for s in STATE["sets"].values():
        for e in s["emails"]:
            if e["id"] == email_id:
                return e["subject"]
    return None


def read_body(params):
    """GET /api/emails/body?email_id=…: one email's own text — the header block
    and the body, as get_email renders them, uncut. Two callers: the page's
    expanded email row, and the agent's read_email tool widening a scan
    snippet. Neither sets nor the scan store bodies, so the text is fetched
    live. A failed fetch is a 502 with the tool's first line; the page shows
    that in the row."""
    email_id = params.get("email_id") or ""
    with LOCK:
        subject = _known_subject(email_id)
    if subject is None:
        return 404, {"error": "unknown email"}
    iris = email_id.startswith(IRIS_PREFIX)
    raw = email_id[len(IRIS_PREFIX):] if iris else email_id
    try:
        ok, text = _fetch_body(raw, iris=iris)
        if not ok and _register_id(raw, subject, iris):
            ok, text = _fetch_body(raw, iris=iris)
        if not ok:
            return 502, {"error": _first_line(text)}
        return 200, {"text": _unfenced(text)}
    except (WebmailError, ListingError) as e:
        return 502, {"error": f"{type(e).__name__}: {e}"}


def rescan():
    """POST /api/emails/rescan: start `hermes cron run actions-inbox-scan` detached. That
    agent run is the whole loop — /api/emails/inbox-scan only hands emails to a caller, it
    proposes nothing. The run claims the job, so a second tap while one is
    going cannot fire it twice. Touches no state, so it takes no lock."""
    try:
        subprocess.Popen(["hermes", "cron", "run", CRON_JOB],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start hermes: {e}"}
    return 200, {"started": CRON_JOB}


def _intent_expired(intent):
    added = datetime.fromisoformat(
        intent["added_at"].replace("Z", "+00:00")).timestamp()
    return added < time.time() - INTENT_DAYS * 86400


def intake(state, body):
    """POST /api/emails/intake: the chat agent's add_to_actions path.
    Validates the category against ACTIONS_CATEGORIES, resolves the
    sender/subject/received selector against the iris account's inbox
    (exactly one match: none 404, several 400 with the candidates), and
    records an intent the next scan queues like any new mail. The iris
    read runs outside LOCK; the state write under it."""
    if not IRIS_ENABLED:
        return 503, {"error": "the iris inbox is not configured "
                              "(JMAP_TOKEN_READONLY_IRIS is not set)"}
    category = body.get("category")
    if category not in ACTIONS_CATEGORIES:
        return 400, {"error": "category must be one of: "
                              + ", ".join(sorted(ACTIONS_CATEGORIES))}
    sender, subject = body.get("sender"), body.get("subject")
    if not isinstance(sender, str) or not sender.strip():
        return 400, {"error": "sender is required"}
    if not isinstance(subject, str) or not subject.strip():
        return 400, {"error": "subject is required"}
    received = body.get("received", "")
    if not isinstance(received, str) or (received and not _DATE.match(received)):
        return 400, {"error": "received must be YYYY-MM-DD"}
    try:
        entries, _ = _search_iris_inbox()
    except (WebmailError, ListingError) as e:
        return 502, {"error": f"iris inbox unreadable: {e}"}
    s, q = sender.strip().lower(), subject.strip().lower()
    hits = [e for e in entries
            if s in e["from"].lower() and q in e["subject"].lower()
            and (not received or e["receivedAt"].startswith(received))]
    if not hits:
        return 404, {"error": f"no iris-inbox email matches sender {sender!r}, "
                              f"subject {subject!r}"}
    if len(hits) > 1:
        return 400, {"error": f"{len(hits)} emails match — pass received "
                              "(YYYY-MM-DD) or a longer subject",
                     "candidates": hits[:10]}
    e = hits[0]
    with LOCK:
        if IRIS_PREFIX + e["id"] in state["ledger"]:
            return 409, {"error": "that email is already processed (in a set "
                                  "or ignored) — reset it on the page first"}
        state["intents"][e["id"]] = {
            "snapshot": {"id": e["id"], "subject": e["subject"],
                         "from": e["from"], "receivedAt": e["receivedAt"]},
            "category": category, "added_at": _now()}
        save_state(state)
    _log({"event": "email_intent_added",
          "args": {"email_id": e["id"], "category": category},
          "outcome": "done", "result": f"{e['from']} — {e['subject']}"})
    return 200, {"ok": True, "id": e["id"], "category": category,
                 "email": dict(e)}


# ---------------------------------------------------------------- scan + save
# The cron agent's write path, reached through the MCP proxy — kind="ignore"
# included: it marks newsletters and the like processed with no set.

def _trim(state, inbox_ids):
    """All email-area state for an email dies when it leaves the
    inbox; never touches pending sets, in_progress rows, or the
    denials attached to pending sets' emails. Caller holds LOCK; the listing
    is provably complete."""
    inbox = set(inbox_ids)
    state["ledger"] = {i: v for i, v in state["ledger"].items() if i in inbox}
    state["listing"] = {i: v for i, v in state["listing"].items() if i in inbox}
    pending_members = {tuple(sorted(s["email_ids"])) for s in state["sets"].values()
                       if s["state"] == "pending"}
    # a denial lives while any of its set's emails is still in the inbox;
    # once the email is gone its denial dies
    state["denials"] = [d for d in state["denials"]
                        if tuple(sorted(d.get("email_ids", []))) in pending_members
                        or set(d.get("email_ids", [])) & inbox]
    for sid in [sid for sid, s in state["sets"].items()
                if s["state"] != "pending"
                and not _has_in_progress(s)
                and not (set(s["email_ids"]) & inbox)]:
        del state["sets"][sid]


def _email_parts(text):
    """(header block, body) out of a fenced get_email result. get_email is
    used for bodies only — receivedAt comes from the search_mail summary line
    (JMAP), never the message Date: header — and for the To: line, which the
    listing does not carry. A result with no blank line is all body."""
    head, _, body = _unfenced(text).partition("\n\n")
    return (head, body) if body else ("", head)


def _names_address(text, addresses):
    """True when a From value or a header line names one of the addresses."""
    lowered = (text or "").lower()
    return any(a in lowered for a in addresses)


def _to_ignored(header):
    """True when the get_email header block is addressed to an
    IGNORE_EMAIL_TO address. Costs the body fetch, because only get_email
    reports recipients."""
    return any(l.startswith("To:") and _names_address(l, IGNORE_EMAIL_TO)
               for l in header.splitlines())


def _cap_body(text, cap=BODY_CAP):
    """~4 KB of body text, favoring date/time/link lines: those first (in
    original order), then filled with the rest."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    interesting, plain = [], []
    for l in lines:
        (interesting if _INTERESTING_RE.search(l) else plain).append(l)
    out, used = [], 0
    for l in interesting + plain:
        if used + len(l) + 1 > cap:
            if out:
                continue  # a long line loses its place to shorter ones
            out.append(l[:cap])
            break
        out.append(l)
        used += len(l) + 1
        if used >= cap:
            break
    return "\n".join(out) + f"\n[body capped ~{cap // 1024} KB — date/time/link lines first]"


def _snippet(text, cap=SNIPPET_CAP):
    """The opening of the body, in reading order. _cap_body is the wrong cut
    here: it hoists date and link lines to the front, which reads well at 4 KB
    and not at all at 400 characters. The agent judges from this whether the
    mail is worth a read_email call, so the first sentences are what matter."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return (text[:cap].rstrip()
            + "\n[snippet — call read_email for this email's full text]")


# the section header get_email writes above its link list, fixed-format so the
# snippet cut lands on prose and never inside a URL
LINKS_HEADER = "Links in the HTML version (anchor text -> URL):"


def _split_links(body):
    """(main, links_section) — get_email appends the links after the body text
    and before the ICS section."""
    main, _, links = body.partition(LINKS_HEADER)
    return main.rstrip(), (LINKS_HEADER + links.rstrip()) if links else ""


# the section header and the "- METHOD uid=… sequence=… stamped=…" event line
# the webmail tool's _fmt_ics_event writes — fixed-format so the scan can
# split the section off the body and build the per-uid timeline
ICS_HEADER = "Calendar invitation data (parsed from the email's calendar attachment):"
_ICS_EVENT_RE = re.compile(r"- (\w+) uid=(\S+) sequence=(\S+) stamped=(.+)")


def _split_ics(body):
    """(main, ics_section) — the ICS facts ride outside the body cap the way
    get_email keeps links outside its own cap: a long invitation body must
    not push the authoritative event data over the edge."""
    main, _, ics = body.partition(ICS_HEADER)
    return main.rstrip(), (ICS_HEADER + ics.rstrip()) if ics else ""


def _invitation_timeline(emails):
    """One per-uid timeline over the scan batch, from the fixed-format event
    lines in each email's ICS section. A uid is one series; two same-title
    series in one batch is exactly where subject-based resolution cross-wired
    an occurrence cancel onto the wrong series."""
    by_uid = {}
    for e in emails:
        _, ics = _split_ics(e["body"])
        cur = None
        for line in ics.splitlines():
            m = _ICS_EVENT_RE.match(line)
            if m:
                cur = {"email_id": e["id"], "method": m[1], "uid": m[2],
                       "sequence": m[3], "stamped": m[4], "detail": ""}
                by_uid.setdefault(m[2], []).append(cur)
            elif cur is not None and line.startswith("  ") and not cur["detail"]:
                cur["detail"] = line.strip()
    if sum(len(v) for v in by_uid.values()) < 2:
        return ""
    lines = ["Invitation timeline (grouped by uid — one uid is one event/series, "
             "same titles notwithstanding; ordered by ICS stamp):"]
    for uid, evs in by_uid.items():
        lines.append(f"uid {uid}")
        for ev in sorted(evs, key=lambda x: x["stamped"]):
            lines.append(f"  {ev['stamped']} — {ev['method']} sequence={ev['sequence']}"
                         + (f" — {ev['detail']}" if ev["detail"] else "")
                         + f" (email id {ev['email_id']})")
    return "\n".join(lines)


def _fetch_body(email_id, iris=False):
    """(ok, text) from one get_email. On a transport failure the child has
    respawned and every id it issued is dead — re-run the inbox search to
    re-register ids against the new session (the operation-retry pattern
    _exec_archive uses), then retry this fetch once. iris=True goes through
    the iris account's child and re-lists its inbox instead."""
    call = call_webmail_iris if iris else call_webmail
    try:
        return call("get_email", {"email_id": email_id}, timeout=20)
    except WebmailError:
        (_search_iris_inbox if iris else _search_inbox)()
        return call("get_email", {"email_id": email_id}, timeout=20)


def _merge_newest(hello, iris):
    """Two newest-first lists merged into one; each side keeps its own order
    on ties, so an empty iris side is the identity."""
    out, i, j = [], 0, 0
    while i < len(hello) and j < len(iris):
        if iris[j]["receivedAt"] > hello[i]["receivedAt"]:
            out.append(iris[j])
            j += 1
        else:
            out.append(hello[i])
            i += 1
    return out + hello[i:] + iris[j:]


def scan(state):
    """POST /api/emails/inbox-scan. Single-flight; see the module docstring for the trim."""
    if not SCAN_LOCK.acquire(blocking=False):
        return 409, {"error": "a scan is already running"}
    try:
        return _scan(state)
    finally:
        SCAN_LOCK.release()


def _scan(state):
    started = time.monotonic()  # the budget covers the listing too

    def _failed(e):
        with LOCK:
            state["last_scan_at"] = _now()
            state["last_scan_status"] = "failed"
            save_state(state)
        return 500, {"error": f"{type(e).__name__}: {e}"}

    try:
        entries, total = _search_inbox()
    except Exception as e:
        return _failed(e)
    # the iris listing never fails the scan: on an error injected mail and
    # the trim simply skip this run
    iris_entries, iris_complete = [], False
    if IRIS_ENABLED:
        try:
            iris_entries, _ = _search_iris_inbox()
            iris_complete = True   # returning at all means provably complete
        except Exception as e:
            print(f"iris inbox listing failed: {type(e).__name__}: {e}",
                  flush=True)
    inbox_ids = [e["id"] for e in entries]
    complete = total == len(inbox_ids) and total > 0
    iris_tagged = [IRIS_PREFIX + e["id"] for e in iris_entries]
    with LOCK:
        # the listing cache save_set stamps member facts from: every id this
        # scan listed, facts as the mailbox reported them. Merged, never
        # replaced — an incomplete listing must not drop known ids; the trim
        # prunes ids once a listing is provably complete.
        for e in entries:
            state["listing"][e["id"]] = {"subject": e["subject"],
                                         "from": e["from"],
                                         "receivedAt": e["receivedAt"]}
        for e in iris_entries:
            state["listing"][IRIS_PREFIX + e["id"]] = {
                "subject": e["subject"], "from": e["from"],
                "receivedAt": e["receivedAt"]}
        if complete and (not IRIS_ENABLED or iris_complete):
            _trim(state, inbox_ids + iris_tagged)
            state["last_inbox_ids"] = inbox_ids
            if IRIS_ENABLED:
                state["last_iris_ids"] = iris_tagged
        else:
            _log({"event": "trim_skipped", "total": total,
                  "returned": len(inbox_ids),
                  "reason": "inbox listing not provably complete" if not complete
                            else "iris inbox listing failed"})
        # intent lifecycle: expired intents die on any scan; an intent whose
        # email left the iris inbox dies once that listing is provably
        # complete
        intents = state["intents"]
        iris_by_id = {e["id"]: e for e in iris_entries}
        dead = [i for i, it in intents.items()
                if _intent_expired(it)
                or (iris_complete and i not in iris_by_id)]
        for i in dead:
            it = intents.pop(i)
            _log({"event": "email_intent_dropped", "args": {"email_id": i},
                  "outcome": "done",
                  "result": "expired" if _intent_expired(it)
                            else "left the iris inbox"})
        # processed = assigned or ignored; merely scanned is not processed
        new = [e for e in entries if e["id"] not in state["ledger"]]
        # injected mail: intent-matched iris entries join under a tagged id,
        # carrying the intent's category as a hint; chat mail never does
        injected = []
        if iris_complete:
            for e in iris_entries:   # listing order, newest first
                it = intents.get(e["id"])
                if it is None or IRIS_PREFIX + e["id"] in state["ledger"]:
                    continue
                injected.append({"id": IRIS_PREFIX + e["id"], "from": e["from"],
                                 "subject": e["subject"],
                                 "receivedAt": e["receivedAt"],
                                 "actions_category": it["category"]})
        pending_sets = []
        for s in state["sets"].values():
            if s["state"] != "pending":
                continue
            p = {"id": s["id"], "title": s["title"], "created_at": s["created_at"],
                 "subjects": [m["subject"] for m in s["emails"]],
                 "rows": [{"label": r["label"], "status": r["status"]} for r in s["rows"]]}
            # sets waiting on the user (time suggestions, a reply row) get a
            # thread check below, anchored on their newest member email. A
            # categorize row's suggestion (the answer's wording) never
            # anchors: the reply already arrived, and any newer one lists as
            # a new email on its own.
            if any((r.get("suggestion") and r["kind"] != "categorize_transaction")
                   or r["kind"] == "open_email" for r in s["rows"]):
                a = max(s["emails"], key=lambda m: m["receivedAt"])
                # an iris-source anchor has no thread to check — the check
                # below runs on the hi@ child
                if not a["id"].startswith(IRIS_PREFIX):
                    p["_anchor"] = {"id": a["id"], "receivedAt": a["receivedAt"]}
            pending_sets.append(p)
        # asks a pending set already references stay out of pending_asks, so
        # a second set never double-proposes them
        referenced_asks = {s.get("ask_id") for s in state["sets"].values()
                           if s["state"] == "pending" and s.get("ask_id")}
    # newest first (the search orders by JMAP receivedAt, never the Date
    # header); injected iris entries merge into that order. Beyond the cap
    # the oldest stay new for the next scan
    new = _merge_newest(new, injected)
    capped = len(new) > NEW_CAP
    new = new[:NEW_CAP]
    out_emails, fetch_failed = [], []
    for e in new:
        # injected mail was vetted at intake: the ignore lists and the
        # finance-review check are hi-side rules and skip it
        iris = e["id"].startswith(IRIS_PREFIX)
        # an ignored sender, caught before the body fetch. Skipped emails keep
        # no ledger entry, so every scan drops them again — free here, one
        # get_email per scan for the To: case below.
        if not iris and _names_address(e["from"], IGNORE_EMAIL_FROM):
            continue
        # one iteration can cost ~75 s (respawn wait 40 + fut timeout 35);
        # 405 + 75 = 480 < the proxy's 600 s read timeout, and the listing
        # time is already inside the clock
        if time.monotonic() - started > BODY_BUDGET_S - 75:
            fetch_failed.append(e["id"])
            continue
        try:  # a failed fetch never fails the scan — it lands in fetch_failed
            ok, text = _fetch_body(
                e["id"][len(IRIS_PREFIX):] if iris else e["id"], iris=iris)
        except Exception:
            ok, text = False, ""
        if not ok:
            # failed ids are NOT returned: they keep no ledger entry, so
            # they come back next scan instead of being saved on subject
            # alone; fetch_failed still reports them
            fetch_failed.append(e["id"])
            continue
        header, raw = _email_parts(text)
        if not iris and _to_ignored(header):
            continue
        main, ics = _split_ics(raw)
        prose, links = _split_links(main)
        # a sender who is an open ask's recipient makes this a finance-review
        # email: the entry carries the ask id and the whole thread is attached
        # below, so the agent's only job is pairing the answers to the ask's
        # items
        ask_id = None if iris else finance.open_ask_for_sender(e["from"])
        # Two classes are read in full, because the agent is required to act on
        # their content and both are rare: injected mail, where the body IS
        # the user's instruction, and a finance-review reply, whose numbered
        # answers have to pair against the ask. An invitation keeps its link
        # list too — the meeting link is part of the event the agent proposes.
        # Everything else gets a snippet; read_email widens it on demand.
        if iris or ask_id:
            parts = [_cap_body(prose), links, ics]
        elif ics:
            parts = [_snippet(prose), links, ics]
        else:
            parts = [_snippet(prose)]
        entry = {"id": e["id"], "from": e["from"], "subject": e["subject"],
                 "receivedAt": e["receivedAt"],
                 "body": "\n\n".join(p for p in parts if p)}
        if iris:
            entry["actions_category"] = e["actions_category"]
        elif ask_id:
            entry["finance_review"] = ask_id
        out_emails.append(entry)
    # thread phase: has anyone followed up on these emails? Follow-up
    # messages (the user's replies included — they live in Sent, never the
    # inbox) feed the reply-row and settled-time rules. A failed check
    # attaches thread_error instead of thread_after, so absence of
    # thread_after with no error genuinely means "no follow-ups".
    thread_cache, body_cache = {}, {}

    def _out_of_time():
        return time.monotonic() - started > BODY_BUDGET_S - 75

    def _thread_after(email_id, received_at, cap=THREAD_AFTER_CAP, full=False):
        """(messages newer than received_at, error). Bodies come back as
        snippets — the agent reads a follow-up in full with read_email, and
        five 4 KB follow-ups per new email is how a scan reaches the MCP result
        cap. full=True is the finance-review chain, whose numbered questions
        and answers have to be paired verbatim."""
        if email_id not in thread_cache:
            if _out_of_time():
                return None, "not checked — scan ran out of time"
            try:
                ok, text = call_webmail("list_thread",
                                         {"email_id": email_id, "format": "json"})
            except Exception as e:
                return None, f"{type(e).__name__}: {e}"
            if not ok:
                return None, _first_line(text)
            try:
                thread = parse_thread_listing(text)
            except ListingError as e:
                return None, str(e)
            # every member maps to the whole thread, so a pending set and a
            # new email sharing one thread cost one list_thread call
            for t in thread:
                thread_cache[t["id"]] = thread
            thread_cache.setdefault(email_id, thread)
        newer = [t for t in thread_cache[email_id]
                 if t["receivedAt"] > received_at][-cap:]
        out = []
        for t in newer:
            if t["id"] not in body_cache:
                if _out_of_time():
                    return None, "not checked — scan ran out of time"
                try:
                    ok, text = _fetch_body(t["id"])
                except Exception:
                    ok = False
                # "" means the body fetch failed — the message exists, its
                # content is unknown
                body_cache[t["id"]] = _cap_body(_email_parts(text)[1]) if ok else ""
            body = body_cache[t["id"]]
            out.append({**t, "body": body if full else _snippet(body)})
        return out, None

    def _attach(target, email_id, received_at):
        after, err = _thread_after(email_id, received_at)
        if after:
            target["thread_after"] = after
        elif err:
            target["thread_error"] = err

    for p in pending_sets:
        a = p.pop("_anchor", None)
        if a:
            _attach(p, a["id"], a["receivedAt"])
    for e in out_emails:
        if e["id"].startswith(IRIS_PREFIX):
            continue   # injected mail: there is no thread to check
        if e.get("finance_review"):
            # the full chain, the ask mail's questions included — the cap
            # only bounds a runaway thread, it must not cut the bottom off
            after, err = _thread_after(e["id"], "", cap=FINANCE_THREAD_CAP,
                                       full=True)
            if after:
                e["thread"] = after
            elif err:
                e["thread_error"] = err
        else:
            _attach(e, e["id"], e["receivedAt"])

    # the categorize asks the agent can still answer-pair: open, unreferenced,
    # with still-open flags and the valid category list. Built outside LOCK —
    # it reads the budget copy and may prune the ask store
    pending_asks = finance.open_asks_payload(referenced_asks)
    global SCAN_BATCH
    with LOCK:
        SCAN_BATCH = [e["id"] for e in out_emails]
        state["last_scan_at"] = _now()
        state["last_scan_status"] = "ok"
        save_state(state)
    return 200, {"emails": out_emails,
                 "invitation_timeline": _invitation_timeline(out_emails),
                 "pending_sets": pending_sets, "capped_new": capped,
                 "fetch_failed": fetch_failed, "pending_asks": pending_asks}


def _void_set(state, old, by):
    """Supersede a pending set and drop its members' ledger entries in the
    same pass — no orphans pointing at a superseded set. Caller holds LOCK."""
    old["state"] = "superseded"
    old["superseded_by"] = by
    for i in old["email_ids"]:
        if state["ledger"].get(i, {}).get("set_id") == old["id"]:
            del state["ledger"][i]
    _log({"event": "set_superseded", "set_id": old["id"],
          "superseded_by": by, "email_ids": old["email_ids"]})


def _consume_intents(state, email_ids):
    """An iris-injected email entering a set (action or ignore) consumes its
    intake intent. Caller holds LOCK."""
    for i in email_ids:
        if i.startswith(IRIS_PREFIX):
            state["intents"].pop(i[len(IRIS_PREFIX):], None)


def save_set(state, body):
    """POST /api/emails/sets. Caller holds LOCK — everything here is state-only, so
    the whole save (validate, supersede, ledger, insert, log) is atomic."""
    kind = body.get("kind", "action")
    if kind not in ("action", "ignore"):
        return 400, {"error": "kind must be action or ignore"}
    email_ids = body.get("emails")
    if (not isinstance(email_ids, list) or not email_ids or not all(
            isinstance(i, str) and i for i in email_ids)):
        return 400, {"error": "emails must be a non-empty list of email id "
                              "strings — the service stamps subject, from and "
                              "receivedAt from its own inbox listing"}
    if len(set(email_ids)) != len(email_ids):
        return 400, {"error": "emails carry the same id twice"}
    # display facts come from the scan's listing cache, never from the
    # caller: a mistyped subject once broke an archive precheck for good
    # (same deal as args.transaction from the ask store)
    unknown = [i for i in email_ids if i not in state["listing"]]
    if unknown:
        return 400, {"error": f"unknown email id {unknown[0]!r} — not in the "
                              "scanned inbox listing; use ids exactly as scan "
                              "returned them"}
    emails = [{"id": i, **state["listing"][i]} for i in email_ids]
    now = _now()

    if kind == "ignore":
        targets = [o for o in state["sets"].values()
                   if o["state"] == "pending" and set(o["email_ids"]) & set(email_ids)]
        # same guard as the action path: a set with an approved-in-flight
        # row must not be voided — reject the whole save, nothing is stored
        for old in targets:
            if _has_in_progress(old):
                return 409, {"error": f"set {old['id']} has an in_progress row — nothing saved"}
        for old in targets:
            _void_set(state, old, None)
        for i in email_ids:
            state["ledger"][i] = {"state": "ignored", "first_seen": now}
        _consume_intents(state, email_ids)
        _log({"event": "set_created", "kind": "ignore", "email_ids": email_ids,
              "rationale": body.get("rationale", "")})
        save_state(state)
        return 200, {"ignored": len(email_ids)}

    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        return 400, {"error": "an action set needs at least one row"}
    rationale = body.get("rationale", "")
    if len(rationale) > RATIONALE_CAP:
        return 400, {"error": f"rationale is {len(rationale)} characters, the cap "
                              f"is {RATIONALE_CAP} — resend with 2-3 short sentences"}
    supersedes = body.get("supersedes") or []
    if not isinstance(supersedes, list) or not all(isinstance(x, str) for x in supersedes):
        return 400, {"error": "supersedes must be a list of set ids"}
    targets = []
    for sid in supersedes:
        old = state["sets"].get(sid)
        if old is None or old["state"] != "pending":
            return 400, {"error": f"supersedes: {sid!r} is not a pending set"}
        targets.append(old)
    created_by = body.get("created_by")
    if not isinstance(created_by, dict):
        created_by = {"job": "", "session": ""}

    set_id = "set_" + uuid.uuid4().hex[:12]
    built = []
    for i, br in enumerate(rows):
        if not isinstance(br, dict):
            return 400, {"error": "each row must be an object"}
        b = {"id": br.get("id") or f"r{i + 1}",
             "kind": br.get("kind"), "label": br.get("label", ""),
             "args": br.get("args")}
        if br.get("series") is not None:
            b["series"] = br["series"]
        if br.get("suggestion") is not None:
            b["suggestion"] = br["suggestion"]
        built.append(b)
    # email-referencing rows carry member ids only; the service stamps the
    # full display facts from the members resolved above
    by_id = {m["id"]: m for m in emails}
    for b in built:
        if b["kind"] == "archive_email" and isinstance(b.get("args"), dict):
            ids = b["args"].get("emails")
            if not (isinstance(ids, list)
                    and all(isinstance(i, str) and i for i in ids)):
                return 400, {"error": "archive_email args.emails must be a list "
                                      "of member email id strings — the service "
                                      "stamps the display facts"}
            outside = [i for i in ids if i not in by_id]
            if outside:
                return 400, {"error": f"archive_email covers {outside[0]!r}, "
                                      "not a member of this set"}
            b["args"] = dict(b["args"], emails=[dict(by_id[i]) for i in ids])
        elif b["kind"] == "open_email" and isinstance(b.get("args"), dict):
            i = b["args"].get("email")
            if not (isinstance(i, str) and i):
                return 400, {"error": "open_email args.email must be one member "
                                      "email id string — the service stamps the "
                                      "display facts"}
            if i not in by_id:
                return 400, {"error": f"open_email points at {i!r}, not a "
                                      "member of this set"}
            b["args"] = dict(b["args"], email=dict(by_id[i]))
    ask_id = body.get("ask_id")
    has_cat = any(b["kind"] == "categorize_transaction" for b in built)
    if has_cat and not (isinstance(ask_id, str) and ask_id):
        return 400, {"error": "categorize rows need ask_id"}
    if ask_id is not None and not has_cat:
        return 400, {"error": "ask_id needs categorize rows"}
    s = {"id": set_id,
         "title": body.get("title", ""), "rationale": rationale,
         "email_ids": email_ids, "emails": emails, "created_at": now,
         "created_by": created_by,
         "ask_id": ask_id if has_cat else None,
         "state": "pending", "superseded_by": None, "rows": built}
    try:
        finalize_set(s)
    except (TypeError, ValueError) as e:
        return 400, {"error": str(e)}

    # auto-supersede any pending set sharing a member
    targets += [old for old in state["sets"].values()
                if old["state"] == "pending" and set(old["email_ids"]) & set(email_ids)]
    targets = list({o["id"]: o for o in targets}.values())
    # a set with an approved-in-flight row must not be voided (same guard as
    # reset) — reject the whole save, nothing is stored
    for old in targets:
        if _has_in_progress(old):
            return 409, {"error": f"set {old['id']} has an in_progress row — nothing saved"}
    # deny guard: identical args overlapping a denied set's
    # members come back already denied, never silently dropped
    denied = {d["args_sha256"] for d in state["denials"]
              if set(d.get("email_ids", [])) & set(email_ids)}
    for r in s["rows"]:
        if r["args_sha256"] in denied:
            r["status"] = "denied"
            r["status_text"] = "denied before — reset to clear"

    for old in targets:
        _void_set(state, old, set_id)
    for i in email_ids:
        state["ledger"][i] = {"state": "in_set", "set_id": set_id, "first_seen": now}
    state["sets"][set_id] = s
    _consume_intents(state, email_ids)
    _maybe_resolve_set(s)  # a fully pre-denied set resolves at birth
    _log({"event": "set_created", "set_id": set_id, "rationale": s["rationale"],
          "email_ids": email_ids,
          "rows": [{"kind": r["kind"], "args": r["args"]} for r in s["rows"]]})
    save_state(state)
    return 200, {"set_id": set_id,
                 "rows": [{"id": r["id"], "status": r["status"]} for r in s["rows"]]}


# ---------------------------------------------------------------- calendar colors

# {calendar title: "#rrggbb"} from the host app's macos_calendar server, for
# the page's calendar dots: fetched at boot and retried until the fetch
# lands (launchd can start this service before the host app is up); empty
# until then, and the page shows no dots
_CALENDAR_COLORS = {}
COLORS_RETRY_S = 300


def _calendar_colors_once():
    """One fetch attempt. True once colors are served, False on any failure."""
    global _CALENDAR_COLORS
    ok, text = call_calendar("list_calendars", {"format": "json"})
    if not ok:
        print("calendar colors: fetch failed: "
              f"{text.splitlines()[0] if text else 'no output'}", flush=True)
        return False
    try:
        entries = json.loads(text)["calendars"]
        colors = {c["title"]: c["color"] for c in entries}
    except (ValueError, KeyError, TypeError):
        print("calendar colors: fetch failed: bad list_calendars json",
              flush=True)
        return False
    hexd = set("0123456789abcdef")
    _CALENDAR_COLORS = {
        t: c.lower() for t, c in colors.items()
        if isinstance(t, str) and isinstance(c, str)
        and len(c) == 7 and c.startswith("#")
        and set(c[1:].lower()) <= hexd}
    return True


def _calendar_colors_loop():
    while not _calendar_colors_once():
        time.sleep(COLORS_RETRY_S)


# ---------------------------------------------------------------- area interface

NAME = "emails"


def boot():
    """Load the state file and start the boot-reconciliation and
    calendar-colors threads. Called once by server.main before serving;
    server.main installs the SIGTERM handler that lets the atexit webmail
    shutdown run. Also enables the iris inbox when its read-only token is
    configured (a boot line says when it is not)."""
    global STATE, IRIS_ENABLED
    STATE = load_state()
    if STATE is None:
        STATE = empty_state()
        with LOCK:
            save_state(STATE)
    STATE.setdefault("intents", {})
    STATE.setdefault("last_iris_ids")
    IRIS_ENABLED = _iris_token() is not None
    if not IRIS_ENABLED:
        print("iris inbox: no JMAP_TOKEN_READONLY_IRIS in "
              "~/.hermes/.env — email intake disabled", flush=True)
    atexit.register(webmail.shutdown)
    atexit.register(webmail_iris.shutdown)
    threading.Thread(target=reconcile_boot, daemon=True).start()
    threading.Thread(target=_calendar_colors_loop, daemon=True).start()


def state():
    """The emails part of GET /api/state. The jobs-file and executions-db
    reads (disk + sqlite, up to 1 s busy wait) run after LOCK is released,
    so a poll never queues resolves or scan sections behind its I/O."""
    with LOCK:
        out = page_state(STATE)
    job = common.cron_job(CRON_JOB)
    out["job_last_run_at"] = job.get("last_run_at") if job else None
    out["job_next_run_at"] = job.get("next_run_at") if job else None
    out["job_running_since"] = common.job_running_since(job)
    if _CALENDAR_COLORS:
        out["calendar_colors"] = _CALENDAR_COLORS
    return out


def _h_resolve(body):
    with LOCK:
        return resolve(STATE, body)


def _h_reset(body):
    with LOCK:
        return reset(STATE, body)


def _h_hide(body):
    with LOCK:
        return hide(STATE, body)



def _ask_rows(body):
    """Stamp every categorize_transaction row's args.transaction from the
    finance area's ask store and validate the row against that ask: the id
    must be one of the ask's items and still uncategorized, the category a
    real one. The stamped display facts are read by this service, never
    authored by the scan (same deal as args.snapshot for calendar
    selectors). Returns (status, payload) on failure, None when every row
    checked out. Runs before LOCK so the budget reads never block page
    polls. Malformed rows are skipped here — save_set rejects them with the
    field-level error."""
    if body.get("kind", "action") == "ignore":
        return None
    rows = body.get("rows")
    if not isinstance(rows, list):
        return None
    cat_rows = [r for r in rows
                if isinstance(r, dict) and r.get("kind") == "categorize_transaction"]
    if not cat_rows:
        return None
    ask_id = body.get("ask_id")
    if not isinstance(ask_id, str) or not ask_id:
        return 400, {"error": "categorize rows need ask_id"}
    ask = finance.get_open_ask(ask_id)
    if ask is None:
        return 400, {"error": f"no open ask {ask_id!r} — unknown or expired"}
    items = {it["transaction_id"]: it for it in ask["items"]}
    states = finance._transaction_states(list(items))
    conn, seen = None, set()
    for r in cat_rows:
        args = r.get("args")
        if not isinstance(args, dict):
            continue
        tid = args.get("transaction_id")
        if tid not in items:
            return 400, {"error": f"transaction {tid!r} is not in ask "
                                  f"{ask_id} — fix or drop the row"}
        if tid in seen:
            return 400, {"error": f"transaction {tid!r} appears in two rows"}
        seen.add(tid)
        if states is not None and states.get(tid) == "handled":
            return 400, {"error": f"transaction {tid!r} is already handled — "
                                  "drop the row and resend"}
        cat = args.get("category")
        if isinstance(cat, str) and cat.strip():
            if conn is None:
                try:
                    conn = finance._db()
                except (RuntimeError, sqlite3.Error) as e:
                    return 500, {"error": f"category lookup failed: {e}"}
            try:
                finance._category_id(conn, cat)
            except ValueError as e:
                return 400, {"error": str(e)}
        args["transaction"] = {k: items[tid][k] for k in
                               ("transaction_id", "date", "payee", "amount",
                                "account", "notes")}
    return None


def _snapshot_rows(body):
    """Resolve every update/delete selector against the live calendar and
    record the event's current fields as args.snapshot — the facts the
    tap-time precheck compares against are read by this service, never
    authored by the scan. The optional args.expected hint (containment keys)
    picks the target when lookalikes exist and must land on exactly one
    event. Returns (status, payload) on failure, None when every selector
    resolved. Runs before LOCK so the calendar calls never block page polls.
    Rows whose shape is wrong are skipped here — save_set rejects them with
    the field-level error."""
    if body.get("kind", "action") == "ignore":
        return None
    rows = body.get("rows")
    if not isinstance(rows, list):
        return None
    for r in rows:
        if not isinstance(r, dict) \
                or r.get("kind") not in ("update_event", "delete_event"):
            continue
        args = r.get("args")
        if not isinstance(args, dict):
            continue
        sel = [args.get(k) for k in ("calendar", "title", "start_local")]
        if not all(isinstance(v, str) and v for v in sel):
            continue
        hint = args.get("expected")
        if hint is not None and not (
                isinstance(hint, dict) and hint and not set(hint) - EXPECTED_KEYS
                and all(isinstance(v, str) and v for v in hint.values())):
            continue
        label = f"{r['kind']} {sel[1]!r} {sel[2]}"
        try:
            matches = _resolve(*sel)
        except (CalendarError, ListingError) as exc:
            return 502, {"error": f"calendar read failed while recording the "
                                  f"snapshot for {label} — retry the save: {exc}"}
        if not matches:
            return 400, {"error": f"{label}: no event matches the selector — "
                                  "fix calendar/title/start_local or drop the row"}
        if len(matches) == 1:
            e = matches[0]
            note = _check_expected(e, hint)
            if note is not None:
                return 400, {"error": f"{label}: the event does not match "
                                      f"args.expected ({note}) — fix or drop "
                                      "the hint"}
        else:
            fitting = [e for e in matches if _check_expected(e, hint) is None]
            if len(fitting) != 1:
                return 400, {"error":
                             f"{label}: {len(matches)} events match the selector "
                             f"and args.expected picks {len(fitting)} of them — "
                             "supply a hint that matches exactly one"}
            e = fitting[0]
        args["snapshot"] = _take_snapshot(e)
    return None


def _h_sets(body):
    err = _snapshot_rows(body)
    if err is not None:
        return err
    err = _ask_rows(body)
    if err is not None:
        return err
    with LOCK:
        return save_set(STATE, body)


def _h_scan(body):
    return scan(STATE)  # takes LOCK in sections, never as a whole


def _h_intake(body):
    return intake(STATE, body)  # iris reads outside LOCK, the write under it


def _h_rescan(body):
    return rescan()


HANDLERS = {"/api/emails/resolve": _h_resolve, "/api/emails/reset": _h_reset,
            "/api/emails/hide": _h_hide,
            "/api/emails/sets": _h_sets, "/api/emails/inbox-scan": _h_scan,
            "/api/emails/rescan": _h_rescan, "/api/emails/intake": _h_intake}

GET_HANDLERS = {"/api/emails/body": read_body}
