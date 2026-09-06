"""Research area — the vault's recurring research topics: one row per
topic with its state, a run key, and two delete keys (report, topic).

Served by server.py (one process, one page). The topics and reports live in
the vault (vault/research/topics, vault/research/reports), written by
the research MCP server (services/mcp/research) and its driver's
batch; this area only reads that tree. A run spawns the same driver the MCP
server's run_report spawns, detached the same way: the page's feedback ends at
"started", and the finished report arrives by email like a batch run's would.
Delete report removes the topic's report and failure dump, keeping the topic,
so the next run starts from scratch — the MCP server's delete_report
semantics. Delete topic removes the topic file and, with it, its report and
failure dump, so nothing is left behind as an orphan.

A row's state carries the topic's last successful run (from the driver's
runs log), a failure count while the log shows consecutive failures, and a
running flag while the driver's in-flight marker (running.json beside the
runs log) holds a fresh stamp for the slug — entries older than the driver's
run timeout are ignored, so a killed driver's leftover ages out. The
next batch time is one stamp for the whole area (the research-batch cron
job's next_run_at). The failure flag is clearable from the page:
state/research-cleared.json (this service's own file) records the stamp of
the log entry a clear covers, so the flag stays down until the next failure
appends a new stamp — the audit area's dismissed-findings pattern.

Endpoints (HANDLERS/GET_HANDLERS; guards and dispatch live in server.py):

- GET  /api/research/report  ?slug=: the topic's full prompt, its stored
  report's text (null while there is none) and updated stamp — what an open
  row shows. 400 on a malformed or unknown slug
- POST /api/research/run     body {"slug"}: spawn driver.py --topic slug.
  400 on a malformed or unknown slug, 500 when the driver cannot start
- POST /api/research/delete_report  body {"slug"}: delete the slug's
  report and failure dump. 400 on a malformed slug or when neither file exists
- POST /api/research/delete_topic   body {"slug"}: delete the topic file
  plus its report and failure dump. 400 on a malformed or unknown slug
- POST /api/research/clear   body {"slug"}: clear the topic's failure
  flag. 400 on a malformed or unknown slug, or when the topic is not failing
"""

import json
import os
import pathlib
import re
import subprocess
from datetime import datetime

from common import STATE_DIR, cron_job

APP = pathlib.Path(__file__).resolve().parent
IRIS = APP.parent.parent
DRIVER = IRIS / "services" / "mcp" / "research" / "driver.py"
VAULT = pathlib.Path.home() / "Iris" / "vault" / "research"
TOPICS = VAULT / "topics"
REPORTS = VAULT / "reports"
RUNS = pathlib.Path.home() / "Local" / "iris-research" / "runs.jsonl"
RUNNING = RUNS.with_name("running.json")  # the driver's in-flight marker
RUNNING_TTL = 2100  # seconds an entry stays live: the driver's 1800 s run timeout plus slack
PYTHON = "/usr/bin/python3"        # the interpreter the MCP server spawns the driver with

CLEARED = STATE_DIR / "research-cleared.json"   # this service's own file

# the cron job whose batch runs the topics; its next_run_at gives the area
# its next-batch stamp (the same read as the messages area)
CRON_JOB = "research-batch"

# what the driver's text_to_filename produces; a slug is one path component,
# so a matching slug cannot walk out of the vault
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _read(path):
    """Minimal '--- key: value ---' frontmatter + body, the format the driver writes."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            meta = {}
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            return meta, text[end + 5:].lstrip("\n")
    return {}, text


def _log_state():
    """One pass over the driver's runs log: slug -> the last entry's
    consecutive-failure count ("failures") and stamp ("ts"), and the last
    successful run's stamp ("last_ok")."""
    out = {}
    try:
        with open(RUNS, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                slug = entry.get("slug")
                if not slug:
                    continue
                s = out.setdefault(slug, {"failures": 0, "ts": None,
                                          "last_ok": None})
                if entry.get("consecutive_failures") is not None:
                    s["failures"] = entry["consecutive_failures"]
                    s["ts"] = entry.get("ts")
                if entry.get("status") == "ok":
                    s["last_ok"] = entry.get("ts")
    except OSError:
        pass
    return out


def _running():
    """Slugs with a run in flight: RUNNING entries younger than RUNNING_TTL.
    Older ones are ignored — a driver killed mid-run leaves its entry
    behind, and it ages out here instead of pinning the flag."""
    try:
        data = json.loads(RUNNING.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    now = datetime.now()
    live = set()
    for slug, ts in data.items():
        try:
            age = (now - datetime.fromisoformat(str(ts))).total_seconds()
        except (TypeError, ValueError):
            continue
        if age < RUNNING_TTL:
            live.add(slug)
    return live


def _cleared():
    """The cleared failure flags: slug -> the stamp of the log entry the clear
    covered. Values that are not strings are dropped rather than raising:
    this runs inside the page's one state call."""
    try:
        saved = json.loads(CLEARED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items()
            if isinstance(k, str) and isinstance(v, str)}


def _next_batch():
    """The batch job's next_run_at stamp, None when the cron file is
    unreadable or the job unknown."""
    return (cron_job(CRON_JOB) or {}).get("next_run_at") or None


def _topics():
    """One entry per topic file: its slug, the stored report's updated
    stamp, the run state the driver's log shows (a cleared failure flag
    reads as 0), an in-flight flag from the driver's running marker, and
    the last successful run's stamp."""
    log = _log_state()
    cleared = _cleared()
    running = _running()
    out = []
    for path in sorted(TOPICS.glob("*.md")):
        slug = path.name[:-3]
        report = REPORTS / (slug + ".md")
        updated = _read(report)[0].get("updated") if report.exists() else None
        s = log.get(slug, {})
        failing = s.get("failures", 0)
        # cleared values are non-empty strings, so a log entry without a
        # stamp (ts None) can never read as cleared
        if failing and cleared.get(slug) and cleared.get(slug) == s.get("ts"):
            failing = 0
        out.append({"slug": slug,
                    "updated": updated,
                    "failing": failing,
                    "running": slug in running,
                    "report": report.exists(),
                    "error": (REPORTS / ("error-" + slug + ".md")).exists(),
                    "last_run": s.get("last_ok")})
    return out


# ---------------------------------------------------------------- area interface

NAME = "research"


def boot():
    """Nothing to load: the topics live in the vault, read fresh per state call."""


def state():
    """The research part of GET /api/state: one entry per topic row, plus
    the batch job's next-run stamp for the area heading."""
    return {"topics": _topics(), "next_batch": _next_batch()}


def _h_report(params):
    """An open row's content: the topic's full prompt, and the stored
    report's text and updated stamp (both None while there is no report)."""
    slug = params.get("slug") or ""
    if not SLUG_RE.fullmatch(slug) or not (TOPICS / (slug + ".md")).exists():
        return 400, {"error": "unknown topic"}
    report = REPORTS / (slug + ".md")
    meta, text = _read(report) if report.exists() else ({}, None)
    return 200, {"question": _read(TOPICS / (slug + ".md"))[1].strip(),
                 "report": text, "updated": meta.get("updated")}


def _h_run(body):
    slug = body.get("slug") or ""
    if not SLUG_RE.fullmatch(slug) or not (TOPICS / (slug + ".md")).exists():
        return 400, {"error": "unknown topic"}
    try:
        subprocess.Popen([PYTHON, str(DRIVER), "--topic", slug],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return 500, {"error": f"could not start the driver: {e}"}
    return 200, {"started": slug}


def _report_files(slug):
    return [p for p in (REPORTS / (slug + ".md"),
                        REPORTS / ("error-" + slug + ".md")) if p.exists()]


def _unlink(targets):
    try:
        for p in targets:
            p.unlink()
    except OSError as e:
        return 500, {"error": str(e)}
    return 200, {"deleted": [p.name for p in targets]}


def _h_delete_report(body):
    slug = body.get("slug") or ""
    if not SLUG_RE.fullmatch(slug):
        return 400, {"error": "bad slug"}
    targets = _report_files(slug)
    if not targets:
        return 400, {"error": "no report for this topic"}
    return _unlink(targets)


def _h_delete_topic(body):
    slug = body.get("slug") or ""
    topic = TOPICS / (slug + ".md")
    if not SLUG_RE.fullmatch(slug) or not topic.exists():
        return 400, {"error": "unknown topic"}
    return _unlink([topic] + _report_files(slug))


def _h_clear(body):
    slug = body.get("slug") or ""
    if not SLUG_RE.fullmatch(slug) or not (TOPICS / (slug + ".md")).exists():
        return 400, {"error": "unknown topic"}
    s = _log_state().get(slug, {})
    if not s.get("failures") or not s.get("ts"):
        return 400, {"error": "the topic is not failing"}
    cleared = _cleared()
    cleared[slug] = s["ts"]
    tmp = CLEARED.with_name(CLEARED.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cleared, f)
    os.replace(tmp, CLEARED)
    return 200, {"cleared": slug}


HANDLERS = {"/api/research/run": _h_run,
            "/api/research/delete_report": _h_delete_report,
            "/api/research/delete_topic": _h_delete_topic,
            "/api/research/clear": _h_clear}

GET_HANDLERS = {"/api/research/report": _h_report}
