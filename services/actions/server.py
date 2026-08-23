#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1"]
# ///
"""Actions service — plumbing only: HTTP surface, guards, dispatch.

The page's areas live in their own modules (emails.py, finance.py,
messages.py, hermes_audit.py, finance_report.py, research.py,
system.py); each
exposes NAME, boot(), state(), HANDLERS ({POST path: fn(body) -> (code,
payload)}) and optionally GET_HANDLERS ({GET path: fn(params) -> (code,
payload)}, params a single-valued query dict). This file owns everything
else:

- binding: 127.0.0.1:13727 plus the tailscale address 100.64.0.1:13727,
  the latter retried in a loop at boot (tailscaled may not be up when the
  service starts). Tailnet + localhost only, no bearer token.
- guards, run before anything else (403 on failure): the Host header must be
  one of the two bound addresses, localhost:13727, or
  mac-mini.your-tailnet.ts.net — with :13727 when reached directly,
  bare when reached through the tailnet root (tailscale serve proxies
  https/443 to the service, so no port rides the Host header); an Origin
  header, when present, must be the service's own; mutating endpoints
  require X-Actions-Local: 1.
- GET / (the page, Cache-Control: no-cache), GET /tap-feedback.css,
  GET /bar.js and GET /dev-bar.js (key-feedback styles and the shared
  top-bar and dev-tile components, shared with the dashboard page; the files
  live in app/), GET /apple-touch-icon.png (the home-screen icon, next to
  page.html),
  GET /dev1.html../dev5.html (design-proposal pages next to page.html,
  404 when the file is absent; HEAD gives the page's [dev] tiles an
  existence probe), GET /api/state — {"emails": ..., "finance": ...,
  "finance_report": ..., "research": ..., "messages": ...,
  "hermes_audit": ..., "system": ..., "status_issues": N,
  "status_checked_at": iso}: each area's state() plus the dashboard-page issue
  count. status_issues is -1 when the check itself failed; both keys are
  absent until the first check finishes.
- the status-indicator thread: every 5 minutes runs
  hermes/iris-status/hermes-iris-status --json and counts the items whose
  state is not "ok" or "--" (the script exits 1 when problems exist — that
  is a count, not a check failure).
- SIGTERM -> clean exit so the areas' atexit hooks run (the emails area's
  webmail child dies with us).
"""

import http.server
import json
import os
import pathlib
import re
import signal
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import common
import emails
import finance
import finance_report
import research
import messages
import hermes_audit
import system

APP = pathlib.Path(__file__).resolve().parent
IRIS = APP.parent.parent
PAGE = APP / "page.html"
TAP_CSS = IRIS / "app" / "tap-feedback.css"   # shared with the dashboard page
BAR_JS = IRIS / "app" / "bar.js"              # shared with the dashboard page
DEV_BAR_JS = IRIS / "app" / "dev-bar.js"      # shared with the dashboard page
ARMED_JS = IRIS / "app" / "armed-button.js"   # shared with the dashboard page
TOUCH_ICON = APP / "apple-touch-icon.png"     # the home-screen icon
IRIS_STATUS = IRIS / "hermes" / "iris-status" / "hermes-iris-status"

PORT = 13727
TAILSCALE_IP = "100.64.0.1"
MAGICDNS = "mac-mini.your-tailnet.ts.net"
# the bare MagicDNS pair serves the tailnet root (tailscale serve https/443
# proxies here); the :13727 pairs are the direct addresses
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"{MAGICDNS}:{PORT}",
                 f"{TAILSCALE_IP}:{PORT}", MAGICDNS}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}",
                   f"http://{MAGICDNS}:{PORT}", f"http://{TAILSCALE_IP}:{PORT}",
                   f"https://{MAGICDNS}"}

AREAS = (emails, finance, finance_report, research, messages,
         hermes_audit, system)

STATUS_INTERVAL_S = 300
STATUS_TIMEOUT_S = 60
# {"issues": N, "checked_at": iso}; rebound atomically, empty until the
# first check finishes
_STATUS = {}


def _dev_file(path):
    """The design-proposal file a /devN.html request names, None otherwise."""
    if re.fullmatch(r"/dev[1-5]\.html", path):
        f = APP / path[1:]
        if f.exists():
            return f
    return None


def _status_once():
    """One dashboard-page issue count; any failure of the check itself is -1."""
    global _STATUS
    try:
        out = subprocess.run([str(IRIS_STATUS), "--json"], capture_output=True,
                             text=True, timeout=STATUS_TIMEOUT_S)
        data = json.loads(out.stdout)
        issues = sum(1 for c in data["categories"] for i in c["items"]
                     if i["state"] not in ("ok", "--"))
        _STATUS = {"issues": issues, "checked_at": common._now()}
    except Exception:
        _STATUS = {"issues": -1, "checked_at": common._now()}


def _status_loop():
    while True:
        _status_once()
        time.sleep(STATUS_INTERVAL_S)


def _state():
    out = {area.NAME: area.state() for area in AREAS}
    if _STATUS:
        out["status_issues"] = _STATUS["issues"]
        out["status_checked_at"] = _STATUS["checked_at"]
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "actions/3"

    def log_message(self, format, *args):
        # request lines go to stdout (actions.log); stderr keeps only real
        # errors — the default writes everything to stderr
        print("%s - - [%s] %s" % (self.address_string(),
                                  self.log_date_time_string(), format % args),
              flush=True)

    def _guard(self, mutating):
        """The three checks that run before anything else (403 on failure)."""
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self._reply(403, {"error": "host not allowed"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            self._reply(403, {"error": "origin not allowed"})
            return False
        if mutating and self.headers.get("X-Actions-Local") != "1":
            self._reply(403, {"error": "mutating calls need X-Actions-Local: 1"})
            return False
        return True

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file_reply(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # phone browsers otherwise cache heuristically and keep showing
        # an old page after changes (same as the dashboard page)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._guard(mutating=False):
            return
        split = urlsplit(self.path)
        path = split.path
        if path == "/":
            self._file_reply(PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/tap-feedback.css":
            self._file_reply(TAP_CSS.read_bytes(), "text/css; charset=utf-8")
            return
        if path == "/dev-bar.js":
            self._file_reply(DEV_BAR_JS.read_bytes(),
                             "text/javascript; charset=utf-8")
            return
        if path == "/bar.js":
            self._file_reply(BAR_JS.read_bytes(),
                             "text/javascript; charset=utf-8")
            return
        if path == "/apple-touch-icon.png":
            self._file_reply(TOUCH_ICON.read_bytes(), "image/png")
            return
        if path == "/armed-button.js":
            self._file_reply(ARMED_JS.read_bytes(),
                             "text/javascript; charset=utf-8")
            return
        if (dev := _dev_file(path)):
            self._file_reply(dev.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._reply(200, _state())
            return
        handler = next((h for area in AREAS
                        if (h := getattr(area, "GET_HANDLERS", {}).get(path))), None)
        if handler is None:
            self._reply(404, {"error": "not found"})
            return
        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        self._reply(*handler(params))

    def do_HEAD(self):
        # existence probe for the dev design pages (the page's [dev] tiles)
        if not self._guard(mutating=False):
            return
        code = 200 if _dev_file(urlsplit(self.path).path) else 404
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._guard(mutating=True):
            return
        path = urlsplit(self.path).path
        handler = next((area.HANDLERS.get(path) for area in AREAS
                        if path in area.HANDLERS), None)
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
        self._reply(*handler(body))


def _tailscale_server():
    """Binds the tailscale address once the interface is up, then serves."""
    while True:
        try:
            srv = http.server.ThreadingHTTPServer((TAILSCALE_IP, PORT), Handler)
            break
        except OSError:
            time.sleep(5)
    print(f"actions on {TAILSCALE_IP}:{PORT}", flush=True)
    srv.serve_forever()


def _sigterm(signum, frame):
    raise SystemExit(0)


def main():
    common.STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(common.STATE_DIR, 0o700)

    # newsyslog reads this to bounce the service after rotating the logs
    # (launchd KeepAlive restarts us, giving fresh handles on the new files)
    (pathlib.Path.home() / ".hermes" / "logs" / "actions.pid").write_text(
        str(os.getpid()))

    # SIGTERM exits cleanly so atexit hooks run (the webmail child dies
    # with us); areas boot before serving starts
    signal.signal(signal.SIGTERM, _sigterm)
    for area in AREAS:
        area.boot()

    threading.Thread(target=_status_loop, daemon=True).start()
    threading.Thread(target=_tailscale_server, daemon=True).start()
    print(f"actions on 127.0.0.1:{PORT}; binding {TAILSCALE_IP}:{PORT} in the "
          "background until the tailnet interface is up", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
