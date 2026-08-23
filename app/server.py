#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Iris dashboard page server.

Serves the static files in app/ plus GET /api/snapshot, the live data the
dashboard page renders:

- host, service and job state: hermes/iris-status/hermes-iris-status --json
- service ports: hermes/iris-status/manifest.json http probe URLs
- job run intervals, first firing times and last runs: ~/.hermes/cron/jobs.json
- model roles: ~/.hermes/config.yaml (iris) and
  ~/.hermes/profiles/ops/config.yaml (ops), plus the models iris
  cron jobs run on
  (~/.hermes/cron/jobs.json, RESEARCH_MODEL in services/mcp/research/env)
- tool catalogue: tool functions parsed out of the MCP server sources named in
  config.yaml, so a renamed or removed tool shows up instead of going stale
- tool calls and sessions: ~/.hermes/state.db, 24-hour and 7-day windows

POST /api/job/run?name=<job> starts `hermes cron run` for that job, detached;
the jobs panel shows the outcome on later refreshes.

Binds 127.0.0.1; tailscale serve forwards the tailnet here
(30655 -> 127.0.0.1:30655).
"""

import ast
import http.server
import json
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import yaml

APP = pathlib.Path(__file__).resolve().parent
IRIS = APP.parent
HERMES = pathlib.Path.home() / ".hermes"
STATUS = IRIS / "hermes/iris-status/hermes-iris-status"
MANIFEST = IRIS / "hermes/iris-status/manifest.json"
STATE_DB = HERMES / "state.db"
JOBS = HERMES / "cron/jobs.json"
EXECUTIONS = HERMES / "cron/executions.db"
CONFIG = HERMES / "config.yaml"
OPS_CONFIG = HERMES / "profiles/ops/config.yaml"
OPS_JOBS = HERMES / "profiles/ops/cron/jobs.json"
# the macOS bootstrap file the oMLX GUI writes when the data root moves; the
# model folders are read from the server's own settings.json under it, so a
# bake-off directory added there shows up on the page too
OMLX_BASE_FILE = pathlib.Path.home() / "Library/Application Support/oMLX/base-path"
HOST_SERVERS = pathlib.Path.home() / "Library/Application Support/com.example.iris.host/servers.json"
PORT = 30655

# hermes has no machine-readable list of its built-in tools, so the ones the
# iris profile can reach are kept by hand.
BUILTINS = ["web_search", "web_extract", "todo", "memory", "cronjob",
            "skills_list", "skill_view"]

# tailscale serve mappings, local port -> tailnet port. The Tailscale CLI
# answers only from a GUI login shell, not from launchd, so this is kept by
# hand — hermes/settings.md lists the same mappings.
TS_PORTS = {35422: 35422, 9273: 443, 60195: 52737, 30655: 30655, 13727: 443}

LINKS = [
    {"name": "actions", "url": "https://mac-mini.your-tailnet.ts.net/"},
    {"name": "dashboard", "url": "https://mac-mini.your-tailnet.ts.net:30655/"},
    {"name": "webui", "url": "https://mac-mini.your-tailnet.ts.net:35422/"},
    {"name": "actual", "url": "https://mac-mini.your-tailnet.ts.net:52737/"},
]

WEEKDAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays",
            "Fridays", "Saturdays"]

# research-batch is a no-agent script job, so jobs.json carries no model for
# it; driver.py reads RESEARCH_MODEL out of the env file next to it
RESEARCH_ENV = IRIS / "services/mcp/research/env"


# ---------------------------------------------------------------- gathering

def _ago(seconds):
    m = round(seconds / 60)
    if m < 60:
        return "%dm ago" % max(m, 1)
    if round(m / 60) < 24:
        return "%dh ago" % round(m / 60)
    return "%dd ago" % round(m / 60 / 24)


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


def _omlx_model_dirs():
    """Every folder oMLX scans for models, from its own settings.json."""
    try:
        base = pathlib.Path(OMLX_BASE_FILE.read_text().strip())
    except OSError:
        base = pathlib.Path.home() / ".omlx"
    settings = json.loads((base / "settings.json").read_text())
    return [pathlib.Path(d) for d in settings["model"]["model_dirs"]]


def _model_sizes():
    """{model name: bytes on disk} from the oMLX model folders."""
    sizes = {}
    for models_dir in _omlx_model_dirs():
        if not models_dir.is_dir():
            continue  # a bake-off folder can be deleted while still listed
        for org in models_dir.iterdir():
            if org.is_dir():
                for d in org.iterdir():
                    if d.is_dir():
                        sizes[d.name] = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return sizes


def _research_model():
    """RESEARCH_MODEL from the research driver's env file, "" when unset."""
    try:
        for line in RESEARCH_ENV.read_text().splitlines():
            if line.startswith("RESEARCH_MODEL="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _models(cfg, jobs, tests=False):
    sizes = _model_sizes()

    def row(name, label="", provider=""):
        size = "%.0f GB" % (sizes[name] / 1e9) if name in sizes else ""
        return {"name": name, "size": size, "label": label, "provider": provider}

    aux, vision = [], []
    for task, block in cfg.get("auxiliary", {}).items():
        pair = (block.get("model"), block.get("provider") or "")
        bucket = vision if task == "vision" else aux
        if pair[0] and pair not in bucket:
            bucket.append(pair)

    # cron jobs: unpinned agent jobs follow cron.model, else model.default —
    # the resolution cron/scheduler.py run_job does. Jobs on that model fold
    # into one "cron" row; a job with its own model or provider pin gets a
    # row under a short label (the role column is 8ch, so not the full job
    # name). A provider pin alone still reroutes the default model name to
    # that provider, so it carries through to the page for the source
    # column. A profile without a jobs file has no cron rows.
    LABELS = {"research-batch": "research", "actions-inbox-scan": "inbox",
              "hermes-updates": "upstream"}
    # Every row carries the provider that will serve it, so the page merges a
    # role with a job that pins the same pair by hand. Without this the main
    # row and a job pinned to the identical provider and model draw twice.
    main_provider = cfg["model"].get("provider") or ""
    cron_cfg = cfg.get("cron") or {}
    cron_default = cron_cfg.get("model") or cfg["model"]["default"]
    # a cron model named in config carries no provider of its own; only the
    # inherited default brings one along
    cron_provider = "" if cron_cfg.get("model") else main_provider
    try:
        job_list = json.loads(jobs.read_text())["jobs"]
    except OSError:
        job_list = []
    named, shared = {}, False
    for j in job_list:
        if not j["enabled"]:
            continue
        if j.get("no_agent"):
            model = _research_model() if j["name"] == "research-batch" else ""
        else:
            model = j.get("model") or cron_default
        if not model:
            continue
        if model == cron_default and not j.get("provider"):
            shared = True
        else:
            named[LABELS.get(j["name"], j["name"])] = (model, j.get("provider") or "")
    cron = [row(m, label=n, provider=p) for n, (m, p) in sorted(named.items())]
    if job_list and (shared or not cron):
        cron.append(row(cron_default, label="cron", provider=cron_provider))

    roles = {
        "main": [row(cfg["model"]["default"], provider=main_provider)],
        "vision": [row(n, provider=p) for n, p in vision],
        "aux": [row(n, provider=p) for n, p in aux],
        "fallback": [row(f["model"], provider=f.get("provider") or "")
                     for f in cfg.get("fallback_providers", [])],
        "cron": cron,
    }
    if tests:
        # the SOUL suite copies the live config's model block into its
        # throwaway homes, so its model is the main pair by definition
        roles["tests"] = [row(cfg["model"]["default"], provider=main_provider)]
    return roles


def _models_block(cfg):
    """Both profiles' role tables plus the local models no role uses —
    on oMLX for manual /model switches, pi.dev (the coder), TTS/STT, or a
    bake-off candidate, none of which config.yaml roles carry."""
    block = {
        "iris": _models(cfg, JOBS, tests=True),
        "ops": _models(yaml.safe_load(OPS_CONFIG.read_text()), OPS_JOBS),
    }
    used = {r["name"] for prof in ("iris", "ops")
            for rows in block[prof].values() for r in rows}
    sizes = _model_sizes()
    block["otherLocal"] = [
        {"name": n, "size": ("%.1f GB" if sizes[n] < 1e9 else "%.0f GB") % (sizes[n] / 1e9)}
        for n in sorted(sizes) if n not in used]
    return block


_STATE = {"ok": "ok", "--": "idle"}  # anything else iris-status emits is bad


def _services(st):
    ports = {}
    for cat in json.loads(MANIFEST.read_text())["categories"]:
        for it in cat["items"]:
            if "http" in it and (port := urlsplit(it["http"]).port):
                ports[it["name"]] = port
    out = []
    for cat in st["categories"]:
        for it in cat["items"]:
            if it["kind"] not in ("service", "heartbeat"):
                continue
            detail = " · ".join(p for p in it["detail"].split(" · ")
                                if not re.fullmatch(r"http \d+", p))
            port = ports.get(it["name"])
            out.append({"name": it["name"],
                        "port": ":%d" % port if port else "",
                        "tsPort": ":%d" % TS_PORTS[port] if port in TS_PORTS else "",
                        "state": _STATE.get(it["state"], "bad"),
                        "detail": detail or "–"})
    return out


def _listening(port):
    """A loopback connect: a closed port refuses at once, so this never hangs."""
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _mcp_servers():
    """The MCP servers the Iris Studio Host app runs, read from the app's own
    child list so this cannot drift from what is actually running. Only the name
    and port are taken — that file also holds API keys."""
    try:
        entries = json.loads(HOST_SERVERS.read_text())["servers"]
    except (OSError, ValueError, KeyError):
        return []
    out = []
    for entry in entries:
        up = _listening(entry["port"])
        out.append({"name": entry["name"], "port": ":%d" % entry["port"],
                    "state": "ok" if up else "bad", "detail": "up" if up else "down"})
    return out


def _first_run(expr):
    """(first firing time, note): ('02:00', '') for a daily job,
    ('02:00', 'Mondays') for a weekly one — the note carries what the
    interval and the time don't say. ('', '') when the expr pins no fixed
    time."""
    parts = expr.split()
    if len(parts) != 5:
        return "", ""
    minute, hour, dom, mon, dow = parts
    minutes = _cron_field(minute, 0, 59)
    hours = _cron_field(hour, 0, 23)
    if not minutes or not hours:
        return "", ""
    first = "%02d:%02d" % (hours[0], minutes[0])
    if dom == mon == "*" and dow != "*":
        dows = _cron_field(dow, 0, 7)
        if dows:
            return first, WEEKDAYS[dows[0] % 7]
    return first, ""


def _cron_field(field, lo, hi):
    """One cron field -> the values it matches (handles *, */n, lists and
    ranges); None for forms outside that (month and weekday names)."""
    out = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit():
                return None
            step = int(s)
        if part == "*":
            first, last = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            first, last = int(a), int(b)
        elif part.isdigit():
            first = last = int(part)
        else:
            return None
        out.update(range(first, last + 1, step))
    return sorted(out)


def _every(expr):
    """(interval between runs, runs per day) from a cron expr: ('3h', 8),
    ('1d', 1), ('7d', 1/7). ('', None) when the expr pins a month or
    day-of-month — there the gap is not fixed (and the one such job,
    finance-uncategorized, only ever fires from the actions page)."""
    parts = expr.split()
    if len(parts) != 5:
        return "", None
    minute, hour, dom, mon, dow = parts
    if dom != "*" or mon != "*":
        return "", None
    minutes = _cron_field(minute, 0, 59)
    hours = _cron_field(hour, 0, 23)
    if not minutes or not hours:
        return "", None
    per_day = len(minutes) * len(hours)
    if dow != "*":
        dows = _cron_field(dow, 0, 7)
        if not dows:
            return "", None
        days = 7 / len({d % 7 for d in dows})
        return "%dd" % round(days), per_day / days
    gap = 24 / per_day
    if gap >= 24 and gap % 24 == 0:
        return "%dd" % (gap // 24), per_day
    if gap >= 1:
        return "%dh" % round(gap), per_day
    return "%dm" % round(gap * 60), per_day


def _running_jobs():
    """Job ids whose newest attempt in hermes' executions ledger is still
    claimed/running — a run going right now, page-started or scheduled.
    Any read problem -> none, shown as not running."""
    try:
        conn = sqlite3.connect(f"file:{EXECUTIONS}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute("SELECT job_id, status FROM executions "
                                "ORDER BY claimed_at DESC, id DESC").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    newest = {}
    for job_id, status in rows:
        newest.setdefault(job_id, status)
    return {j for j, s in newest.items() if s in ("claimed", "running")}


def _jobs(st):
    # the scheduler file is the job list; iris-status supplies the state word
    # where it watches the job, since it also checks the job's output files
    states = {it["name"]: _STATE.get(it["state"], "bad")
              for cat in st["categories"] for it in cat["items"]
              if it["kind"] == "cron"}
    running = _running_jobs()
    out = []
    for j in json.loads(JOBS.read_text())["jobs"]:
        if not j["enabled"]:
            continue
        # interval and once schedules carry no cron expr, only a display string
        expr = j["schedule"].get("expr")
        if expr:
            first, note = _first_run(expr)
            every, per_day = _every(expr)
        else:
            first, note, every, per_day = "", "", "", None
        if j["last_run_at"]:
            last = _ago(time.time() - datetime.fromisoformat(j["last_run_at"]).timestamp())
            state = "ok" if j["last_status"] == "ok" else "bad"
        else:
            last, state = "never ran", "idle"
        out.append({"name": j["name"], "every": every, "perDay": per_day,
                    "first": first, "note": note,
                    "last": last, "state": states.get(j["name"], state),
                    "running": j["id"] in running})
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
    """{server name: source path} for the servers the Iris Studio Host app runs.

    Their hermes entries are urls, so the source cannot be read out of config.yaml
    the way a stdio entry's args give it — the app's child list is where it lives.
    """
    try:
        entries = json.loads(HOST_SERVERS.read_text())["servers"]
    except (OSError, ValueError, KeyError):
        return {}
    out = {}
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
            # docs runs through a run.sh; its server sits next to that script
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
    if round(sec / 360) < 240:      # rounds to under 24.0h once printed
        return "%.1fh" % (sec / 3600)
    return "%dd" % max(1, sec // 86400)


def _args_note(raw):
    """'{"query": "jet washing"}' -> 'query: jet washing'.

    Kept long enough to stay useful when the dashboard page expands the row; the
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
                       "ago": _short_age(now - ts), "cid": cid})
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


def _run_job(name):
    """Start `hermes cron run` for one job, detached. The run claims the job,
    so a concurrent gateway tick cannot fire it twice."""
    names = [j["name"] for j in json.loads(JOBS.read_text())["jobs"]]
    if name not in names:
        raise RuntimeError("no job named %r" % name)
    subprocess.Popen(["hermes", "cron", "run", name],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return "started %s" % name


def snapshot():
    st = json.loads(subprocess.run([str(STATUS), "--json"], capture_output=True,
                                   text=True, timeout=20).stdout)
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
            if s["name"] in last_used else "–"} for s in _mcp_servers()]
    catalog = _catalog(cfg)
    # calls recorded under a name the catalogue no longer lists (a renamed or
    # removed tool) still show instead of silently dropping from the counts
    for server, tool, _ in tools["7d"]:
        if tool not in catalog.setdefault(server, []):
            catalog[server].append(tool)
    return {
        "host": _host(st),
        "models": _models_block(cfg),
        "services": _services(st),
        "mcpServers": mcp,
        "cron": _jobs(st),
        "links": LINKS,
        "catalog": catalog,
        "recentTools": recent_tools,
        "tools1h": tools["1h"], "tools24h": tools["24h"], "tools7d": tools["7d"],
        "models1h": models["1h"], "models24h": models["24h"], "models7d": models["7d"],
        "surfaces1h": surfaces["1h"], "surfaces24h": surfaces["24h"], "surfaces7d": surfaces["7d"],
    }


# ---------------------------------------------------------------- serving

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP), **kwargs)

    def end_headers(self):
        # without this, phone browsers cache the page and JS heuristically and
        # keep showing an old dashboard page after changes
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path == "/api/snapshot":
            fn = snapshot
            try:
                body = json.dumps(fn()).encode()
            except Exception as e:
                self.send_error(500, "snapshot failed: %s" % e)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/":
            self.path = "/dashboard.html"
        super().do_GET()

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/api/job/run":
            name = parse_qs(u.query).get("name", [""])[0]
            fn = lambda: _run_job(name)
        else:
            self.send_error(404)
            return
        try:
            body = fn().encode()
        except Exception as e:
            self.send_error(500, "action failed: %s" % e)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"dashboard on 127.0.0.1:{PORT}")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
