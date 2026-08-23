"""Finance report area — the daily report email's body, built on demand and
shown on the page.

Served by server.py (one process, one page). In the full setup the preview
key runs finance_scan.py the way the finance-daily cron job does, in its
write-free mode: no debts lines, no monthly-balance lines, no
printed-transactions file, no card batch — and the full body opens with a
line saying it is test data. Nothing is emailed either; the text lands on
the page and nowhere else.

This public copy is a stub of that: the build returns canned sample text
instead of running the script, so the area renders without the private
report pipeline. The area interface, the part switching, and the endpoint
contract are the real ones.

The page's chip row picks a part: full is the whole body, the rest one
component alone. The preview key builds the picked part, and each part's
last build is kept, so the chips switch between cached texts with no new
run. The request only starts the run: a thread builds and keeps the result
in this process. The page polls /api/state and shows the text when it
lands. Nothing is stored on disk, so a service restart drops every cached
part.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/finance-report/preview  build one part; body {"section": NAME},
  "full" or absent for the whole body. 409 while one is building, 400 on an
  unknown name
"""

import threading

from common import _now

# after "full", the components of the daily report email's body
PARTS = ("full", "summary", "cashflow", "lenses", "odd", "new", "links")

LOCK = threading.Lock()
_building_since = None             # iso stamp while a run is going, else None
_building_section = None           # the running build's part name, else None
_reports = {}    # part name -> {"built_at": iso, "text"|"error": str}

# what the real script's parts look like, shrunk to a screenful each
_SAMPLES = {
    "summary": """\
Summary
- August is on track: spending sits 4% under the projection.
- Groceries runs warm again; dining out is flat.
- One odd charge below is worth a look.""",
    "cashflow": """\
Budget status — August 2026, day 21 of 31 | 3 uncategorized

Estimated:
- Projected month end: $612 over income
- Income estimate: $8,467 monthly
  - Acme (every 2 weeks): $4,333 monthly
  - Rentals (monthly): $2,400 monthly
  - Stocks (4 per year): $1,734 monthly

Actual:
So far: $5,842 spent of $6,180 expected by today
Tracked categories: Groceries $1,204 · Kids $610 · Transport $322
How long the money lasts: 74 days at this month's pace
Debt: $0""",
    "lenses": """\
Lenses
- Groceries: $1,204 this month, 12% over its 6-month mean.
- Subscriptions: $86, unchanged four months running.
- Kids: $610, school-start bump, expected.""",
    "odd": """\
Odd
- CHKCARDPAYPAL 2929 $84.10 — no payee rule matched, twice this month.""",
    "new": """\
New transactions this week
Checking
- 08-19 Grocery Outlet $118.42 (Groceries)
- 08-18 Shell $52.07 (Transport)
Savings
- 08-17 Transfer in $500.00 (To savings)""",
    "links": """\
Links
- budget: https://mac-mini.your-tailnet.ts.net:52737/budget
- rules: https://mac-mini.your-tailnet.ts.net:52737/rules""",
}


def _build(section):
    """The stub build: canned text on the same thread shape the real run
    uses, so the page's polling and key states behave the same."""
    global _building_since, _building_section
    if section == "full":
        text = ("this is test data — the public stub's canned body, "
                "nothing ran and nothing was emailed\n\n"
                + "\n\n".join(_SAMPLES[s] for s in PARTS[1:]) + "\n")
    else:
        text = _SAMPLES[section] + "\n"
    with LOCK:
        _reports[section] = {"built_at": _now(), "text": text}
        _building_since = None
        _building_section = None


def preview(section=None):
    """POST /api/finance-report/preview: start the build of one part. One at
    a time — the real run is up to a minute or two of work, and two runs of
    the same part would only race to replace each other's text."""
    global _building_since, _building_section
    section = section or "full"
    if section not in PARTS:
        return 400, {"error": "unknown section"}
    with LOCK:
        if _building_since:
            return 409, {"error": "a preview is already building"}
        _building_since = _now()
        _building_section = section
    threading.Thread(target=_build, args=(section,), daemon=True).start()
    return 200, {"started": True}


# ---------------------------------------------------------------- area interface

NAME = "finance_report"


def boot():
    """Nothing to load: the previews live in this process only."""


def state():
    """The finance-report part of GET /api/state: whether a build is going and
    which part it is, and each part's last build — its text, or the error
    that replaced it."""
    with LOCK:
        return {"building_since": _building_since,
                "building_section": _building_section,
                "reports": dict(_reports)}


def _h_preview(body):
    return preview(body.get("section"))


HANDLERS = {"/api/finance-report/preview": _h_preview}
