#!/usr/bin/env python3
"""api_cache — the api-cache copy layer, shared by server.py and cash_flow.py.

pull_api_cache_if_stale() pulls the budget from the server when the copy's
last download is older than FRESH_SECONDS; every caller of the copy reaches it
through db(), so any read refreshes a stale copy. A failed pull fails the
read; the stamp stays unwritten, so the next read retries. run_budget_helper()
runs one budget_helper.mjs command (pull or update) in a fresh Node process
and stamps the time — every successful run's downloadBudget made the copy
current, whatever the command. Stdlib only: cash_flow.py readers run on the
system python.
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

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.expanduser("~/Iris/services/actual")
API_CACHE = os.path.expanduser("~/.local/state/actual-api-cache")
CONFIG = os.path.expanduser("~/.config/actual/env")
NODE = "/opt/homebrew/bin/node"
SERVER_URL = "http://127.0.0.1:60195"
HELPER_TIMEOUT = 300
STAMP_FILE = os.path.join(API_CACHE, "refresh-stamp")
FRESH_SECONDS = 600     # a download younger than this makes the next one redundant


def _config() -> dict:
    """KEY=VALUE pairs from CONFIG; raises when the required keys are missing."""
    try:
        values = hermes_env.read(CONFIG)
    except OSError:
        values = {}
    missing = [k for k in ("ACTUAL_PASSWORD", "ACTUAL_SYNC_ID") if not values.get(k)]
    if missing:
        raise RuntimeError(f"missing {', '.join(missing)} in {CONFIG}")
    return values


def run_budget_helper(command: dict) -> dict:
    """Run budget_helper.mjs in a fresh Node process; returns {results, pushed}."""
    cfg = _config()
    helper = os.path.join(HERE, "budget_helper.mjs")
    env = dict(os.environ)
    env.update({
        "ACTUAL_SERVER_URL": cfg.get("ACTUAL_SERVER_URL", SERVER_URL),
        "ACTUAL_PASSWORD": cfg["ACTUAL_PASSWORD"],
        "ACTUAL_SYNC_ID": cfg["ACTUAL_SYNC_ID"],
        "ACTUAL_CACHE_DIR": API_CACHE,
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
            with open(STAMP_FILE, "w") as f:
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
        with open(STAMP_FILE) as f:
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
    paths = glob.glob(os.path.join(API_CACHE, "*", "db.sqlite"))
    if len(paths) != 1:
        raise RuntimeError(f"expected one budget copy under {API_CACHE}, found {len(paths)}")
    conn = sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
