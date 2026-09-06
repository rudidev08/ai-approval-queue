"""Shared plumbing for the actions service's area modules.

STATE_DIR holds every area's persistent files: emails' state.json and the
decisions log all areas append to. _log is the one appender — one JSON line
per event, flushed + fsynced, under its own lock (independent of any area
lock; an area may call it while holding its own). _sha256 is the one
canonicalization for every hash the service logs or compares (row
args_sha256) — two copies would let the hashes drift apart. write_json is
the one state-file writer (tmp + fsync + replace); cron_job and
job_running_since are the one read of hermes' cron files. Everything here
is stdlib-only."""

import hashlib
import json
import os
import pathlib
import sqlite3
import threading
from datetime import datetime, timezone

STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"
# hermes' own cron files: the jobs file and the executions ledger
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
    """tmp file + fsync + os.replace + dir fsync, mode 0600. Caller holds
    the area's lock."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(STATE_DIR, os.O_RDONLY)
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
    path = STATE_DIR / f"decisions-{event['ts'][:4]}.jsonl"
    with _LOG_LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
            os.fsync(f.fileno())
