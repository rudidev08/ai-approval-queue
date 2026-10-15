"""audit_snoozes — the Hermes audit findings the user snoozed.

The actions page's snooze key writes them through the ops service. audit.py
reads them when it writes the report; the ops service reads them when it
counts open findings for the page and the menu bar badge.

The file is snoozes.json in the folder ASSISTANT_HERMES_AUDIT_STATE names:
{category: {finding text: until}}, until a local stamp with offset. A
finding matches by its exact text, so a finding whose text changes shows
again. audit.py --reset leaves the file: a snooze is the user's decision, not
something the audit learned. stdlib only.
"""

import json
import os
from datetime import datetime, timedelta

from data_root import root

DAYS = 14


def path():
    return root("ASSISTANT_HERMES_AUDIT_STATE") / "snoozes.json"


def read():
    """The snoozes not yet over; a missing or unreadable file snoozes
    nothing."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = datetime.now().astimezone()
    return {label: live for label, found in data.items()
            if (live := {f: until for f, until in found.items()
                         if datetime.fromisoformat(until) > now})}


def snooze(label, finding):
    """Snooze one finding for DAYS. Returns its until stamp."""
    until = (datetime.now().astimezone().replace(microsecond=0)
             + timedelta(days=DAYS)).isoformat()
    snoozes = read()
    snoozes.setdefault(label, {})[finding] = until
    _write(snoozes)
    return until


def unsnooze(label, finding):
    snoozes = read()
    snoozes.get(label, {}).pop(finding, None)
    _write({k: v for k, v in snoozes.items() if v})


def _write(snoozes):
    """Snoozes that ran out drop here: read() left them out."""
    tmp = path().with_name(f"snoozes.json.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(snoozes, indent=1), encoding="utf-8")
    os.replace(tmp, path())
