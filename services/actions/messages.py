"""Messages area — save proposals for attachments in watched Apple Messages
chats: card store and the approve path.

Served by server.py (one process, one page). The messages-attach-scan cron
job (messages_scan.py — a --no-agent script) drives the scan: it calls
/api/messages/candidates, makes one structured LLM call, and posts the
verdicts to /api/messages/batch. This module owns all MCP I/O — the cron
python has no Full Disk Access, so chat.db reads go through the host app's
macos_messages server (streamable HTTP, 8853), sender names come from its
macos_contacts server (3119), and records writes go through
the records server (one short stdio session per call; the calls are one
list_locations per scan and one save_file per approve, so no long-lived
child is worth it).

One attachment is one card: proposed filename, the LLM's location pick, a
one-line reason, and the source facts (sender, chat label, received date,
original name, kind). The page offers a destination dropdown from the cached
records catalog; the chosen location travels with the approve call and is
validated against the cache here — it is never stored card data. Approving
runs export_attachment_to_inbox (copy into the records inbox) then save_file (move
out of the inbox into the location). Denying drops the card; a success card
leaves at the next scan. run_failed cards stay approvable: export only
copies, and a failed save_file already deleted the inbox copy, so an
approve is always a clean retry.

The ledger (state/messages.json, keyed by attachment ROWID) is the whole
scan memory: every verdict lands there (ignored or proposed), so a scanned
attachment is never proposed again — denied ones included. Entries are kept
for LEDGER_DAYS (30), trimmed on each batch save; an aged-out entry belongs
to an attachment weeks past the 72-hour scan lookback, so nothing can come
back. A candidate the LLM omits gets no ledger entry and simply re-appears
next scan.

macos_messages returns fenced human text, not JSON, so the candidates build
parses its line formats and fails closed when a page does not parse or the
parsed entries cannot reach the header's total. Paging advances the offset
by the entries actually parsed — _fit can return fewer than requested.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/messages/candidates   build the candidate list: attachments in
                                  the watched chats newer than the lookback,
                                  minus the user's own, minus ledgered ones —
                                  documents first, then media, newest first,
                                  capped — each with the nearby message text
                                  from read_chat, the sender's contact name
                                  (cached in state: a number's owner does not
                                  change), and the attachment's extracted
                                  text (export to the records inbox,
                                  read_file, delete). Refreshes the locations
                                  cache. 409 while a build is running
- POST /api/messages/batch        {cards: [...], ignored: [ids]} from a good
                                  run, {error: {step, message}} from a failed
                                  one. A good batch clears the error record
- POST /api/messages/deny         {attachment_id}: drop the card; the ledger
                                  keeps it from coming back
- POST /api/messages/apply        {items: [{attachment_id, location_id}]}: one
                                  or many cards, all-or-nothing — any invalid
                                  item refuses the whole call. Runs export +
                                  save_file for each in one thread, in order
                                  (202). One apply at a time area-wide (409)
- POST /api/messages/hide         {attachment_id} for one saved card,
                                  {all: true} for every saved card: the card
                                  leaves the page before the next scan would
                                  drop it. The ledger entry stays
- POST /api/messages/scan-request start the messages-attach-scan cron job
                                  detached (the page's rescan key)
- POST /api/messages/reset        clear the ledger and drop every card — the
                                  next scan re-proposes whatever is still in
                                  the lookback window. 409 while a card is
                                  executing
"""

import asyncio
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
IRIS = APP.parent.parent
sys.path.append(str(IRIS / "services" / "mcp" / "common"))
import call_host_tool  # noqa: E402
import hermes_env  # noqa: E402

STATE_FILE = common.STATE_DIR / "messages.json"
ENV_FILE = APP / "messages.env"

MESSAGES_PORT = 8853   # host app's macos_messages
CONTACTS_PORT = 3119   # host app's macos_contacts
RECORDS_SERVER = IRIS / "services" / "mcp" / "records" / "server.py"
UV = "/Users/me/.local/bin/uv"
TOOL_TIMEOUT = 45
FAILURE_MARKERS = ("FAILED:", "REJECTED:", "PARTIAL:")

# the cron job the page's rescan key starts (the run claims the job, so a
# second tap while one is going cannot fire it twice)
CRON_JOB = "messages-attach-scan"

STATUS_CAP = 300     # card status_text bound (the log bounds its result at 500)
REASON_CAP = 150     # LLM reason bound — the page shows it verbatim
FILENAME_CAP = 200   # records' own bound; enforced there too
TEXT_CAP = 500       # nearby message text per candidate
CONTENT_CAP = 2000   # extracted attachment text bound — shown under the card's chevron
LEDGER_DAYS = 30     # verdict memory; aged out on each batch save

CARD_KEYS = {"attachment_id", "filename", "location_id", "reason", "sender",
             "chat", "received_at", "original_name", "kind",
             "sender_name", "text"}

LOCK = threading.Lock()   # serializes every state mutation
_SCAN_BUSY = False        # one candidates build at a time (LOCK-guarded)

# media kinds lose the sort to documents, so a photo flood can never push a
# document out of the cap; the LLM still judges the media that fits. kind is
# the attachment's mime type, falling back to its UTI in the database
MEDIA_PREFIXES = ("image/", "video/", "audio/")
MEDIA_UTIS = {"public.jpeg", "public.png", "public.heic", "public.heif",
              "public.tiff", "public.mpeg-4", "public.mov", "public.mp3",
              "com.apple.quicktime-movie", "com.compuserve.gif"}
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp",
              ".mov", ".mp4", ".m4v", ".mp3", ".m4a", ".aac", ".wav"}


# ---------------------------------------------------------------- state

def empty_state():
    return {"version": 1, "cards": [], "ledger": {},
            "locations": [], "locations_at": None,
            "senders": {},
            "last_scan_at": None, "last_scan_status": None, "error": None}


# boot() replaces this with the loaded file; the default keeps state() safe
# in tests that never boot the area
STATE = empty_state()


def save_state(state):
    """Caller holds LOCK."""
    common.write_json(STATE_FILE, state)


def _find_card(state, attachment_id):
    return next((c for c in state["cards"]
                 if c["attachment_id"] == attachment_id), None)


def _trim_ledger(state):
    """Drop ledger entries older than LEDGER_DAYS. Returns the drop count.
    first_seen is within the scan lookback of the attachment's arrival, so an
    aged-out entry belongs to an attachment long past any scan window.
    ISO Z stamps compare lexically. Caller holds LOCK."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LEDGER_DAYS))
    cutoff = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    before = len(state["ledger"])
    state["ledger"] = {k: v for k, v in state["ledger"].items()
                       if v.get("first_seen", "") >= cutoff}
    return before - len(state["ledger"])


# ---------------------------------------------------------------- configuration

def env_config():
    """The area's keys out of messages.env (plain KEY=VALUE parse — the same
    semantics as finance_jobs's env_config, so repeated keys collapse to the
    last one and MESSAGES_CHATS is one comma-separated list)."""
    values = hermes_env.read(ENV_FILE)
    raw = values.get("MESSAGES_CHATS", "")
    chats = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        label, sep, identifier = pair.partition("=")
        if not sep or not label.strip() or not identifier.strip():
            raise RuntimeError(f"malformed MESSAGES_CHATS entry {pair!r} "
                               f"in {ENV_FILE} — expected label=chat_identifier")
        chats.append({"label": label.strip(), "chat": identifier.strip()})
    if not chats:
        raise RuntimeError(f"MESSAGES_CHATS missing or empty in {ENV_FILE}")
    return {"chats": chats,
            "lookback_hours": int(values.get("MESSAGES_LOOKBACK_HOURS", "72")),
            "cap": int(values.get("MESSAGES_CAP", "30"))}


# ---------------------------------------------------------------- tool layer
# Every actual MCP tool invocation lives behind these three functions, so
# tests stub exactly them.

def _result_ok(result):
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    ok = not result.isError and not text.lstrip().startswith(FAILURE_MARKERS)
    return ok, text


def call_host(port, tool, args):
    """(ok, text) from one call to one of the host app's servers — common's
    call_host_tool, any exception turned into a FAILED: text."""
    try:
        text, ok = asyncio.run(call_host_tool.call_host_tool(port, tool, args))
    except Exception as e:
        return False, f"FAILED: {type(e).__name__}: {e}"
    return ok, text


def call_messages(tool, args):
    return call_host(MESSAGES_PORT, tool, args)


def call_contacts(tool, args):
    return call_host(CONTACTS_PORT, tool, args)


def call_records(tool, args):
    """(ok, text) from one stdio call to the records server, one short-lived
    session per call (uv pinned absolutely — launchd's PATH is minimal)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def call():
        params = StdioServerParameters(
            command=UV, args=["run", "--no-project", str(RECORDS_SERVER)])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await session.call_tool(tool, args)

    try:
        result = asyncio.run(asyncio.wait_for(call(), TOOL_TIMEOUT))
    except Exception as e:
        return False, f"FAILED: {type(e).__name__}: {e}"
    return _result_ok(result)


# ---------------------------------------------------------------- listing parsers
# macos_messages speaks fenced human text; every parser fails closed.

class ListingError(Exception):
    pass


def _unfenced(text):
    """The payload lines of a fenced tool result."""
    return [l for l in text.splitlines() if "MESSAGES DATA" not in l]


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


LOC_LINE = re.compile(r"^- (.*) \(([^()]+)\)$")


def parse_locations(text):
    """[{id, label}] out of a list_locations result."""
    out = []
    for line in text.splitlines():
        m = LOC_LINE.match(line.strip())
        if m:
            out.append({"id": m.group(2), "label": m.group(1)})
    if not out:
        raise ListingError("list_locations returned no parseable locations")
    return out


# ---------------------------------------------------------------- candidates build

def _is_media(name, kind):
    kind = kind.lower()
    if any(kind.startswith(p) for p in MEDIA_PREFIXES) or kind in MEDIA_UTIS:
        return True
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS


def _attachments(chat, from_date):
    """Every list_attachments entry for the chat inside the date filter —
    pages advance by the entries actually parsed (_fit can print fewer than
    requested), until the accumulated count reaches the header's total."""
    out, offset = [], 0
    while True:
        ok, text = call_messages("list_attachments", {
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
        ok, text = call_messages("read_chat", {
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


def _contact_name(handle):
    """The sender handle's contact name, '' when unresolved or the lookup
    fails. Email-shaped handles search by email, the rest by phone. The
    result is fenced human text; the first match's line is '1. <name>'."""
    key = "email" if "@" in handle else "phone"
    ok, text = call_contacts("search_contacts", {key: handle})
    if not ok:
        return ""
    m = CONTACT_HIT.search(text)
    return m.group(1).strip() if m else ""


def _resolve_senders(handles):
    """{handle: contact name} for the given handles — the state cache first
    (a number's owner does not change, so a hit is cached forever), one
    contacts call per miss. Misses stay uncached, so a contact added later
    resolves on a later scan. Lookup failures yield no name, never an error."""
    with LOCK:
        cached = dict(STATE["senders"])
    names, new = {}, {}
    for h in handles:
        name = cached.get(h)
        if name is None:
            name = _contact_name(h)
            if name:
                new[h] = name
        if name:
            names[h] = name
    if new:
        with LOCK:
            STATE["senders"].update(new)
            save_state(STATE)
    return names


EXPORT_RE = re.compile(r"copied to (inbox/.+?) — file it with")


def _body_text(result):
    """The payload of a records read_file result: fence lines, the 'Inbox
    (inbox) — … chars x-y of z' header, and the bracketed paging/OCR notes
    dropped."""
    lines = []
    for l in result.splitlines():
        if "RECORDS DATA" in l or l.startswith("Inbox (inbox) — "):
            continue
        if l.startswith("[more —") or l.startswith("[OCR in progress:"):
            continue
        lines.append(l)
    return "\n".join(lines).strip()


def _extract_text(attachment_id):
    """Best-effort text of one attachment, through the records tools: export
    into the inbox (the only way past chat.db's permissions), read_file —
    the PDF text layer, else Vision OCR — then delete the inbox copy. '' on
    any failure: one unreadable attachment never fails the build."""
    ok, text = call_messages("export_attachment_to_inbox",
                             {"attachment_id": attachment_id})
    if not ok:
        return ""
    m = EXPORT_RE.search(text)
    if m is None:
        return ""
    name = m.group(1)[len("inbox/"):]
    ok, text = call_records("read_file", {"location_id": "inbox",
                                          "filename": name})
    ok2, note = call_records("delete_file", {"location_id": "inbox",
                                             "filenames": [name]})
    if not ok2:
        _log({"event": "messages_extract_cleanup",
              "args": {"attachment_id": attachment_id, "filename": name},
              "outcome": "failed", "result": note})
    if not ok:
        return ""
    return _body_text(text)[:CONTENT_CAP]


def _build_candidates():
    """The candidates payload: watched-chat attachments minus the user's own
    and ledgered ones, documents first then media, newest first, capped —
    each with its nearby message text, the sender's contact name when one
    resolves, and the attachment's extracted text when it has any. Also
    refreshes the locations cache. Raises ListingError/RuntimeError on any
    read failure."""
    cfg = env_config()
    cutoff = (datetime.now()
              - timedelta(hours=cfg["lookback_hours"])).strftime("%Y-%m-%d %H:%M:%S")
    from_date = cutoff[:10]
    with LOCK:
        ledgered = set(STATE["ledger"])
    found = {}
    for w in cfg["chats"]:
        for e in _attachments(w["chat"], from_date):
            if e["sender"] == "me" or e["ts"] < cutoff:
                continue
            if str(e["id"]) in ledgered or e["id"] in found:
                continue
            found[e["id"]] = {"attachment_id": e["id"], "sender": e["sender"],
                              "chat": w["label"], "received_at": e["ts"],
                              "original_name": e["name"], "kind": e["kind"]}
    # stable sorts: newest first inside each class, documents before media
    candidates = sorted(found.values(), key=lambda c: c["received_at"],
                        reverse=True)
    candidates.sort(key=lambda c: _is_media(c["original_name"], c["kind"]))
    candidates = candidates[:cfg["cap"]]
    for w in cfg["chats"]:
        mine = [c for c in candidates if c["chat"] == w["label"]]
        if not mine:
            continue
        oldest = min(c["received_at"] for c in mine)
        entries = _nearby(w["chat"], oldest)
        for c in mine:
            c["context"] = _context(entries, c["attachment_id"])
    names = _resolve_senders({c["sender"] for c in candidates})
    for c in candidates:
        c["sender_name"] = names.get(c["sender"], "")
        c["text"] = _extract_text(c["attachment_id"])
    ok, text = call_records("list_locations", {})
    if ok:
        try:
            locations = parse_locations(text)
        except ListingError:
            locations = None
        if locations:
            with LOCK:
                STATE["locations"] = locations
                STATE["locations_at"] = _now()
                save_state(STATE)
    with LOCK:
        locations = list(STATE["locations"])
    if not locations:
        raise ListingError(f"records catalog unavailable: {text}")
    return {"candidates": candidates, "locations": locations}


# ---------------------------------------------------------------- handlers

def _job_fields():
    """last-run stamp, whether that run failed, and the start time of a run
    going right now — from hermes' own cron files (same read as the finance
    area). Unreadable files -> None fields."""
    job = common.cron_job(CRON_JOB)
    return {"job_last_run_at": job.get("last_run_at") if job else None,
            "job_next_run_at": job.get("next_run_at") if job else None,
            "job_last_failed": bool(job and job.get("last_status")
                                    not in (None, "ok")),
            "job_running_since": common.job_running_since(job)}


def scan_request(body):
    """POST /api/messages/scan-request: start the cron job detached."""
    try:
        subprocess.Popen(["hermes", "cron", "run", CRON_JOB],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start hermes: {e}"}
    return 200, {"started": CRON_JOB}


def reset(state, body):
    """POST /api/messages/reset: clear the ledger — the scan's whole memory —
    and drop every card on the page, so the next run re-proposes whatever is
    still in the lookback window (denied attachments included). The caches
    and stamps stay: a run in flight still posts its batch against the
    locations cache. 409 while a card is executing. Caller holds LOCK."""
    if any(c["status"] == "in_progress" for c in state["cards"]):
        return 409, {"error": "a card is executing"}
    n = len(state["ledger"])
    dropped = len(state["cards"])
    state["ledger"] = {}
    state["cards"] = []
    _log({"event": "messages_ledger_reset",
          "args": {"cards_dropped": dropped},
          "result": f"{n} entries cleared"})
    save_state(state)
    return 200, {"ok": True, "cleared": n, "dropped": dropped}


def hide(state, body):
    """POST /api/messages/hide: {attachment_id} for one saved card,
    {all: true} for every saved card on the page. The next scan drops success
    cards anyway — this takes them off sooner. The ledger entry stays, so a
    hidden attachment is never proposed again. A card in any other status
    still has a decision waiting, so only success can be hidden. Caller holds
    LOCK."""
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
        _log({"event": "messages_cards_hidden", "rows": sorted(gone)})
        save_state(state)
    return 200, {"hidden": len(matched)}


def candidates(body):
    """POST /api/messages/candidates. The build can take a minute (each chat
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
    """POST /api/messages/batch: cards from a good run, or an error record
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
        _log({"event": "messages_batch_error", "step": err["step"],
              "result": err["message"]})
        save_state(state)
        return 200, {"ok": True}

    cards = body.get("cards")
    ignored = body.get("ignored")
    if not isinstance(cards, list) \
            or not isinstance(ignored, list) \
            or any(type(i) is not int for i in ignored):
        return 400, {"error": "cards must be a list and ignored a list of ids"}
    locations = {l["id"] for l in state["locations"]}
    if cards and not locations:
        return 503, {"error": "no records catalog yet — run a scan first"}
    stored = []
    for c in cards:
        if not isinstance(c, dict):
            return 400, {"error": "each card must be an object"}
        unknown = set(c) - CARD_KEYS
        if unknown:
            return 400, {"error": f"unknown card keys: {sorted(unknown)}"}
        if type(c.get("attachment_id")) is not int:
            return 400, {"error": "card attachment_id must be an integer"}
        for k in ("filename", "location_id", "sender", "chat", "received_at",
                  "original_name", "kind"):
            if not isinstance(c.get(k), str) or not c[k]:
                return 400, {"error": f"card {k} must be a non-empty string"}
        if not isinstance(c.get("reason"), str):
            return 400, {"error": "card reason must be a string"}
        for k in ("sender_name", "text"):
            if not isinstance(c.get(k, ""), str):
                return 400, {"error": f"card {k} must be a string when given"}
        if len(c["filename"]) > FILENAME_CAP:
            return 400, {"error": f"card filename over {FILENAME_CAP} characters"}
        if c["location_id"] not in locations:
            return 400, {"error": f"unknown location_id {c['location_id']!r}"}
        stored.append({"attachment_id": c["attachment_id"],
                       "filename": c["filename"],
                       "location_id": c["location_id"],
                       "reason": c["reason"][:REASON_CAP],
                       "sender": c["sender"][:200], "chat": c["chat"][:200],
                       "received_at": c["received_at"][:200],
                       "original_name": c["original_name"][:200],
                       "kind": c["kind"][:200],
                       "sender_name": c.get("sender_name", "")[:200],
                       "text": c.get("text", "")[:CONTENT_CAP],
                       "status": "pending", "status_text": ""})
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
            _log({"event": "messages_card_dropped",
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
    _log({"event": "messages_batch_saved",
          "rows": [c["attachment_id"] for c in kept],
          "args": {"ignored": len(ignored), "success_cleared": settled,
                   "ledger_trimmed": trimmed}})
    save_state(state)
    return 200, {"ok": True, "count": len(kept)}


def deny(state, body):
    """POST /api/messages/deny: drop the card. The ledger entry stays, so the
    attachment is never proposed again. Caller holds LOCK."""
    attachment_id = body.get("attachment_id")
    if type(attachment_id) is not int:
        return 400, {"error": "attachment_id must be an integer"}
    c = _find_card(state, attachment_id)
    if c is None:
        return 404, {"error": f"unknown card {attachment_id!r}"}
    if c["status"] == "in_progress":
        return 409, {"error": "card is executing"}
    state["cards"] = [cc for cc in state["cards"] if cc is not c]
    _log({"event": "messages_card_denied",
          "args": {"attachment_id": attachment_id},
          "outcome": "denied",
          "result": f"denied in status {c['status']}"})
    save_state(state)
    return 200, {"ok": True}


def apply(state, body):
    """POST /api/messages/apply: one or many items, all-or-nothing — any
    invalid item refuses the whole call before anything is marked. Each
    location rides with its item and is validated against the cached
    catalog — it is not stored card data. Caller holds LOCK; the saves run
    one after another in a single thread after in_progress is persisted."""
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return 400, {"error": "items must be a non-empty list"}
    # one apply at a time area-wide — the page disables the save keys while
    # one works; this 409 is the real rule (second tab, curl)
    if any(cc["status"] == "in_progress" for cc in state["cards"]):
        return 409, {"error": "another card is executing"}
    labels = {l["id"]: l["label"] for l in state["locations"]}
    if not labels:
        return 503, {"error": "no records catalog yet — run a scan first"}
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
        location_id = item.get("location_id")
        if not isinstance(location_id, str) or not location_id.strip():
            return 400, {"error": "location_id is required"}
        location_id = location_id.strip()
        if location_id not in labels:
            return 400, {"error": f"unknown location_id {location_id!r}"}
        work.append((attachment_id, location_id))
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
    for attachment_id, location_id in work:
        _execute(attachment_id, location_id)


def _execute(attachment_id, location_id):
    """One approved card: export the attachment into the records inbox, then
    save_file it into the chosen location. The inbox name comes from the
    export's SUCCESS line (a collision renames to -2, so it is never
    assumed); a failed save deletes the orphaned inbox copy so the retry
    starts clean."""
    outcome, text = "run_failed", ""
    ok, text = call_messages("export_attachment_to_inbox",
                             {"attachment_id": attachment_id})
    if ok:
        m = EXPORT_RE.search(text)
        if m is None:
            ok, text = False, f"FAILED: export result did not parse: {text[:200]}"
        else:
            inbox_name = m.group(1)
    if ok:
        with LOCK:
            card = _find_card(STATE, attachment_id)
            filename = card["filename"] if card else ""
        ok, text = call_records("save_file", {
            "location_id": location_id, "source_path": inbox_name,
            "filename": filename})
        if not ok:
            ok2, text2 = call_records("delete_file", {
                "location_id": "inbox", "filenames":
                    [inbox_name[len("inbox/"):]]})
            if not ok2:
                text += f" · the inbox copy cleanup failed too: {text2}"
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
        _log({"event": "messages_card_resolved",
              "args": {"attachment_id": attachment_id,
                       "location_id": location_id},
              "outcome": outcome, "result": text})
        save_state(STATE)


# ---------------------------------------------------------------- area interface

NAME = "messages"


def boot():
    """Load the state file and settle cards a crash left in_progress: the
    write may or may not have landed, so they go to run_failed with a retry
    note (a save that did land surfaces as a name collision on the retry,
    which is already a run_failed with the tool's text). No MCP I/O here —
    a cold records child at boot would hold the whole page down."""
    global STATE
    loaded = None
    if STATE_FILE.exists():
        loaded = json.loads(STATE_FILE.read_text())
    if loaded is None:
        with LOCK:
            save_state(STATE)
    else:
        STATE = loaded
        STATE.setdefault("senders", {})   # state files from before the cache
    stuck = [c for c in STATE["cards"] if c["status"] == "in_progress"]
    if not stuck:
        return
    with LOCK:
        for c in stuck:
            c["status"] = "run_failed"
            c["status_text"] = "interrupted by a service restart — approve to retry"
            _log({"event": "messages_card_resolved",
                  "args": {"attachment_id": c["attachment_id"]},
                  "outcome": "reconciled",
                  "result": "boot settle: interrupted in_progress"})
        save_state(STATE)


def state():
    """The messages part of GET /api/state: the cards, the scan stamps and
    error record, the cached records catalog for the page's dropdowns, and
    the cron job's stamps. The jobs-file reads run outside LOCK, so a poll
    never queues behind their I/O."""
    with LOCK:
        out = {"cards": [dict(c) for c in STATE["cards"]],
               "error": STATE["error"],
               "last_scan_at": STATE["last_scan_at"],
               "last_scan_status": STATE["last_scan_status"],
               "locations": [dict(l) for l in STATE["locations"]]}
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


HANDLERS = {"/api/messages/candidates": _h_candidates,
            "/api/messages/batch": _h_batch,
            "/api/messages/deny": _h_deny,
            "/api/messages/apply": _h_apply,
            "/api/messages/hide": _h_hide,
            "/api/messages/scan-request": _h_scan_request,
            "/api/messages/reset": _h_reset}
