"""Finance report area — a report email's body, built on demand, shown on
the page, and drafted as an email or saved from there.

Served by server.py (one process, one page). The run key runs
finance_jobs.py the way the finance-daily and finance-weekly cron jobs do,
with one difference: --preview is the script's write-free mode. No
assets/debts lines, no monthly-balance lines, no printed-transactions
file, no card batch, and nothing is emailed; the text lands on the page
and nowhere else.

The page's rows pick a part: full is the whole body, the rest one
component alone (the script's --section flag). Every run carries the
page's four settings — the report (daily, weekly, or a past YYYY-MM month:
the script's --weekly and --month flags; the current month is daily),
details (--detailed: income sub-lines, history rows, debt payoff and
interest math), categories (--categories: every group's per-category
rows) and combine personal (--combine-personal: the per-person categories
print as one "Personal" and one "Personal Subscription" row per group).
Each part's last build is kept with the settings it was built with. Every
part but summary and full skips the script's LLM call and builds in
seconds; those two take a minute or two. A part outside the picked
report's parts (finance_jobs.PARTS) is the script's own refusal, kept as
the row's error. The request only starts the run: a thread runs the
script and keeps the result in this process. The page polls /api/state
and shows the text when it lands. Nothing is stored on disk, so a service
restart drops every cached part.

The draft key puts a part's cached text — the text on the page, never a
fresh build — into the hi@ Drafts folder, addressed to finance.env's
REPORT_MAIL_TO, through draft_mail.py (one attempt: someone is waiting on
the row). Nothing is sent; the user reviews and sends it from their mail
client. The save key writes the cached text to REPORT_DIR as a markdown
file named by the report's month or build date and the settings it was
built with:
<label>-finance[-weekly][-details][-categories][-personal][-<part>].md.
The state lists that folder, parsed back into those settings and the
file's time, so the page can say when the picked combination was last
saved.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/finance-report/preview  build one part; body {"section": NAME,
  "report": "daily" | "weekly" | "YYYY-MM", "detailed": bool,
  "categories": bool, "combine_personal": bool}, "full" or absent for the
  whole body, daily for an absent report. 409 while a build or a draft is
  going, 400 on an unknown name or a bad report
- POST /api/finance-report/draft  draft one part's cached text; body
  {"section": NAME}. 409 while a build or a draft is going, 400 with no
  text to draft or no REPORT_MAIL_TO
- POST /api/finance-report/save  write one part's cached text; body
  {"section": NAME}; answers {"path": ...}. 400 with no text to save
"""

import pathlib
import re
import subprocess
import threading
from datetime import date, datetime, timezone

from common import _now, cron_job
from finance_jobs import cash_flow, env_config, JMAP_PYTHON

APP = pathlib.Path(__file__).resolve().parent
SCRIPT = APP / "finance_jobs.py"
PYTHON = "/usr/bin/python3"        # the interpreter the cron scripts use
REPORT_DIR = pathlib.Path.home() / "Iris" / "vault" / "docs" / "finances" / "reports"
DRAFT_MAIL = APP.parent / "mcp" / "jmap_mail" / "draft_mail.py"

# the cron jobs that mail the real reports, per kind; their stamps give the
# area heading its "email sent / next" line (the same read as the research
# area's batch)
CRON_JOB = {"daily": "finance-daily", "weekly": "finance-weekly"}

# kept by hand — full, then every part name in finance_jobs.PARTS plus its
# page-only part, so junk is refused here instead of failing a run
PARTS = ("full", "summary", "cashflow", "check", "outliers", "new", "links")

# the script's own steps are bounded by the oMLX call; this is the outer stop
TIMEOUT = 600
DRAFT_TIMEOUT = 120                # one attempt: a few JMAP calls at 30 s
ERR_TAIL = 800                     # chars of a failed run's stderr the page shows

LOCK = threading.Lock()
_building_since = None             # iso stamp while a run is going, else None
_building_section = None           # the running build's part name, else None
_drafting_since = None             # iso stamp while a draft is going, else None
_drafting_section = None           # the drafting part's name, else None
# part name -> {"built_at": iso, "label": month or build date, "report",
# "detailed", "categories", "combine_personal", "text"|"error": str, then
# "drafted_at" or "draft_error" once a draft ended}
_reports = {}


def _build(section, report, detailed, categories, combine_personal):
    """The run itself, on its own thread. Every ending — good, bad exit,
    timeout, missing interpreter — leaves a report the page can show."""
    global _building_since, _building_section
    argv = [PYTHON, str(SCRIPT), "--email", "--preview"]
    if report == "weekly":
        argv.append("--weekly")
    elif report != "daily":
        argv += ["--month", report]
    if detailed:
        argv.append("--detailed")
    if categories:
        argv.append("--categories")
    if combine_personal:
        argv.append("--combine-personal")
    if section != "full":
        argv += ["--section", section]
    try:
        run = subprocess.run(argv, capture_output=True, text=True,
                             timeout=TIMEOUT)
        if run.returncode:
            tail = (run.stderr or "").strip()[-ERR_TAIL:]
            out = {"error": tail or f"the script exited {run.returncode}"}
        else:
            out = {"text": run.stdout}
    except (OSError, subprocess.SubprocessError) as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    with LOCK:
        _reports[section] = {"built_at": _now(),
                             "label": (date.today().isoformat()
                                       if report in CRON_JOB else report),
                             "report": report, "detailed": detailed,
                             "categories": categories,
                             "combine_personal": combine_personal, **out}
        _building_since = None
        _building_section = None


def preview(section=None, report="", detailed=False, categories=False,
            combine_personal=False):
    """POST /api/finance-report/preview: start the build of one part. One
    thing at a time — a run is up to a minute or two of work, two runs of
    the same part would only race to replace each other's text, and a
    build must not swap the text out under a draft."""
    global _building_since, _building_section
    section = section or "full"
    report = (report or "daily").strip()
    if section not in PARTS:
        return 400, {"error": "unknown section"}
    if report not in CRON_JOB and not re.fullmatch(r"\d{4}-\d{2}", report):
        return 400, {"error": "report needs daily, weekly or YYYY-MM"}
    if report == date.today().strftime("%Y-%m"):
        report = "daily"           # the current month is today's report
    with LOCK:
        if _building_since:
            return 409, {"error": "a preview is already building"}
        if _drafting_since:
            return 409, {"error": "a report is being drafted"}
        _building_since = _now()
        _building_section = section
    threading.Thread(target=_build,
                     args=(section, report, bool(detailed), bool(categories),
                           bool(combine_personal)),
                     daemon=True).start()
    return 200, {"started": True}


def recipients():
    """The addresses in finance.env's REPORT_MAIL_TO, comma-separated."""
    return [a.strip() for a in env_config().get("REPORT_MAIL_TO", "").split(",")
            if a.strip()]


def _subject(rep, section):
    kind = " weekly" if rep["report"] == "weekly" else ""
    part = "" if section == "full" else f" {section}"
    return f"Finance{kind}{part} — {rep['label']}"


def _draft(section, text, subject, to):
    """The draft itself, on its own thread: draft_mail.py once, the body on
    stdin. The outcome lands on the part's entry."""
    global _drafting_since, _drafting_section
    try:
        run = subprocess.run([JMAP_PYTHON, str(DRAFT_MAIL), subject, *to],
                             input=text, capture_output=True, text=True,
                             timeout=DRAFT_TIMEOUT)
        error = None if run.returncode == 0 else \
            ((run.stderr or "").strip()[-ERR_TAIL:]
             or f"draft_mail.py exited {run.returncode}")
    except (OSError, subprocess.SubprocessError) as e:
        error = f"{type(e).__name__}: {e}"
    with LOCK:
        rep = _reports.get(section)
        if rep is not None:
            rep.pop("draft_error", None)
            if error:
                rep["draft_error"] = error
            else:
                rep["drafted_at"] = _now()
        _drafting_since = None
        _drafting_section = None


def draft(section=None):
    """POST /api/finance-report/draft: put one part's cached text into the
    hi@ Drafts folder, addressed to the preset addresses. Refused while
    a build or a draft is going, with no text, or with no addresses
    configured."""
    global _drafting_since, _drafting_section
    section = section or "full"
    if section not in PARTS:
        return 400, {"error": "unknown section"}
    try:
        to = recipients()
    except (OSError, RuntimeError) as e:
        return 400, {"error": str(e)}
    if not to:
        return 400, {"error": "REPORT_MAIL_TO in finance.env is empty"}
    with LOCK:
        rep = _reports.get(section)
        if not rep or "text" not in rep:
            return 400, {"error": "nothing to draft — build the part first"}
        if _building_since:
            return 409, {"error": "a preview is building"}
        if _drafting_since:
            return 409, {"error": "a report is already being drafted"}
        _drafting_since = _now()
        _drafting_section = section
        text, subject = rep["text"], _subject(rep, section)
    threading.Thread(target=_draft, args=(section, text, subject, to),
                     daemon=True).start()
    return 200, {"started": True}


def file_name(label, report, section, detailed, categories, combine_personal):
    """<label>-finance[-weekly][-details][-categories][-personal][-<part>].md
    — the settings a text was built with, readable back by saved_files."""
    name = f"{label}-finance"
    if report == "weekly":
        name += "-weekly"
    if detailed:
        name += "-details"
    if categories:
        name += "-categories"
    if combine_personal:
        name += "-personal"
    if section != "full":
        name += f"-{section}"
    return name + ".md"


# a file name written by file_name, read back: the label (a month or a
# build date), the kind and the three settings, and the part
FILE_NAME = re.compile(r"(\d{4}-\d{2}(?:-\d{2})?)-finance(-weekly)?"
                       r"(-details)?(-categories)?(-personal)?"
                       r"(?:-(\w+))?\.md")


def saved_files():
    """Every report in REPORT_DIR the page can match to a row and the
    header's settings: [{name, report, detailed, categories,
    combine_personal, section, saved_at}], report being weekly, the month,
    or daily for a dated current-month build. Files named some other way
    are left out."""
    out = []
    try:
        paths = sorted(REPORT_DIR.iterdir())
    except OSError:
        return out
    for path in paths:
        m = FILE_NAME.fullmatch(path.name)
        if not m or (m.group(6) or "full") not in PARTS:
            continue
        label = m.group(1)
        out.append({"name": path.name,
                    "report": ("weekly" if m.group(2)
                               else label if len(label) == 7 else "daily"),
                    "detailed": bool(m.group(3)),
                    "categories": bool(m.group(4)),
                    "combine_personal": bool(m.group(5)),
                    "section": m.group(6) or "full",
                    "saved_at": _iso(path.stat().st_mtime)})
    return out


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def save(section=None):
    """POST /api/finance-report/save: write one part's cached text to
    REPORT_DIR under file_name's name, replacing an earlier save of the
    same name."""
    section = section or "full"
    if section not in PARTS:
        return 400, {"error": "unknown section"}
    with LOCK:
        rep = _reports.get(section)
        if not rep or "text" not in rep:
            return 400, {"error": "nothing to save — build the part first"}
        text = rep["text"]
        path = REPORT_DIR / file_name(rep["label"], rep["report"], section,
                                      rep["detailed"], rep["categories"],
                                      rep["combine_personal"])
    # an empty folder is not restored from backup, so the first save after
    # a restore makes it again
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return 200, {"path": str(path)}


# ---------------------------------------------------------------- area interface

NAME = "finance_report"


def boot():
    """Nothing to load: the previews live in this process only."""


def _email_jobs():
    """{kind: {last_run_at, last_failed, next_run_at}} for the two report
    jobs out of hermes' jobs file; a job the file does not hold (or an
    unreadable file) reads as never run."""
    out = {}
    for kind, name in CRON_JOB.items():
        job = cron_job(name) or {}
        out[kind] = {"last_run_at": job.get("last_run_at"),
                     "last_failed": bool(job.get("last_run_at")
                                         and job.get("last_status") != "ok"),
                     "next_run_at": job.get("next_run_at")}
    return out


def state():
    """The finance-report part of GET /api/state: whether a build or a draft
    is going and which part, each part's last build — its text, or the
    error that replaced it, with the settings it was built with and its
    draft outcome — the saved files with their settings and times, the
    first month the cash-flow data covers (the page's report dropdown ends
    there) and the two report jobs' stamps: when each real email last
    went out and when the next one is due."""
    jobs = _email_jobs()
    with LOCK:
        return {"building_since": _building_since,
                "building_section": _building_section,
                "drafting_since": _drafting_since,
                "drafting_section": _drafting_section,
                "reports": {k: dict(v) for k, v in _reports.items()},
                "first_month": cash_flow.FIRST_MONTH,
                "saved": saved_files(),
                "jobs": jobs}


def _h_preview(body):
    return preview(body.get("section"), body.get("report"),
                   body.get("detailed"), body.get("categories"),
                   body.get("combine_personal"))


def _h_draft(body):
    return draft(body.get("section"))


def _h_save(body):
    return save(body.get("section"))


HANDLERS = {"/api/finance-report/preview": _h_preview,
            "/api/finance-report/draft": _h_draft,
            "/api/finance-report/save": _h_save}
