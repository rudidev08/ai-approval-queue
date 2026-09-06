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

The weekly hermes-audit cron job (hermes-home/scripts/hermes-audit.sh) is a
third starter; state() reads its stamps out of hermes' cron files so the page
can show when the scheduled pass last ran and runs next.

The run is detached (start_new_session): a pass can take an hour, and log
rotation SIGTERMs this service in the middle of one. Nothing is kept in this
process — the files carry it all across a restart.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/hermes-audit/run     start a pass. 409 when one is already going
- POST /api/hermes-audit/stop    stop the run in progress (audit.py --stop):
                                SIGTERM to its process group; it writes no
                                report and commits no state
- POST /api/hermes-audit/reset   audit.py --reset: remove the audit's state file,
                                so the next run is a first pass — every check
                                reviews from scratch, every baseline reseeds
- POST /api/hermes-audit/ignore-doctor {category, finding}: append the finding's
                                doctor warning to doctor_known in the audit's
                                expected.yaml, so future runs stop flagging it
"""

import fcntl
import json
import os
import pathlib
import subprocess
import threading
from datetime import datetime

from common import _log, cron_job

APP = pathlib.Path(__file__).resolve().parent
SCRIPT = APP.parent / "hermes-audit" / "audit.py"
EXPECTED = APP.parent / "hermes-audit" / "expected.yaml"
UV = pathlib.Path.home() / ".local" / "bin" / "uv"

# how audit.py prefixes a doctor finding; the text after it is the doctor
# line itself, which is what doctor_known entries match against
DOCTOR_MARK = ": new doctor warning — "

STATE = pathlib.Path.home() / "Local" / "iris-hermes-audit"
LOCK = STATE / "lock"
RUN = STATE / "run.json"
ERR = STATE / "last-run.err"

# the weekly cron job that also runs the audit; its stamps give the page the
# last-run and next-run indicators (same read as the messages area)
CRON_JOB = "hermes-audit"

# ignore-doctor is a read-modify-write on expected.yaml, and two warnings
# tapped in quick succession land on two request threads
_KNOWN_LOCK = threading.Lock()

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


def _job_fields():
    """last-run stamp, next scheduled run, and whether the last run failed —
    from hermes' own cron files (same read as the messages area). Unreadable
    files -> None fields. A run going right now shows through the audit's own
    lock probe instead, so no executions-ledger read here."""
    job = cron_job(CRON_JOB)
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


def ignore_doctor(body):
    """POST /api/hermes-audit/ignore-doctor: this doctor warning is expected
    from now on. Appends the warning (the text after DOCTOR_MARK) to the
    doctor_known list in expected.yaml — audit.py reads that list, so every
    future run stops flagging it. The yaml edit is plain line insertion inside the
    doctor_known block, so the file's comments survive. Only doctor findings
    qualify: no other category has a known-list to write to."""
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
    if DOCTOR_MARK not in finding:
        return 400, {"error": "only doctor warnings have a known-list to "
                              "join"}
    warning = finding.split(DOCTOR_MARK, 1)[1]
    entry = '  - "' + warning.replace("\\", "\\\\").replace('"', '\\"') + '"'
    try:
        with _KNOWN_LOCK:
            lines = EXPECTED.read_text(encoding="utf-8").splitlines()
            try:
                start = lines.index("doctor_known:")
            except ValueError:
                return 500, {"error": "expected.yaml has no doctor_known list"}
            end = start + 1
            while end < len(lines) and lines[end].startswith("  - "):
                end += 1
            if entry not in lines[start + 1:end]:
                lines.insert(end, entry)
                tmp = EXPECTED.with_name(EXPECTED.name + ".tmp")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(tmp, EXPECTED)
    except OSError as e:
        return 500, {"error": f"could not update the known list: {e}"}
    _log({"event": "hermes_audit_doctor_ignored", "result": warning})
    return 200, {"ignored": True}


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

    The cron job's stamps (last run, next run, failed) ride along for the
    page's schedule indicators."""
    running = _running()
    record = _run_record()
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
            "/api/hermes-audit/ignore-doctor": ignore_doctor}
