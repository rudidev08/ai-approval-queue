"""Job rules area — the rules the user approved for a page area's model job, and
the weekly job's suggestions for new ones.

State is one file in the folder common.state_dir() names, rules.json
(0600, written under LOCK through common.write_json):

  {"areas": {"<area>": {"rules": [...], "suggestions": [...],
                        "rejected": [...], "evidence_through": iso | null}}}

- rules: {id, text, reason, added_at} — what the area's model job follows.
  The inbox scan gets them through the actions_inbox server (GET /api/rules),
  in their own block outside the email content.
- suggestions: {id, change: add|edit|remove, rule_id, text, why, evidence,
  created_at} — waiting for the user's thumbs up or down. evidence holds the
  service's own copies of the items the suggestion cites, never text the
  model wrote.
- rejected: {change, rule_id, text, reason, rejected_at} — handed to the job
  every week, so a suggestion the user turned down is never made again.
- evidence_through: the newest evidence stamp the job has seen.

Evidence comes from the decisions log (common._log's decisions-YYYY.jsonl).
AREAS names, per area, what its model job does and the function that turns
one log line into an evidence item ({id, at, summary}) or None. An area joins
by adding its entry there. The job gets every item of the last EVIDENCE_DAYS
days, so a pattern can build up over several weeks, and the area is skipped
when no item is newer than evidence_through.

Endpoints:

- GET  /api/rules             ?area — {rules, suggestions}: the scan's block
                              and the chat tools' listing
- POST /api/rules/candidates  {} — the job's input: per area with new
                              evidence, {area, about, evidence, rules,
                              suggestions, rejected, through}
- POST /api/rules/batch       {area, through, suggestions} — the job's
                              answer. A suggestion that breaks a rule (unknown
                              change, a rule id that is gone, bad text, no
                              known evidence) is dropped and named in the
                              answer; the rest are stored and evidence_through
                              moves to through
- POST /api/rules/decide      {area, id, decision: up|down, text?, reason?} —
                              up applies the suggestion (text replaces the
                              suggested wording of an add or edit), down
                              moves it to rejected with the reason
- POST /api/rules/save        {area, text, reason?, rule_id?} — add a rule,
                              or replace rule_id's text (chat)
- POST /api/rules/remove      {area, rule_id} — the page's remove key, chat

Every change lands in the decisions log as one rule_* or rules_suggested
line.
"""

import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import common
import inbox
from common import _log, _now

AREAS = {
    "inbox": {
        "about": ("The inbox scan reads the user's inbox and proposes calendar "
                  "events, shipments to track and documents to file, which "
                  "The user approves on the actions page. It picks a calendar "
                  "and a title for each event; the user can move an event to "
                  "another calendar or rename it before approving."),
        "evidence": inbox.rule_evidence,
    },
}
EVIDENCE_DAYS = 180
TEXT_CAP = 200   # a rule, a reason, a suggestion's why: one short line each
CHANGES = ("add", "edit", "remove")
_LINE_BREAK = re.compile(r"[\r\n]")

LOCK = threading.Lock()
STATE = {}


def state_file():
    return common.state_dir() / "rules.json"


def _area(state, name):
    """The area's record, created on first use."""
    return state["areas"].setdefault(name, {"rules": [], "suggestions": [],
                                            "rejected": [],
                                            "evidence_through": None})


def _save():
    """Caller holds LOCK."""
    common.write_json(state_file(), STATE)


def _line(v, field, required=True):
    """v as one stripped line under TEXT_CAP; ValueError otherwise."""
    if v is None or v == "":
        if required:
            raise ValueError(f"{field} is required")
        return ""
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{field} must be text")
    v = v.strip()
    if _LINE_BREAK.search(v):
        raise ValueError(f"{field} must be one line")
    if len(v) > TEXT_CAP:
        raise ValueError(f"{field} is {len(v)} characters, the cap is {TEXT_CAP}")
    return v


def _new_id(prefix):
    return prefix + uuid.uuid4().hex[:8]


def _find(items, item_id):
    return next((x for x in items if x["id"] == item_id), None)


# ---------------------------------------------------------------- evidence

def evidence(area):
    """The area's evidence items of the last EVIDENCE_DAYS days, oldest
    first. Unreadable log lines are skipped."""
    since = (datetime.now(timezone.utc) - timedelta(days=EVIDENCE_DAYS)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    to_item = AREAS[area]["evidence"]
    year = datetime.now(timezone.utc).year
    out = []
    for y in (year - 1, year):
        path = common.state_dir() / f"decisions-{y}.jsonl"
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        for raw in text.splitlines():
            try:
                line = json.loads(raw)
                if line.get("ts", "") < since:
                    continue
                item = to_item(line)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if item:
                out.append(item)
    return out


def candidates(body):
    """POST /api/rules/candidates: per area with evidence newer than the
    last run, everything the job's model call needs. The log read runs
    outside LOCK."""
    out = []
    for name, cfg in AREAS.items():
        items = evidence(name)
        with LOCK:
            a = json.loads(json.dumps(_area(STATE, name)))
        through = a["evidence_through"]
        if not any(through is None or i["at"] > through for i in items):
            continue
        out.append({"area": name, "about": cfg["about"], "evidence": items,
                    "rules": a["rules"], "suggestions": a["suggestions"],
                    "rejected": a["rejected"],
                    "through": max(i["at"] for i in items)})
    return 200, {"areas": out}


def _check_suggestion(x, rules, known):
    """A stored suggestion out of one the job sent; ValueError names what
    is wrong with it."""
    if not isinstance(x, dict):
        raise ValueError("not an object")
    change = x.get("change")
    if change not in CHANGES:
        raise ValueError(f"change {change!r} is not add, edit or remove")
    rule_id = x.get("rule_id") or ""
    if change == "add":
        rule_id = ""
    elif not _find(rules, rule_id):
        raise ValueError(f"rule {rule_id!r} does not exist")
    text = _line(x.get("text"), "text") if change != "remove" else ""
    why = _line(x.get("why"), "why")
    ids = x.get("evidence")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValueError("evidence must be a non-empty list of evidence ids")
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"cites evidence {unknown[0]!r}, which does not exist")
    cited = [known[i] for i in dict.fromkeys(ids)]
    return {"id": _new_id("sug_"), "change": change, "rule_id": rule_id,
            "text": text, "why": why, "evidence": cited, "created_at": _now()}


def batch(body):
    """POST /api/rules/batch: store the job's suggestions for one area.
    Evidence ids are checked against the log read again here, so a
    suggestion can only cite items the service itself recorded."""
    name = body.get("area")
    if name not in AREAS:
        return 400, {"error": f"unknown area {name!r}"}
    through = body.get("through")
    if not isinstance(through, str) or not through:
        return 400, {"error": "through must be the candidates' through stamp"}
    sent = body.get("suggestions")
    if not isinstance(sent, list):
        return 400, {"error": "suggestions must be a list"}
    known = {i["id"]: i for i in evidence(name)}
    with LOCK:
        a = _area(STATE, name)
        kept, dropped = [], []
        for x in sent:
            try:
                kept.append(_check_suggestion(x, a["rules"], known))
            except ValueError as e:
                dropped.append(str(e))
        a["suggestions"] += kept
        a["evidence_through"] = through
        _save()
    _log({"event": "rules_suggested", "area": name, "added": len(kept),
          "dropped": dropped, "through": through})
    return 200, {"added": len(kept), "dropped": dropped}


# ---------------------------------------------------------------- decisions

def _add_rule(a, text, reason):
    rule = {"id": _new_id("rule_"), "text": text, "reason": reason,
            "added_at": _now()}
    a["rules"].append(rule)
    return rule


def decide(body):
    """POST /api/rules/decide: thumbs up applies the suggestion, thumbs down
    rejects it. Either way it leaves the list."""
    name = body.get("area")
    if name not in AREAS:
        return 400, {"error": f"unknown area {name!r}"}
    decision = body.get("decision")
    if decision not in ("up", "down"):
        return 400, {"error": "decision must be up or down"}
    try:
        reason = _line(body.get("reason"), "reason", required=False)
        # only a thumbs up uses the wording
        text = _line(body.get("text"), "text", required=False) if decision == "up" else ""
    except ValueError as e:
        return 400, {"error": str(e)}
    with LOCK:
        a = _area(STATE, name)
        s = _find(a["suggestions"], body.get("id"))
        if s is None:
            return 404, {"error": "unknown suggestion"}
        target = _find(a["rules"], s["rule_id"]) if s["rule_id"] else None
        if decision == "down":
            a["rejected"].append({"change": s["change"], "rule_id": s["rule_id"],
                                  "text": s["text"], "reason": reason,
                                  "rejected_at": _now()})
            event = {"event": "rule_rejected", "text": s["text"]}
        elif s["change"] != "add" and target is None:
            return 409, {"error": "the rule this suggestion changes is gone — "
                                  "turn it down instead"}
        elif s["change"] == "add":
            rule = _add_rule(a, text or s["text"], reason)
            event = {"event": "rule_added", "rule_id": rule["id"], "text": rule["text"]}
        elif s["change"] == "edit":
            target["text"] = text or s["text"]
            if reason:
                target["reason"] = reason
            event = {"event": "rule_changed", "rule_id": target["id"],
                     "text": target["text"]}
        else:
            a["rules"].remove(target)
            event = {"event": "rule_removed", "rule_id": target["id"],
                     "text": target["text"]}
        a["suggestions"].remove(s)
        _save()
    _log({**event, "area": name, "suggestion": s["id"], "reason": reason})
    return 200, {"ok": True}


def save(body):
    """POST /api/rules/save: add a rule, or replace rule_id's text."""
    name = body.get("area")
    if name not in AREAS:
        return 400, {"error": f"unknown area {name!r}"}
    try:
        text = _line(body.get("text"), "text")
        reason = _line(body.get("reason"), "reason", required=False)
    except ValueError as e:
        return 400, {"error": str(e)}
    with LOCK:
        a = _area(STATE, name)
        rule_id = body.get("rule_id")
        if rule_id:
            rule = _find(a["rules"], rule_id)
            if rule is None:
                return 404, {"error": f"unknown rule {rule_id!r}"}
            rule["text"] = text
            if reason:
                rule["reason"] = reason
            event = "rule_changed"
        else:
            rule = _add_rule(a, text, reason)
            event = "rule_added"
        _save()
    _log({"event": event, "area": name, "rule_id": rule["id"], "text": text,
          "reason": reason})
    return 200, {"rule": dict(rule)}


def remove(body):
    """POST /api/rules/remove: drop one approved rule."""
    name = body.get("area")
    if name not in AREAS:
        return 400, {"error": f"unknown area {name!r}"}
    with LOCK:
        a = _area(STATE, name)
        rule = _find(a["rules"], body.get("rule_id"))
        if rule is None:
            return 404, {"error": "unknown rule"}
        a["rules"].remove(rule)
        _save()
    _log({"event": "rule_removed", "area": name, "rule_id": rule["id"],
          "text": rule["text"]})
    return 200, {"ok": True}


def listing(params):
    """GET /api/rules?area=…"""
    name = params.get("area")
    if name not in AREAS:
        return 400, {"error": f"unknown area {name!r}"}
    with LOCK:
        a = _area(STATE, name)
        return 200, {"rules": a["rules"], "suggestions": a["suggestions"]}


# ---------------------------------------------------------------- area interface

NAME = "rules"


def boot():
    global STATE
    path = state_file()
    STATE = json.loads(path.read_text()) if path.exists() else {"areas": {}}
    for name in AREAS:
        _area(STATE, name)


def state():
    """Per area: the approved rules and the waiting suggestions."""
    with LOCK:
        return {name: {"rules": a["rules"], "suggestions": a["suggestions"]}
                for name, a in json.loads(json.dumps(STATE["areas"])).items()
                if name in AREAS}


HANDLERS = {"/api/rules/candidates": candidates, "/api/rules/batch": batch,
            "/api/rules/decide": decide, "/api/rules/save": save,
            "/api/rules/remove": remove}
GET_HANDLERS = {"/api/rules": listing}
