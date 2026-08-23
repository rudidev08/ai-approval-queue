"""Shared plumbing for the actions service's area modules.

STATE_DIR holds every area's persistent files: emails' state.json and the
decisions log all areas append to. _log is the one appender — one JSON line
per event, flushed + fsynced, under its own lock (independent of any area
lock; an area may call it while holding its own). _sha256 is the one
canonicalization for every hash the service logs or compares (row
args_sha256) — two copies would let the hashes drift apart. Everything here
is stdlib-only."""

import hashlib
import json
import os
import pathlib
import threading
from datetime import datetime, timezone

STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"

_LOG_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(obj):
    """sha256 of the canonical JSON (sorted keys, tight separators)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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
