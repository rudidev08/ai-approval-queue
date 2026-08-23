"""Hermes-audit area — starts the deterministic Hermes audit and shows where the
run is.

Served by server.py (one process, one page). The work all happens in
services/hermes-audit/audit.py; this module only starts that script, stops it,
resets it, and reads the three files it leaves in ~/.local/state/hermes-audit/:

- lock       flock held for the length of a run. Whether a run is going is a
             lock probe, so a run started from hermes chat shows on the page
             too, and a killed run never reads as still running.
- run.json   this pass as it happens: step, category, the LLM call in flight,
             and per category waiting / running / ok / warn with its findings.
             finished_at stays null until the report lands, so a run that died
             is told apart from one that finished.
- last-run.err  the run's stderr, kept for a run that ended without a report.
             iris_ops writes the same file, so either starter's failure shows.
             It counts as this run's only when it is newer than the record's
             own stamp — a start that died before audit.py wrote a record
             leaves the previous pass on file, and that must not read as the
             pass that was just asked for.

The weekly hermes-audit cron job (~/.hermes/scripts/hermes-audit.sh) is a
third starter; state() reads its stamps out of hermes' cron files so the page
can show when the scheduled pass last ran and runs next.

The run is detached (start_new_session): a pass can take an hour, and log
rotation SIGTERMs this service in the middle of one. Nothing is kept in this
process — the files carry it all across a restart.

Dismissing a finding hides it on the page and nowhere else: this service owns
state/hermes-audit-dismissed.json, audit.py knows nothing about it, so the
markdown report and the iris_ops tools keep carrying every finding. The file
holds the pass it belongs to and the [category, finding] pairs hidden in it:

    {"run": "<the record's started_at>", "findings": [["backups", "..."], ...]}

A file whose run is not the record's started_at is an older pass's list and
reads as empty. seed_run() stamps a fresh started_at at the top of every run,
so any starter — this page, iris_hermes_audit in chat, a hand-run script —
clears the dismissed findings, and the file never holds more than one pass.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/hermes-audit/run     start a pass. 409 when one is already going
- POST /api/hermes-audit/stop    stop the run in progress (audit.py --stop):
                                SIGTERM to its process group; it writes no
                                report and commits no state
- POST /api/hermes-audit/reset   audit.py --reset: remove the audit's state file,
                                so the next run is a first pass — every check
                                reviews from scratch, every baseline reseeds
- POST /api/hermes-audit/dismiss {category, finding}: hide one finding of this
                                pass. 404 when the record does not carry it
- POST /api/hermes-audit/restore bring back everything dismissed in this pass
"""

import fcntl
import json
import os
import pathlib
import sqlite3
import subprocess
import threading
from datetime import datetime

from common import STATE_DIR, _log

APP = pathlib.Path(__file__).resolve().parent
SCRIPT = APP.parent / "hermes-audit" / "audit.py"
UV = pathlib.Path.home() / ".local" / "bin" / "uv"

STATE = pathlib.Path.home() / ".local" / "state" / "hermes-audit"
LOCK = STATE / "lock"
RUN = STATE / "run.json"
ERR = STATE / "last-run.err"
DISMISSED = STATE_DIR / "hermes-audit-dismissed.json"   # this service's own file

# the weekly cron job that also runs the audit; its stamps give the page the
# last-run and next-run indicators (same read as the messages area)
CRON_JOB = "hermes-audit"
CRON_JOBS = pathlib.Path.home() / ".hermes/cron/jobs.json"
CRON_EXECUTIONS = pathlib.Path.home() / ".hermes/cron/executions.db"

# dismiss and restore are read-modify-write on one file, and two findings
# tapped in quick succession land on two request threads
_DISMISS_LOCK = threading.Lock()

ERR_TAIL = 800        # chars of the failed run's stderr the page shows
CONTROL_TIMEOUT = 120  # --stop waits 5s for the run to go; uv start-up is the rest

# the last run this service started, polled only to reap it — a finished child
# of a service that lives for weeks is a zombie until someone waits on it
_child = None


def _running():
    """True while a run holds the audit's lock."""
    global _child
    if _child is not None and _child.poll() is not None:
        _child = None  # reaped
    try:
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _run_record():
    """The pass audit.py is writing (or wrote), None when unreadable. A file
    that is not an object would break every reader below, and this area's
    state() is part of the page's one state call — one bad file must not blank
    the whole page."""
    try:
        record = json.loads(RUN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _dismissed(record):
    """The (category, finding) pairs hidden in the pass `record` holds. Empty
    when the file is missing, unreadable, or stamped with another run — that
    last case is what makes a new run clear the lot, with nothing to delete.
    Entries that are not a pair of strings are dropped rather than raising:
    this runs inside the page's one state call."""
    try:
        saved = json.loads(DISMISSED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(saved, dict):
        return set()
    if saved.get("run") != (record or {}).get("started_at"):
        return set()
    out = set()
    for pair in saved.get("findings") or []:
        if (isinstance(pair, list) and len(pair) == 2
                and all(isinstance(s, str) for s in pair)):
            out.add(tuple(pair))
    return out


def _write_dismissed(run, pairs):
    """The dismissed list for one pass, replaced whole. 0600 like every other
    file in this service's state directory."""
    tmp = DISMISSED.with_name(DISMISSED.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"run": run, "findings": sorted(pairs)}, f)
    os.replace(tmp, DISMISSED)


def _hide(record, pairs):
    """Take the dismissed findings out of the record the page reads and leave
    each category the count of what went. The record is parsed fresh on every
    call, so this only ever edits the copy this reply carries."""
    for cat in record.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        findings = cat.get("findings") or []
        kept = [f for f in findings if (cat.get("label"), f) not in pairs]
        cat["dismissed"] = len(findings) - len(kept)
        cat["findings"] = kept


def _job_fields():
    """last-run stamp, next scheduled run, and whether the last run failed —
    from hermes' own cron files (same read as the messages area). Unreadable
    files -> None fields. A run going right now shows through the audit's own
    lock probe instead, so no executions-ledger read here."""
    try:
        job = next((j for j in json.loads(CRON_JOBS.read_text())["jobs"]
                    if j.get("name") == CRON_JOB), None)
    except (OSError, ValueError, KeyError):
        job = None
    return {"job_last_run_at": job.get("last_run_at") if job else None,
            "job_next_run_at": job.get("next_run_at") if job else None,
            "job_last_failed": bool(job and job.get("last_status")
                                    not in (None, "ok"))}


def _error_tail(after):
    """The failed run's stderr, but only when it was written after `after` —
    the stamp of the pass the record holds. Older text belongs to an earlier
    run: a hand-started run writes none of its own, and a start that died
    before audit.py could seed the record leaves the record on the pass
    before it.

    The whole second of grace is the stamp's own resolution: audit.py writes it
    with timespec="seconds", and uv prints its start-up lines to this same file
    a fraction of a second before audit.py seeds the stamp. Without the grace
    that noise reads as newer than the run it started, and a stopped run shows
    an error card full of it. A run that really dies writes its stderr steps
    into the pass, far past one second."""
    try:
        if ERR.stat().st_mtime <= datetime.fromisoformat(after).timestamp() + 1:
            return None
    except (OSError, TypeError, ValueError):
        pass
    try:
        return ERR.read_text(encoding="utf-8").strip()[-ERR_TAIL:] or None
    except OSError:
        return None


def start():
    """POST /api/hermes-audit/run: audit.py detached. The lock refuses a second
    run anyway; the probe here turns that into a 409 the page can show."""
    global _child
    if _running():
        return 409, {"error": "an audit run is already in progress"}
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        # stdin closed: a child CLI that reads stdin when it is not a terminal
        # would hang forever. Both LLM calls run under this.
        with open(ERR, "w", encoding="utf-8") as err:
            _child = subprocess.Popen(
                [str(UV), "run", "--no-project", str(SCRIPT)],
                stdout=subprocess.DEVNULL, stderr=err,
                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start the audit: {e}"}
    _log({"event": "hermes_audit_started", "result": "run started"})
    return 200, {"started": True}


def dismiss(body):
    """POST /api/hermes-audit/dismiss: hide one finding of the pass on screen.
    The record is the whole check — a category still running carries no
    findings yet, so it refuses itself, and a finding the record does not hold
    comes from a page that has not polled since the last run. Dismissing while
    a run goes on is allowed: a category that has landed never changes again."""
    label, finding = body.get("category"), body.get("finding")
    record = _run_record()
    if not record:
        return 404, {"error": "no audit pass on record"}
    cat = next((c for c in record.get("categories") or []
                if isinstance(c, dict) and c.get("label") == label), None)
    if cat is None:
        return 404, {"error": f"the last pass has no category {label!r}"}
    if finding not in (cat.get("findings") or []):
        return 404, {"error": "that finding is not in the pass on record — "
                              "reload the page"}
    try:
        with _DISMISS_LOCK:
            pairs = _dismissed(record)
            pairs.add((label, finding))
            _write_dismissed(record.get("started_at"), pairs)
    except OSError as e:
        return 500, {"error": f"could not save the dismissed list: {e}"}
    _log({"event": "hermes_audit_dismissed", "result": f"{label}: {finding}"})
    return 200, {"dismissed": True}


def restore(body):
    """POST /api/hermes-audit/restore: every finding dismissed in this pass
    comes back. Removing the file is the whole undo — a list from an older
    pass already reads as empty."""
    try:
        with _DISMISS_LOCK:
            os.remove(DISMISSED)
    except FileNotFoundError:
        pass
    except OSError as e:
        return 500, {"error": f"could not clear the dismissed list: {e}"}
    _log({"event": "hermes_audit_restored", "result": "dismissed findings back"})
    return 200, {"restored": True}


def control(flag, event):
    """One audit.py control run (--stop, --reset), in the request thread. Its
    own message is the page's answer, refusals included."""
    try:
        p = subprocess.run([str(UV), "run", "--no-project", str(SCRIPT), flag],
                           capture_output=True, text=True,
                           timeout=CONTROL_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 500, {"error": f"{flag} failed: {type(e).__name__}: {e}"}
    if p.returncode != 0:
        return 500, {"error": (p.stderr or p.stdout).strip()[-400:]}
    result = p.stdout.strip()
    _log({"event": event, "result": result})
    return 200, {"result": result}


# ---------------------------------------------------------------- area interface

NAME = "hermes_audit"


def boot():
    """Nothing to load: every answer is read from the audit's own files."""


def state():
    """The hermes-audit part of GET /api/state: whether a run is going, the pass
    itself (null before the first one), and the stderr of a run that ended
    without a report.

    A finished record with newer stderr is the other failure: the last start
    died before audit.py seeded a record, so the pass on file is the one
    before it. The page says which of the two happened.

    Dismissed findings are already out of the record here, and each category
    carries how many of its own went, so the page draws what is left without
    knowing the list. The cron job's stamps (last run, next run, failed) ride
    along for the page's schedule indicators."""
    running = _running()
    record = _run_record()
    if record:
        _hide(record, _dismissed(record))
    error = None
    if not running:
        stamp = record and (record.get("finished_at") or record.get("started_at"))
        error = _error_tail(stamp)
    out = {"running": running, "run": record, "error": error}
    out.update(_job_fields())
    return out


def _h_run(body):
    return start()


def _h_stop(body):
    return control("--stop", "hermes_audit_stopped")


def _h_reset(body):
    return control("--reset", "hermes_audit_reset")


HANDLERS = {"/api/hermes-audit/run": _h_run,
            "/api/hermes-audit/stop": _h_stop,
            "/api/hermes-audit/reset": _h_reset,
            "/api/hermes-audit/dismiss": dismiss,
            "/api/hermes-audit/restore": restore}
