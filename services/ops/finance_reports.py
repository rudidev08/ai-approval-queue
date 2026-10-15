"""Finance reports area — work on the finance emails: build a kind's whole
email or one part of it, from demo data or the budget, and set the
kind's preset that family emails sends.

The page picks a kind (daily, weekly, monthly), the data (demo, now, or a
past day or month) and the sections and options. A build runs
finance_email.py through family_emails.finance_build, on its own thread;
one build or draft runs at a time. A real build reads no seen list and no
printed ids, so the daily's outliers and new transactions print the whole
7-day window under a note that says so. Demo data reads no budget and
calls no model. Each row's last build is kept in this process with what
it was built from, so a service restart drops them all. The make-default
key posts to family emails' /preset; this module stores no preset.

Draft puts a row's built text into the main account's Drafts folder, addressed to
the picked person's family emails address, through draft_mail.py (one
attempt: someone is waiting on the row). Nothing is sent. Save writes
the text to report_dir() as markdown, named by the day or month, the
kind, the options and the part:
<label>-finance-<kind>[-details][-personal][-no-new-payees][-<part>].md.
Neither takes a demo build.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/finance-reports/build  {"kind", "part", "data", "settings"}:
  part is "email" (the whole email), a section of the kind, or Links;
  data is "demo", "now", a past day (YYYY-MM-DD, daily and weekly) or a
  month (YYYY-MM, monthly); settings are the sections and options
  (family_emails.settings). 409 while a build or a draft is going, and
  while the assistant's model runs are paused when the build would call the model
  (a real build with Summary)
- POST /api/finance-reports/draft  {"kind", "part", "person"}. 409 while a
  build or a draft is going, 400 with no real build or no saved address
- POST /api/finance-reports/save  {"kind", "part"}; answers {"path": ...}.
  400 with no real build
"""

import pathlib
import re
import subprocess
import threading
from datetime import date

import family_emails
from common import _now
from data_root import root
from finance_email import LINKS, SECTIONS
from finance_jobs import cash_flow

NAME = "finance_reports"
APP = pathlib.Path(__file__).resolve().parent
DRAFT_MAIL = APP.parent / "mcp" / "mail_tools" / "draft_mail.py"
DRAFT_TIMEOUT = 120                # one attempt: a few mail API calls at 30 s
WHOLE = "email"                    # the part name of the whole email


def report_dir():
    """Where the save key writes a report's markdown."""
    return root("ASSISTANT_FINANCE_NOTES") / "reports"


LOCK = threading.Lock()
_building = None   # {"kind", "part", "since"} while a build runs
_drafting = None   # {"kind", "part", "since"} while a draft runs
# "kind/part" -> {"built_at", "data", "label", "settings", "subject",
# "text" | "error", then "drafted_at" and "drafted_to" or "draft_error",
# and "saved_at" and "saved_name"}
_builds = {}


def parts(kind):
    """The rows of a kind: the whole email, its sections, then Links."""
    return (WHOLE, *family_emails.SECTIONS[kind], LINKS)


def request(kind, part, data, spec):
    """The finance_email.py request of a build, and the day or month it
    covers; ValueError on bad data."""
    today = date.today()
    out = {"kind": kind, "settings": spec, "day": today.isoformat(),
           "sample": data == "demo", "full_window": data != "demo"}
    if part != WHOLE:
        out["part"] = part
    if kind == "monthly":
        if data == "demo":
            out["month"] = family_emails.last_month(today)
        elif data == "now":
            out["month"] = today.strftime("%Y-%m")
        elif re.fullmatch(r"\d{4}-\d{2}", data) and cash_flow.FIRST_MONTH <= data <= out["day"][:7]:
            out["month"] = data
        else:
            raise ValueError("A monthly build needs demo, now, or a month since " + cash_flow.FIRST_MONTH + ".")
        return out, out["month"]
    if data not in ("demo", "now"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data) or data > out["day"]:
            raise ValueError("A daily or weekly build needs demo, now, or a past day.")
        out["day"] = data
    return out, out["day"]


def _build(key, req, label, data, spec):
    """The build itself, on its own thread. Every ending leaves a row the
    page can show."""
    global _building
    try:
        result = family_emails.finance_build(req)
        subject = result["subject"] + (f" · {req['part']}" if "part" in req else "")
        out = {"subject": subject, "text": result["text"]}
    except Exception as e:
        out = {"error": str(e) or type(e).__name__}
    with LOCK:
        _builds[key] = {"built_at": _now(), "data": data, "label": label, "settings": spec, **out}
        _building = None


def build(body):
    """POST /api/finance-reports/build: start one build. One thing at a
    time: a build is up to a few minutes of work, and a build must not
    swap the text out under a draft."""
    global _building
    kind, part, data = body.get("kind"), body.get("part"), body.get("data")
    if kind not in family_emails.SECTIONS or part not in parts(kind):
        return 400, {"error": "Choose a kind and one of its parts."}
    try:
        spec = family_emails.settings(kind, body.get("settings"))
        req, label = request(kind, part, data if isinstance(data, str) else "", spec)
    except ValueError as e:
        return 400, {"error": str(e)}
    if data != "demo" and (refused := family_emails.paused(kind, req["settings"] if part == WHOLE
                                                           else {"sections": [part]})):
        return refused
    with LOCK:
        if _building or _drafting:
            return 409, {"error": "A build or a draft is already running."}
        _building = {"kind": kind, "part": part, "since": _now()}
    threading.Thread(target=_build, args=(f"{kind}/{part}", req, label, data, spec),
                     daemon=True).start()
    return 200, {"started": True}


def _real_build(kind, part):
    """The row's last build when it holds real text; caller holds LOCK."""
    rep = _builds.get(f"{kind}/{part}")
    return rep if rep and "text" in rep and rep["data"] != "demo" else None


def _draft(rep, to):
    """The draft itself, on its own thread: draft_mail.py once, the body on
    stdin. The outcome lands on the row."""
    global _drafting
    try:
        run = subprocess.run([family_emails.MAIL_PYTHON, str(DRAFT_MAIL), rep["subject"], to],
                             input=rep["text"], capture_output=True, text=True,
                             timeout=DRAFT_TIMEOUT)
        error = None if run.returncode == 0 else \
            ((run.stderr or "").strip()[-family_emails.ERR_TAIL:]
             or f"draft_mail.py exited {run.returncode}")
    except (OSError, subprocess.SubprocessError) as e:
        error = f"{type(e).__name__}: {e}"
    with LOCK:
        for k in ("drafted_at", "drafted_to", "draft_error"):
            rep.pop(k, None)
        if error:
            rep["draft_error"] = error
        else:
            rep.update(drafted_at=_now(), drafted_to=to)
        _drafting = None


def draft(body):
    """POST /api/finance-reports/draft: put one row's built text into the
    main account's Drafts folder, addressed to the person's saved address."""
    global _drafting
    kind, part, person = body.get("kind"), body.get("part"), body.get("person")
    with family_emails.LOCK:
        to = family_emails.DATA["addresses"].get(person, "") if isinstance(person, str) else ""
    if not to:
        return 400, {"error": "Choose a person with a saved address."}
    with LOCK:
        rep = _real_build(kind, part)
        if not rep:
            return 400, {"error": "Build it from real data first."}
        if _building or _drafting:
            return 409, {"error": "A build or a draft is already running."}
        _drafting = {"kind": kind, "part": part, "since": _now()}
    threading.Thread(target=_draft, args=(rep, to), daemon=True).start()
    return 200, {"started": True}


def file_name(kind, part, label, spec):
    """<label>-finance-<kind>[-details][-personal][-no-new-payees][-<part>].md"""
    name = f"{label}-finance-{kind}"
    for key, tag in (("detailed", "details"), ("combine", "personal")):
        if spec[key]:
            name += "-" + tag
    if "show_new_payees" in family_emails.OPTIONS[kind] and not spec["show_new_payees"]:
        name += "-no-new-payees"
    if part != WHOLE:
        name += "-" + ("links" if part == LINKS else SECTIONS[part])
    return name + ".md"


def save(body):
    """POST /api/finance-reports/save: write one row's built text to
    report_dir(), replacing an earlier save of the same name."""
    kind, part = body.get("kind"), body.get("part")
    with LOCK:
        rep = _real_build(kind, part)
        if not rep:
            return 400, {"error": "Build it from real data first."}
        text = rep["text"]
        path = report_dir() / file_name(kind, part, rep["label"], rep["settings"])
    # an empty folder is not restored from backup, so the first save after
    # a restore makes it again
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    with LOCK:
        rep.update(saved_at=_now(), saved_name=path.name)
    return 200, {"path": str(path)}


# ---------------------------------------------------------------- area interface

def boot():
    """Nothing to load: the builds live in this process only."""


def state():
    """The finance reports part of GET /api/state: the running build or
    draft, each row's last build, and the first month the cash-flow data
    covers (the monthly's data dropdown ends there). The kinds, their
    sections and options, and the presets come with family emails' state."""
    with LOCK:
        return {"building": _building and dict(_building),
                "drafting": _drafting and dict(_drafting),
                "builds": {k: dict(v) for k, v in _builds.items()},
                "first_month": cash_flow.FIRST_MONTH}


HANDLERS = {"/api/finance-reports/build": build,
            "/api/finance-reports/draft": draft,
            "/api/finance-reports/save": save}
