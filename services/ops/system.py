"""System area — the hermes doctor and gateway plist repairs, the model
pause, and the status check's problem items.

Served by server.py (one process, one page). The gateway restart
keys live in the gateways area.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/system/doctor-fix        run `hermes doctor --fix`
- POST /api/system/gateway-plist     run `hermes gateway start` to rewrite a
  stale gateway plist, then copy it into the folder ASSISTANT_LAUNCHD_SOURCE names
- POST /api/system/llm-pause   body {"hours": 1|2|4|8}: write the pause
  file (services/mcp/common/llm_pause.py), replacing one in effect
- POST /api/system/llm-resume        delete the pause file
- GET /api/system/status-report  {text}: a fresh run of the stack status
  script (hermes/assistant-status/hermes-assistant-status), its aligned text report
- GET /api/system/health-check   {text, passed}: services/mcp/health_check.py,
  every read-only tool of every registered MCP server; write tools are never
  called. Up to a few minutes

doctor-fix is the audit's doctor-warning key: doctor repairs what it knows
how to (the macOS TCC anchor on the venv python, the CA bundle, the
~/.local/bin/hermes link) in the shared install and leaves the rest as
warnings. 200 with the count doctor reports as fixed, 502 with doctor's own
text when it exits non-zero.

gateway-plist is the audit's stale-plist key. A hermes update can change the
plist template; `hermes gateway start` then rewrites the installed plist and
reloads the gateway (the gateways area's restart never writes it).
- hermes writes the PATH of the shell that runs it into the plist. This
  service's PATH is short, so the run gets the PATH the gateway plist already
  holds, and the gateway keeps every folder it has now.
- It runs ~/.local/bin/hermes, the launcher the user types. The venv's own
  entry point resolves the checkout's real path and writes that into the plist,
  which hermes then reports as stale forever. The PATH above starts with the
  venv's bin folder, so the bare name would find that entry point.
The new plist is then copied to the setup/launchd copy, which the audit
diffs against the live one. 200 once both steps land, 502 with hermes' own
text when it exits non-zero or the copy fails.

state() carries when the gateway plist was last written, in the audit
run record's own stamp format. A plist written after the pass that flagged
it is the fix landing, so the audit card reads Fixed until the next pass.

state() also carries the newest status check, handed over by server.py's
status thread via set_status().
items holds one entry per assistant-status item whose state is not "ok" or "--",
so the page can list what the wordmark's issue count counts. items is
None when the check itself failed, and both fields are None until the first
check finishes.

state() also carries the pause in effect (llm_pause, the file's since,
until and source), or None, so the page's wordmark says PAUSE and the
system area shows the time left.
"""

import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
from datetime import datetime, timezone

from common import _log
from data_root import root
import llm_pause

PROJECT = pathlib.Path(__file__).resolve().parent.parent.parent
STATUS_SCRIPT = PROJECT / "hermes" / "assistant-status" / "hermes-assistant-status"
STATUS_TIMEOUT = 60
HEALTH_CHECK = PROJECT / "services" / "mcp" / "health_check.py"
HEALTH_TIMEOUT = 600
UV = pathlib.Path.home() / ".local" / "bin" / "uv"
HERMES = "hermes"
HERMES_LAUNCHER = pathlib.Path.home() / ".local" / "bin" / "hermes"
GATEWAY_PLIST = (pathlib.Path.home() / "Library" / "LaunchAgents"
                 / "ai.hermes.gateway.plist")

# doctor probes every provider and tool server on the way; the audit gives it
# the same budget
DOCTOR_TIMEOUT = 120
# gateway start writes the plist and hands the reload to a detached helper,
# so it returns before the gateway is back; the audit's status call gets 60
GATEWAY_START_TIMEOUT = 60


def doctor_fix():
    """Run `hermes doctor --fix` once, for the shared install."""
    try:
        p = subprocess.run([HERMES, "doctor", "--fix"], capture_output=True,
                           text=True, timeout=DOCTOR_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 502, {"error": f"hermes doctor: {e}"}
    if p.returncode != 0:
        text = (p.stderr or p.stdout).strip()[-200:]
        return 502, {"error": f"hermes doctor --fix exited {p.returncode}: "
                              f"{text}"}
    m = re.search(r"Fixed (\d+) issue", p.stdout)
    fixed = int(m.group(1)) if m else 0
    _log({"event": "doctor_fix", "result": f"fixed {fixed}"})
    return 200, {"fixed": fixed}


def gateway_plist():
    """Rewrite the stale gateway plist through hermes, keeping its PATH, then
    copy it into setup/launchd."""
    copy = root("ASSISTANT_LAUNCHD_SOURCE") / GATEWAY_PLIST.name
    try:
        with open(GATEWAY_PLIST, "rb") as f:
            path = plistlib.load(f)["EnvironmentVariables"]["PATH"]
    except (OSError, ValueError, KeyError) as e:
        return 502, {"error": f"cannot read the gateway PATH: {e}"}
    try:
        p = subprocess.run([str(HERMES_LAUNCHER), "gateway", "start"],
                           capture_output=True, text=True,
                           timeout=GATEWAY_START_TIMEOUT,
                           stdin=subprocess.DEVNULL,
                           env={**os.environ, "PATH": path})
    except (OSError, subprocess.TimeoutExpired) as e:
        return 502, {"error": f"hermes gateway start: {e}"}
    if p.returncode != 0:
        text = (p.stderr or p.stdout).strip()[-200:]
        return 502, {"error": f"hermes gateway start exited {p.returncode}: "
                              f"{text}"}
    try:
        shutil.copyfile(GATEWAY_PLIST, copy)
    except OSError as e:
        return 502, {"error": f"plist rewritten, but the copy failed: {e}"}
    _log({"event": "gateway_plist", "result": "plist rewritten and copied"})
    return 200, {"refreshed": True}


def plist_written_at():
    """When the gateway plist was last written, as a local stamp in the
    format audit.py writes its run stamps in. None when it is missing."""
    try:
        mtime = GATEWAY_PLIST.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


# ---------------------------------------------------------------- area interface

NAME = "system"

# {"items": [{"name", "state", "detail"}] | None, "checked_at": iso | None}
_STATUS = {"items": None, "checked_at": None}


def set_status(items, checked_at):
    """Called by server.py's status thread after each status check."""
    global _STATUS
    _STATUS = {"items": items, "checked_at": checked_at}


def boot():
    """Nothing to load: every answer is read at call time."""


def state():
    """The system part of GET /api/state: "status" is the newest status
    check's problem items and its stamp, "llm_pause" the pause in effect or
    None."""
    return {"status": _STATUS, "llm_pause": llm_pause.read()}


def _h_doctor_fix(body):
    return doctor_fix()


def _h_gateway_plist(body):
    return gateway_plist()


PAUSE_HOURS = (1, 2, 4, 8)


def _h_llm_pause(body):
    hours = body.get("hours")
    if hours not in PAUSE_HOURS:
        return 400, {"error": "hours must be one of 1, 2, 4, 8"}
    pause = llm_pause.start(hours * 60, "ops")
    _log({"event": "llm_pause", "result": pause["until"]})
    return 200, pause


def _h_llm_resume(body):
    llm_pause.clear()
    _log({"event": "llm_resume"})
    return 200, {}


def status_json():
    """The status check's full report: hermes-assistant-status --json, parsed.
    The script exits 1 when it finds problems; that is a report, not a
    failure. Raises when the script fails or prints no JSON."""
    p = subprocess.run([str(STATUS_SCRIPT), "--json"], capture_output=True,
                       text=True, timeout=STATUS_TIMEOUT)
    return json.loads(p.stdout)


def problems(report):
    """A status report's items whose state is not "ok" or "--": what both
    pages' WARN and the menu-bar badge count."""
    return [{"name": i["name"], "state": i["state"], "detail": i["detail"]}
            for c in report["categories"] for i in c["items"]
            if i["state"] not in ("ok", "--")]


def _h_status_report(params):
    """The script exits 1 when it finds problems; that is a report, not a
    failure. No output at all is the failure."""
    try:
        p = subprocess.run([str(STATUS_SCRIPT)], capture_output=True, text=True,
                           timeout=STATUS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 500, {"error": f"status script failed: {e}"}
    if not p.stdout:
        return 500, {"error": f"status script failed: {p.stderr.strip()}"}
    return 200, {"text": p.stdout.rstrip()}


def _h_health_check(params):
    try:
        p = subprocess.run([str(UV), "run", "--no-project", str(HEALTH_CHECK)],
                           capture_output=True, text=True, timeout=HEALTH_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 500, {"error": f"health check failed to run: {e}"}
    if p.returncode == 0:
        return 200, {"passed": True, "text": "health check passed: every read-only "
                     "tool of every registered MCP server answered."}
    out = (p.stdout + p.stderr).strip()
    return 200, {"passed": False, "text": "health check FAILED:\n" + (out or "(no output)")}


GET_HANDLERS = {"/api/system/status-report": _h_status_report,
                "/api/system/health-check": _h_health_check}

HANDLERS = {"/api/system/doctor-fix": _h_doctor_fix,
            "/api/system/gateway-plist": _h_gateway_plist,
            "/api/system/llm-pause": _h_llm_pause,
            "/api/system/llm-resume": _h_llm_resume}
