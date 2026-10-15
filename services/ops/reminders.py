"""Reminders area — an overview of the Apple Reminders lists, read through
the host app's reminders server (services/mcp/reminders).

Served by server.py (one process, one page). Nothing here writes: the area
has no keys, so HANDLERS is empty.

- the lists come grouped by the first word of their name ("Casey WAITING"
  belongs to Casey), the lists in IGNORED_LISTS left out. A list whose
  last word is one of STAGES (NEXT, READY, WAITING ...) shows that stage
  as its label; another list shows its name minus the group word.
- inside a group the lists follow COLUMN order: the stages more groups
  share come first, so a shared stage lands in the same column on every
  card; the stages only one group uses follow, in workflow order, and the
  lists with no stage come last. Groups sort by open count, most first.
- each list carries its open count, how many of those are due today or
  earlier, its color as set in Reminders.app, and its first ITEMS open
  reminders in the server's own order (priority, then due date).
- the host app is read only while the page is open: server.py calls
  page_poll() on every poll the page itself makes (the one carrying the
  hold-update flag, never the menu-bar app's badge poll), and page_poll()
  starts one background read when the last one is older than REFRESH_S.
  The answer lives in STATE; state() only returns it, so a slow host app
  never holds a page poll. Each read is a full EventKit fetch. With the
  page closed, only lists() reads: the family emails area's clients that are
  not the page need the list names, so it reads once, in the caller's
  thread, when none are known. The first page poll after a service start or
  a cache clear returns the empty state, the next poll has the lists.
- clear() empties STATE and lets the next page poll read at once; the
  system area's clear-cache key calls it.
- due_today, for the tile, counts the open reminders due today or earlier
  in the lists whose name holds a word of MINE — the user's own lists, the
  ignored Alex list included; the other groups' due items stay off it.
"""

import datetime
import json
import pathlib
import sys
import threading
import time

APP = pathlib.Path(__file__).resolve().parent
PROJECT = APP.parent.parent
sys.path.append(str(PROJECT / "services" / "mcp" / "common"))
import call_host_tool  # noqa: E402

REMINDERS_PORT = 8352   # host app's reminders server
IGNORED_LISTS = {"Alex"}
MINE = ("casey", "alex")   # the tile counts only these lists
STAGES = ("next", "working", "ready", "scheduled", "waiting", "done")
ITEMS = 3
REFRESH_S = 60


def _call(tool, args):
    """(ok, text) from one call to the host app's reminders server."""
    return call_host_tool.call_sync(REMINDERS_PORT, tool, args)


def _empty(error):
    return {"error": error, "lists": [], "groups": [], "columns": 1, "open": 0, "due_today": 0}


def _stage(name):
    """'Casey WAITING' -> 'waiting'; a name whose last word is no stage -> ''."""
    last = name.split(" ")[-1]
    return last.lower() if last.isupper() and last.lower() in STAGES else ""


def _group(name):
    return name.split(" ")[0]


def _mine(name):
    return any(w in name.lower() for w in MINE)


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

    due = lambda rs: sum(1 for r in rs if r["due"] and r["due"] <= today)
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
                         "due": due(by_list[l["name"]]),
                         "items": [{"name": r["name"], "due": r["due"]}
                                   for r in by_list[l["name"]][:ITEMS]]})
        out.append({"label": label, "open": sum(r["count"] for r in rows), "lists": rows})
    out.sort(key=lambda g: (-g["open"], g["label"]))
    return {"groups": out,
            "columns": max((len(g["lists"]) for g in out), default=1),
            "open": sum(g["open"] for g in out),
            "due_today": due(r for r in reminders if _mine(r["list"]))}


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
    # the list names ride along for the family emails area's list choices
    return {"error": None, "lists": [l["name"] for l in lists],
            **build(lists, reminders, datetime.date.today().isoformat())}


def refresh():
    """One fetch into STATE; a malformed answer is kept as its error."""
    global STATE
    try:
        STATE = fetch()
    except (ValueError, KeyError, TypeError) as e:
        STATE = _empty(f"reminders server answered badly: {e}")


def lists():
    """The list names; one read in this thread when none are known yet."""
    if not STATE["lists"]:
        refresh()
    return STATE["lists"]


def page_poll():
    """One background read when the last one is older than REFRESH_S."""
    global _NEXT_READ
    if time.monotonic() < _NEXT_READ:
        return
    _NEXT_READ = time.monotonic() + REFRESH_S
    threading.Thread(target=refresh, daemon=True).start()


def clear():
    """Forget the last answer; the next page poll reads the host app."""
    global STATE, _NEXT_READ
    STATE = _empty(None)
    _NEXT_READ = 0.0


# ---------------------------------------------------------------- area interface

NAME = "reminders"
STATE = _empty(None)   # the last answer; empty with no error until the first read lands
_NEXT_READ = 0.0       # monotonic time before which page_poll() starts no read


def boot():
    """Nothing to load: the host app is read on the page's polls."""


def state():
    return STATE


HANDLERS = {}
