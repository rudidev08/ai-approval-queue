"""Reminders area — an overview of the Apple Reminders lists, read through
the host app's reminders server (services/mcp/reminders).

Served by server.py (one process, one page). Nothing here writes: the area
has no keys, so HANDLERS is empty.

- the lists come grouped by the first word of their name ("Alex WAITING"
  belongs to Alex), the lists in IGNORED_LISTS left out. A list whose
  last word is one of STAGES (NEXT, READY, WAITING ...) shows that stage
  as its label; another list shows its name minus the group word.
- inside a group the lists follow COLUMN order: the stages more groups
  share come first, so a shared stage lands in the same column on every
  card; the stages only one group uses follow, in workflow order, and the
  lists with no stage come last. Groups sort by open count, most first.
- each list carries its open count, its color as set in Reminders.app,
  and its first ITEMS open reminders in the server's own order (priority,
  then due date).
- a thread started in boot() reads the host app every REFRESH_S and keeps
  the answer in STATE; state() only returns it, so a slow host app never
  holds a page poll. Each read is a full EventKit fetch.
- due_today counts the open reminders due today or earlier, for the tile.
"""

import datetime
import json
import pathlib
import sys
import threading
import time

APP = pathlib.Path(__file__).resolve().parent
IRIS = APP.parent.parent
sys.path.append(str(IRIS / "services" / "mcp" / "common"))
import call_host_tool  # noqa: E402

REMINDERS_PORT = 4471   # host app's reminders server
IGNORED_LISTS = {"Next", "Later"}   # the user's own lists; the area shows the others
STAGES = ("next", "working", "ready", "scheduled", "waiting", "done")
ITEMS = 3
REFRESH_S = 60


def _call(tool, args):
    """(ok, text) from one call to the host app's reminders server."""
    return call_host_tool.call_sync(REMINDERS_PORT, tool, args)


def _empty(error):
    return {"error": error, "groups": [], "columns": 1, "open": 0, "due_today": 0}


def _stage(name):
    """'Alex WAITING' -> 'waiting'; a name whose last word is no stage -> ''."""
    last = name.split(" ")[-1]
    return last.lower() if last.isupper() and last.lower() in STAGES else ""


def _group(name):
    return name.split(" ")[0]


def build(lists, reminders, today):
    """The area's state out of get_lists' and search_reminders' json data:
    {groups, columns, open, due_today}."""
    lists = [l for l in lists if l["name"] not in IGNORED_LISTS]
    by_list = {l["name"]: [] for l in lists}
    for r in reminders:
        if r["list"] in by_list:
            by_list[r["list"]].append(r)

    # the stage columns: shared by more groups first, then workflow order
    users = {s: {_group(l["name"]) for l in lists if _stage(l["name"]) == s} for s in STAGES}
    columns = sorted((s for s in STAGES if users[s]),
                     key=lambda s: (-len(users[s]), STAGES.index(s)))
    rank = lambda name: columns.index(_stage(name)) if _stage(name) else len(columns)

    groups = {}
    for l in lists:
        groups.setdefault(_group(l["name"]), []).append(l)
    out = []
    for label, ls in groups.items():
        ls.sort(key=lambda l: (rank(l["name"]), l["name"]))
        rows = []
        for l in ls:
            stage = _stage(l["name"])
            rest = l["name"][len(label) + 1:] if l["name"].startswith(label + " ") else ""
            rows.append({"label": stage or rest, "color": l["color"], "count": l["open"],
                         "items": [{"name": r["name"], "due": r["due"]}
                                   for r in by_list[l["name"]][:ITEMS]]})
        out.append({"label": label, "open": sum(r["count"] for r in rows), "lists": rows})
    out.sort(key=lambda g: (-g["open"], g["label"]))
    return {"groups": out,
            "columns": max((len(g["lists"]) for g in out), default=1),
            "open": sum(g["open"] for g in out),
            "due_today": sum(1 for rs in by_list.values() for r in rs
                             if r["due"] and r["due"] <= today)}


def fetch():
    """One read of the host app: the lists, then every open reminder."""
    ok, text = _call("get_lists", {"format": "json"})
    if not ok:
        return _empty(text)
    lists = json.loads(text)["lists"]
    ok, text = _call("search_reminders", {"limit": 1000, "format": "json"})
    if not ok:
        return _empty(text)
    reminders = json.loads(text)["reminders"]
    return {"error": None, **build(lists, reminders, datetime.date.today().isoformat())}


def refresh():
    """One fetch into STATE; a malformed answer is kept as its error."""
    global STATE
    try:
        STATE = fetch()
    except (ValueError, KeyError, TypeError) as e:
        STATE = _empty(f"reminders server answered badly: {e}")


def _refresh_loop():
    while True:
        refresh()
        time.sleep(REFRESH_S)


# ---------------------------------------------------------------- area interface

NAME = "reminders"
STATE = _empty(None)   # the last answer; empty with no error until the first read lands


def boot():
    """Nothing to load: start the thread that reads the host app."""
    threading.Thread(target=_refresh_loop, daemon=True).start()


def state():
    return STATE


HANDLERS = {}
