"""The overview page's data. An API module: no page area, only GET_HANDLERS.

GET /api/overview/snapshot builds everything the overview page renders, live
on each request:

- host and service state: a fresh status check (system.status_json), which
  also carries each service's probed address (a shared server such as the
  speech server may sit on another Mac); "issues" counts its problem items
  the same way the actions page does (system.problems)
- model roles, each with its backups: every job of the assistant and the voice page
  as its chain, from model_chains.py
- tool catalogue: tool functions parsed out of the MCP server sources (named
  in config.yaml, or the Assistant Host app's servers.json for its servers), so a
  renamed or removed tool shows up instead of going stale
- tool calls and sessions: ~/.hermes/state.db, 1-hour, 24-hour and 7-day
  windows
- the pause in effect on the assistant's model runs (llm_pause), which the wordmark
  shows as PAUSE

GET /api/overview/links is the link list alone: the menu-bar app reads it on
every menu open.
"""

import ast
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit

import yaml

import common
from host_service import APP_SUPPORT
import llm_pause
import model_chains
import system

HERMES = pathlib.Path.home() / ".hermes"
STATE_DB = HERMES / "state.db"
CONFIG = HERMES / "config.yaml"
HOST_SERVERS = pathlib.Path(APP_SUPPORT) / "servers.json"

# hermes has no machine-readable list of its built-in tools, so the ones the
# assistant profile can reach are kept by hand.
BUILTINS = ["web_search", "web_extract", "todo", "memory", "cronjob",
            "skills_list", "skill_view"]

# name, address
LINKS = [
    ("actions", "https://assistant.example.net/"),
    ("overview", "https://assistant.example.net/overview"),
    ("hermes dashboard", "https://dashboard.example.net/"),
]

LOOPBACK = ("127.0.0.1", "localhost", "::1")
_STATE = {"ok": "ok", "--": "idle"}  # anything else assistant-status emits is bad


def _host(st):
    mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True).stdout)
    disk = os.statvfs("/")
    h = st["host"]
    ram_total = mem / 2**30  # binary GB, the unit macOS reports RAM in
    used = (disk.f_blocks - disk.f_bavail) * disk.f_frsize
    return {
        "cpu": h["cpu"], "gpu": h["gpu"], "ram": h["ram"],
        "ramGb": round(h["ram"] * ram_total / 100, 1),
        "diskUsedGb": round(used / 1e9),
        "disk": round(100 * used / (disk.f_blocks * disk.f_frsize)),
    }


def _models_block(cfg):
    """Each group's rows, one per model of each chain: the first under the
    job's label, each backup under "label backup", "label backup 2" and so
    on. An assistant row carries its section (model_chains.assistant_section); other
    groups' rows carry "". The assistant adds a tests row: the SOUL suite copies the
    live config's model block into its throwaway homes, so its model is the
    main one."""
    def rows(g, chains):
        return [{"name": m, "label": label if i == 0 else f"{label} backup"
                 + (f" {i}" if i > 1 else ""), "provider": p,
                 "section": model_chains.assistant_section(cfg, label) if g == "assistant" else ""}
                for label, pairs in chains for i, (m, p) in enumerate(pairs)]
    groups = {g: rows(g, c) for g, c in model_chains.chains(cfg, common.CRON_JOBS).items()}
    groups["assistant"].append({"name": cfg["model"]["default"], "label": "tests",
                           "provider": cfg["model"].get("provider") or "",
                           "section": "chat"})
    return groups


def _services(st):
    """One row per status item; the address column is the port for a server
    on this Mac and host:port for one on another Mac."""
    out = []
    for cat in st["categories"]:
        for it in cat["items"]:
            if it["kind"] not in ("service", "heartbeat"):
                continue
            u = urlsplit(it["address"] or "")
            # the address has its own column here, and the http code says
            # nothing the state column does not
            detail = " · ".join(p for p in it["detail"].split(" · ")
                                if not re.fullmatch(r"http \d+", p)
                                and p != u.netloc)
            local = u.hostname in LOOPBACK
            out.append({"name": it["name"],
                        "port": ("" if not u.port else ":%d" % u.port if local
                                 else "%s:%d" % (u.hostname, u.port)),
                        "state": _STATE.get(it["state"], "bad"),
                        "detail": detail or "–"})
    return out


def _host_entries():
    """The Assistant Host app's own child list of MCP servers."""
    return json.loads(HOST_SERVERS.read_text())["servers"]


def _mcp_servers(st):
    """The MCP servers the Assistant Host app runs: the status check's mcp rows,
    which it reads from the app's own child list."""
    out = []
    for cat in st["categories"]:
        for it in cat["items"]:
            if it["kind"] != "mcp":
                continue
            ok = it["state"] == "ok"
            port = urlsplit(it["address"]).port if it["address"] else None
            out.append({"name": it["name"], "port": ":%d" % port if port else "–",
                        "state": "ok" if ok else "bad",
                        "detail": "up" if ok else "down" if port else it["detail"]})
    return out


def _tool_names(path):
    names = []
    for node in ast.parse(path.read_text()).body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")):
            for dec in node.decorator_list:
                f = dec.func if isinstance(dec, ast.Call) else dec
                dec_name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if dec_name in ("tool", "_tool"):
                    names.append(node.name)
                    break
    return names


def _host_sources():
    """{server name: source path} for the servers the Assistant Host app runs.

    Their hermes entries are urls, so the source cannot be read out of config.yaml
    the way a stdio entry's args give it — the app's child list is where it lives.
    """
    out = {}
    try:
        entries = _host_entries()
    except (OSError, ValueError, KeyError):
        return out
    for entry in entries:
        src = next((a for a in entry["command"] if a.endswith(".py")), "")
        if src:
            out[entry["name"]] = pathlib.Path(entry["directory"]) / src
    return out


def _catalog(cfg):
    cat = {"hermes": BUILTINS}
    hosted = _host_sources()
    for name, server in cfg["mcp_servers"].items():
        if not server.get("enabled", True):
            continue
        if "url" in server:
            path = hosted.get(name)
            if path is None:
                continue
        else:
            src = next((a for a in server.get("args") or [] if a.endswith(".py")), "")
            path = pathlib.Path(src) if src else pathlib.Path(server["command"]).parent / "server.py"
        cat[name] = _tool_names(path)
    return cat


def _split_tool(tool_name):
    if (m := re.match(r"mcp__(.+?)__(.+)", tool_name)):
        return m[1], m[2]
    return "hermes", tool_name


def _usage(db):
    now = time.time()
    tools, models, surfaces = {}, {}, {}
    for key, cutoff in (("1h", now - 3600), ("24h", now - 86400), ("7d", now - 7 * 86400)):
        tools[key] = [[*_split_tool(t), n] for t, n in db.execute(
            "SELECT tool_name, COUNT(*) FROM messages"
            " WHERE role='tool' AND tool_name IS NOT NULL AND timestamp > ?"
            " GROUP BY tool_name", (cutoff,))]
        models[key] = [{"name": m, "sessions": n} for m, n in db.execute(
            "SELECT model, COUNT(*) FROM sessions"
            " WHERE model IS NOT NULL AND started_at > ?"
            " GROUP BY model ORDER BY 2 DESC", (cutoff,))]
        surfaces[key] = [{"name": s, "sessions": n} for s, n in db.execute(
            "SELECT source, COUNT(*) FROM sessions WHERE started_at > ?"
            " GROUP BY source ORDER BY 2 DESC", (cutoff,))]
    return tools, models, surfaces


def _server_last_used(db):
    """{server name: newest tool-call timestamp}."""
    out = {}
    for t, ts in db.execute(
            "SELECT tool_name, MAX(timestamp) FROM messages"
            " WHERE role='tool' AND tool_name IS NOT NULL GROUP BY tool_name"):
        server, _ = _split_tool(t)
        if server not in out or ts > out[server]:
            out[server] = ts
    return out


def _short_age(sec):
    if sec < 60:
        return "now"
    if sec < 3600:
        return "%dm" % (sec // 60)
    h = sec / 3600
    if round(h, 1) < 3:             # one decimal only under 3h
        return "%.1fh" % h
    if sec < 23.5 * 3600:           # rounds to a whole hour under a day
        return "%dh" % (h + 0.5)
    return "%dd" % max(1, sec // 86400)


def _args_note(raw):
    """'{"query": "jet washing"}' -> 'query: jet washing'.

    Kept long enough to stay useful when the overview page expands the row; the
    page shows one line of it until then."""
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(d, dict):
        return ""  # a model can emit null or a bare list as its arguments
    return ", ".join("%s: %s" % (k, v) for k, v in d.items())[:400]


def _recent_tools(db, limit):
    """Last tool calls, newest first, consecutive repeats collapsed into one
    row with a count; ago is the newest call of the group, p its arguments."""
    now = time.time()
    groups = []
    for t, ts, cid in db.execute(
            "SELECT tool_name, timestamp, tool_call_id FROM messages"
            " WHERE role='tool' AND tool_name IS NOT NULL"
            " ORDER BY timestamp DESC LIMIT ?", (limit * 30,)):
        server, tool = _split_tool(t)
        if groups and groups[-1]["server"] == server and groups[-1]["tool"] == tool:
            groups[-1]["n"] += 1
            continue
        if len(groups) == limit:
            break
        groups.append({"server": server, "tool": tool, "n": 1,
                       "ago": _short_age(now - ts), "ts": ts, "cid": cid})
    ids = {g["cid"] for g in groups if g["cid"]}
    args = {}
    for (raw,) in db.execute(
            "SELECT tool_calls FROM messages WHERE role='assistant'"
            " AND tool_calls IS NOT NULL ORDER BY timestamp DESC LIMIT 400"):
        try:
            for c in json.loads(raw):
                if c.get("id") in ids:
                    args[c["id"]] = c["function"]["arguments"]
        except (ValueError, KeyError, TypeError):
            pass
    for g in groups:
        g["p"] = _args_note(args.get(g.pop("cid")))
    return groups


def snapshot():
    st = system.status_json()
    cfg = yaml.safe_load(CONFIG.read_text())
    db = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        tools, models, surfaces = _usage(db)
        recent_tools = _recent_tools(db, 18)
        last_used = _server_last_used(db)
    finally:
        db.close()
    now = time.time()
    mcp = [{**s, "ago": _short_age(now - last_used[s["name"]])
            if s["name"] in last_used else "–"} for s in _mcp_servers(st)]
    catalog = _catalog(cfg)
    # calls recorded under a name the catalogue no longer lists (a renamed or
    # removed tool) still show instead of silently dropping from the counts
    for server, tool, _ in tools["7d"]:
        if tool not in catalog.setdefault(server, []):
            catalog[server].append(tool)
    return {
        "host": _host(st),
        "issues": len(system.problems(st)),
        "models": _models_block(cfg),
        "services": _services(st),
        "mcpServers": mcp,
        "links": [{"name": n, "url": url} for n, url in LINKS],
        "llmPause": llm_pause.read(),
        "catalog": catalog,
        "recentTools": recent_tools,
        "tools1h": tools["1h"], "tools24h": tools["24h"], "tools7d": tools["7d"],
        "models1h": models["1h"], "models24h": models["24h"], "models7d": models["7d"],
        "surfaces1h": surfaces["1h"], "surfaces24h": surfaces["24h"], "surfaces7d": surfaces["7d"],
    }


GET_HANDLERS = {
    "/api/overview/snapshot": lambda params: (200, snapshot()),
    "/api/overview/links": lambda params: (200, [{"name": n, "url": url}
                                                 for n, url in LINKS]),
}
