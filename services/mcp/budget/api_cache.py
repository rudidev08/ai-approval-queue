#!/usr/bin/env python3
"""api_cache — the api-cache copy layer, shared by server.py and cash_flow.py.

Every reader reaches the copy through db(), so any read refreshes a stale
copy. A failed pull fails the read; the stamp stays unwritten, so the next
read retries. Stdlib only: cash_flow.py readers run on the system python.
"""

import glob
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import hermes_env  # noqa: E402
from data_root import root  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.expanduser("~/Assistant/services/budget")
CONFIG = os.path.expanduser("~/.config/assistant/budget.env")
NODE = "/opt/homebrew/bin/node"
SERVER_URL = "http://127.0.0.1:8303"
HELPER_TIMEOUT = 300
FRESH_SECONDS = 600     # a download younger than this makes the next one redundant


def cache_dir():
    """The api-cache copy: the budget folder budget_helper.mjs downloads
    into, plus the refresh stamp."""
    return root("ASSISTANT_BUDGET_API_CACHE")


def _stamp_file():
    return cache_dir() / "refresh-stamp"


def _config() -> dict:
    """KEY=VALUE pairs from CONFIG; raises when the required keys are missing."""
    try:
        values = hermes_env.read(CONFIG)
    except OSError:
        values = {}
    missing = [k for k in ("BUDGET_PASSWORD", "BUDGET_SYNC_ID") if not values.get(k)]
    if missing:
        raise RuntimeError(f"missing {', '.join(missing)} in {CONFIG}")
    return values


def run_budget_helper(command: dict) -> dict:
    """Run budget_helper.mjs in a fresh Node process; returns {results, pushed}."""
    cfg = _config()
    helper = os.path.join(HERE, "budget_helper.mjs")
    cache = cache_dir()
    # The API's weekly full upload (load-budget -> possiblyUpload, 7 days after
    # metadata.json's lastUploaded) copies the budget to backup-for-export-*.db
    # in the background; a short run's process.exit(0) leaves the copy behind.
    for stray in glob.glob(os.path.join(cache, "backup-for-export-*")):
        os.remove(stray)
    env = dict(os.environ)
    env.update({
        "BUDGET_SERVER_URL": cfg.get("BUDGET_SERVER_URL", SERVER_URL),
        "BUDGET_PASSWORD": cfg["BUDGET_PASSWORD"],
        "BUDGET_SYNC_ID": cfg["BUDGET_SYNC_ID"],
        "BUDGET_CACHE_DIR": str(cache),
    })
    try:
        result = subprocess.run([NODE, helper, json.dumps(command)], cwd=APP, env=env,
                                capture_output=True, text=True, timeout=HELPER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"write helper timed out after {HELPER_TIMEOUT}s") from None
    # The result line means the run's work completed; a crash after it (the
    # API's schedule service racing the budget close on the first sync of a
    # day) must not fail the call.
    for line in result.stdout.splitlines():
        if line.startswith("WRITE_RESULT "):
            # the run's downloadBudget made the copy current, whatever the command
            with open(_stamp_file(), "w") as f:
                f.write(str(time.time()))
            return json.loads(line[len("WRITE_RESULT "):])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        # tail: Node and Python both put the exception line just before the
        # stack; 1000 chars lost it behind a 9-frame stack
        raise RuntimeError(f"write helper failed: {detail[-2000:]}")
    raise RuntimeError(f"write helper returned no result JSON: {result.stdout[-500:]!r}")


def _stamp_age() -> float:
    """Seconds since the last successful download; infinite when unknown."""
    try:
        with open(_stamp_file()) as f:
            return time.time() - float(f.read())
    except (OSError, ValueError):
        return float("inf")


_fresh_lock = threading.Lock()


def pull_api_cache_if_stale():
    """Pull the budget from the server when the copy's last download is older
    than FRESH_SECONDS."""
    if _stamp_age() < FRESH_SECONDS:
        return
    with _fresh_lock:
        if _stamp_age() < FRESH_SECONDS:    # a concurrent call just refreshed
            return
        run_budget_helper({"cmd": "pull"})


def db():
    """The api-cache copy read-only, pulled fresh first when it is stale."""
    pull_api_cache_if_stale()
    cache = cache_dir()
    paths = glob.glob(os.path.join(cache, "*", "db.sqlite"))
    if len(paths) != 1:
        raise RuntimeError(f"expected one budget copy under {cache}, found {len(paths)}")
    conn = sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
