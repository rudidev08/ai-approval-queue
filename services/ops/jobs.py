"""Jobs area — every enabled hermes cron job: one row per job with its
schedule, last run, next run, current state, and a run key.

Served by server.py (one process, one page). The job list, schedule,
last/next run stamps and last_status come from hermes' own jobs file; the
schedule renders as an interval plus its firing times ("1d" at "04:00",
"8h" at "06:00, 14:00, 22:00", "7d" at "Mon 09:00"). A job's
running flag and the area's runs-in-24h count come from hermes' executions
ledger; the flag is the job's newest attempt, while it is still
claimed/running — scheduled runs and page-started ones alike. The same
pass collects how long each job's last finished runs took (newest first,
at most 3), for the row's "took" cell, and the claim stamps of each job's
failed runs over the last 7 days (newest first, at most 3), for the row's
"failed" cell.

In-run retries never reach the ledger (a run that healed by retrying is one
"completed" row), so scripts/retry.sh and the budget and web research
drivers append one line per retried attempt to
~/.hermes/logs/cron-retries.log. Each row carries its job's 7-day retry
count and the newest
retry message, matched through the job's script name.

Per-job state comes from the newest assistant-status pass: server.py's status
thread hands the cron items' states here via set_assistant_states(), so a job
reads bad for what last_status alone cannot see (overdue, disabled, stale
backup files). A job assistant-status does not list falls back to last_status.
The pass runs every 15 minutes; a job that finished a run after the pass
falls back to last_status until the next pass, so a fresh run's result is
never overridden by the pass's stale verdict.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/jobs/run  body {"name"}: start `hermes cron run <name>` detached.
  The run claims the job, so a second tap while one is going cannot fire it
  twice. 400 on a job the file does not list as enabled, 500 when hermes
  cannot start.
- GET /api/jobs/log?job=NAME&runs=N  {text}: the saved output of the job's
  newest N runs (1 to 5, default 1), newest first, from hermes' own output
  folder — the full stderr of a failure. Any job the file lists, enabled or
  not; 404 names the jobs when the name matches none (any case).
"""

import json
import pathlib
import sqlite3
import subprocess
from datetime import datetime, timedelta

JOBS_FILE = pathlib.Path.home() / ".hermes/cron/jobs.json"
OUTPUT = pathlib.Path.home() / ".hermes/cron/output"   # one folder per job id
EXECUTIONS = pathlib.Path.home() / ".hermes/cron/executions.db"
RETRIES_LOG = pathlib.Path.home() / ".hermes/logs/cron-retries.log"

WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# {job name: {"state": ok|--|other, "detail": text}} from the newest
# assistant-status pass; empty until the first pass finishes
_ASSISTANT_STATES = {}
_ASSISTANT_GENERATED = None  # when that pass ran, an aware datetime

_STATE = {"ok": "ok", "--": "idle"}  # anything else assistant-status emits is bad


def set_assistant_states(states, generated):
    """Called by server.py's status thread after each assistant-status pass;
    generated is the pass's own timestamp from its JSON."""
    global _ASSISTANT_STATES, _ASSISTANT_GENERATED
    _ASSISTANT_GENERATED = datetime.fromisoformat(generated)
    _ASSISTANT_STATES = states


def _ran_after_pass(j):
    """True when the job finished a run after the newest assistant-status pass."""
    last = j.get("last_run_at")
    return (_ASSISTANT_GENERATED is not None and last is not None
            and datetime.fromisoformat(last) > _ASSISTANT_GENERATED)


def _cron_field(field, lo, hi):
    """One cron field -> the values it matches (handles *, */n, lists and
    ranges); None for forms outside that (month and weekday names)."""
    out = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit():
                return None
            step = int(s)
        if part == "*":
            first, last = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            first, last = int(a), int(b)
        elif part.isdigit():
            first = last = int(part)
        else:
            return None
        out.update(range(first, last + 1, step))
    return sorted(out)


def _schedule(sched):
    """(interval, firing times) from a job's schedule: ('1d', '04:00'),
    ('3h', '00:00, 03:00, …' — every firing, comma separated), ('7d',
    'Mon 09:00'). A job firing more than 12 times a day would list dozens,
    so it keeps the bare minute (':30') instead. (the schedule's own display
    string, '') when the expr fits none of those shapes (interval and once
    schedules, pinned months or days of the month)."""
    display = sched.get("display") or ""
    expr = sched.get("expr")
    if not expr or len(expr.split()) != 5:
        return display, ""
    minute, hour, dom, mon, dow = expr.split()
    minutes = _cron_field(minute, 0, 59)
    hours = _cron_field(hour, 0, 23)
    if dom != "*" or mon != "*" or not minutes or not hours:
        return display, ""
    if dow != "*":
        dows = _cron_field(dow, 0, 7)
        if not dows or len(dows) != 1 or len(minutes) != 1 or len(hours) != 1:
            return display, ""
        return "7d", "%s %02d:%02d" % (WEEKDAYS[dows[0] % 7],
                                       hours[0], minutes[0])
    per_day = len(minutes) * len(hours)
    if per_day == 1:
        return "1d", "%02d:%02d" % (hours[0], minutes[0])
    gap = 24 / per_day
    every = "%dh" % round(gap) if gap >= 1 else "%dm" % round(gap * 60)
    if per_day > 12:
        return every, (":%02d" % minutes[0] if len(minutes) == 1 else "")
    return every, ", ".join("%02d:%02d" % (h, m)
                            for h in hours for m in minutes)


def _enabled_jobs():
    """The enabled jobs from hermes' jobs file; None when unreadable."""
    try:
        job_list = json.loads(JOBS_FILE.read_text())["jobs"]
    except (OSError, ValueError, KeyError):
        return None
    return [j for j in job_list if j.get("enabled")]


def _ledger():
    """One pass over hermes' executions ledger: ({job id: start stamp} for
    jobs whose newest attempt is still claimed/running, runs claimed in the
    last 24 hours, {job id: seconds each of the last finished runs took,
    newest first, at most 3}, {job id: claim stamps of failed runs in the
    last 7 days, newest first, at most 3}).
    A run that dies uncleanly keeps its running row until hermes proves the
    owner process gone, so a dead run can read as running for a while. Any
    read problem -> ({}, 0, {}, {})."""
    try:
        conn = sqlite3.connect(f"file:{EXECUTIONS}?mode=ro", uri=True,
                               timeout=1)
        try:
            rows = conn.execute(
                "SELECT job_id, status, started_at, finished_at, claimed_at "
                "FROM executions "
                "ORDER BY claimed_at DESC, id DESC").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}, 0, {}, {}
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)
    runs_24h = 0
    newest = {}
    took = {}
    fails_7d = {}
    for job_id, status, started, finished, claimed in rows:
        newest.setdefault(job_id, (status, started or claimed))
        if (status in ("completed", "failed") and started and finished
                and len(took.setdefault(job_id, [])) < 3):
            try:
                took[job_id].append(round(
                    (datetime.fromisoformat(finished)
                     - datetime.fromisoformat(started)).total_seconds(), 1))
            except ValueError:
                pass
        try:
            at = datetime.fromisoformat(claimed)
        except ValueError:
            continue
        if at.tzinfo is None:   # a bare stamp is local time
            at = at.astimezone()
        if at > cutoff:
            runs_24h += 1
        if (status == "failed" and at > cutoff_7d
                and len(fails_7d.setdefault(job_id, [])) < 3):
            fails_7d[job_id].append(claimed)
    running = {j: since for j, (status, since) in newest.items()
               if status in ("claimed", "running")}
    return running, runs_24h, took, fails_7d


def _retries():
    """{script name: (retries in the last 7 days, newest message)} from the
    retry log the cron scripts append to — one tab-separated line per
    retried attempt: stamp, script name (no .sh), message. The file is
    append-only, so the last line kept is the newest. Missing or unreadable
    log -> {}."""
    try:
        lines = RETRIES_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    cutoff = datetime.now().astimezone() - timedelta(days=7)
    out = {}
    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        stamp, script, msg = parts
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.astimezone()
        if at <= cutoff:
            continue
        count, _ = out.get(script, (0, ""))
        out[script] = (count + 1, msg)
    return out


def _row(j, running, took, fails_7d, retries):
    assistant = _ASSISTANT_STATES.get(j["name"])
    if assistant and _ran_after_pass(j):
        assistant = None
    if assistant:
        state = _STATE.get(assistant["state"], "bad")
        # a bad detail's first " · " segment is the schedule (_check_cron
        # leads with it), which the row already shows in its own column —
        # what follows is what went wrong
        detail = assistant["detail"].split(" · ", 1)[-1] if state == "bad" else ""
    elif j.get("last_run_at"):
        state = "ok" if j.get("last_status") == "ok" else "bad"
        detail = ""
    else:
        state, detail = "idle", ""
    every, at = _schedule(j.get("schedule") or {})
    script = (j.get("script") or "").removesuffix(".sh")
    retries_7d, last_retry = retries.get(script, (0, ""))
    return {"name": j["name"],
            "every": every, "at": at,
            "last_run_at": j.get("last_run_at"),
            "last_status": j.get("last_status"),
            "next_run_at": j.get("next_run_at"),
            "running_since": running.get(j.get("id")),
            "took": took.get(j.get("id"), []),
            "fails_7d": fails_7d.get(j.get("id"), []),
            "retries_7d": retries_7d, "last_retry": last_retry,
            "state": state, "detail": detail}


# ---------------------------------------------------------------- area interface

NAME = "jobs"


def boot():
    """Nothing to load: every answer is read from hermes' files at call time."""


def state():
    """The jobs part of GET /api/state: one entry per enabled job, A to Z,
    plus the runs claimed in the last 24 hours for the area's tile."""
    job_list = _enabled_jobs()
    if job_list is None:
        return {"jobs": [], "runs_24h": 0,
                "error": "could not read the cron jobs file"}
    running, runs_24h, took, fails_7d = _ledger()
    retries = _retries()
    return {"jobs": sorted((_row(j, running, took, fails_7d, retries)
                            for j in job_list),
                           key=lambda x: x["name"]),
            "runs_24h": runs_24h}


def _h_run(body):
    name = body.get("name") or ""
    if not any(j["name"] == name for j in _enabled_jobs() or []):
        return 400, {"error": "unknown job"}
    try:
        subprocess.Popen(["hermes", "cron", "run", name],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start hermes: {e}"}
    return 200, {"started": name}


def _h_log(params):
    try:
        job_list = json.loads(JOBS_FILE.read_text())["jobs"]
    except (OSError, ValueError, KeyError) as e:
        return 500, {"error": f"cannot read the cron job list: {e}"}
    wanted = params.get("job", "").strip().lower()
    job = next((j for j in job_list if str(j.get("name", "")).lower() == wanted), None)
    if job is None:
        names = ", ".join(sorted(str(j.get("name", "?")) for j in job_list))
        return 404, {"error": f"no cron job named {params.get('job', '')!r} — jobs are: {names}"}
    try:
        runs = max(1, min(int(params.get("runs") or 1), 5))
    except ValueError:
        return 400, {"error": "runs must be a whole number"}
    try:
        files = sorted(p for p in (OUTPUT / job["id"]).iterdir() if p.is_file())[::-1][:runs]
    except OSError:
        files = []
    if not files:
        return 200, {"text": f"{job['name']}: no saved run output. Last run "
                             f"{job.get('last_run_at') or 'never'}, status "
                             f"{job.get('last_status') or 'unknown'}: "
                             f"{job.get('last_error') or 'no error recorded'}"}
    return 200, {"text": "\n\n---\n\n".join(p.read_text(errors="replace").rstrip()
                                             for p in files)}


HANDLERS = {"/api/jobs/run": _h_run}
GET_HANDLERS = {"/api/jobs/log": _h_log}
