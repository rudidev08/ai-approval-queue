#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1", "pyyaml==6.0.3"]
# ///
"""Ops service — plumbing only: HTTP surface, guards, dispatch.

The one place every assistant control runs. The actions page is one client; the
assistant_ops MCP server, the cron wrappers and the menu-bar app are others. The
page's areas (AREAS) live in their own modules; each area exposes NAME,
boot(), state(),
HANDLERS ({POST path: fn(body) -> (code, payload)}) and optionally
GET_HANDLERS ({GET path: fn(params) -> (code, payload)}, params a
single-valued query dict). The API module overview.py has no page area:
it exposes GET_HANDLERS.
This file owns everything else:

- binding: 127.0.0.1:8300 for everything; a reverse proxy on the private
  network publishes it as https://assistant.example.net. The port binds before
  the areas boot.
- guards, run before anything else (403 on failure): the Host header must be
  127.0.0.1:8300, localhost:8300, or assistant.example.net (no port: the
  proxy keeps the service's own Host header); an Origin
  header, when present, must be the service's own; every API request
  (/api/ and /demo/api/, any method) requires X-Local-Request: 1. A cross-site
  page cannot send a custom header without a preflight this service never
  answers, so no page but these can call the API; the page files load
  without it.
- page files, all in pages/ (GET and HEAD, Cache-Control: no-cache): / is
  the actions page (actions.html), /overview the overview page
  (overview.html), /<name> any other file directly in pages/ with a known
  type (the shared bar, styles and scripts, the icons, the dev1..dev5.html
  design proposals). HEAD gives the pages' [dev] tiles an existence probe.
- GET /demo/api/... and POST /demo/api/... (the actions page's [demo] mode:
  demo.py answers the GETs with canned data, every POST is a 202 that does
  nothing), GET /api/state — each area's state() under its NAME, plus
  "status_issues": N and "status_checked_at": iso, the status check's issue
  count. status_issues is -1 when the check itself failed; both keys are
  absent until the first check finishes. The page's own poll carries the
  hold-update query flag, which stamps the finance area's page-open marker
  (finance.page_seen) and lets the reminders and inbox areas start their
  host-app reads (reminders.page_poll, inbox.page_poll: the Reminders
  lists once a minute, the calendar list once); pollers without the flag
  — the menu-bar app's badge count — never hold a scheduled finance save
  and never start a host-app read.
- POST /api/system/clear-cache — the system area's clear-cache key, handled
  here because the status thread lives here: drops the reminders area's
  last answer and the inbox area's calendar list (both read again on the
  next page poll) and starts one status check now, whose result replaces
  the cached one when it lands.
- the status-indicator thread: 2 minutes after start, then every 15
  minutes, runs hermes/assistant-status/hermes-assistant-status --json and counts the
  items whose state is not "ok" or "--". The wait lets services that start
  at the same moment (the other web services) open their ports first. The same
  pass hands the cron items' states to the jobs area, which reports them
  per job.
- a handler that raises answers 500 with the error's type and text, its
  traceback on stderr (ops.error.log), so every client sees the real error
  instead of a dropped connection.
- SIGTERM -> clean exit so the areas' atexit hooks run (the inbox area's
  mail child dies with us).
"""

import http.server
import json
import os
import pathlib
import signal
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit

import common
from data_root import root
import demo
import inbox
import finance
import finance_reports
import family_emails
import research
import reminders
import chat_area
import hermes_audit
import jobs
import system
import job_rules
import overview

PAGES = pathlib.Path(__file__).resolve().parent / "pages"
# the two pages' paths; every other page file is served under its own name
ROUTES = {"/": "actions.html", "/overview": "overview.html"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png"}

PORT = 8300
SERVICE_DNS = "assistant.example.net"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", SERVICE_DNS}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}",
                   f"https://{SERVICE_DNS}"}

AREAS = (inbox, finance, finance_reports, family_emails, research, chat_area,
         hermes_audit, jobs, reminders, system, job_rules)
API_MODULES = (overview,)

STATUS_FIRST_WAIT_S = 120
STATUS_INTERVAL_S = 900
# every folder or file this process or a child it starts writes, one variable
# each; the launchd plist sets them, main() checks them before a port opens
VARIABLES = ("ASSISTANT_OPS_STATE", "ASSISTANT_FINANCE_NOTES", "ASSISTANT_BUDGET_API_CACHE",
             "ASSISTANT_WEB_RESEARCH", "ASSISTANT_WEB_RESEARCH_STATE",
             "ASSISTANT_HERMES_AUDIT_STATE", "ASSISTANT_AUDIT_REPORT", "ASSISTANT_AUDIT_EXPECTED",
             "ASSISTANT_PARCEL_CARRIERS",
             "ASSISTANT_PAPERS", "ASSISTANT_SHARED_PAPERS", "ASSISTANT_TOOL_APPROVALS_LOG",
             "ASSISTANT_LLM_PAUSE",
             "ASSISTANT_MAIL_DOCS", "ASSISTANT_LAUNCHD_SOURCE")


def _page_file(path):
    """The pages/ file a request path names, None otherwise: a route, or a
    file directly in pages/ whose type is known."""
    name = ROUTES.get(path) or path[1:]
    if "/" in name or name.startswith(".") or pathlib.Path(name).suffix not in TYPES:
        return None
    f = PAGES / name
    return f if f.is_file() else None


def _status_once():
    """One status check: the problem items go to the system area, which
    lists them on the page; _state() counts them for the badge."""
    try:
        data = system.status_json()
        items = system.problems(data)
        # a failed pass leaves the jobs area's previous states standing
        # (stale beats none)
        jobs.set_assistant_states({i["name"]: {"state": i["state"],
                                          "detail": i["detail"]}
                              for c in data["categories"] for i in c["items"]
                              if i["kind"] == "cron"}, data["generated"])
        system.set_status(items, common._now())
    except Exception:
        system.set_status(None, common._now())


def _status_loop():
    time.sleep(STATUS_FIRST_WAIT_S)
    while True:
        _status_once()
        time.sleep(STATUS_INTERVAL_S)


def _clear_cache():
    """The clear-cache key: forget every cached read, check status now."""
    reminders.clear()
    inbox.clear_calendars()
    threading.Thread(target=_status_once, daemon=True).start()
    return 200, {}


def _state():
    out = {area.NAME: area.state() for area in AREAS}
    status = out["system"]["status"]
    if status["checked_at"]:
        out["status_issues"] = -1 if status["items"] is None else len(status["items"])
        out["status_checked_at"] = status["checked_at"]
    return out


def _run(fn, *args):
    """fn's (code, payload); an exception is a 500 carrying its text."""
    try:
        return fn(*args)
    except Exception as e:
        traceback.print_exc()
        return 500, {"error": f"{type(e).__name__}: {e}"}


class _Base(http.server.BaseHTTPRequestHandler):
    server_version = "ops/1"

    def log_message(self, format, *args):
        # request lines go to stdout (ops.log); stderr keeps only real
        # errors — the default writes everything to stderr
        print("%s - - [%s] %s" % (self.address_string(),
                                  self.log_date_time_string(), format % args),
              flush=True)

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Handler(_Base):
    def _guard(self):
        """The three checks that run before anything else (403 on failure)."""
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self._reply(403, {"error": "host not allowed"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            self._reply(403, {"error": "origin not allowed"})
            return False
        api = urlsplit(self.path).path.startswith(("/api/", "/demo/api/"))
        if api and self.headers.get("X-Local-Request") != "1":
            self._reply(403, {"error": "API calls need X-Local-Request: 1"})
            return False
        return True

    def _file_reply(self, f, body=True):
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[f.suffix])
        self.send_header("Content-Length", str(len(data)))
        # phone browsers otherwise cache heuristically and keep showing
        # an old page after changes
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self):
        if not self._guard():
            return
        split = urlsplit(self.path)
        path = split.path
        if (f := _page_file(path)):
            self._file_reply(f)
            return
        if path == "/api/state":
            if "hold-update" in parse_qs(split.query, keep_blank_values=True):
                finance.page_seen()
                reminders.page_poll()
                inbox.page_poll()
            self._reply(*_run(lambda: (200, _state())))
            return
        handler = next((h for area in (*AREAS, *API_MODULES, demo)
                        if (h := getattr(area, "GET_HANDLERS", {}).get(path))), None)
        if handler is None:
            self._reply(404, {"error": "not found"})
            return
        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        self._reply(*_run(handler, params))

    def do_HEAD(self):
        if not self._guard():
            return
        if (f := _page_file(urlsplit(self.path).path)):
            self._file_reply(f, body=False)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path.startswith("/demo/api/"):
            # the page's optimistic row states show, then its next poll
            # brings the canned rows back; 202 satisfies every check the
            # page makes on a write's answer
            self._reply(202, {})
            return
        if path == "/api/system/clear-cache":
            self._reply(*_run(_clear_cache))
            return
        handler = next((h for area in (*AREAS, *API_MODULES)
                        if (h := getattr(area, "HANDLERS", {}).get(path))), None)
        if handler is None:
            self._reply(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
        except (ValueError, OSError):
            self._reply(400, {"error": "bad json"})
            return
        if not isinstance(body, dict):
            self._reply(400, {"error": "bad json"})
            return
        self._reply(*_run(handler, body))


def _sigterm(signum, frame):
    raise SystemExit(0)


def main():
    for name in VARIABLES:
        root(name)
    state_dir = common.state_dir()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)

    # newsyslog reads this to bounce the service after rotating the logs
    # (launchd KeepAlive restarts us, giving fresh handles on the new files)
    (pathlib.Path.home() / ".hermes" / "logs" / "ops.pid").write_text(
        str(os.getpid()))

    # the port binds before the areas boot, so a port another process holds
    # fails the start before the mail child and the threads exist
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    signal.signal(signal.SIGTERM, _sigterm)
    for area in AREAS:
        area.boot()

    threading.Thread(target=_status_loop, daemon=True).start()
    print(f"ops on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
