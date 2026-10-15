"""Shared plumbing for the ops service's area modules; stdlib only.

write_json is the shared state-file writer; cron_job and job_running_since
read hermes' cron files; lock_held probes a script's run lock; since and
elapsed give the "4 min" wording. _log takes its own lock, apart from any
area lock, so an area may call it while holding its own. _sha256 is the one
canonical form for every hash the service logs or compares (row
args_sha256): two copies would let the hashes drift apart."""

import fcntl
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent / "mcp" / "common"))
from data_root import root  # noqa: E402


def state_dir():
    """The folder every area's state file and the decisions log live in."""
    return root("ASSISTANT_OPS_STATE")

CRON_JOBS = pathlib.Path.home() / ".hermes/cron/jobs.json"
CRON_EXECUTIONS = pathlib.Path.home() / ".hermes/cron/executions.db"

_LOG_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(obj):
    """sha256 of the canonical JSON (sorted keys, tight separators)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_json(path, obj):
    """tmp file + fsync + os.replace + fsync of path's folder, mode 0600.
    Caller holds the area's lock."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def cron_job(name):
    """The named entry out of hermes' own jobs file. Its last_run_at answers
    "did the job run" rather than "did a run finish its work" — a run that
    dies early still moves it. Unreadable file or unknown name -> None."""
    try:
        jobs = json.loads(CRON_JOBS.read_text())["jobs"]
    except (OSError, ValueError, KeyError):
        return None
    return next((j for j in jobs if j.get("name") == name), None)


def job_running_since(job):
    """Start time of a run going right now: the newest attempt for the job
    in hermes' executions ledger, while it is still claimed/running. Covers
    scheduled runs and page-started ones alike. A run that dies uncleanly
    keeps its running row until hermes proves the owner process gone, so a
    dead run can read as running for a while. Any read problem -> None,
    shown as not running."""
    if not job or not job.get("id"):
        return None
    try:
        conn = sqlite3.connect(f"file:{CRON_EXECUTIONS}?mode=ro", uri=True,
                               timeout=1)
        try:
            row = conn.execute(
                "SELECT status, coalesce(started_at, claimed_at) "
                "FROM executions WHERE job_id = ? "
                "ORDER BY claimed_at DESC, id DESC LIMIT 1",
                (job["id"],)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row and row[0] in ("claimed", "running"):
        return row[1]
    return None


def lock_held(path):
    """True while a run holds the flock on path. flock releases itself when
    the holder dies, so a killed run never reads as still running."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
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


def since(started):
    """'4 min' or '1 h 5 min' from an epoch time to now."""
    m = int(max(0.0, time.time() - started) // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def elapsed(stamp):
    """since() from an ISO stamp, None when absent or unreadable."""
    try:
        return since(datetime.fromisoformat(stamp).timestamp())
    except (ValueError, TypeError):
        return None


def file_age(path):
    """since() from a file's mtime, None when it cannot be read: a run lock's
    mtime is the run's start."""
    try:
        return since(os.path.getmtime(path))
    except OSError:
        return None


def _log(event):
    """Append one JSON line to decisions-YYYY.jsonl, flushed + fsynced.
    result is capped at 500 chars, any args/rows blob at 1000; no tokens,
    env values, or headers are ever logged."""
    event = {"ts": _now(), **event}
    if "result" in event:
        event["result"] = str(event["result"])[:500]
    for key in ("args", "rows"):
        if key in event:
            blob = json.dumps(event[key], sort_keys=True)
            if len(blob) > 1000:
                event[key] = blob[:1000] + "…[capped]"
    path = state_dir() / f"decisions-{event['ts'][:4]}.jsonl"
    with _LOG_LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
            os.fsync(f.fileno())
