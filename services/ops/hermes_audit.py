"""Hermes-audit area — starts the deterministic Hermes audit and shows where the
run is.

Served by server.py (one process, one page). The work all happens in
services/hermes-audit/audit.py; this module only starts that script, stops it,
resets it, and reads the three files it leaves in the folder ASSISTANT_HERMES_AUDIT_STATE names:

- lock       flock held for the length of a run. Whether a run is going is a
             lock probe, so a run started from hermes chat shows on the page
             too, and a killed run never reads as still running.
- run.json   this pass as it happens: step, category, the LLM call in flight,
             and per category waiting / running / ok / warn with its findings.
             Each finding is {text, note, act}: act names the page keys it
             carries and what they need (audit.py lists them). finished_at
             stays null until the report lands, so a run that died is told
             apart from one that finished.
- last-run.err  the run's stderr, kept for a run that ended without a report.
             It counts as this run's only when it is newer than the record's
             own stamp.

It also writes snoozes.json there, the findings the user snoozed
(services/mcp/common/audit_snoozes.py); audit.py reads it for the report.

The weekly hermes-audit cron job (hermes-home/scripts/hermes-audit.sh) is a
second starter; state() reads its stamps out of hermes' cron files so the page
can show when the scheduled pass last ran and runs next.

The run is detached (start_new_session): a pass can take an hour, and log
rotation SIGTERMs this service in the middle of one. Nothing is kept in this
process — the files carry it all across a restart.

Endpoints (HANDLERS; guards and dispatch live in server.py):

- POST /api/hermes-audit/run     start a pass. 409 when one is already going
                                or the assistant's model runs are paused (llm_pause)
- POST /api/hermes-audit/stop    stop the run in progress (audit.py --stop):
                                SIGTERM to its process group; it writes no
                                report and commits no state
- POST /api/hermes-audit/reset   audit.py --reset: remove the audit's state file,
                                so the next run is a first pass — every check
                                reviews from scratch, every baseline reseeds
- POST /api/hermes-audit/accept-changes: audit.py --accept, the memory
                                review the last pass parked becomes the
                                accepted one
- POST /api/hermes-audit/acknowledge {category, finding}: write the finding's
                                acknowledge edits into the lists of the
                                audit's expected.yaml, so future runs take the
                                change as expected
- POST /api/hermes-audit/restore-skill {category, finding}: hermes skills
                                reset <skill> --restore, for a skill hermes
                                stopped updating
- POST /api/hermes-audit/copy-plist {category, finding}: copy the live gateway
                                plist over its copy in the folder
                                ASSISTANT_LAUNCHD_SOURCE names, when the pass found
                                the two differ
- POST /api/hermes-audit/snooze {category, finding}: leave the finding out of
                                the counts and the report's warnings for two
                                weeks (services/mcp/common/audit_snoozes.py)
- POST /api/hermes-audit/unsnooze {category, finding}: end that snooze now
- GET /api/hermes-audit/status  {text}: while a run is going, its step and the
                                LLM call in flight; when idle, when the last
                                pass finished and which categories carry open
                                findings. Never the finding text
- GET /api/hermes-audit/report  {text}: the last finished pass's report; none
                                while a run is going (the file on disk is the
                                pass before it), the error output when the last
                                run ended without one
"""

import filecmp
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
from datetime import datetime

import yaml

from common import _log, cron_job, elapsed, lock_held
from data_root import root
import audit_snoozes
import llm_pause
import system

APP = pathlib.Path(__file__).resolve().parent
SCRIPT = APP.parent / "hermes-audit" / "audit.py"
UV = pathlib.Path.home() / ".local" / "bin" / "uv"


def state_dir():
    """The folder audit.py keeps its lock, run record and error log in."""
    return root("ASSISTANT_HERMES_AUDIT_STATE")


def lock_file():
    return state_dir() / "lock"


def run_file():
    return state_dir() / "run.json"


def err_file():
    return state_dir() / "last-run.err"


def report_file():
    """The report audit.py writes when a pass finishes."""
    return root("ASSISTANT_AUDIT_REPORT")


def expected_file():
    """audit.py's expected values; acknowledge writes into its lists."""
    return root("ASSISTANT_AUDIT_EXPECTED")


def launchd_source():
    """The plist source folder (setup/launchd); copy_plist writes the gateway
    plist's copy into it."""
    return root("ASSISTANT_LAUNCHD_SOURCE")


CRON_JOB = "hermes-audit"

# acknowledge is a read-modify-write on expected.yaml, and two findings
# tapped in quick succession land on two request threads
_EXPECTED_LOCK = threading.Lock()
# the same for snooze and unsnooze on the snooze file
_SNOOZE_LOCK = threading.Lock()

ERR_TAIL = 800        # chars of the failed run's stderr the page shows
CONTROL_TIMEOUT = 120  # --stop waits 5s for the run to go; uv start-up is the rest
RESTORE_TIMEOUT = 120

# the last run this service started; poll() also reaps it (a finished child
# of a service that lives for weeks is a zombie until someone waits on it)
_child = None


def _running():
    """True while the run this service started lives (audit.py takes its lock
    only after uv starts up) or while any run holds the audit's lock."""
    return (_child is not None and _child.poll() is None) or lock_held(lock_file())


def _run_record():
    """The pass audit.py is writing (or wrote), None when unreadable. A file
    that is not an object would break every reader below, and this area's
    state() is part of the page's one state call — one bad file must not blank
    the whole page."""
    try:
        record = json.loads(run_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _job_fields():
    """last-run stamp, next scheduled run, and whether the last run failed —
    from hermes' own cron files. Unreadable files -> None fields. A run going
    right now shows through the audit's own lock probe instead, so no
    executions-ledger read here."""
    job = cron_job(CRON_JOB)
    return {"job_last_run_at": job.get("last_run_at") if job else None,
            "job_next_run_at": job.get("next_run_at") if job else None,
            "job_last_failed": bool(job and job.get("last_status")
                                    not in (None, "ok"))}


def _error_tail(after):
    """The failed run's stderr, but only when it was written after `after` —
    the stamp of the pass the record holds. Older text belongs to an earlier
    run: a hand-started run writes none of its own, and a start that died
    before audit.py could seed the record leaves the record on the pass
    before it.

    The whole second of grace is the stamp's own resolution: audit.py writes it
    with timespec="seconds", and uv prints its start-up lines to this same file
    a fraction of a second before audit.py seeds the stamp. Without the grace
    that noise reads as newer than the run it started, and a stopped run shows
    an error card full of it. A run that really dies writes its stderr steps
    into the pass, far past one second."""
    try:
        if err_file().stat().st_mtime <= datetime.fromisoformat(after).timestamp() + 1:
            return None
    except (OSError, TypeError, ValueError):
        pass
    try:
        return err_file().read_text(encoding="utf-8").strip()[-ERR_TAIL:] or None
    except OSError:
        return None


def start():
    """POST /api/hermes-audit/run: audit.py detached. The lock refuses a second
    run anyway; the probe here turns that into a 409 the page can show."""
    global _child
    if _running():
        return 409, {"error": "an audit run is already in progress"}
    if (pause := llm_pause.read()):
        return 409, {"error": f"the assistant's model runs are paused until {pause['until']}"}
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        # stdin closed: a child CLI that reads stdin when it is not a terminal
        # would hang forever. Both LLM calls run under this.
        with open(err_file(), "w", encoding="utf-8") as err:
            _child = subprocess.Popen(
                [str(UV), "run", "--no-project", str(SCRIPT)],
                stdout=subprocess.DEVNULL, stderr=err,
                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return 500, {"error": f"could not start the audit: {e}"}
    _log({"event": "hermes_audit_started", "result": "run started"})
    return 200, {"started": True}


def _pass_finding(label, text):
    """(None, the finding) when a finding with this text is on the `label`
    card of the pass on record, else (the error answer, None). A key may act
    only on a finding the pass reported, and what it does comes from that
    finding's act in the record, never from the request."""
    record = _run_record()
    if not record:
        return (404, {"error": "no audit pass on record"}), None
    cat = next((c for c in record.get("categories") or []
                if isinstance(c, dict) and c.get("label") == label), None)
    if cat is None:
        return (404, {"error": f"the last pass has no category {label!r}"}), None
    f = next((f for f in cat.get("findings") or []
              if isinstance(f, dict) and f.get("text") == text), None)
    if f is None:
        return (404, {"error": "that finding is not in the pass on record — "
                               "reload the page"}), None
    return None, f


def _act(body, key):
    """(None, the finding's act[key]) for a finding of the pass on record
    that carries that key, else (the error answer, None)."""
    refused, f = _pass_finding(body.get("category"), body.get("finding"))
    if refused:
        return refused, None
    value = (f.get("act") or {}).get(key)
    if not value:
        return (400, {"error": f"that finding has no {key} key"}), None
    return None, value


# ---------------------------------------------------------------- expected.yaml lists
# acknowledge edits lists in the hand-kept expected.yaml by line, so every
# comment survives. A list is a key line at two spaces per level of its path,
# then one "- item" line per item two spaces deeper, or "key: []" when empty.

PLAIN_ITEM = re.compile(r"[A-Za-z0-9_./-]+")


def _item_line(depth, name):
    """One list item; quoted unless it is a plain name YAML reads back as
    itself (JSON's double quotes are YAML's)."""
    plain = PLAIN_ITEM.fullmatch(name) and yaml.safe_load(name) == name
    return "  " * depth + "- " + (name if plain else json.dumps(name, ensure_ascii=False))


def _block_end(lines, k, indent):
    """The line after the block that the key on line k (at indent) opens."""
    for i in range(k + 1, len(lines)):
        text = lines[i].strip()
        if text and not text.startswith("#") and \
                len(lines[i]) - len(lines[i].lstrip()) <= indent:
            return i
    return len(lines)


def _walk(doc, path):
    for key in path:
        doc = doc.get(key) if isinstance(doc, dict) else None
    return doc


def edit_list(text, path, add, remove):
    """expected.yaml's text with the list at path changed: the remove names'
    lines dropped, each add name inserted in sorted place (after the last
    item of an unsorted list). Every other line stays as it was. Raises
    ValueError when the list is not there in the one form this edits, or
    when the result reads back as anything but that change."""
    lines = text.splitlines()
    lo, hi = 0, len(lines)
    for depth, key in enumerate(path):
        head = "  " * depth + key + ":"
        k = next((i for i in range(lo, hi) if lines[i] == head
                  or lines[i].startswith(head + " ")), None)
        if k is None:
            raise ValueError(f"expected.yaml has no {'.'.join(path)}")
        lo, hi = k + 1, _block_end(lines, k, 2 * depth)
    if lines[k][len(head):].split("#")[0].strip() not in ("", "[]"):
        raise ValueError(f"{'.'.join(path)} in expected.yaml is not a block list")
    lines[k] = head
    block = [l for l in lines[lo:hi] if not (
        l.strip().startswith("- ") and yaml.safe_load(l.strip()[2:]) in remove)]
    items = [(j, yaml.safe_load(l.strip()[2:])) for j, l in enumerate(block)
             if l.strip().startswith("- ")]
    names = [v for _, v in items]
    for n in add:
        if n in names:
            continue
        if names == sorted(names):
            at = next((j for j, v in items if v > n),
                      items[-1][0] + 1 if items else 0)
        else:
            at = items[-1][0] + 1
        block.insert(at, _item_line(len(path), n))
        items = [(j, yaml.safe_load(l.strip()[2:])) for j, l in enumerate(block)
                 if l.strip().startswith("- ")]
        names = [v for _, v in items]
    if not items:
        lines[k] = head + " []"
    new_text = "\n".join(lines[:lo] + block + lines[hi:]) + "\n"
    before, after = yaml.safe_load(text), yaml.safe_load(new_text)
    want = (set(_walk(before, path) or []) - set(remove)) | set(add)
    if set(_walk(after, path) or []) != want:
        raise ValueError(f"the edit of {'.'.join(path)} did not read back")
    return new_text


def _edits_hold(doc, edits):
    """True when expected.yaml (parsed) already holds every edit."""
    for e in edits:
        listed = set(_walk(doc, e["path"]) or [])
        if not set(e["add"]) <= listed or set(e["remove"]) & listed:
            return False
    return True


def acknowledge(body):
    """POST /api/hermes-audit/acknowledge: the finding's change is expected
    from now on. Its acknowledge edits (audit.py writes them: a doctor
    warning for doctor_known, tools for platform_tools, skills for
    bundled_skills or own_skill_edits) go into expected.yaml's lists,
    which audit.py reads on every pass. The finding stays until the next
    pass; state()'s handled list says it is acknowledged."""
    refused, ack = _act(body, "acknowledge")
    if refused:
        return refused
    try:
        with _EXPECTED_LOCK:
            expected = expected_file()
            text = expected.read_text(encoding="utf-8")
            for e in ack["edits"]:
                text = edit_list(text, e["path"], e["add"], e["remove"])
            tmp = expected.with_name(expected.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, expected)
    except (OSError, ValueError, yaml.YAMLError) as e:
        return 500, {"error": f"could not update expected.yaml: {e}"}
    _log({"event": "hermes_audit_acknowledged", "result": body.get("finding")})
    return 200, {"acknowledged": True}


def restore_skill(body):
    """POST /api/hermes-audit/restore-skill: hermes skills reset <skill>
    --restore --yes, which replaces the local copy with hermes' own and
    resumes its updates. The finding stays until the next pass."""
    refused, name = _act(body, "restore_skill")
    if refused:
        return refused
    if not re.fullmatch(r"[\w.-]+", name):
        return 400, {"error": f"not a skill name: {name!r}"}
    try:
        p = subprocess.run([system.HERMES, "skills", "reset", name, "--restore",
                            "--yes"], capture_output=True, text=True,
                           timeout=RESTORE_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 502, {"error": f"hermes skills reset: {e}"}
    if p.returncode != 0:
        return 502, {"error": (p.stderr or p.stdout).strip()[-400:]}
    _log({"event": "hermes_audit_skill_restored", "result": name})
    return 200, {"restored": name}


def copy_plist(body):
    """POST /api/hermes-audit/copy-plist: the live gateway plist replaces its
    copy in setup/launchd. Only a finding whose act carries copy_plist in the
    pass on record qualifies, so the page can name no other file. Nothing
    reloads: the live plist is what launchd already runs. The finding stays
    until the next pass; state()'s handled list is what says the copy
    landed."""
    refused, _ = _act(body, "copy_plist")
    if refused:
        return refused
    name = system.GATEWAY_PLIST.name
    try:
        shutil.copyfile(system.GATEWAY_PLIST, launchd_source() / name)
    except OSError as e:
        return 500, {"error": f"could not copy {name}: {e}"}
    _log({"event": "hermes_audit_plist_copied", "result": name})
    return 200, {"copied": name}


def snooze(body):
    """POST /api/hermes-audit/snooze: the finding moves to the page's snoozed
    card and is left out of the page's count, the menu bar badge, the
    status text and the report's warnings for audit_snoozes.DAYS days. Only a
    finding of the pass on record qualifies."""
    label, finding = body.get("category"), body.get("finding")
    refused, _ = _pass_finding(label, finding)
    if refused:
        return refused
    try:
        with _SNOOZE_LOCK:
            until = audit_snoozes.snooze(label, finding)
    except OSError as e:
        return 500, {"error": f"could not snooze: {e}"}
    _log({"event": "hermes_audit_snoozed", "result": finding})
    return 200, {"until": until}


def unsnooze(body):
    """POST /api/hermes-audit/unsnooze: the finding counts again at once."""
    label, finding = body.get("category"), body.get("finding")
    try:
        with _SNOOZE_LOCK:
            audit_snoozes.unsnooze(label, finding)
    except OSError as e:
        return 500, {"error": f"could not end the snooze: {e}"}
    _log({"event": "hermes_audit_unsnoozed", "result": finding})
    return 200, {}


def _copied():
    """True when the gateway plist's copy in setup/launchd matches the live
    plist byte for byte, the check audit.py makes."""
    try:
        return filecmp.cmp(launchd_source() / system.GATEWAY_PLIST.name,
                           system.GATEWAY_PLIST, shallow=False)
    except OSError:
        return False


def _expected_doc():
    """expected.yaml parsed, {} when unreadable: then nothing reads as
    acknowledged."""
    try:
        return yaml.safe_load(expected_file().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _handled(record):
    """The findings the page shows as handled, {category: {finding text:
    {"as": word}}}, and the count of the rest: the open findings that the
    page's audit tab, the menu bar badge and the status text count. Only warn
    categories count. The words, checked in this order against each
    finding's act:
    - accepted: the memory review this pass parked is accepted
    - fixed: the gateway plist was written after the pass that called it stale
    - copied: the plist's setup/launchd copy matches the live plist again
    - acknowledged: expected.yaml holds the finding's acknowledge edits
    - snoozed: the user snoozed the finding; "until" says when the snooze ends
    The first four hold until the next pass rebuilds the record."""
    if not record:
        return {}, 0
    snoozes = audit_snoozes.read()
    finished = record.get("finished_at")
    plist_at = system.plist_written_at()
    plist_fixed = bool(plist_at) and plist_at > (finished or record.get("started_at") or "")
    doc = None
    handled, open_count = {}, 0
    for c in record.get("categories") or []:
        if not isinstance(c, dict) or c.get("state") != "warn":
            continue
        label = c.get("label")
        for f in c.get("findings") or []:
            if not isinstance(f, dict):
                continue
            act, text = f.get("act") or {}, f.get("text")
            if act.get("acknowledge") and doc is None:
                doc = _expected_doc()
            how = ("accepted" if act.get("accept") and record.get("accepted")
                   else "fixed" if act.get("fix_plist") and plist_fixed
                   else "copied" if act.get("copy_plist") and _copied()
                   else "acknowledged" if act.get("acknowledge")
                   and _edits_hold(doc, act["acknowledge"]["edits"])
                   else "snoozed" if text in snoozes.get(label, {})
                   else None)
            if how is None:
                open_count += 1
                continue
            entry = {"as": how}
            if how == "snoozed":
                entry["until"] = snoozes[label][text]
            handled.setdefault(label, {})[text] = entry
    return handled, open_count


def control(flag, event, *keys):
    """One audit.py control run (--stop, --reset, --accept keys), in the
    request thread. Its own message is the page's answer, refusals included."""
    try:
        p = subprocess.run([str(UV), "run", "--no-project", str(SCRIPT), flag,
                            *keys],
                           capture_output=True, text=True,
                           timeout=CONTROL_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 500, {"error": f"{flag} failed: {type(e).__name__}: {e}"}
    if p.returncode != 0:
        return 500, {"error": (p.stderr or p.stdout).strip()[-400:]}
    result = p.stdout.strip()
    _log({"event": event, "result": result})
    return 200, {"result": result}


# ---------------------------------------------------------------- text answers

# An unfinished record cannot say which of two things happened: the run it
# holds died mid-pass, or a start after it died before it could seed a record
# of its own. Both leave that record with newer stderr under it, so the
# headline names both and the tail is the newest attempt's either way.
NO_REPORT = ("no report from the last audit run — it died mid-pass, or a start "
             "after it died before it could record one. The output below is "
             "the newest attempt's.")
NEVER_STARTED = ("the last audit run never started, so nothing has changed "
                 "since the pass before it.")


def _progress_line():
    """Where the run is now: the category, and the LLM call in flight. While
    a run starts up (before audit.py takes its lock and seeds its record) the
    record on file is the previous, finished pass."""
    p = _run_record()
    if p is None or p.get("finished_at"):
        return "starting up (no step written yet)"
    line = f"step {p.get('step')}/{p.get('total')}: {p.get('category')}"
    if (on_step := elapsed(p.get("step_started"))):
        line += f", {on_step} on this step"
    if p.get("llm"):
        line += f"\nLLM call in flight: {p['llm']}"
        if (in_call := elapsed(p.get("llm_started"))):
            line += f", running {in_call}"
    return line


def _failed(headline, tail):
    return f"FAILED: {headline}\n" + (tail or "(no error output captured)")


def _err_tail():
    """The stderr of the newest start, whatever its age."""
    try:
        return err_file().read_text(encoding="utf-8").strip()[-ERR_TAIL:] or None
    except OSError:
        return None


def status_text():
    if _running():
        record = _run_record()
        age = f", running {age}" if record and not record.get("finished_at") \
            and (age := elapsed(record.get("started_at"))) else ""
        return (f"Audit run in progress{age}. Not finished — no report for it "
                f"yet.\n{_progress_line()}")
    record = _run_record()
    if record is None:
        return "No audit run recorded yet."
    if not record.get("finished_at"):
        return _failed(NO_REPORT, _err_tail())
    if (tail := _error_tail(record["finished_at"])):
        return _failed(NEVER_STARTED, tail)
    cats = [c for c in record.get("categories") or [] if isinstance(c, dict)]
    handled, _ = _handled(record)
    # by text, as _handled() keys them: a finding the model repeated is one
    # handled entry but two lines
    def texts(c):
        return [f.get("text") for f in c.get("findings") or [] if isinstance(f, dict)]
    warn = [c.get("label") for c in cats if c.get("state") == "warn"
            and any(t not in handled.get(c.get("label"), {}) for t in texts(c))]
    when = f"Last pass finished {record['finished_at']}"
    n = sum(t in handled.get(c.get("label"), {}) for c in cats
            if c.get("state") == "warn" for t in texts(c))
    aside = (f" {n} other finding{'s' if n != 1 else ''} handled or snoozed "
             "on the actions page.") if n else ""
    if not warn:
        return (f"{when}: no open findings.{aside}" if n
                else f"{when}: every category clean, no findings.")
    return (f"{when}: {len(warn)} of {len(cats)} categories carry open "
            f"findings — {', '.join(warn)}.{aside}")


def report_text():
    if _running():
        return ("A run is in progress, so this pass has no report yet. The "
                "previous pass's findings are not this run's result.\n"
                + _progress_line())
    record = _run_record()
    if record is not None and not record.get("finished_at"):
        return _failed(NO_REPORT, _err_tail())
    try:
        text = report_file().read_text(encoding="utf-8").strip()
    except OSError:
        return (f"FAILED: no report at {report_file()} — no run has ever "
                "finished, or the file was removed.")
    if record is not None and _error_tail(record["finished_at"]):
        return ("The last audit run never started, so this is the pass before "
                f"it ({record['finished_at']}), not a fresh one:\n\n{text}")
    return text


# ---------------------------------------------------------------- area interface

NAME = "hermes_audit"


def boot():
    """Nothing to load: every answer is read from the audit's own files."""


def state():
    """The hermes-audit part of GET /api/state: whether a run is going, the pass
    itself (null before the first one), and the stderr of a run that ended
    without a report.

    A finished record with newer stderr is the other failure: the last start
    died before audit.py seeded a record, so the pass on file is the one
    before it. The page says which of the two happened.

    The cron job's stamps (last run, next run, failed) ride along for the
    page's schedule indicators, and _handled()'s findings and open count for
    the page's words and keys, its audit tab and the menu bar badge."""
    running = _running()
    record = _run_record()
    error = None
    if not running:
        stamp = record and (record.get("finished_at") or record.get("started_at"))
        error = _error_tail(stamp)
    handled, open_count = _handled(record)
    out = {"running": running, "run": record, "error": error,
           "handled": handled, "open": open_count}
    out.update(_job_fields())
    return out


def _h_run(body):
    return start()


def _h_stop(body):
    return control("--stop", "hermes_audit_stopped")


def _h_reset(body):
    return control("--reset", "hermes_audit_reset")


def _h_accept(body):
    """POST /api/hermes-audit/accept-changes: the memory review the last pass
    parked becomes the accepted one, so its findings stop being reported. No
    pass follows. The record still lists the findings until the next pass;
    until then the memory card reads Accepted, and _handled() leaves them
    out of the counts."""
    return control("--accept", "hermes_audit_memory_accepted")


HANDLERS = {"/api/hermes-audit/run": _h_run,
            "/api/hermes-audit/stop": _h_stop,
            "/api/hermes-audit/reset": _h_reset,
            "/api/hermes-audit/accept-changes": _h_accept,
            "/api/hermes-audit/acknowledge": acknowledge,
            "/api/hermes-audit/restore-skill": restore_skill,
            "/api/hermes-audit/copy-plist": copy_plist,
            "/api/hermes-audit/snooze": snooze,
            "/api/hermes-audit/unsnooze": unsnooze}
GET_HANDLERS = {"/api/hermes-audit/status": lambda params: (200, {"text": status_text()}),
                "/api/hermes-audit/report": lambda params: (200, {"text": report_text()})}
