"""Inbox area — inbox-derived action sets: state store and execution path.

Served by server.py (one process, one page); this module owns everything
email: the LLM cron job's scan/save surface, the set/row state, and the
executors that run approved rows for real — calendar create/update/delete
through the host app's macos_calendar server, reminder create through
the host app's reminders server, email archive through a long-lived
mail_tools stdio child, delivery add through Parcel's add-delivery web API (services/mcp/parcel/parcel_api.py,
the client the parcel MCP server shares; PARCEL_KEY from ~/.hermes/.env),
file copy through the files server (files_client.py, the host app's
files server): the mail child downloads an attachment or a linked
file into the hermes document cache (ASSISTANT_MAIL_DOCS), and copy copies it
from there into the row's folder.

State lives in the folder common.state_dir() names (dir 0700, files 0600):

- state.json — seen ledger, sets with their action rows, denials, scan
  stamps and last_inbox_ids. Every write is a tmp
  file + fsync + os.replace + dir fsync, all under one global lock.
- the shared decisions log (common._log) gets one line per event:
  set_created (rationale, member ids, rows snapshot), set_superseded,
  set_reset, set_hidden, trim_skipped, row_resolved (set id,
  row id, kind + exact args,
  done/denied/run_failed/precheck_failed, result text capped at 500 chars,
  args blob capped at 1000; a create_event row also carries its title, its
  calendar, the calendar the scan proposed and the member emails' subjects
  and senders — the rules job's calendar evidence, see rule_evidence),
  row_reconciled.

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
open button is a plain webmail link, approve (the done button) is a no-op
that marks the row handled. create_reminder writes one Apple Reminders
entry through the host app's reminders server: the tool's own SUCCESS line
is the proof (the id comes back from the EventKit save). add_delivery posts
one delivery to Parcel; the carrier name the scan wrote is resolved to
Parcel's carrier code at save time against parcel_api's carrier list, and
the API's own success flag is the proof. copy takes one file a member
email carries — an attachment by its exact name, or a document link from
the email's own link list (https only; a web page answer means a login wall
and fails the row) — and the files server's SUCCESS line is the proof; a
name already taken in the folder fails the row, nothing is overwritten. One row
executes at a time area-wide:
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
unknown; create_reminder — an open reminder of that
exact name on that list = success, none = pending; add_delivery — the
tracking number in Parcel's delivery list = success, absent = pending;
copy — absent = pending, on disk after the row's own free-name check
("saving") = success, on disk before it = run_failed (the name was taken).
Unreachable servers leave the row unknown;
every outcome lands in the log as a row_reconciled line.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/inbox/resolve  {set_id, row_id, decision: approve|deny, args_sha256,
                     calendar?, title?, start?}; one row executes at a time
                     (409 while another runs). calendar is the user's pick
                     from the page's dropdown, title their text from the row's
                     title field, start their pick from a suggested row's time
                     field, on an approved create_event row only: the
                     calendar checked against the live list of writable
                     calendars before the lock, the title held to the
                     scan's title checks, the start held to the row's form
                     (a date on an all-day row, a date and time otherwise);
                     all written into args after the hash check, the end
                     moved with the start so the length stays, the hash
                     stamped again, the scan's calendar and title kept as
                     the row's proposed_calendar and proposed_title
- POST /api/inbox/approve-set  {set_id, rows: [{row_id, args_sha256, calendar?, title?, start?}, ...]} —
                     the card's green check: approve the listed rows (the
                     set's waiting rows, in the set's order) and run them
                     one after another in one thread, each marked
                     in_progress only when its turn comes. A row that does
                     not succeed stops the run; the rows after it stay
                     pending with their own keys. Same 409s as resolve:
                     set not pending, a row not pending, a stale
                     args_sha256, another row executing; calendar, title
                     and start the same as resolve's
- POST /api/inbox/reset    {set_id} (any state — the curl repair path) or
                     {all: true}; 409 when a matched set has an in_progress
                     row; voids pending sets, wipes overlapping denials,
                     drops member ledger entries. {all: true} also deletes
                     every set record — a blank slate, with the decisions
                     log as the record
- POST /api/inbox/hide     {set_id} or {all: true}: take a finished set's
                     card off the page before its 24 h window ends. A
                     resolved set qualifies; by set_id a stuck one does too
                     (still pending, no row waiting or running, at least
                     one row failed) — hiding it settles it as resolved,
                     accepting the failed rows as final (409 otherwise).
                     all: true takes resolved sets only. The ledger and the
                     log are untouched
- GET  /api/inbox/body     ?email_id — that email's own text (header block +
                     body, get_email's rendering, uncut), read live for the
                     page's expanded row and for the agent's read_email tool.
                     The id must be one the service has seen: the scan listing
                     while the mail is in the inbox, or any set that holds it.
                     An id the mail child no longer knows is re-registered
                     by one mailbox-wide search on its sender and received day
                     before the fetch
                     retries; a failed fetch is a 502 carrying the tool's first
                     line
- POST /api/inbox/rescan   starts the email-scan cron job detached — the
                     agent run that scans and proposes; returns at once,
                     results arrive over the next few page polls
- POST /api/inbox/inbox-scan  the cron agent's scan (single-flight, 409 while one
                     runs): provably-complete inbox listing, trim, every new
                     email with a ~400-character snippet (read_email widens
                     one), a per-uid invitation timeline over the batch,
                     pending-set summaries, and the writable calendars
                     with what each is for (a failed calendar list is a
                     scan warning and an empty list); failed body fetches
                     land in fetch_failed, never fail the scan. An
                     invitation also keeps its link list; ICS facts always
                     ride outside the cut. Mail from an
                     IGNORE_EMAIL_FROM sender, and mail addressed to an
                     IGNORE_EMAIL_TO recipient (both lists in inbox.env),
                     is dropped and never handed over. An email carrying files gets a files list
                     (its Attachments line, and each link whose text is a
                     document's file name), and the scan then hands over
                     the filing folders too (files_client.filing_folders:
                     every folder under Papers and SharedPapers outside
                     the ignore folder and hidden folders). New emails and
                     pending sets waiting on the user (a suggestion or
                     open_email row) carry thread_after: follow-up messages
                     in the same thread (list_thread), from_you flagged,
                     capped bodies — or thread_error when the check failed
- POST /api/inbox/sets     save a set (kind action|ignore): before the lock,
                     every calendar a row names is checked against the live
                     list of writable calendars (editable, title unique —
                     400 otherwise, 502 when the list cannot be read), and
                     every update/delete selector is resolved against the
                     live calendar and the event's current fields are
                     recorded as args.snapshot (the optional args.expected
                     hint picks the target among lookalikes and must land
                     on exactly one event — otherwise 400, nothing saved);
                     then, under the global lock: emails is a list of id
                     strings, each resolved against the scan's listing cache
                     (state["listing"]) — subject, from and receivedAt are
                     stamped from that cache, never authored by the scan, an
                     unknown id is a 400; archive_email/open_email rows
                     likewise carry member ids only and get the same stamped
                     facts; whitelist/typed row validation (including
                     optional row-level display data: series, suggestion —
                     one suggestion row per set at most),
                     archive rows bound to member emails, at least one
                     calendar row (create, update
                     or delete) or add_delivery row per action
                     set — archive and open_email rows only ride along with
                     one; copy is an anchor too: its email is a member id
                     stamped like open_email's, its folder must be one of
                     the filing folders, and before the lock the member
                     email is read live to prove it carries the named
                     attachment or link; one set per tracking number (a number already in
                     a set that is not superseded is rejected — the email is
                     ignored instead), auto-supersede on shared members (ignore included),
                     deny guard on overlapping denials

Scan trim: only when the inbox listing is provably complete (query
total == returned count, non-empty; otherwise nothing is dropped and a
trim_skipped line lands in the log). All email-area state for an email
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
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import common
from common import _log, _now, _sha256
from files_client import (FILENAME_CAP, call_files, file_warning,
                          file_ext, filing_folders, stored_name)

APP = pathlib.Path(__file__).resolve().parent
PROJECT = APP.parent.parent
sys.path.append(str(PROJECT / "services" / "mcp" / "common"))
sys.path.append(str(PROJECT / "services" / "mcp" / "parcel"))
import call_host_tool  # noqa: E402
import hermes_env  # noqa: E402
import parcel_api  # noqa: E402

def state_file():
    return common.state_dir() / "state.json"


# server-side whitelist: every saved row is validated against these, and
# anything else is rejected before it can ever reach an executor
ROW_KINDS = {"create_event", "update_event", "delete_event", "archive_email",
             "open_email", "create_reminder", "add_delivery", "copy"}
# an action set exists for a calendar change, a shipment to track or a file
# to save; every other row rides along
CALENDAR_KINDS = {"create_event", "update_event", "delete_event"}
ANCHOR_KINDS = CALENDAR_KINDS | {"add_delivery", "copy"}
# the Apple Reminders lists a row may write: the user's own two (Next for soon,
# Alex for later) and one shared list per person.
# create_reminder is wired end to end (validation, executor, reconcile, page)
# but intentionally unused for now: the save tool's description and the
# actions-inbox skill do not name it, so the scan never proposes one.
REMINDER_LISTS = {"Next", "Alex", "Sam"}
REMINDER_NOTES_CAP = 200   # the reminders server's own cap on notes
SPANS = {"this", "future"}
REPEATS = {"daily", "weekly", "monthly", "yearly"}
EXPECTED_KEYS = {"location", "notes_contains", "end_local", "repeats_contains"}
SNAPSHOT_FIELDS = ("notes", "location", "end", "repeats")
UPDATE_FIELDS = {"new_title", "start", "end", "location", "notes"}
DOWNLOAD_TIMEOUT = 120
# a files-list line from an email: its Attachments header line, and each
# link whose text is a document's file name, as get_email's link list
# writes it
DOC_LINK_RE = re.compile(
    r"^- .+\.(?:pdf|docx?|xlsx?|csv|txt|jpe?g|png|heic) -> https://\S+$", re.I)
# download_attachment's and download_link's answers name the cached copy on this line
SAVED_RE = re.compile(r"^Saved (?:attachment|linked file) \(\d+ KB\) to: (.+)$", re.M)
# the mail tools' refusal of an id their session never issued
UNKNOWN_ID = "the server restarted since this id was issued"

# row states that settle a set. Nothing further happens to a run_failed,
# precheck_failed or unknown row either, but the set stays pending so the
# page keeps showing it:
# a row that did not do what it said needs a human, and the page is the only
# place one is visible. The user's two exits there: retry (reset) or hide
# (accept the failed rows as final).
SETTLED = {"success", "denied"}

# ---- tool-call layer constants ----
CALENDAR_PORT = 8351   # host app's macos_calendar
REMINDERS_PORT = 8352   # host app's reminders
MAIL_PYTHON = "/Users/demo/.venvs/mail_tools/bin/python"
MAIL_SERVER = PROJECT / "services/mcp/mail_tools/mail_tools.py"
MAIL_TOKEN_VARS = ("MAIL_READ_TOKEN", "MAIL_WRITE_TOKEN")
TOOL_TIMEOUT = 45
FAILURE_MARKERS = ("FAILED:", "REJECTED:", "PARTIAL:")

STATUS_CAP = 300   # row status_text bound (the log bounds its result at 500)
RATIONALE_CAP = 300   # action-set rationale bound — the page shows it verbatim
SUGGESTION_CAP = 150   # row suggestion note bound — the page shows it verbatim

# the cron job whose agent run calls /api/inbox/inbox-scan and posts the sets back
CRON_JOB = "actions-inbox-scan"

# /api/inbox/inbox-scan: one scan at a time, newest 50 new emails max
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
# body-fetch budget per scan: the clock starts before the inbox listing,
# and fetching ends by BODY_BUDGET_S. No mail call starts unless its
# worst case still ends by then: the respawn wait (40 s) plus the call's
# future timeout — 35 s for a get_email, 60 s for a call on the default
# timeout (search_mail, list_thread). 480 < the proxy's 600 s read
# timeout, with room for the folder walk, the calendar list, the trim and
# the response
BODY_BUDGET_S = 480
GET_WORST_S = 75
CALL_WORST_S = 100

# The addresses the scan drops live in inbox.env next to this file:
# IGNORE_EMAIL_FROM (senders) and IGNORE_EMAIL_TO (recipients), comma-
# separated, read on every scan and matched lowercase as "contains" against
# the From value and the To: header.
ENV_FILE = str(APP / "inbox.env")


def _ignore_lists():
    """(senders, recipients): the lowercase address lists out of inbox.env."""
    values = hermes_env.read(ENV_FILE)
    def split(key):
        return [a.strip().lower() for a in values.get(key, "").split(",")
                if a.strip()]
    return split("IGNORE_EMAIL_FROM"), split("IGNORE_EMAIL_TO")

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


class MailError(RuntimeError):
    pass


class MailTimeout(MailError):
    """A call got no answer in its time limit: the child is restarted, but a
    repeat would only wait the same way again."""


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
    "open_email": {"email"},
    "create_reminder": {"name", "list", "due", "notes"},
    "add_delivery": {"tracking_number", "carrier", "carrier_code", "description"},
    "copy": {"email", "attachment", "link", "folder", "filename"},
}

_LINE_BREAK = re.compile(r"[\r\n]")


def _validate_row(r):
    """Typed arg schemas per kind; also normalizes (span default, tz
    overwrite, derived all_day, carrier code, file name). Raises ValueError
    on any problem; save_set also catches TypeError."""
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

    elif kind == "open_email":
        m = args.get("email")
        if not isinstance(m, dict) or not all(
                isinstance(m.get(k), str) and m[k]
                for k in ("id", "subject", "from", "receivedAt")):
            raise ValueError("open_email needs an email object with id, subject, "
                             "from, receivedAt")

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

    elif kind == "add_delivery":
        for k in ("tracking_number", "carrier", "description"):
            args[k] = _req_str(args, k).strip()
            if _LINE_BREAK.search(args[k]):
                raise ValueError(f"{k} must not contain line breaks")
        if len(args["description"]) > parcel_api.DESCRIPTION_CAP:
            raise ValueError(f"description is {len(args['description'])} characters, "
                             f"the cap is {parcel_api.DESCRIPTION_CAP} — shop and item, "
                             "like 'Shop Desk Lamp'")
        # the scan names the carrier as the email does; the code is Parcel's
        # own, stamped here (an incoming carrier_code is overwritten)
        args["carrier_code"], args["carrier"] = parcel_api.carrier(args["carrier"])

    elif kind == "copy":
        m = args.get("email")
        if not isinstance(m, dict) or not all(
                isinstance(m.get(k), str) and m[k]
                for k in ("id", "subject", "from", "receivedAt")):
            raise ValueError("copy needs an email object with id, subject, "
                             "from, receivedAt")
        sources = [k for k in ("attachment", "link") if k in args]
        if len(sources) != 1:
            raise ValueError("copy takes exactly one of attachment or link")
        src = args[sources[0]] = _req_str(args, sources[0]).strip()
        if _LINE_BREAK.search(src):
            raise ValueError(f"{sources[0]} must not contain line breaks")
        if sources[0] == "link" and (not src.startswith("https://") or " " in src):
            raise ValueError("link must be one https:// address")
        _req_str(args, "folder")
        name = args["filename"] = _req_str(args, "filename").strip()
        if "/" in name or "\\" in name or _LINE_BREAK.search(name):
            raise ValueError("filename must be a plain file name — no folders, "
                             "no line breaks")
        # the files server cleans the name it gets: it drops control
        # characters, strips leading dots and trailing dots and spaces, and
        # adds the source file's extension. The row keeps a name that
        # process leaves as it is — no whitespace or dots at either end — so
        # the crash check finds the file the server stored.
        name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)
        name = args["filename"] = re.sub(r"^[\s.]+|[\s.]+$", "", name)
        # an attachment's source is the mail tool's cached copy, named
        # with control characters dropped and the dots and spaces at its ends
        # stripped. copy adds that copy's extension to the name, so it goes
        # on the row's name here the same way, and the page shows the name
        # the files server will store. The warning is made first, while the
        # name is still the one the scan chose.
        if sources[0] == "attachment":
            cached = re.sub(r"[\x00-\x1f\x7f]", "", src).strip(". ")
            warning = file_warning(name, cached)
            if warning:
                r["warning"] = warning
            name = args["filename"] = stored_name(name, cached)
        if not file_ext(name):
            raise ValueError("filename needs its extension, like '.pdf'")
        if len(name) > FILENAME_CAP:
            raise ValueError(f"filename is {len(name)} characters, the cap is "
                             f"{FILENAME_CAP}")

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
    # was suggested. Lives outside args like series — it describes the
    # proposal, not the action, so it never enters args_sha256.
    suggestion = r.get("suggestion")
    if suggestion is not None:
        if kind != "create_event":
            raise ValueError("suggestion is only allowed on create_event rows")
        if not isinstance(suggestion, str) or not suggestion.strip():
            raise ValueError("suggestion must be a non-empty string")
        if _LINE_BREAK.search(suggestion):
            raise ValueError("suggestion must not contain line breaks")
        if len(suggestion) > SUGGESTION_CAP:
            raise ValueError(f"suggestion is {len(suggestion)} characters, the "
                             f"cap is {SUGGESTION_CAP} — one short line")


def _validate_set(s):
    """Set-level rules: at least one calendar, delivery or file row; row ids
    unique; archive rows bound to the set's own member emails; open and
    file rows point at a member; no two file rows copy the same name into
    the same folder (copy never overwrites, so the second would fail)."""
    rows = s["rows"]
    if not any(r["kind"] in ANCHOR_KINDS for r in rows):
        raise ValueError("an action set needs a create_event, update_event, "
                         "delete_event, add_delivery or copy "
                         "row — an email that changes nothing on the calendar, "
                         "ships nothing and carries no file worth keeping is "
                         "ignored, not saved")
    if sum(1 for r in rows if r.get("suggestion") is not None) > 1:
        raise ValueError("a set offers one suggested time — one create_event "
                         "row with a suggestion; the user picks another time on "
                         "the page")
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate row id")
    saves = [(r["args"]["folder"].lower(), r["args"]["filename"].lower())
             for r in rows if r["kind"] == "copy"]
    if len(set(saves)) != len(saves):
        raise ValueError("two copy rows copy the same filename into the "
                         "same folder")
    for r in rows:
        if r["kind"] == "archive_email":
            extra = {m["id"] for m in r["args"]["emails"]} - set(s["email_ids"])
            if extra:
                raise ValueError("archive_email rows may only carry the set's own "
                                 "member emails")
        if r["kind"] in ("open_email", "copy"):
            if r["args"]["email"]["id"] not in s["email_ids"]:
                raise ValueError(f"{r['kind']} rows may only point at the set's "
                                 "own member emails")


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
            "last_inbox_ids": [], "listing": {}}


def save_state(state):
    """Caller holds LOCK."""
    common.write_json(state_file(), state)


def load_state():
    """None when no state file exists. Rows left in_progress stay so — the
    boot reconciliation thread settles them against reality."""
    path = state_file()
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _find_row(s, row_id):
    return next((r for r in s["rows"] if r["id"] == row_id), None)


def _has_in_progress(s):
    return any(r["status"] == "in_progress" for r in s["rows"])


def _busy(state):
    """True while any row of any set is in_progress: one row executes at a
    time service-wide."""
    return any(_has_in_progress(s) for s in state["sets"].values())


def _stuck(s):
    """Pending with nothing left to decide or run. In practice that means a
    failed row (run_failed, precheck_failed, unknown) is holding it — an
    all-settled set resolves through _maybe_resolve_set before this is ever
    asked."""
    return (s["state"] == "pending"
            and all(r["status"] not in ("pending", "in_progress")
                    for r in s["rows"]))


def _maybe_resolve_set(s):
    """A pending set resolves once every row succeeded or was denied. One that
    failed at run time, failed its pre-check or ended unknown keeps the set
    pending."""
    if s["state"] == "pending" and all(r["status"] in SETTLED for r in s["rows"]):
        s["state"] = "resolved"
        s["resolved_at"] = _now()


# ---------------------------------------------------------------- parsers
# All three tool surfaces are asked for format="json".

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
# Every MCP tool call goes through call_calendar, call_reminders,
# call_mail or call_files, so tests stub exactly them.

def call_calendar(tool, args):
    """(ok, text) from one call to the host app's macos_calendar server."""
    return call_host_tool.call_sync(CALENDAR_PORT, tool, args)


def call_reminders(tool, args):
    """(ok, text) from one call to the host app's reminders server."""
    return call_host_tool.call_sync(REMINDERS_PORT, tool, args)


def _hermes_env():
    """KEY=VALUE pairs out of ~/.hermes/.env (the launchd environment carries
    none of them). Missing file -> {}."""
    try:
        return hermes_env.read()
    except OSError:
        return {}


def _mail_env():
    """os.environ plus the mail account's read-only + write token pair out
    of ~/.hermes/.env."""
    env = dict(os.environ)
    values = _hermes_env()
    for k in MAIL_TOKEN_VARS:
        if k in values:
            env[k] = values[k]
    return env


class MailChild:
    """One long-lived mail_tools stdio child, owned by a dedicated
    asyncio loop in its own thread; sync callers hand requests over through
    a queue the single session task consumes.

    Email ids are valid only in the child session that search_mail issued
    them, so all mail traffic lives in this one process. A call gets ONE
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

    def __init__(self):
        self._op_lock = threading.Lock()  # one mail operation at a time
        self._ready = threading.Event()
        self._loop = None
        self._req_q = None
        self._thread = None
        self._stopping = False
        self._started = False   # this attempt reached a ready session

    # ---- sync side ----

    def call(self, tool, args, timeout=TOOL_TIMEOUT):
        with self._op_lock:
            try:
                self._ensure_running()
                return self._request(tool, args, timeout)
            except MailError:
                raise
            except TimeoutError:
                raise MailTimeout(f"no answer in {timeout} s")
            except Exception as e:
                raise MailError(f"{type(e).__name__}: {e}")

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
            raise MailError("mail child did not come up")

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
                # lands in ops.error.log; the child's own stderr is
                # already inherited there — this covers parent-side failures
                # (bad interpreter path, handshake errors)
                print(f"mail child session ended: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
            never_ready = 0 if self._started else never_ready + 1
            if not self._stopping:
                await asyncio.sleep(self.RESPAWN_DELAY)

    async def _session_task(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        try:
            params = StdioServerParameters(command=MAIL_PYTHON,
                                           args=[str(MAIL_SERVER)],
                                           env=_mail_env())
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
                    fut.set_exception(MailError("mail child restarted mid-call"))


mail = MailChild()


def call_mail(tool, args, timeout=TOOL_TIMEOUT):
    """(ok, text) through the long-lived mail child."""
    return mail.call(tool, args, timeout)


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
    after html.unescape, single pass, both sides: some booking systems
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
    same-time events (only the notes may tell them apart), and it proves the
    entry still reads exactly as it did at save."""
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
    provably complete. Raises MailError on a failed call, ListingError
    on a broken one."""
    entries, offset, seen, total = [], 0, set(), 0
    while True:
        ok, text = call_mail("search_mail",
                                 {"scope": "inbox", "limit": 200, "offset": offset,
                                  "format": "json"})
        if not ok:
            raise MailError(_first_line(text))
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
    ok, text = call_mail("archive_email", {
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
    except MailError:
        return _archive_once(args)  # second failure propagates as row run_failed


def _exec_reminder(args):
    ok, text = call_reminders("create_reminder", {
        "name": args["name"], "list_name": args["list"],
        "notes": args.get("notes", ""), "due": args.get("due", "")})
    # the tool's SUCCESS line ends with the reminder id, an internal handle
    # the reminders server says never to show
    return ("success" if ok else "run_failed"), _first_line(text).rsplit(" — id:", 1)[0]


def _exec_delivery(args):
    if parcel_api.key() is None:
        return "run_failed", f"{parcel_api.KEY_VAR} is not set in ~/.hermes/.env"
    ok, text = parcel_api.add({"tracking_number": args["tracking_number"],
                               "carrier_code": args["carrier_code"],
                               "description": args["description"]})
    if ok:
        return "success", f"added to Parcel: {args['description']} ({args['carrier']})"
    return "run_failed", f"Parcel refused the delivery: {text}"


def _exec_copy(r):
    """The name must be free before anything is fetched: copy never
    overwrites, and the crash check trusts a file on disk only when this row
    put it there. The "saving" status text, persisted, marks that point."""
    args = r["args"]
    if _file_saved(args):
        return "run_failed", (f'"{args["filename"]}" already exists in '
                              f'{args["folder"]} — nothing saved')
    with LOCK:
        r["status_text"] = "saving"
        save_state(STATE)
    if "attachment" in args:
        tool, tool_args = "download_attachment", {"attachment_name": args["attachment"]}
    else:
        tool, tool_args = "download_link", {"url": args["link"]}
    ok, text = _call_known(tool, args["email"], tool_args, DOWNLOAD_TIMEOUT)
    if not ok:
        return "run_failed", "download failed: " + _first_line(text)
    found = SAVED_RE.search(text)
    if found is None:
        return "run_failed", "download answer did not parse: " + _first_line(text)
    source = found.group(1).strip()
    ok, text = call_files("copy", {"source": source,
                                   "destination": os.path.join(args["folder"], args["filename"])})
    if ok:
        return "success", _first_line(text)
    if text.lstrip().startswith("REJECTED:"):
        # the files server refused the call before copying anything
        return "run_failed", _first_line(text)
    # the name was free before the call, so a file there now is this copy:
    # it landed and only the answer was lost (a timeout)
    if _file_saved(args):
        return "success", "saved — the files server's answer was lost: " + _first_line(text)
    return "run_failed", _first_line(text)


def _file_saved(args):
    """True when the row's file is in its folder, read on the disk."""
    return os.path.lexists(os.path.join(args["folder"], args["filename"]))


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


def execute_row(r):
    """(status, status_text) — the real executor; status
    success|run_failed|precheck_failed, or unknown when a save cannot tell
    whether its copy landed.
    Tool-transport problems raise (CalendarError/MailError); the worker
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
    if kind == "create_reminder":
        return _exec_reminder(args)
    if kind == "add_delivery":
        return _exec_delivery(args)
    if kind == "copy":
        return _exec_copy(r)
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
    calendar/mailbox reality. Unreachable servers raise; the caller marks the
    row unknown."""
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
    if kind == "create_reminder":
        if _reminder_exists(args):
            return "success", "added before the crash"
        return "pending", "reminder never landed — re-fire"
    if kind == "add_delivery":
        if parcel_api.key() is None:
            return "unknown", f"{parcel_api.KEY_VAR} is not set in ~/.hermes/.env"
        if args["tracking_number"] in parcel_api.tracking_numbers():
            return "success", "added before the crash"
        return "pending", "add never landed — re-fire"
    if kind == "copy":
        if not _file_saved(args):
            return "pending", "copy never landed — re-fire"
        if r["status_text"] == "saving":
            return "success", "saved before the crash"
        return "run_failed", (f'"{args["filename"]}" already exists in '
                              f'{args["folder"]} — nothing saved')
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


def _spawn_set(set_id, row_ids):
    """The approve-set run's thread; module-level like _spawn."""
    threading.Thread(target=_work_set, args=(set_id, row_ids), daemon=True).start()


def _work_set(set_id, row_ids):
    """Run the listed rows in order, one after another: the first is already
    in_progress (approve_set marked it under LOCK); each next one is marked
    when its turn comes, so a stop leaves the rest pending. Stops at the
    first row that does not succeed, when the set or the next row is no
    longer where it was (a deny from another tab, a reset), and when
    another row slipped into execution between two of its own (an approve
    from a second tab lands while no row is in_progress): the rest stay
    pending, and the user taps approve again."""
    for i, row_id in enumerate(row_ids):
        if i:
            with LOCK:
                s = STATE["sets"].get(set_id)
                r = _find_row(s, row_id) if s else None
                if (r is None or s["state"] != "pending" or r["status"] != "pending"
                        or _busy(STATE)):
                    return
                r["status"] = "in_progress"
                r["status_text"] = "working"
                save_state(STATE)
        _work_item(set_id, row_id)
        with LOCK:
            s = STATE["sets"].get(set_id)
            r = _find_row(s, row_id) if s else None
        if r is None or r["status"] != "success":
            return


def _work_item(set_id, row_id):
    """Execute one approved row already persisted as in_progress (resolve's
    thread, or one turn of _work_set). No void path can touch the row
    meanwhile — reset
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
        line = {"event": "row_resolved", "set_id": set_id, "row_id": row_id,
                "kind": r["kind"], "args": r["args"], "outcome": outcome,
                "result": text}
        if r["kind"] == "create_event":
            # the rules job's calendar and title evidence (rule_evidence);
            # args can be cut at 1000 characters, so these ride on their own
            line.update(title=r["args"]["title"], calendar=r["args"]["calendar"],
                        proposed_calendar=r.get("proposed_calendar",
                                                r["args"]["calendar"]),
                        proposed_title=r.get("proposed_title", r["args"]["title"]),
                        emails=[{"subject": m["subject"], "from": m["from"]}
                                for m in s["emails"]])
        _log(line)
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
    if r.get("warning"):
        view["warning"] = r["warning"]
    return view


def _recently_resolved(s):
    """A resolved set stays on the page (collapsed) for 24 h."""
    if s["state"] != "resolved":
        return False
    resolved = datetime.fromisoformat(s["resolved_at"])
    return (datetime.now(timezone.utc) - resolved).total_seconds() <= 24 * 3600


def _gone(email_id, inbox):
    """A set member left the inbox — only once its listing was provably
    complete (an empty list means it never was)."""
    return bool(inbox) and email_id not in inbox


def page_state(state):
    """GET /api/state: scan stamps plus the pending sets and the sets
    resolved in the last 24 h, minus the ones hidden by hand, page-ready.
    Caller holds LOCK; state() merges in the job fields and the calendars
    after releasing it."""
    sets = []
    inbox = state["last_inbox_ids"]
    for s in state["sets"].values():
        if s.get("hidden"):
            continue
        if s["state"] != "pending" and not _recently_resolved(s):
            continue
        emails = [{**e, "gone": _gone(e["id"], inbox)}
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
    """POST /api/inbox/resolve. Returns (http status, payload). Caller holds LOCK."""
    s = state["sets"].get(body.get("set_id"))
    if s is None:
        return 404, {"error": "unknown set"}
    r = _find_row(s, body.get("row_id"))
    if r is None:
        return 404, {"error": "unknown row"}
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        return 400, {"error": "decision must be approve or deny"}
    pick, name, start = body.get("calendar"), body.get("title"), body.get("start")
    if (pick, start) != (None, None) and (
            decision != "approve" or r["kind"] != "create_event"):
        return 400, {"error": "only an approved create_event row takes a "
                              "calendar or start"}
    if name is not None and (decision != "approve" or r["kind"] not in TITLE_KEY):
        return 400, {"error": "only an approved create_event or add_delivery "
                              "row takes a title"}
    if name is not None and (err := _title_error(r, name)):
        return 400, {"error": err}
    if start is not None and (err := _start_error(r, start)):
        return 400, {"error": err}
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
    if _busy(state):
        return 409, {"error": "another row is executing", "row": view()}
    _apply_edits(r, pick, name, start)
    r["status"] = "in_progress"
    r["status_text"] = "working"
    save_state(state)
    _spawn(s["id"], r["id"])
    return 202, {"ok": True, "row": view()}


# the args key the page's title field edits, per row kind: an event's
# title, a delivery's name in Parcel
TITLE_KEY = {"create_event": "title", "add_delivery": "description"}


def _title_error(r, title):
    """Why a title from the page's title field is refused, or None: the
    scan's own checks for that field."""
    if not isinstance(title, str) or not title.strip():
        return "title must be a non-empty string"
    if _LINE_BREAK.search(title):
        return "title must not contain line breaks"
    if r["kind"] == "add_delivery" and len(title.strip()) > parcel_api.DESCRIPTION_CAP:
        return f"title is over {parcel_api.DESCRIPTION_CAP} characters"
    return None


def _start_error(r, start):
    """Why a start from the page's time field is refused, or None: it keeps
    the row's form, a date on an all-day row and a date and time otherwise."""
    try:
        all_day = _valid_date(start, "start")
    except ValueError as e:
        return str(e)
    if all_day != r["args"]["all_day"]:
        return ("start must be a date on an all-day event and a date and "
                "time otherwise")
    return None


def _apply_edits(r, calendar, title, start):
    """Write the calendar the user picked, the title they typed and the start they
    picked on the page into an approved row: all three on a create_event
    row, the title alone on an add_delivery row, where it is the
    description. None leaves that field alone. A new start moves the end
    with it, so the length stays. args change, so the hash is stamped
    again; the scan's values stay as proposed_<key>. The decisions log
    carries an event's proposed_calendar and proposed_title as rules
    evidence. The caller checked all three."""
    a = r["args"]
    for k, v in (("calendar", calendar),
                 (TITLE_KEY.get(r["kind"]), title and title.strip())):
        if v is None or v == a[k]:
            continue
        r.setdefault("proposed_" + k, a[k])
        a[k] = v
    if start is not None and start != a["start"]:
        if "end" in a:
            fmt = "%Y-%m-%d" if a["all_day"] else "%Y-%m-%d %H:%M"
            p = lambda v: datetime.strptime(v, fmt)
            a["end"] = (p(start) + (p(a["end"]) - p(a["start"]))).strftime(fmt)
        a["start"] = start
    r["args_sha256"] = _sha256(a)


def approve_set(state, body):
    """POST /api/inbox/approve-set. Returns (http status, payload). Caller
    holds LOCK. The rows list is checked whole before anything is marked, so
    a refused call changes nothing."""
    s = state["sets"].get(body.get("set_id"))
    if s is None:
        return 404, {"error": "unknown set"}
    if s["state"] != "pending":
        return 409, {"error": f"set is {s['state']}"}
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows or not all(
            isinstance(x, dict) and isinstance(x.get("row_id"), str) for x in rows):
        return 400, {"error": "rows must be a non-empty list of {row_id, args_sha256}"}
    picked = []
    for x in rows:
        r = _find_row(s, x["row_id"])
        if r is None:
            return 404, {"error": f"unknown row {x['row_id']!r}"}
        if x.get("args_sha256") != r["args_sha256"]:
            return 409, {"error": "args changed — re-render and retry"}
        if r["status"] != "pending":
            return 409, {"error": f"row {r['id']} is {r['status']}"}
        if x.keys() & {"calendar", "start"} and r["kind"] != "create_event":
            return 400, {"error": f"row {r['id']} is no create_event row — "
                                  "only those take a calendar or start"}
        if "title" in x and r["kind"] not in TITLE_KEY:
            return 400, {"error": f"row {r['id']} takes no title — only "
                                  "create_event and add_delivery rows do"}
        if "title" in x and (err := _title_error(r, x["title"])):
            return 400, {"error": f"row {r['id']}: {err}"}
        if "start" in x and (err := _start_error(r, x["start"])):
            return 400, {"error": f"row {r['id']}: {err}"}
        picked.append((r, x.get("calendar"), x.get("title"), x.get("start")))
    if _busy(state):
        return 409, {"error": "another row is executing"}
    for r, *edits in picked:
        _apply_edits(r, *edits)
    picked = [r for r, *_ in picked]
    picked[0]["status"] = "in_progress"
    picked[0]["status_text"] = "working"
    save_state(state)
    _spawn_set(s["id"], [r["id"] for r in picked])
    return 202, {"ok": True, "rows": [_row_view(r) for r in picked]}


def reset(state, body):
    """POST /api/inbox/reset: {set_id} (any state — the curl repair path for
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
    """POST /api/inbox/hide. A stuck set is hidden by set_id only (the card's
    accept key); settling it as resolved lets the trim clear the record like
    any resolved set. all: true (the done section's clear key) never touches
    a stuck set: it still needs the user's choice. Caller holds LOCK."""
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



def _fits(deadline, cost):
    """True when a call whose worst case is cost seconds ends by deadline
    (a time.monotonic value). No deadline always fits."""
    return deadline is None or time.monotonic() + cost <= deadline


def _register_id(email_id, sender, received_at, deadline=None):
    """A search over the whole mailbox to put an id back in the child's
    session: get_email answers only for ids that session issued, and a member
    email that left the inbox (an archive row ran, or the user filed it) is in
    no inbox listing. The search is the sender on the received day (UTC, as
    the listings give it), never the subject: a subject can be empty, and a
    common one matches more than a page. The sender is the address, or the
    whole From value when it has none. Pages of 200 are read until the id is
    seen. True when the id came back."""
    day = received_at[:10]
    args = {"after": day, "before": day, "limit": 200, "format": "json"}
    address = re.search(r"[^\s<>,]+@[^\s<>,]+", sender)
    if address or sender.strip():
        args["sender"] = address.group(0) if address else sender.strip()
    offset = 0
    while _fits(deadline, CALL_WORST_S):
        ok, text = call_mail("search_mail", dict(args, offset=offset))
        if not ok:
            return False
        total, page = parse_inbox_listing(text)
        if any(e["id"] == email_id for e in page):
            return True
        offset += len(page)
        if not page or offset >= total:
            return False
    return False


def _known_email(email_id):
    """{id, subject, from, receivedAt}, or None when the service has never
    seen the id. Two places carry these facts: the scan listing while the mail
    is in the inbox, and any set that holds it — an archived member is in no
    listing, and the page still expands its row. _register_id searches on
    the sender and the day."""
    known = STATE["listing"].get(email_id)
    if known:
        return dict(known, id=email_id)
    for s in STATE["sets"].values():
        for e in s["emails"]:
            if e["id"] == email_id:
                return dict(e)
    return None


def _call_known(tool, email, args, timeout, deadline=None, relisted=None):
    """(ok, text) of one mail tool call on an email the service has seen:
    email is {id, from, receivedAt}. An id
    the child no longer knows (its session restarted, or the email left the
    inbox) is put back and the call made once more. Any other failure is
    returned as it is.

    A scan passes relisted, an empty set it marks once it listed the inbox
    again: the scan's first forgotten id lists the inbox's newest 200 again,
    which puts every id on that page back in one search. An id still unknown
    after that gets its own _register_id search. No follow-up call starts
    unless its worst case ends by deadline."""
    args = dict(args, email_id=email["id"])

    def attempt():
        try:
            return call_mail(tool, args, timeout=timeout)
        except MailTimeout as e:
            return False, f"FAILED: {e}"
        except MailError:   # the child respawned: every id it issued is dead
            return False, UNKNOWN_ID

    ok, text = attempt()
    if ok or UNKNOWN_ID not in text:
        return ok, text
    if (relisted is not None and not relisted
            and _fits(deadline, CALL_WORST_S + GET_WORST_S)):
        relisted.add("inbox")
        try:
            call_mail("search_mail", {"scope": "inbox", "limit": 200, "format": "json"})
        except MailError:
            pass
        ok, text = attempt()
        if ok or UNKNOWN_ID not in text:
            return ok, text
    if (_register_id(email["id"], email["from"], email["receivedAt"], deadline)
            and _fits(deadline, GET_WORST_S)):
        ok, text = attempt()
    return ok, text


def _read_known(email, deadline=None, relisted=None):
    """(ok, text) of one get_email for an email the service has seen."""
    return _call_known("get_email", email, {}, 20, deadline, relisted)


def read_body(params):
    """GET /api/inbox/body?email_id=…: one email's own text — the header block
    and the body, as get_email renders them, uncut. Two callers: the page's
    expanded email row, and the agent's read_email tool widening a scan
    snippet. Neither sets nor the scan store bodies, so the text is fetched
    live. A failed fetch is a 502 with the tool's first line; the page shows
    that in the row."""
    email_id = params.get("email_id") or ""
    with LOCK:
        email = _known_email(email_id)
    if email is None:
        return 404, {"error": "unknown email"}
    try:
        ok, text = _read_known(email)
        if not ok:
            return 502, {"error": _first_line(text)}
        return 200, {"text": _unfenced(text)}
    except (MailError, ListingError) as e:
        return 502, {"error": f"{type(e).__name__}: {e}"}


def rescan():
    """POST /api/inbox/rescan: start `hermes cron run actions-inbox-scan` detached. That
    agent run is the whole loop — /api/inbox/inbox-scan only hands emails to a caller, it
    proposes nothing. The run claims the job, so a second tap while one is
    going cannot fire it twice. Touches no state, so it takes no lock."""
    try:
        subprocess.Popen(["hermes", "cron", "run", CRON_JOB],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start hermes: {e}"}
    return 200, {"started": CRON_JOB}


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
    (the mail API), never the message Date: header — and for the To: line, which the
    listing does not carry. A result with no blank line is all body."""
    head, _, body = _unfenced(text).partition("\n\n")
    return (head, body) if body else ("", head)


def _names_address(text, addresses):
    """True when a From value or a header line names one of the addresses."""
    lowered = (text or "").lower()
    return any(a in lowered for a in addresses)


def _to_ignored(header, addresses):
    """True when the get_email header block is addressed to one of the
    IGNORE_EMAIL_TO addresses. Costs the body fetch, because only get_email
    reports recipients."""
    return any(l.startswith("To:") and _names_address(l, addresses)
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
# the mail tool's _fmt_ics_event writes — fixed-format so the scan can
# split the section off the body and build the per-uid timeline
ICS_HEADER = "Calendar invitation data (parsed from the email's calendar attachment):"
_ICS_EVENT_RE = re.compile(r"- (\w+) uid=(\S+) sequence=(\S+) stamped=(.+)")


def _split_ics(body):
    """(main, ics_section) — the ICS facts ride outside the body cap the way
    get_email keeps links outside its own cap: a long invitation body must
    not push the authoritative event data over the edge."""
    main, _, ics = body.partition(ICS_HEADER)
    return main.rstrip(), (ICS_HEADER + ics.rstrip()) if ics else ""


def _email_files(header, links):
    """The files an email carries, as lines for the scan: its Attachments
    header line, and each link whose text is a document's file name (a
    portal mail that links its PDF instead of attaching it)."""
    files = [l for l in header.splitlines() if l.startswith("Attachments:")]
    return files + [l.strip() for l in links.splitlines()
                    if DOC_LINK_RE.match(l.strip())]


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


def scan(state):
    """POST /api/inbox/inbox-scan. Single-flight."""
    if not SCAN_LOCK.acquire(blocking=False):
        return 409, {"error": "a scan is already running"}
    try:
        return _scan(state)
    finally:
        SCAN_LOCK.release()


# what the last scan could not do without failing: the page shows each
# entry as a warning card, since a scan that skips a step still answers 200
# and "ok"
_SCAN_WARNINGS = []


def _scan(state):
    global _SCAN_WARNINGS
    started = time.monotonic()  # the budget covers the listing too
    _SCAN_WARNINGS = []

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
    inbox_ids = [e["id"] for e in entries]
    complete = total == len(inbox_ids) and total > 0
    with LOCK:
        # the listing cache save_set stamps member facts from: every id this
        # scan listed, facts as the mailbox reported them. Merged, never
        # replaced — an incomplete listing must not drop known ids; the trim
        # prunes ids once a listing is provably complete.
        for e in entries:
            state["listing"][e["id"]] = {"subject": e["subject"],
                                         "from": e["from"],
                                         "receivedAt": e["receivedAt"]}
        if complete:
            _trim(state, inbox_ids)
            state["last_inbox_ids"] = inbox_ids
        else:
            _log({"event": "trim_skipped", "total": total,
                  "returned": len(inbox_ids),
                  "reason": "inbox listing not provably complete"})
        # processed = assigned or ignored; merely scanned is not processed
        new = [e for e in entries if e["id"] not in state["ledger"]]
        pending_sets = []
        for s in state["sets"].values():
            if s["state"] != "pending":
                continue
            p = {"id": s["id"], "title": s["title"], "created_at": s["created_at"],
                 "subjects": [m["subject"] for m in s["emails"]],
                 "rows": [{"label": r["label"], "status": r["status"]} for r in s["rows"]]}
            # sets waiting on the user (time suggestions, a reply row) get a
            # thread check below, anchored on their newest member email
            if any(r.get("suggestion") or r["kind"] == "open_email"
                   for r in s["rows"]):
                a = max(s["emails"], key=lambda m: m["receivedAt"])
                p["_anchor"] = {"id": a["id"], "receivedAt": a["receivedAt"]}
            pending_sets.append(p)
    # newest first (the search orders by receivedAt, never the Date
    # header). Beyond the cap the oldest stay new for the next scan
    capped = len(new) > NEW_CAP
    new = new[:NEW_CAP]
    out_emails, fetch_failed = [], []
    ignore_from, ignore_to = _ignore_lists()
    deadline = started + BODY_BUDGET_S
    relisted = set()   # marked once a forgotten id listed the inbox again
    for e in new:
        # an ignored sender, caught before the body fetch. Skipped emails keep
        # no ledger entry, so every scan drops them again — free here, one
        # get_email per scan for the To: case below.
        if _names_address(e["from"], ignore_from):
            continue
        if not _fits(deadline, GET_WORST_S):
            fetch_failed.append(e["id"])
            continue
        try:  # a failed fetch never fails the scan — it lands in fetch_failed
            ok, text = _read_known(e, deadline, relisted)
        except Exception:
            ok, text = False, ""
        if not ok:
            # failed ids are NOT returned: they keep no ledger entry, so
            # they come back next scan instead of being saved on subject
            # alone; fetch_failed still reports them
            fetch_failed.append(e["id"])
            continue
        header, raw = _email_parts(text)
        if _to_ignored(header, ignore_to):
            continue
        main, ics = _split_ics(raw)
        prose, links = _split_links(main)
        # An invitation keeps its link list — the meeting link is part of the
        # event the agent proposes. Everything else gets a snippet; read_email
        # widens it on demand.
        if ics:
            parts = [_snippet(prose), links, ics]
        else:
            parts = [_snippet(prose)]
        entry = {"id": e["id"], "from": e["from"], "subject": e["subject"],
                 "receivedAt": e["receivedAt"],
                 "body": "\n\n".join(p for p in parts if p)}
        files = _email_files(header, links)
        if files:
            entry["files"] = files
        out_emails.append(entry)
    # thread phase: has anyone followed up on these emails? Follow-up
    # messages (The user's replies included — they live in Sent, never the
    # inbox) feed the reply-row and settled-time rules. A failed check
    # attaches thread_error instead of thread_after, so absence of
    # thread_after with no error genuinely means "no follow-ups".
    thread_cache, body_cache = {}, {}


    def _thread_after(email_id, received_at, cap=THREAD_AFTER_CAP):
        """(messages newer than received_at, error). Bodies come back as
        snippets — the agent reads a follow-up in full with read_email, and
        five 4 KB follow-ups per new email is how a scan reaches the MCP result
        cap."""
        if email_id not in thread_cache:
            if not _fits(deadline, CALL_WORST_S):
                return None, "not checked — scan ran out of time"
            try:
                ok, text = call_mail("list_thread",
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
                if not _fits(deadline, GET_WORST_S):
                    return None, "not checked — scan ran out of time"
                try:
                    ok, text = _read_known(t, deadline, relisted)
                except Exception:
                    ok = False
                # "" means the body fetch failed — the message exists, its
                # content is unknown
                body_cache[t["id"]] = _cap_body(_email_parts(text)[1]) if ok else ""
            body = body_cache[t["id"]]
            out.append({**t, "body": _snippet(body)})
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
        _attach(e, e["id"], e["receivedAt"])

    # an email carrying files may get a copy row, which names a filing
    # folder: the folders ride along, walked fresh
    folders = []
    if any(e.get("files") for e in out_emails):
        folders = filing_folders()

    # the calendars a row may write, each with what it is for: the agent
    # picks from these, and the save check holds it to them
    try:
        calendars = [{"title": t, "note": n}
                     for t, n in sorted(writable(calendar_list()).items())]
    except CalendarError as e:
        calendars = []
        _SCAN_WARNINGS.append({"step": "calendars", "message":
                               f"list_calendars failed — the scan handed over no "
                               f"calendar list: {e}"})

    global SCAN_BATCH
    with LOCK:
        SCAN_BATCH = [e["id"] for e in out_emails]
        state["last_scan_at"] = _now()
        state["last_scan_status"] = "ok"
        save_state(state)
    return 200, {"emails": out_emails,
                 "invitation_timeline": _invitation_timeline(out_emails),
                 "pending_sets": pending_sets, "capped_new": capped,
                 "fetch_failed": fetch_failed, "folders": folders,
                 "calendars": calendars}


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


def save_set(state, body):
    """POST /api/inbox/sets. Caller holds LOCK — everything here is state-only
    apart from a copy row's walk of the filing folders, so the whole save
    (validate, supersede, ledger, insert, log) is atomic."""
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
        elif b["kind"] == "copy" and isinstance(b.get("args"), dict):
            i = b["args"].get("email")
            if not (isinstance(i, str) and i):
                return 400, {"error": "copy args.email must be one member "
                                      "email id string — the service stamps the "
                                      "display facts"}
            if i not in by_id:
                return 400, {"error": f"copy takes a file from {i!r}, not "
                                      "a member of this set"}
            folder = b["args"].get("folder")
            if folder not in filing_folders():
                return 400, {"error": f"unknown folder {folder!r} — use a full "
                                      "path from the folders the scan listed"}
            b["args"] = dict(b["args"], email=dict(by_id[i]))
    s = {"id": set_id,
         "title": body.get("title", ""), "rationale": rationale,
         "email_ids": email_ids, "emails": emails, "created_at": now,
         "created_by": created_by,
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
    # one set per tracking number: a carrier's later mail for the same
    # shipment (out for delivery, delivered) must not add it to Parcel again.
    # A denied or refused row never added it, so a set holding only that
    # frees the number for a second try (a re-scan, or a fixed carrier)
    held = {r["args"]["tracking_number"]: old
            for old in state["sets"].values()
            if old["state"] != "superseded" and old not in targets
            for r in old["rows"]
            if r["kind"] == "add_delivery" and r["status"] not in ("denied", "run_failed")}
    for r in s["rows"]:
        if r["kind"] == "add_delivery" and r["args"]["tracking_number"] in held:
            old = held[r["args"]["tracking_number"]]
            return 400, {"error": f"tracking number {r['args']['tracking_number']} "
                                  f"is already in set {old['id']} ({old['title']}) — "
                                  "save this email as ignored instead"}
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
    _maybe_resolve_set(s)  # a fully pre-denied set resolves at birth
    _log({"event": "set_created", "set_id": set_id, "rationale": s["rationale"],
          "email_ids": email_ids,
          "rows": [{"kind": r["kind"], "args": r["args"]} for r in s["rows"]]})
    save_state(state)
    return 200, {"set_id": set_id,
                 "rows": [{"id": r["id"], "status": r["status"]} for r in s["rows"]]}


# ---------------------------------------------------------------- calendars

def calendar_list():
    """[{title, color, editable, note}] from the host app's macos_calendar
    server, read live. Raises CalendarError on a failed call or a bad answer."""
    ok, text = call_calendar("list_calendars", {"format": "json"})
    if not ok:
        raise CalendarError(_first_line(text) or "no output")
    try:
        return [{"title": c["title"], "color": c["color"],
                 "editable": c["editable"], "note": c["note"]}
                for c in json.loads(text)["calendars"]]
    except (ValueError, KeyError, TypeError):
        raise CalendarError("bad list_calendars json") from None


def writable(cals):
    """{title: note} of the calendars a row may write: editable, and a title
    no other calendar shares (the calendar server refuses a shared title)."""
    titles = [c["title"].strip().lower() for c in cals]
    return {c["title"]: c["note"] for c in cals
            if c["editable"] and titles.count(c["title"].strip().lower()) == 1}


def _calendars_check(names, retry):
    """None when every name is a writable calendar, else (status, payload).
    The list is read live; no names costs no call."""
    if not names:
        return None
    try:
        allowed = writable(calendar_list())
    except CalendarError as e:
        return 502, {"error": f"the calendar list could not be read — {retry}: {e}"}
    bad = [c for c in names if not isinstance(c, str) or c not in allowed]
    if bad:
        return 400, {"error": f"calendar {bad[0]!r} not allowed — the writable "
                              f"calendars are {', '.join(sorted(allowed))}"}
    return None


# The calendars for the page — dots in each calendar's color, and the new
# event rows' dropdown of writable calendars: fetched once, on the page's own
# polls (server.py calls page_poll() on every poll carrying the hold-update
# flag), and kept for the life of the process or until clear_calendars();
# empty until the first fetch lands, and the page shows no dots and no
# dropdown. A failed fetch (the host app not up yet) is tried again on a
# later poll, CALENDARS_RETRY_S apart. The approve and save paths never use
# this copy: they read the list live.
_CALENDARS = []
_CALENDARS_ERROR = None   # why the last fetch failed; the page shows it as a warning
CALENDARS_RETRY_S = 60
_CALENDARS_NEXT_TRY = 0.0   # monotonic time before which page_poll() starts no fetch


def _calendars_once():
    """One fetch attempt. True once the list is held, False on any failure."""
    global _CALENDARS, _CALENDARS_ERROR
    try:
        _CALENDARS = calendar_list()
    except CalendarError as e:
        _CALENDARS_ERROR = f"fetch failed: {e}"
        print(f"calendars: {_CALENDARS_ERROR}", flush=True)
        return False
    _CALENDARS_ERROR = None
    return True


def _colors(cals):
    """{calendar title: "#rrggbb"}, calendars with no valid color left out."""
    hexd = set("0123456789abcdef")
    return {c["title"]: c["color"].lower() for c in cals
            if isinstance(c["title"], str) and isinstance(c["color"], str)
            and len(c["color"]) == 7 and c["color"].startswith("#")
            and set(c["color"][1:].lower()) <= hexd}


def page_poll():
    """One background fetch while no list is held, CALENDARS_RETRY_S apart."""
    global _CALENDARS_NEXT_TRY
    if _CALENDARS or time.monotonic() < _CALENDARS_NEXT_TRY:
        return
    _CALENDARS_NEXT_TRY = time.monotonic() + CALENDARS_RETRY_S
    threading.Thread(target=_calendars_once, daemon=True).start()


def clear_calendars():
    """Forget the list; the next page poll fetches it again."""
    global _CALENDARS, _CALENDARS_NEXT_TRY
    _CALENDARS = []
    _CALENDARS_NEXT_TRY = 0.0


# ---------------------------------------------------------------- rules evidence

def rule_evidence(line):
    """One decisions-log line as calendar evidence for the rules job
    (job_rules.py), or None. Evidence is a create_event row that ran: the
    calendar it went to, whether the user moved it there from the scan's
    pick, and whether they renamed it."""
    if (line.get("event") != "row_resolved" or line.get("kind") != "create_event"
            or line.get("outcome") != "done" or "calendar" not in line):
        return None
    if line["proposed_calendar"] != line["calendar"]:
        what = f"the user moved it from {line['proposed_calendar']} to {line['calendar']}"
    else:
        what = f"approved on {line['calendar']} as proposed"
    # lines from before the title field carry no proposed_title
    if line.get("proposed_title", line["title"]) != line["title"]:
        what = f"the user renamed it from \"{line['proposed_title']}\"; {what}"
    mails = "; ".join(f"{m['from']}: {m['subject']}" for m in line["emails"])
    return {"id": f"{line['set_id']}/{line['row_id']}", "at": line["ts"],
            "summary": f"Event \"{line['title']}\": {what}. Email: {mails}"}


# ---------------------------------------------------------------- area interface

NAME = "inbox"


def boot():
    """Load the state file and start the boot-reconciliation thread.
    Called once by server.main before serving;
    server.main installs the SIGTERM handler that lets the atexit mail
    shutdown run."""
    global STATE
    STATE = load_state()
    if STATE is None:
        STATE = empty_state()
        with LOCK:
            save_state(STATE)
    if parcel_api.key() is None:
        print(f"parcel: no {parcel_api.KEY_VAR} in ~/.hermes/.env — add_delivery "
              "rows fail at approval", flush=True)
    atexit.register(mail.shutdown)
    threading.Thread(target=reconcile_boot, daemon=True).start()


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
    cals = _CALENDARS
    if cals:
        out["calendar_colors"] = _colors(cals)
        out["calendars"] = sorted(writable(cals))
    out["warnings"] = list(_SCAN_WARNINGS)
    if _CALENDARS_ERROR:
        out["warnings"].append({"step": "calendars",
                                "message": _CALENDARS_ERROR})
    return out


def _h_resolve(body):
    picks = [body["calendar"]] if body.get("calendar") is not None else []
    err = _calendars_check(picks, "try again")
    if err is not None:
        return err
    with LOCK:
        return resolve(STATE, body)


def _h_approve_set(body):
    rows = body.get("rows")
    picks = [x["calendar"] for x in rows if isinstance(x, dict)
             and x.get("calendar") is not None] if isinstance(rows, list) else []
    err = _calendars_check(picks, "try again")
    if err is not None:
        return err
    with LOCK:
        return approve_set(STATE, body)


def _h_reset(body):
    with LOCK:
        return reset(STATE, body)


def _h_hide(body):
    with LOCK:
        return hide(STATE, body)



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


def _check_file_rows(body):
    """Every copy row must name a file its member email carries: the
    attachment on the email's Attachments line (a name holding ", " cannot
    be split off that line, so this proves only that the name is on it —
    download_attachment matches it exactly), or a document link in its link
    list, one the scan offered (_email_files) — the service downloads
    nothing an email did not name as a file. The email is read
    live, before LOCK like _snapshot_rows. Returns (status, payload) on
    failure, None when every row checks out. Rows whose shape is wrong, or
    whose email the service never listed, are skipped here — save_set
    rejects them with the field-level error."""
    if body.get("kind", "action") == "ignore":
        return None
    rows = body.get("rows")
    if not isinstance(rows, list):
        return None
    read = {}   # email id -> (ok, text): one read per email, however many rows
    for r in rows:
        if not isinstance(r, dict) or r.get("kind") != "copy" \
                or not isinstance(r.get("args"), dict):
            continue
        args = r["args"]
        email_id = args.get("email")
        if not isinstance(email_id, str):
            continue
        with LOCK:
            email = _known_email(email_id)
        if email is None:
            continue
        if email_id not in read:
            try:
                read[email_id] = _read_known(email)
            except (MailError, ListingError) as e:
                read[email_id] = False, f"{type(e).__name__}: {e}"
        ok, text = read[email_id]
        if not ok:
            return 502, {"error": f"could not read {email_id} to check its "
                                  f"file — retry the save: {_first_line(text)}"}
        header, rest = _email_parts(text)
        att, link = args.get("attachment"), args.get("link")
        if isinstance(att, str):
            name = att.strip()
            if not any(l.startswith("Attachments:")
                       and (f": {name} (" in l or f", {name} (" in l)
                       for l in header.splitlines()):
                return 400, {"error": f"{email_id} has no attachment named "
                                      f"{name!r} — copy the name from its "
                                      "Attachments line exactly"}
        elif isinstance(link, str):
            if not any(l.endswith(f" -> {link.strip()}")
                       for l in _email_files("", _split_links(rest)[1])):
                return 400, {"error": f"{email_id} has no document link "
                                      f"{link!r} — use a full address from "
                                      "its files list"}
    return None


def _check_calendars(body):
    """Every calendar a row names must be a writable one (_calendars_check),
    read live before LOCK like _snapshot_rows. Returns (status, payload) on
    failure, None when every calendar is allowed."""
    if body.get("kind", "action") == "ignore" or not isinstance(body.get("rows"), list):
        return None
    names = [c for r in body["rows"] if isinstance(r, dict)
             for c in _find_calendars(r.get("args"))]
    return _calendars_check(names, "retry the save")


def _h_sets(body):
    err = _check_calendars(body) or _snapshot_rows(body) or _check_file_rows(body)
    if err is not None:
        return err
    with LOCK:
        return save_set(STATE, body)


def _h_scan(body):
    return scan(STATE)  # takes LOCK in sections, never as a whole


def _h_rescan(body):
    return rescan()


HANDLERS = {"/api/inbox/resolve": _h_resolve, "/api/inbox/reset": _h_reset,
            "/api/inbox/approve-set": _h_approve_set,
            "/api/inbox/hide": _h_hide,
            "/api/inbox/sets": _h_sets, "/api/inbox/inbox-scan": _h_scan,
            "/api/inbox/rescan": _h_rescan}

GET_HANDLERS = {"/api/inbox/body": read_body}
