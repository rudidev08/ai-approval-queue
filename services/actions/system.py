"""System area — one restart key per long-lived hermes process.

Served by server.py (one process, one page). The hermes gateway and the
hermes webui each read the tool-approvals plugin once, at start. A plugin
deploy made after that is unenforced until the process restarts, which is
what the Hermes audit reports as "started before the newest plugin deploy".
These keys clear that finding without a terminal.

Both are launchd agents in this user's GUI domain, and so is this service, so
`launchctl kickstart -k` reaches them: -k kills the running process, and the
agent's KeepAlive starts it again. kickstart never writes a plist, so the
gateway keeps the launchd settings it has (`hermes gateway install`, `start`,
and `restart --all` would rewrite them).

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/system/restart-gateway  restart ai.hermes.gateway
- POST /api/system/restart-webui     restart com.example.iris.webui
- POST /api/system/doctor-fix        run `hermes doctor --fix`

Each restart answers 200 once launchctl exits 0, and 502 with launchctl's own
text otherwise (an unknown label, a domain that is not loaded).

doctor-fix is the audit's doctor-warning key: `hermes doctor --fix` repairs
what doctor knows how to (the macOS TCC anchor on the venv python, the CA
bundle, the ~/.local/bin/hermes link) and leaves the rest as warnings. One
run covers both profiles: the repairs live in the shared install, not in a
profile home. It answers 200 with the count doctor reports as fixed, and 502
with doctor's own text when it exits non-zero.

state() reports when each agent's current process started, which is how the
page says whether a restart landed: `launchctl list <label>` gives the pid
launchd holds for that label, and ps gives that pid's start time. Going
through the label ties the answer to the agent the key restarts, not to a
command line that could match something else.
"""

import os
import re
import subprocess
import time
from datetime import datetime, timezone

from common import _log

LAUNCHCTL = "/bin/launchctl"
PS = "/bin/ps"
HERMES = "hermes"

LABELS = {"gateway": "ai.hermes.gateway",
          "webui": "com.example.iris.webui"}

# kickstart returns as soon as launchd holds the new process; the timeout is
# only there so a wedged launchd cannot hold the request open
TIMEOUT = 30
# doctor probes every provider and tool server on the way; the audit gives it
# the same budget
DOCTOR_TIMEOUT = 120


def restart(name):
    """Kickstart one agent, killing the process it replaces."""
    label = LABELS[name]
    target = f"gui/{os.getuid()}/{label}"
    try:
        p = subprocess.run([LAUNCHCTL, "kickstart", "-k", target],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 502, {"error": f"launchctl: {e}"}
    if p.returncode != 0:
        text = (p.stderr or p.stdout).strip()[:200]
        return 502, {"error": f"launchctl kickstart {target} exited "
                              f"{p.returncode}: {text}"}
    _log({"event": "system_restart", "result": label})
    return 200, {"restarted": label}


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


def _started_at(label):
    """When the label's current process started, as an ISO stamp. None when
    launchd holds no pid for it, the process left between the two calls, or
    either command fails."""
    try:
        listing = subprocess.run([LAUNCHCTL, "list", label], capture_output=True,
                                 text=True, timeout=TIMEOUT)
        pid = re.search(r'"PID"\s*=\s*(\d+);', listing.stdout)
        if not pid:
            return None
        started = subprocess.run([PS, "-p", pid.group(1), "-o", "lstart="],
                                 capture_output=True, text=True, timeout=TIMEOUT)
        epoch = time.mktime(time.strptime(started.stdout.strip(),
                                          "%a %b %d %H:%M:%S %Y"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- area interface

NAME = "system"


def boot():
    """Nothing to load: every answer is read from launchd at call time."""


def state():
    """The system part of GET /api/state: when each agent's current process
    started, so the page can say how long it has been up. None for an agent
    launchd holds no process for, and the page says that instead."""
    return {name: _started_at(label) for name, label in LABELS.items()}


def _h_gateway(body):
    return restart("gateway")


def _h_webui(body):
    return restart("webui")


def _h_doctor_fix(body):
    return doctor_fix()


HANDLERS = {"/api/system/restart-gateway": _h_gateway,
            "/api/system/restart-webui": _h_webui,
            "/api/system/doctor-fix": _h_doctor_fix}
