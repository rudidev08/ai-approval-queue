"""Demo data — the page's [demo] mode: canned state so every area renders.

Served by server.py under the /demo prefix: the page in demo mode sends
every /api call to /demo/api/... instead. GET /demo/api/state answers with
the state below (stamps computed against now, so ages and countdowns read
sensibly), the two GET_HANDLERS answer the page's per-row reads, and every
POST is accepted with 202 and does nothing — the page's optimistic row
states show, then the next poll puts the canned rows back.

Nothing here touches an area module or a state file. The data is invented
(no real names, mail, or transactions) — it is what the public demo's
screenshots are taken from.
"""

from datetime import datetime, timedelta, timezone

NAME = "demo"


def _utc(minutes):
    """A zoned ISO stamp `minutes` from now (negative = past)."""
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def _local(minutes):
    """A bare local ISO stamp, the audit's and the driver's own format."""
    return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _mail(minutes):
    """An email receivedAt: UTC truncated to the minute, no zone."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def _day(days, hhmm=None):
    """A calendar-style date `days` from today, with an optional time."""
    d = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    return f"{d} {hhmm}" if hhmm else d


H = 60
D = 24 * H

def _email_bodies():
    return {
        "m-coach-1": "From: Nadia Okafor <nadia@example.com>\nSubject: Updated invitation: Coaching group\n\n"
                     "Hi all, the Tuesday group moves to a new Meet link from next week:\n"
                     "https://meet.google.com/abc-defg-hij\n\nSee you there,\nNadia",
        "m-coach-2": "From: Nadia Okafor <nadia@example.com>\nSubject: Canceled event: Coaching group @ Tue\n\n"
                     "This occurrence is canceled. The series continues the week after.",
        "m-dentist": "From: Bayview Dental <appointments@example.com>\nSubject: Appointment reminder\n\n"
                     "Theo has a cleaning on " + _day(3) + " at 3:40 PM.\n"
                     "Please arrive ten minutes early. Reply CANCEL to cancel.",
        "m-mara":    "From: Mara Ellis <mara@example.com>\nSubject: Re: three charges\n\n"
                     "1. Shell — that was gas for the trip, so Transport: Gas\n"
                     "2. Amazon — Theo's headphones, Personal: Theo\n"
                     "3. Venmo — Sam paid me back, Income: Refunds",
        "m-news-1":  "From: Weekly Digest <digest@example.com>\nSubject: Your week in review\n\n(newsletter)",
        "m-news-2":  "From: Weekly Digest <digest@example.com>\nSubject: Your week in review\n\n(newsletter)",
        "m-genius":  "From: Apple <noreply@example.com>\nSubject: Get ready for the Genius Bar\n\n"
                     "Your appointment is today at 6:35 PM. Bring your Mac and your charger.",
    }


RESEARCH = {
    "daily-local-llm-news": (
        "What changed this week for running large language models on Apple Silicon: "
        "new model releases, runtime updates, benchmark posts. Skip marketing.",
        "## Local LLM news\n\n- A new 30B mixture-of-experts model landed with a 4-bit build for MLX.\n"
        "- The MLX runtime added prefix caching; long prompts start faster.\n"
        "- A benchmark thread compares three quantizations on an M4 Max.\n"),
    "weekly-home-battery": (
        "Home battery options for a 3-bedroom house: capacity, price, install lead time, "
        "and which ones work with the existing solar inverter.",
        "## Home battery\n\n- Two vendors ship 13 kWh units this quarter.\n"
        "- Lead times run 6 to 10 weeks.\n"),
    "weekly-kids-camps": (
        "Summer camps within 20 miles for ages 8 to 12: dates, price per week, "
        "and registration opening dates.",
        "## Kids camps\n\n- Registration for the science camp opens next month.\n"),
    "daily-new-topic": (
        "Track price drops on the three listed e-bikes and note any recalls.", None),
}


def _emails():
    coaching = [
        {"id": "m-coach-1", "subject": "Updated invitation: Coaching group @ Weekly (Tue)",
         "from": "Nadia Okafor <nadia@example.com>", "receivedAt": _mail(-3 * H), "gone": False},
        {"id": "m-coach-2", "subject": "Canceled event: Coaching group @ Tue 4pm",
         "from": "Nadia Okafor <nadia@example.com>", "receivedAt": _mail(-3 * H - 5), "gone": True},
    ]
    dentist = [{"id": "m-dentist", "subject": "Appointment reminder",
                "from": "Bayview Dental <appointments@example.com>",
                "receivedAt": _mail(-5 * H), "gone": False}]
    mara = [{"id": "m-mara", "subject": "Re: three charges",
             "from": "Mara Ellis <mara@example.com>", "receivedAt": _mail(-8 * H), "gone": False}]
    news = [{"id": "m-news-1", "subject": "Your week in review",
             "from": "Weekly Digest <digest@example.com>", "receivedAt": _mail(-2 * D), "gone": False},
            {"id": "m-news-2", "subject": "Your week in review",
             "from": "Weekly Digest <digest@example.com>", "receivedAt": _mail(-9 * D), "gone": False}]
    genius = [{"id": "m-genius", "subject": "Get ready for the Genius Bar",
               "from": "Apple <noreply@example.com>", "receivedAt": _mail(-D), "gone": True}]
    snapshot = {"notes": "Meet: https://meet.google.com/old-link-xyz", "location": "",
                "end": _day(7, "17:00"), "repeats": "weekly until " + _day(56)}
    by = {"job": "actions-inbox-scan", "session": "cron"}
    def row(i, kind, label, args, status="pending", text="", **extra):
        return {"id": i, "kind": kind, "label": label, "args": args,
                "args_sha256": "demo", "status": status, "status_text": text, **extra}
    return {
        "inbox_count": 7, "last_scan_at": _utc(-40), "last_scan_status": "ok",
        "batch_total": 0, "batch_decided": 0,
        "job_last_run_at": _utc(-40), "job_next_run_at": _utc(2 * H + 20),
        "job_running_since": None,
        "calendar_colors": {"Personal": "#0088ff", "Family": "#cb30e0",
                            "Personal": "#83d754", "Family": "#cb30e0", "Kids": "#ac7f5e"},
        "sets": [
            {"id": "set-coaching", "title": "Coaching group moves to a new Meet link",
             "rationale": "Updated invitation carries a new Meet link.",
             "created_at": _utc(-2 * H - 50), "created_by": by, "state": "pending",
             "resolved_at": None, "stuck": False, "emails": coaching,
             "rows": [
                 row("r1", "update_event", "Update the coaching group's Meet link from next Tuesday",
                     {"calendar": "Personal", "title": "Coaching group",
                      "start_local": _day(7, "16:00"), "span": "future",
                      "notes": "Meet: https://meet.google.com/abc-defg-hij", "snapshot": snapshot},
                     series={"repeat": "weekly", "repeat_until": _day(56), "occurrences": 8}),
                 row("r2", "archive_email", "Archive both invitation mails",
                     {"emails": [{k: m[k] for k in ("id", "subject", "from", "receivedAt")}
                                 for m in coaching]}),
             ]},
            {"id": "set-dentist", "title": "Theo's dental cleaning",
             "rationale": "A reminder for Theo's cleaning in three days. The Family calendar "
                          "has no block for it; the mirror needs a kick afterwards so the "
                          "busy copy shows.",
             "created_at": _utc(-2 * H), "created_by": by, "state": "pending",
             "resolved_at": None, "stuck": False, "emails": dentist,
             "rows": [
                 row("r1", "create_event", "Create Theo's cleaning",
                     {"calendar": "Family", "title": "Theo — dental cleaning",
                      "start": _day(3, "15:40"), "end": _day(3, "16:30"),
                      "location": "Bayview Dental, 120 Harbor St", "tz": "America/Los_Angeles",
                      "notes": "Arrive ten minutes early", "all_day": False},
                     status="success", text="created; verified by re-listing the day",
                     suggestion="3:40 PM is the time in the mail; the calendar is free then"),
                 row("r2", "create_reminder", "Remind to leave at 3:10",
                     {"name": "Leave for Theo's dentist", "list": "Family", "due": _day(3),
                      "notes": "Bring the insurance card"}),
                 row("r3", "delete_event", "Delete the old placeholder",
                     {"calendar": "Family", "title": "Dentist?", "start_local": _day(3, "15:00"),
                      "span": "this", "snapshot": {"notes": "", "location": "",
                                                   "end": _day(3, "16:00"), "repeats": ""}},
                     status="precheck_failed",
                     text="the event changed since it was proposed: end now reads "
                          + _day(3, "16:30")),
                 row("r4", "open_email", "Reply CANCEL if the time does not work",
                     {"email": {k: dentist[0][k] for k in ("id", "subject", "from", "receivedAt")}}),
                 row("r5", "mirror_kick", "Refresh the busy mirror", {"days": 14}),
             ]},
            {"id": "set-mara", "title": "Answers to the categorize questions",
             "rationale": "Mara answered the three numbered questions from the categorize "
                          "ask; each answer pairs with one transaction.",
             "created_at": _utc(-H), "created_by": by, "state": "pending",
             "resolved_at": None, "stuck": False, "emails": mara,
             "rows": [
                 row("r1", "categorize_transaction", "Categorize Shell as Transport: Gas",
                     {"transaction_id": "t-shell", "category": "Transport: Gas",
                      "update_rule": True, "ask_id": "ask-1",
                      "transaction": {"date": _day(-4), "payee": "Shell", "amount": "-52.30",
                                      "account": "Visa", "notes": "trip"}}),
                 row("r2", "categorize_transaction", "Categorize Amazon as Personal: Theo",
                     {"transaction_id": "t-amazon", "category": "Personal: Theo", "ask_id": "ask-1",
                      "transaction": {"date": _day(-6), "payee": "Amazon", "amount": "-63.99",
                                      "account": "Visa", "notes": ""}},
                     status="success", text="categorized"),
                 row("r3", "categorize_transaction", "Categorize Venmo as Income: Refunds",
                     {"transaction_id": "t-venmo", "category": "Income: Refunds", "ask_id": "ask-1",
                      "transaction": {"date": _day(-2), "payee": "Venmo", "amount": "25.00",
                                      "account": "Checking", "notes": "Sam"}},
                     status="denied", text="denied by user"),
             ]},
            {"id": "set-news", "title": "Newsletter cleanup",
             "rationale": "Two digests, both read. Archive them.",
             "created_at": _utc(-3 * D), "created_by": by, "state": "pending",
             "resolved_at": None, "stuck": True, "emails": news,
             "rows": [row("r1", "archive_email", "Archive the digests",
                          {"emails": [{k: m[k] for k in ("id", "subject", "from", "receivedAt")}
                                      for m in news]},
                          status="run_failed",
                          text="archive: the webmail child did not answer in 30 s")]},
            {"id": "set-genius", "title": "Genius Bar appointment today",
             "rationale": "The reminder confirms a Genius Bar appointment today at 6:35 PM.",
             "created_at": _utc(-D), "created_by": by, "state": "resolved",
             "resolved_at": _utc(-20 * H), "stuck": False, "emails": genius,
             "rows": [
                 row("r1", "create_event", "Create the Genius Bar appointment",
                     {"calendar": "Personal", "title": "Genius Bar — Mac", "start": _day(0, "18:35"),
                      "end": _day(0, "19:35"), "location": "Apple Store, Main Street",
                      "notes": "Case ID 1029", "tz": "America/Los_Angeles", "all_day": False},
                     status="success", text="created"),
                 row("r2", "archive_email", "Archive the reminder",
                     {"emails": [{k: m[k] for k in ("id", "subject", "from", "receivedAt")}
                                 for m in genius]},
                     status="denied", text="denied by user"),
             ]},
        ]}


def _finance():
    def card(tid, date, payee, amount, account, notes="", pick="latest",
             suggestions=(), status="pending", text="", **extra):
        return {"transaction_id": tid, "date": date, "payee": payee, "amount": amount,
                "notes": notes, "account": account, "account_id": "acc-" + account.lower(),
                "pick": pick, "suggestions": list(suggestions), "status": status,
                "status_text": text, **extra}
    return {
        "cards": [
            card("t-tj", _day(-1), "Trader Joe's", "-84.12", "Visa",
                 suggestions=[{"category": "Groceries", "basis": "history"}]),
            card("t-shell", _day(-4), "Shell", "-52.30", "Visa", "trip", pick="email",
                 source={"email_id": "m-mara", "subject": "Re: three charges"},
                 suggestions=[{"category": "Gas", "basis": "email"}]),
            card("t-shell", _day(-4), "Shell", "-52.30", "Visa", "trip",
                 suggestions=[{"category": "Gas", "basis": "history"}]),
            card("t-corner", _day(-2), "SQ *THE CORNER", "-18.00", "Visa"),
            card("t-dental", _day(-3), "Bayview Dental", "-240.00", "Checking",
                 suggestions=[{"category": "Activities", "basis": "guess"}]),
            card("t-pge", _day(-5), "PG&E", "-132.44", "Checking", "autopay",
                 status="done", text="categorized"),
            card("t-venmo", _day(-2), "Venmo", "25.00", "Checking", "Sam",
                 status="already_handled", text="already categorized in Actual"),
            card("t-amazon", _day(-6), "Amazon", "-63.99", "Visa",
                 suggestions=[{"category": "Theo", "basis": "history"}],
                 status="failed", text="budget sync failed: ECONNRESET — retry"),
        ],
        "error": None, "saved_at": _utc(-2 * H), "uncategorized": 12,
        "categories": [
            {"group": "Income", "categories": ["Paycheck", "Refunds"]},
            {"group": "Bills", "categories": ["Electric", "Internet", "Phone", "Rent"]},
            {"group": "Food", "categories": ["Coffee", "Groceries", "Restaurants"]},
            {"group": "Kids", "categories": ["Activities", "School"]},
            {"group": "Personal", "categories": ["Mara", "Mara Subscription", "Theo"]},
            {"group": "Transport", "categories": ["Car repair", "Gas", "Transit"]},
        ],
        "open_asks": [{"ask_id": "ask-1", "to_addr": "mara@example.com",
                       "created_at": _utc(-3 * D), "items": 3}],
        "oddities": [
            {"transaction_id": "t-odd-1", "date": _day(-1), "payee": "Bayview Dental",
             "amount": "-240.00", "account": "Checking",
             "reasons": ["unusually large for this payee (median 90.00)",
                         "possible duplicate of " + _day(-2)]},
            {"transaction_id": "t-odd-2", "date": _day(-3), "payee": "Ridgeline Coffee",
             "amount": "-6.50", "account": "Visa", "reasons": ["new payee"]},
        ],
        "job_last_run_at": _utc(-2 * H), "job_next_run_at": _utc(16 * H),
        "job_last_failed": False, "job_running_since": None,
    }


def _finance_report():
    def rep(report, **kw):
        return {"built_at": _utc(-3 * H), "label": _day(0), "report": report,
                "detailed": False, "categories": False, "combine_personal": False, **kw}
    return {
        "building_since": None, "building_section": None,
        "drafting_since": None, "drafting_section": None,
        "reports": {
            "full": rep("daily", drafted_at=_utc(-H),
                        text="Spending is on track for the month; two charges look odd and "
                             "three transactions still need a category.\n\n"
                             "Check\n- 3 uncategorized transactions, 120.42 total\n\n"
                             "Outliers\n- Bayview Dental -240.00 — unusually large\n\n"
                             "New this week\nVisa\n- Trader Joe's -84.12 (Groceries)\n"
                             "- Shell -52.30 (uncategorized)\n"),
            "summary": rep("weekly", categories=True,
                           text="A quiet week: income landed, bills cleared, groceries a "
                                "little under the average."),
            "cashflow": rep("weekly", categories=True, detailed=True,
                            error="finance_jobs.py exited 1: budget copy is 2 days old — "
                                  "run the rescan first"),
            "check": rep("daily", text="Check\n- nothing to report\n",
                         draft_error="draft_mail.py: no REPORT_MAIL_TO in finance.env"),
        },
        "first_month": "2026-03",
        "saved": [
            {"name": _day(0) + "-finance.md", "report": "daily", "detailed": False,
             "categories": False, "combine_personal": False, "section": "full",
             "saved_at": _utc(-50)},
            {"name": "2026-08-finance-categories-personal.md", "report": "2026-08",
             "detailed": False, "categories": True, "combine_personal": True,
             "section": "full", "saved_at": _utc(-4 * D)},
        ],
        "jobs": {"daily": {"last_run_at": _utc(-9 * H), "last_failed": False,
                           "next_run_at": _utc(15 * H)},
                 "weekly": {"last_run_at": _utc(-2 * D), "last_failed": True,
                            "next_run_at": _utc(5 * D)}},
    }


def _research():
    return {"topics": [
        {"slug": "daily-local-llm-news", "updated": _day(-1), "failing": 0, "running": False,
         "report": True, "error": False, "last_run": _local(-D - 2 * H)},
        {"slug": "weekly-home-battery", "updated": _day(-9), "failing": 2, "running": False,
         "report": True, "error": True, "last_run": _local(-9 * D)},
        {"slug": "weekly-kids-camps", "updated": _day(-7), "failing": 0, "running": True,
         "report": True, "error": False, "last_run": _local(-7 * D)},
        {"slug": "daily-new-topic", "updated": None, "failing": 0, "running": False,
         "report": False, "error": False, "last_run": None},
    ], "next_batch": _utc(3 * D)}


def _messages():
    def card(aid, name, loc, reason, sender, chat, days, orig, kind, text="",
             status="pending", stext=""):
        received = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 10:%M:00")
        return {"attachment_id": aid, "filename": name, "location_id": loc,
                "reason": reason, "sender": "+1415555" + str(aid).zfill(4),
                "sender_name": sender, "chat": chat, "received_at": received,
                "original_name": orig, "kind": kind, "text": text,
                "status": status, "status_text": stext}
    return {
        "cards": [
            card(101, "theo school year calendar 2026-27.pdf", "theo-school",
                 "school calendar for Theo's year — keep with school papers",
                 "Mara", "Family", 1, "IMG_2231.pdf", "application/pdf",
                 text="RIDGE ELEMENTARY\nSchool year 2026-27\n\nFirst day: Aug 20\n"
                      "Fall break: Oct 12-16\nWinter break: Dec 21 - Jan 4\n"),
            card(102, "kitchen faucet quote.jpg", "house-repairs",
                 "a plumber's quote photo — house records", "Sam", "Sam", 2,
                 "IMG_2240.jpg", "image/jpeg"),
            card(103, "lab results 2026-08.pdf", "mara-medical",
                 "lab results — Mara's medical folder", "Mara", "Family", 3,
                 "results.pdf", "application/pdf",
                 text="Order 5521\nPanel: basic metabolic\nAll values within range.\n",
                 status="run_failed",
                 stext="save_file: a file named lab results 2026-08.pdf already exists"),
            card(104, "car insurance card 2026.pdf", "car", "insurance card — car folder",
                 "Sam", "Sam", 4, "insurance.pdf", "application/pdf",
                 status="success", stext="saved to Car"),
        ],
        "error": {"step": "candidates",
                  "message": "macos_messages: chat.db is locked — Messages was syncing"},
        "last_scan_at": _utc(-25), "last_scan_status": "failed",
        "locations": [{"id": i, "label": l} for i, l in [
            ("car", "Car"), ("house-repairs", "House / Repairs"),
            ("mara-medical", "Mara / Medical"), ("sam-medical", "Sam / Medical"),
            ("theo-school", "Theo / School"), ("theo-medical", "Theo / Medical"),
            ("taxes-2026", "Taxes / 2026"), ("inbox", "Inbox")]],
        "job_last_run_at": _utc(-25), "job_next_run_at": _utc(2 * H + 35),
        "job_last_failed": False, "job_running_since": None,
    }


def _hermes_audit():
    def cat(label, findings=(), notes=(), dismissed=0):
        return {"label": label, "state": "warn" if findings or dismissed else "ok",
                "findings": list(findings), "notes": list(notes), "dismissed": dismissed}
    return {
        "running": False, "error": None,
        "run": {"step": 9, "total": 9, "category": "changes to accept",
                "step_started": _local(-2 * D), "llm": None, "llm_started": None,
                "started_at": _local(-2 * D - 6), "finished_at": _local(-2 * D),
                "categories": [
                    cat("tool approvals"),
                    cat("settings", ["config.yaml: model.temperature is 0.9, expected 0.7",
                                     "profile 'iris': skills.external_dirs lost hermes/skills"],
                        ["the expected values live in services/hermes-audit/expected.yaml"]),
                    cat("API keys"),
                    cat("tool servers", ["records: list_locations took 14 s (limit 5 s)"]),
                    cat("permission asking"),
                    cat("hermes diagnostics",
                        ["iris: new doctor warning — browser: playwright chromium not installed"],
                        dismissed=1),
                    cat("github watch"),
                    cat("local files", ["gateway started before the newest plugin deploy ("
                                        + _day(-1, "14:10") + ") — the running process enforces "
                                        "the old copy until restarted"]),
                    cat("changes to accept"),
                ]},
        "job_last_run_at": _utc(-2 * D), "job_next_run_at": _utc(5 * D),
        "job_last_failed": False,
    }


def _jobs():
    def job(name, every, at, last=-H, status="ok", nxt=H, took=(41.2, 38.9, 52.0),
            fails=(), retries=0, last_retry="", state="ok", detail="", running=None):
        return {"name": name, "every": every, "at": at,
                "last_run_at": _utc(last) if last is not None else None,
                "last_status": status, "next_run_at": _utc(nxt), "running_since": running,
                "took": list(took), "fails_7d": [_utc(f) for f in fails],
                "retries_7d": retries, "last_retry": last_retry,
                "state": state, "detail": detail}
    return {"jobs": [
        job("actions-inbox-scan", "3h", "00:24, 03:24, 06:24, 09:24, 12:24, 15:24, 18:24, 21:24",
            last=-40, nxt=2 * H + 20),
        job("backup-mirror", "1d", "03:30", last=-18 * H, nxt=6 * H, took=(612.0, 598.4, 640.1)),
        job("finance-daily", "1d", "07:00", last=-9 * H, status="failed", nxt=15 * H,
            took=(88.0, 91.5, 84.2), fails=(-9 * H, -2 * D, -5 * D), state="bad",
            detail="last run failed: exit 1 — send_mail.py: SMTP connect timed out"),
        job("finance-uncategorized", "1d", "06:30", last=-2 * H, nxt=16 * H,
            took=(64.3, 70.1, 61.8)),
        job("finance-weekly", "7d", "Mon 07:00", last=None, status=None, nxt=5 * D, took=(),
            state="idle"),
        job("hermes-audit", "7d", "Sun 08:10", last=-9 * D, nxt=-2 * D,
            took=(3540.0, 3611.2, 3502.9), state="bad",
            detail="overdue by 2 d — the scheduled run did not fire"),
        job("messages-attach-scan", "3h", "00:41, 03:41, 06:41, 09:41, 12:41, 15:41, 18:41, 21:41",
            last=-25, nxt=2 * H + 35, took=(33.0, 29.7, 35.2), running=_utc(-2)),
        job("research-batch", "7d", "Thu 03:00", last=-4 * D, nxt=3 * D,
            took=(1810.0, 1750.5, 1902.3), fails=(-4 * D,), retries=2,
            last_retry="driver: fetch timed out on 2 of 5 sources, retrying in 60 s"),
    ], "runs_24h": 37}


def state():
    return {"emails": _emails(), "finance": _finance(),
            "finance_report": _finance_report(), "research": _research(),
            "messages": _messages(), "hermes_audit": _hermes_audit(),
            "jobs": _jobs(), "system": {"gateway": _utc(-3 * D), "webui": None},
            "status_issues": 3, "status_checked_at": _utc(-4)}


def _h_body(params):
    text = _email_bodies().get(params.get("email_id"))
    if text is None:
        return 404, {"error": "no such email in the demo data"}
    return 200, {"text": text}


def _h_report(params):
    slug = params.get("slug")
    if slug not in RESEARCH:
        return 400, {"error": "unknown topic"}
    question, text = RESEARCH[slug]
    topic = next(t for t in _research()["topics"] if t["slug"] == slug)
    return 200, {"question": question, "report": text, "updated": topic["updated"]}


GET_HANDLERS = {"/demo/api/state": lambda params: (200, state()),
                "/demo/api/emails/body": _h_body,
                "/demo/api/research/report": _h_report}
