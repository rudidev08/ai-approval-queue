"""Chat area — save proposals for attachments in watched chats: card store
and the approve path.

Served by server.py (one process, one page). The chat-attach-scan cron
job (chat_scan.py — a --no-agent script) drives the scan: it calls
/api/chat/candidates, makes one structured LLM call, and posts the
verdicts to /api/chat/batch. This module owns all MCP I/O — chat reads go
through the host app's chat server (streamable HTTP, 8358), sender names
come from its
contacts server (8353), and document text and filing go through its
files server (files_client.py, shared with the inbox area).

One attachment is one card: proposed filename, the LLM's folder pick, a
one-line reason, and the source facts (sender, chat label, received date,
original name, kind). The page offers a destination dropdown of the filing
folders (every folder under Papers and SharedPapers outside the ignore
folder and hidden folders, walked here); the chosen folder travels with the
approve call and is checked against that list. Approving runs
export_attachment_to_scratch (copy into the scratch folder) then files move
(out of scratch into the folder). Denying drops the card; a success card
leaves at the next scan. run_failed cards stay approvable: export only
copies, and a failed move has the service delete the scratch copy itself,
so an approve is always a clean retry.

The ledger (state/chat.json, keyed by attachment id) is the whole
scan memory: every verdict lands there (ignored or proposed), so a scanned
attachment is never proposed again — denied ones included. Entries are kept
for LEDGER_DAYS (30), trimmed on each batch save; an aged-out entry belongs
to an attachment weeks past the 72-hour scan lookback, so nothing can come
back. A candidate the LLM omits gets no ledger entry and simply re-appears
next scan.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/chat/candidates   build the candidate list for the scan
                                  (_build_candidates). 409 while a build is
                                  running
- POST /api/chat/batch        {cards: [...], ignored: [ids]} from a good
                                  run, {error: {step, message}} from a failed
                                  one. A good batch clears the error record
- POST /api/chat/deny         {attachment_id}: drop the card; the ledger
                                  keeps it from coming back
- POST /api/chat/apply        {items: [{attachment_id, folder}]}: one
                                  or many cards, all-or-nothing — any invalid
                                  item refuses the whole call. Runs export +
                                  move for each in one thread, in order
                                  (202). One apply at a time area-wide (409)
- POST /api/chat/hide         {attachment_id} for one saved card,
                                  {all: true} for every saved card: the card
                                  leaves the page before the next scan would
                                  drop it. The ledger entry stays
- POST /api/chat/scan-request start the chat-attach-scan cron job
                                  detached (the page's rescan key)
- POST /api/chat/reset        clear the ledger and drop every card — the
                                  next scan re-proposes whatever is still in
                                  the lookback window. 409 while a card is
                                  executing
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import common
from common import _log, _now

APP = pathlib.Path(__file__).resolve().parent
PROJECT = APP.parent.parent
sys.path.append(str(PROJECT / "services" / "mcp" / "common"))
import call_host_tool  # noqa: E402
import hermes_env  # noqa: E402
from files_client import (FILENAME_CAP, call_files, file_warning,  # noqa: E402
                          filing_folders, stored_name)

def state_file():
    return common.state_dir() / "chat.json"


ENV_FILE = APP / "chat.env"

CHAT_PORT = 8358   # host app's chat
CONTACTS_PORT = 8353   # host app's contacts server

# the cron job the page's rescan key starts (the run claims the job, so a
# second tap while one is going cannot fire it twice)
CRON_JOB = "chat-attach-scan"

STATUS_CAP = 300     # card status_text bound (the log bounds its result at 500)
REASON_CAP = 150     # LLM reason bound — the page shows it verbatim
TEXT_CAP = 500       # nearby message text per candidate
CONTENT_CAP = 2000   # extracted attachment text bound — shown under the card's chevron
PROBLEM_CAP = 300    # one problem line's tool answer, for the page's error record
# the files server's answer for a kind it never reads (a video): no text,
# not a failure
UNREADABLE_KIND = "no text extraction for"
LEDGER_DAYS = 30     # verdict memory; aged out on each batch save

CARD_KEYS = {"attachment_id", "filename", "folder", "reason", "sender",
             "chat", "received_at", "original_name", "kind",
             "sender_name", "text"}

LOCK = threading.Lock()   # serializes every state mutation
_SCAN_BUSY = False        # one candidates build at a time (LOCK-guarded)

# media kinds lose the sort to documents, so a photo flood can never push a
# document out of the cap; the LLM still judges the media that fits. kind is
# the attachment's mime type
MEDIA_PREFIXES = ("image/", "video/", "audio/")
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp",
              ".mov", ".mp4", ".m4v", ".mp3", ".m4a", ".aac", ".wav"}


# ---------------------------------------------------------------- state

def empty_state():
    return {"version": 1, "cards": [], "ledger": {},
            "senders": {},
            "last_scan_at": None, "last_scan_status": None, "error": None}


# boot() replaces this with the loaded file; the default keeps state() safe
# in tests that never boot the area
STATE = empty_state()


def save_state(state):
    """Caller holds LOCK."""
    common.write_json(state_file(), state)


def _find_card(state, attachment_id):
    return next((c for c in state["cards"]
                 if c["attachment_id"] == attachment_id), None)


def _trim_ledger(state):
    """Drop ledger entries older than LEDGER_DAYS. Returns the drop count.
    ISO Z stamps compare lexically. Caller holds LOCK."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LEDGER_DAYS))
    cutoff = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    before = len(state["ledger"])
    state["ledger"] = {k: v for k, v in state["ledger"].items()
                       if v.get("first_seen", "") >= cutoff}
    return before - len(state["ledger"])


# ---------------------------------------------------------------- configuration

def env_config():
    """The area's keys out of chat.env. A repeated key keeps its last
    value; WATCHED_CHATS is one comma-separated list."""
    values = hermes_env.read(ENV_FILE)
    raw = values.get("WATCHED_CHATS", "")
    chats = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        label, sep, identifiers = pair.partition("=")
        ids = [i.strip() for i in identifiers.split("|")]
        if not sep or not label.strip() or not all(ids):
            raise RuntimeError(f"malformed WATCHED_CHATS entry {pair!r} "
                               f"in {ENV_FILE} — expected label=chat_id"
                               f"[|chat_id...]")
        chats.append({"label": label.strip(), "chats": ids})
    if not chats:
        raise RuntimeError(f"WATCHED_CHATS missing or empty in {ENV_FILE}")
    return {"chats": chats,
            "lookback_hours": int(values.get("CHAT_LOOKBACK_HOURS", "72")),
            "cap": int(values.get("CHAT_CAP", "30"))}


# ---------------------------------------------------------------- tool layer
# Every actual MCP tool invocation lives behind these functions (files
# through call_files, imported from files_client), so tests stub exactly
# them.

def call_chat(tool, args):
    return call_host_tool.call_sync(CHAT_PORT, tool, args)


def call_contacts(tool, args):
    return call_host_tool.call_sync(CONTACTS_PORT, tool, args)


# ---------------------------------------------------------------- listing parsers
# chat speaks fenced human text; every parser fails closed.

class ListingError(Exception):
    pass


def _unfenced(text):
    """The payload lines of a fenced tool result."""
    return [l for l in text.splitlines() if "CHAT DATA" not in l]


ATT_HEADER = re.compile(
    r"^Attachments: (\d+)\.(?: Showing (\d+) from offset (\d+), newest first\.)?$")
ATT_LINE = re.compile(
    r"^- (.*) — ([^,]+), (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}), from (.*?), chat (.*)$")
ATT_ID = re.compile(r"^  id: (\d+)$")


def parse_attachments(text):
    """(total, [entry]) out of a list_attachments result. entry: {id, name,
    kind, ts, sender}. Fails closed: header and every line pair must parse."""
    lines = [l for l in _unfenced(text) if l.strip()]
    if not lines:
        raise ListingError("list_attachments result had no payload")
    m = ATT_HEADER.match(lines[0])
    if not m:
        raise ListingError(f"list_attachments header did not parse: {lines[0]!r}")
    total = int(m.group(1))
    if lines[1:] == ["(no attachments)"]:
        return total, []
    entries = []
    i = 1
    while i < len(lines):
        em = ATT_LINE.match(lines[i])
        if em is None or i + 1 >= len(lines) \
                or (im := ATT_ID.match(lines[i + 1])) is None:
            raise ListingError(f"list_attachments entry did not parse: {lines[i]!r}")
        entries.append({"id": int(im.group(1)), "name": em.group(1),
                        "kind": em.group(2), "ts": em.group(3),
                        "sender": em.group(4)})
        i += 2
    return total, entries


READ_HEADER = re.compile(
    r"^Chat .*: (\d+) messages, newest first\.(?: Showing (\d+) from offset (\d+)\.)?$")
READ_ENTRY = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — (.*)$")
READ_ATTS = re.compile(r"^  attachments: (.*)$")
READ_ATT_ID = re.compile(r"\(id (\d+)\)")


def parse_chat(text):
    """(total, [entry]) out of a read_chat result. entry: {ts, sender, text,
    attachment_ids}. Message text can span lines, so entries are split on the
    timestamp line and everything between two of them is one entry."""
    lines = [l for l in _unfenced(text) if l.strip()]
    if not lines:
        raise ListingError("read_chat result had no payload")
    m = READ_HEADER.match(lines[0])
    if not m:
        raise ListingError(f"read_chat header did not parse: {lines[0]!r}")
    total = int(m.group(1))
    if lines[1:] == ["(no messages)"]:
        return total, []
    entries = []
    for line in lines[1:]:
        em = READ_ENTRY.match(line)
        if em:
            entries.append({"ts": em.group(1), "sender": em.group(2),
                            "text": [], "attachment_ids": []})
            continue
        if not entries:
            raise ListingError(f"read_chat body line before any entry: {line!r}")
        am = READ_ATTS.match(line)
        if am:
            entries[-1]["attachment_ids"] = [int(i) for i in
                                             READ_ATT_ID.findall(am.group(1))]
        else:
            entries[-1]["text"].append(line.strip())
    for e in entries:
        e["text"] = "\n".join(e["text"])
    return total, entries


# ---------------------------------------------------------------- candidates build

def _is_media(name, kind):
    kind = kind.lower()
    if any(kind.startswith(p) for p in MEDIA_PREFIXES):
        return True
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS


def _attachments(chat, from_date):
    """Every list_attachments entry for the chat inside the date filter —
    pages advance by the entries actually parsed (_fit can print fewer than
    requested), until the accumulated count reaches the header's total."""
    out, offset = [], 0
    while True:
        ok, text = call_chat("list_attachments", {
            "chat": chat, "from_date": from_date, "limit": 200,
            "offset": offset})
        if not ok:
            raise ListingError(f"list_attachments failed: {text}")
        total, page = parse_attachments(text)
        out += page
        if len(out) >= total or not page:
            return out
        offset += len(page)


def _nearby(chat, oldest_ts):
    """read_chat entries for the chat, paged (newest first) until the oldest
    candidate's timestamp is covered or the history runs out."""
    out, offset = [], 0
    while True:
        ok, text = call_chat("read_chat", {
            "chat": chat, "limit": 200, "offset": offset})
        if not ok:
            raise ListingError(f"read_chat failed: {text}")
        total, page = parse_chat(text)
        out += page
        if not page or len(out) >= total or page[-1]["ts"] <= oldest_ts:
            return out
        offset += len(page)


def _context(entries, attachment_id):
    """The message carrying the attachment plus the entries around it (±5),
    as 'sender: text' lines, capped at TEXT_CAP characters."""
    pos = next((i for i, e in enumerate(entries)
                if attachment_id in e["attachment_ids"]), None)
    if pos is None:
        return ""
    lines = []
    for e in entries[max(0, pos - 5):pos + 6]:
        body = e["text"] or "(attachment)"
        lines.append(f"{e['sender']}: {body}")
    return "\n".join(lines)[:TEXT_CAP]


CONTACT_HIT = re.compile(r"^1\. (.+)$", re.M)


def _contact_name(sender):
    """(contact name, problem) for the sender: ('', None) when no
    contact matches, ('', the tool's failure text) when the lookup itself
    failed. Email-shaped senders search by email, the rest by phone. The
    result is fenced human text; the first match's line is '1. <name>'."""
    key = "email" if "@" in sender else "phone"
    ok, text = call_contacts("search_contacts", {key: sender})
    if not ok:
        return "", text[:PROBLEM_CAP]
    m = CONTACT_HIT.search(text)
    return (m.group(1).strip() if m else ""), None


def _resolve_senders(senders):
    """({sender: contact name}, [problem lines]) for the given senders — the
    state cache first (a number's owner does not change, so a hit is cached
    forever), one contacts call per miss. Misses stay uncached, so a contact
    added later resolves on a later scan. A failed lookup yields no name
    and one problem line for the page — the scan still runs."""
    with LOCK:
        cached = dict(STATE["senders"])
    names, new, problems = {}, {}, []
    for h in senders:
        name = cached.get(h)
        if name is None:
            name, problem = _contact_name(h)
            if problem:
                problems.append(f"sender name lookup failed for {h} — judged "
                                f"without a name: {problem}")
            if name:
                new[h] = name
        if name:
            names[h] = name
    if new:
        with LOCK:
            STATE["senders"].update(new)
            save_state(STATE)
    return names, problems


EXPORT_RE = re.compile(r"\(source '(/.+)'\)\.$")  # the answer's last part: the name may hold any text


def _body_text(result):
    """The payload of a files read_document result: fence lines, the header
    (the first line inside the fence: '<path> — chars x-y of z'), and the
    bracketed paging and text recognition notes dropped."""
    lines, header = [], True
    for l in result.splitlines():
        if "FILES DATA" in l:
            continue
        if header:
            header = False
            continue
        if l.startswith("[more —") or l.startswith("[text recognition in progress:"):
            continue
        lines.append(l)
    return "\n".join(lines).strip()


def _drop_copy(path):
    """Delete one of this area's own exports from the scratch folder: the
    service does it itself, not the agent. '' on success, else why not."""
    try:
        os.remove(path)
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    return ""


def _extract_text(attachment_id):
    """(text, problem) of one attachment: export into the scratch folder
    (the only way past the chat database's permissions), files
    read_document — the PDF text layer, else text recognition — then
    delete the copy.
    problem is None when the text was read, or when the file's kind has no
    text extraction at all (a video); otherwise the failing step's answer.
    A system update once broke text recognition and every image read as "no text";
    the problem line keeps that from hiding again."""
    ok, text = call_chat("export_attachment_to_scratch",
                             {"attachment_id": attachment_id})
    if not ok:
        return "", f"export failed: {text[:PROBLEM_CAP]}"
    m = EXPORT_RE.search(text)
    if m is None:
        return "", f"export answer did not parse: {text[:PROBLEM_CAP]}"
    path = m.group(1)
    ok, text = call_files("read_document", {"path": path})
    note = _drop_copy(path)
    if note:
        _log({"event": "chat_extract_cleanup",
              "args": {"attachment_id": attachment_id, "filename": path},
              "outcome": "failed", "result": note})
    if not ok:
        if UNREADABLE_KIND in text:
            return "", None
        return "", f"read failed: {text[:PROBLEM_CAP]}"
    return _body_text(text)[:CONTENT_CAP], None


def _build_candidates():
    """The candidates payload: watched-chat attachments minus the user's own
    and ledgered ones, documents first then media, newest first, capped —
    each with its nearby message text, the sender's contact name when one
    resolves, and the attachment's extracted text when it has any. An
    attachment whose text read failed is left out and named in problems,
    so it is neither judged nor ledgered and comes back next scan; a failed
    name lookup is named there too. Raises ListingError/RuntimeError on any
    listing failure."""
    cfg = env_config()
    cutoff = (datetime.now()
              - timedelta(hours=cfg["lookback_hours"])).strftime("%Y-%m-%d %H:%M:%S")
    from_date = cutoff[:10]
    with LOCK:
        ledgered = set(STATE["ledger"])
    found = {}
    for w in cfg["chats"]:
        for chat in w["chats"]:
            for e in _attachments(chat, from_date):
                if e["sender"] == "me" or e["ts"] < cutoff:
                    continue
                if str(e["id"]) in ledgered or e["id"] in found:
                    continue
                found[e["id"]] = {"attachment_id": e["id"], "sender": e["sender"],
                                  "chat": w["label"], "chat_id": chat,
                                  "received_at": e["ts"],
                                  "original_name": e["name"], "kind": e["kind"]}
    # stable sorts: newest first inside each class, documents before media
    candidates = sorted(found.values(), key=lambda c: c["received_at"],
                        reverse=True)
    candidates.sort(key=lambda c: _is_media(c["original_name"], c["kind"]))
    candidates = candidates[:cfg["cap"]]
    for chat in [c for w in cfg["chats"] for c in w["chats"]]:
        mine = [c for c in candidates if c["chat_id"] == chat]
        if not mine:
            continue
        oldest = min(c["received_at"] for c in mine)
        entries = _nearby(chat, oldest)
        for c in mine:
            c["context"] = _context(entries, c["attachment_id"])
    names, problems = _resolve_senders({c["sender"] for c in candidates})
    judged = []
    for c in candidates:
        c["sender_name"] = names.get(c["sender"], "")
        c["text"], problem = _extract_text(c["attachment_id"])
        if problem:
            _log({"event": "chat_extract_failed",
                  "args": {"attachment_id": c["attachment_id"],
                           "original_name": c["original_name"]},
                  "outcome": "failed", "result": problem})
            problems.append(f"text unreadable for {c['original_name']} (id "
                            f"{c['attachment_id']}) — not judged, retries "
                            f"each scan, ages out of the 72-hour window in "
                            f"about 3 days: {problem}")
            continue
        judged.append(c)
    folders = filing_folders()
    if not folders:
        raise ListingError("no filing folders under Papers and SharedPapers")
    return {"candidates": judged, "folders": folders, "problems": problems}


# ---------------------------------------------------------------- handlers

def _job_fields():
    """The cron job's last-run and next-run stamps, whether the last run
    failed, and the start time of a run going right now, from hermes' own
    cron files. Unreadable files -> None fields."""
    job = common.cron_job(CRON_JOB)
    return {"job_last_run_at": job.get("last_run_at") if job else None,
            "job_next_run_at": job.get("next_run_at") if job else None,
            "job_last_failed": bool(job and job.get("last_status")
                                    not in (None, "ok")),
            "job_running_since": common.job_running_since(job)}


def scan_request(body):
    """POST /api/chat/scan-request: start the cron job detached."""
    try:
        subprocess.Popen(["hermes", "cron", "run", CRON_JOB],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start hermes: {e}"}
    return 200, {"started": CRON_JOB}


def reset(state, body):
    """POST /api/chat/reset. Denied attachments come back too. The
    sender names and stamps stay. Caller holds LOCK."""
    if any(c["status"] == "in_progress" for c in state["cards"]):
        return 409, {"error": "a card is executing"}
    n = len(state["ledger"])
    dropped = len(state["cards"])
    state["ledger"] = {}
    state["cards"] = []
    _log({"event": "chat_ledger_reset",
          "args": {"cards_dropped": dropped},
          "result": f"{n} entries cleared"})
    save_state(state)
    return 200, {"ok": True, "cleared": n, "dropped": dropped}


def hide(state, body):
    """POST /api/chat/hide. Only success cards can be hidden: a card in
    any other status still has a decision waiting. Caller holds LOCK."""
    if body.get("all") is True:
        matched = [c for c in state["cards"] if c["status"] == "success"]
    elif type(body.get("attachment_id")) is int:
        c = _find_card(state, body["attachment_id"])
        if c is None:
            return 404, {"error": f"unknown card {body['attachment_id']!r}"}
        if c["status"] != "success":
            return 409, {"error": f"card is {c['status']}"}
        matched = [c]
    else:
        return 400, {"error": "pass attachment_id or all: true"}
    if matched:
        gone = {c["attachment_id"] for c in matched}
        state["cards"] = [c for c in state["cards"]
                          if c["attachment_id"] not in gone]
        _log({"event": "chat_cards_hidden", "rows": sorted(gone)})
        save_state(state)
    return 200, {"hidden": len(matched)}


def candidates(body):
    """POST /api/chat/candidates. The build can take a minute (each chat
    is two paged reads), so it is single-flight and runs outside LOCK."""
    global _SCAN_BUSY
    with LOCK:
        if _SCAN_BUSY:
            return 409, {"error": "a candidates build is running"}
        _SCAN_BUSY = True
    try:
        payload = _build_candidates()
    except (ListingError, RuntimeError, OSError, ValueError) as e:
        return 500, {"error": f"{type(e).__name__}: {e}"}
    finally:
        with LOCK:
            _SCAN_BUSY = False
    return 200, payload


def save_batch(state, body):
    """POST /api/chat/batch: cards from a good run, or an error record
    from a failed one. Caller holds LOCK."""
    err = body.get("error")
    if err is not None:
        if not isinstance(err, dict) or not isinstance(err.get("step"), str) \
                or not isinstance(err.get("message"), str) or not err["step"]:
            return 400, {"error": "error needs step and message strings"}
        if set(err) - {"step", "message"}:
            return 400, {"error": "error keys are step and message"}
        state["error"] = {"step": err["step"], "message": err["message"][:1000]}
        state["last_scan_at"] = _now()
        state["last_scan_status"] = "failed"
        _log({"event": "chat_batch_error", "step": err["step"],
              "result": err["message"]})
        save_state(state)
        return 200, {"ok": True}

    cards = body.get("cards")
    ignored = body.get("ignored")
    if not isinstance(cards, list) \
            or not isinstance(ignored, list) \
            or any(type(i) is not int for i in ignored):
        return 400, {"error": "cards must be a list and ignored a list of ids"}
    folders = set(filing_folders()) if cards else set()
    stored = []
    for c in cards:
        if not isinstance(c, dict):
            return 400, {"error": "each card must be an object"}
        unknown = set(c) - CARD_KEYS
        if unknown:
            return 400, {"error": f"unknown card keys: {sorted(unknown)}"}
        if type(c.get("attachment_id")) is not int:
            return 400, {"error": "card attachment_id must be an integer"}
        for k in ("filename", "folder", "sender", "chat", "received_at",
                  "original_name", "kind"):
            if not isinstance(c.get(k), str) or not c[k]:
                return 400, {"error": f"card {k} must be a non-empty string"}
        if "/" in c["filename"] or "\\" in c["filename"]:
            return 400, {"error": "card filename must be a plain file name — no folders"}
        if not isinstance(c.get("reason"), str):
            return 400, {"error": "card reason must be a string"}
        for k in ("sender_name", "text"):
            if not isinstance(c.get(k, ""), str):
                return 400, {"error": f"card {k} must be a string when given"}
        # the card shows the name the files server will store. The warning is
        # made first, while the name is still the one the scan chose.
        warning = file_warning(c["filename"], c["original_name"])
        filename = stored_name(c["filename"], c["original_name"])
        if len(filename) > FILENAME_CAP:
            return 400, {"error": f"card filename over {FILENAME_CAP} characters"}
        if c["folder"] not in folders:
            return 400, {"error": f"unknown folder {c['folder']!r}"}
        stored.append({"attachment_id": c["attachment_id"],
                       "filename": filename,
                       "folder": c["folder"],
                       "reason": c["reason"][:REASON_CAP],
                       "sender": c["sender"][:200], "chat": c["chat"][:200],
                       "received_at": c["received_at"][:200],
                       "original_name": c["original_name"][:200],
                       "kind": c["kind"][:200],
                       "sender_name": c.get("sender_name", "")[:200],
                       "text": c.get("text", "")[:CONTENT_CAP],
                       "status": "pending", "status_text": "",
                       **({"warning": warning} if warning else {})})
    ids = [c["attachment_id"] for c in stored]
    if len(set(ids)) != len(ids):
        return 400, {"error": "duplicate attachment_id in the batch"}
    if set(ids) & {i for i in ignored}:
        return 400, {"error": "an id cannot be both a card and ignored"}
    now = _now()
    kept = []
    for c in stored:
        key = str(c["attachment_id"])
        if key in state["ledger"]:
            _log({"event": "chat_card_dropped",
                  "args": {"attachment_id": c["attachment_id"]},
                  "outcome": "dropped", "result": "already in the ledger"})
            continue
        state["ledger"][key] = {"state": "proposed", "first_seen": now}
        kept.append(c)
    for i in ignored:
        state["ledger"].setdefault(str(i), {"state": "ignored",
                                            "first_seen": now})
    # success cards have said all they had to say — the ledger, not the card,
    # is the memory; without this the list would grow forever
    settled = sum(1 for c in state["cards"] if c["status"] == "success")
    if settled:
        state["cards"] = [c for c in state["cards"] if c["status"] != "success"]
    trimmed = _trim_ledger(state)
    state["cards"] += kept
    state["error"] = None
    state["last_scan_at"] = now
    state["last_scan_status"] = "ok"
    _log({"event": "chat_batch_saved",
          "rows": [c["attachment_id"] for c in kept],
          "args": {"ignored": len(ignored), "success_cleared": settled,
                   "ledger_trimmed": trimmed}})
    save_state(state)
    return 200, {"ok": True, "count": len(kept)}


def deny(state, body):
    """POST /api/chat/deny. Caller holds LOCK."""
    attachment_id = body.get("attachment_id")
    if type(attachment_id) is not int:
        return 400, {"error": "attachment_id must be an integer"}
    c = _find_card(state, attachment_id)
    if c is None:
        return 404, {"error": f"unknown card {attachment_id!r}"}
    if c["status"] == "in_progress":
        return 409, {"error": "card is executing"}
    state["cards"] = [cc for cc in state["cards"] if cc is not c]
    _log({"event": "chat_card_denied",
          "args": {"attachment_id": attachment_id},
          "outcome": "denied",
          "result": f"denied in status {c['status']}"})
    save_state(state)
    return 200, {"ok": True}


def apply(state, body):
    """POST /api/chat/apply. Every item is checked before any card is
    marked. Caller holds LOCK; the saves start after in_progress is saved."""
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return 400, {"error": "items must be a non-empty list"}
    # one apply at a time area-wide — the page disables the save keys while
    # one works; this 409 is the real rule (second tab, curl)
    if any(cc["status"] == "in_progress" for cc in state["cards"]):
        return 409, {"error": "another card is executing"}
    folders = set(filing_folders())
    work, cards = [], []
    for item in items:
        if not isinstance(item, dict):
            return 400, {"error": "each item must be an object"}
        attachment_id = item.get("attachment_id")
        if type(attachment_id) is not int:
            return 400, {"error": "attachment_id must be an integer"}
        c = _find_card(state, attachment_id)
        if c is None:
            return 404, {"error": f"unknown card {attachment_id!r}"}
        if c in cards:
            return 400, {"error": "duplicate attachment_id in the items"}
        if c["status"] not in ("pending", "run_failed"):
            return 409, {"error": f"card {attachment_id} is {c['status']}"}
        folder = item.get("folder")
        if not isinstance(folder, str) or folder not in folders:
            return 400, {"error": f"unknown folder {folder!r}"}
        work.append((attachment_id, folder))
        cards.append(c)
    for c in cards:
        c["status"] = "in_progress"
        c["status_text"] = "working"
    save_state(state)
    _spawn(work)
    return 202, {"ok": True, "count": len(work)}


def _spawn(work):
    """The approved cards' execution thread, one card after another —
    module-level so tests can run executions synchronously."""
    threading.Thread(target=_run, args=(work,), daemon=True).start()


def _run(work):
    for attachment_id, folder in work:
        _execute(attachment_id, folder)


def _execute(attachment_id, folder):
    """One approved card: export the attachment into the scratch folder, then
    move it out into the chosen folder. The copy's path comes from the
    export's SUCCESS line (a collision renames to -2, so it is never
    assumed); a failed move deletes the orphaned copy so the retry starts
    clean."""
    outcome, text = "run_failed", ""
    ok, text = call_chat("export_attachment_to_scratch",
                             {"attachment_id": attachment_id})
    if ok:
        m = EXPORT_RE.search(text)
        if m is None:
            ok, text = False, f"FAILED: export result did not parse: {text[:200]}"
        else:
            copy = m.group(1)
    if ok:
        with LOCK:
            card = _find_card(STATE, attachment_id)
            filename = card["filename"] if card else ""
        ok, text = call_files("move", {"source": copy,
                                       "destination": os.path.join(folder, filename)})
        if not ok:
            note = _drop_copy(copy)
            if note:
                text += f" · the scratch copy cleanup failed too: {note}"
    if ok:
        outcome = "success"
    with LOCK:
        c = _find_card(STATE, attachment_id)
        if c is not None:
            c["status"] = outcome
            c["status_text"] = text[:STATUS_CAP]
        if outcome == "success":
            entry = STATE["ledger"].get(str(attachment_id))
            if entry is not None:
                entry["state"] = "saved"
            else:
                STATE["ledger"][str(attachment_id)] = {"state": "saved",
                                                       "first_seen": _now()}
        _log({"event": "chat_card_resolved",
              "args": {"attachment_id": attachment_id,
                       "folder": folder},
              "outcome": outcome, "result": text})
        save_state(STATE)


# ---------------------------------------------------------------- area interface

NAME = "chat"


def boot():
    """Load the state file and settle cards a crash left in_progress: the
    write may or may not have landed, so they go to run_failed with a retry
    note (a save that did land surfaces as a name collision on the retry,
    which is already a run_failed with the tool's text). No MCP I/O here:
    an unreachable server at boot would hold the whole page down."""
    global STATE
    loaded = None
    if state_file().exists():
        loaded = json.loads(state_file().read_text())
    if loaded is None:
        with LOCK:
            save_state(STATE)
    else:
        STATE = loaded
    stuck = [c for c in STATE["cards"] if c["status"] == "in_progress"]
    if not stuck:
        return
    with LOCK:
        for c in stuck:
            c["status"] = "run_failed"
            c["status_text"] = "interrupted by a service restart — approve to retry"
            _log({"event": "chat_card_resolved",
                  "args": {"attachment_id": c["attachment_id"]},
                  "outcome": "reconciled",
                  "result": "boot settle: interrupted in_progress"})
        save_state(STATE)


def state():
    """The chat part of GET /api/state: the cards, the scan stamps and
    error record, the filing folders for the page's dropdowns while a card
    waits, and the cron job's stamps. The folder walk and the jobs-file
    reads run outside LOCK, so a poll never queues behind their I/O."""
    with LOCK:
        out = {"cards": [dict(c) for c in STATE["cards"]],
               "error": STATE["error"],
               "last_scan_at": STATE["last_scan_at"],
               "last_scan_status": STATE["last_scan_status"]}
    out["folders"] = filing_folders() if out["cards"] else []
    out.update(_job_fields())
    return out


def _h_candidates(body):
    return candidates(body)


def _h_scan_request(body):
    return scan_request(body)


def _h_reset(body):
    with LOCK:
        return reset(STATE, body)


def _h_hide(body):
    with LOCK:
        return hide(STATE, body)


def _h_batch(body):
    with LOCK:
        return save_batch(STATE, body)


def _h_deny(body):
    with LOCK:
        return deny(STATE, body)


def _h_apply(body):
    with LOCK:
        return apply(STATE, body)


HANDLERS = {"/api/chat/candidates": _h_candidates,
            "/api/chat/batch": _h_batch,
            "/api/chat/deny": _h_deny,
            "/api/chat/apply": _h_apply,
            "/api/chat/hide": _h_hide,
            "/api/chat/scan-request": _h_scan_request,
            "/api/chat/reset": _h_reset}
