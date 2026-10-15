"""Family emails: the emails the assistant sends each person on a schedule.

A queue item is one person and one email kind: finance daily, weekly or
monthly, or reminders daily. It holds an on/off flag; a
reminders item also holds the person's choice of lists and sections, and
the finance daily the transaction ids already printed for that person.
Every finance item of a kind sends that kind's preset: its sections and
options, set on the finance reports area (/preset).

Four hermes cron jobs (JOBS) call /api/family-emails/run through
family_emails_job.py; one run builds and sends every enabled item of one
kind. The page builds a preview of an item, sends that preview as a test,
sends one item at once (run now, outside the schedule), and changes what
a reminders item includes. Every send is recorded with the exact text that went
out; the last HISTORY records per item are kept, and the scheduled and
run-now ones that went out stay readable on the page. A sample (the
text endpoint's person/kind/sample id) is the same email built from
made-up rows, read fresh on every request, never stored and never sent.

Content: finance emails come from finance_email.py (a child of the cron
scripts' interpreter; the finance reports area builds through it too);
reminder emails are built here from the host app's reminders server.
Sends go through family_emails_send.py in the mail venv.
"""

import copy
import json
import pathlib
import subprocess
import threading
import uuid
from datetime import date, datetime, time, timedelta

import common
import llm_pause
import reminders

NAME = "family_emails"
APP = pathlib.Path(__file__).resolve().parent
PYTHON = "/usr/bin/python3"        # the interpreter the cron scripts use
# the mail venv's python: the mail scripts import the MCP server's deps
MAIL_PYTHON = str(pathlib.Path.home() / ".venvs" / "mail_tools" / "bin" / "python")
BUILD_TIMEOUT = 1900   # a finance build: the budget read and one model call, Claude then the backup
ERR_TAIL = 800         # chars of a failed child's stderr kept as its error
PEOPLE = ("Alex", "Sam")
SECTIONS = {"daily": ("Summary", "Items to check", "Unusual charges", "New transactions"),
            "weekly": ("Summary", "Cash flow"),
            "monthly": ("Summary", "Cash flow", "Unusual charges")}
# the options each finance kind takes: details and combine personal shape
# the cash flow, which the daily lacks (the weekly and monthly always print
# its category rows); show new payees shapes the Unusual charges, which the
# weekly lacks
OPTION_KEYS = ("detailed", "combine", "show_new_payees")
OPTIONS = {"daily": ("show_new_payees",), "weekly": ("detailed", "combine"),
           "monthly": ("detailed", "combine", "show_new_payees")}
# the options a preset starts with on: no per-person figure goes out by
# default, and only the daily flags new payees
DEFAULT_ON = {"daily": ("show_new_payees",), "weekly": ("combine",), "monthly": ("combine",)}
CONTENT = ("Overdue", "Due soon", "Added", "Completed")
JOBS = {"daily": "finance-daily", "weekly": "finance-weekly", "monthly": "finance-monthly",
        "reminders_daily": "reminders-changes"}
HISTORY = 20          # delivery records kept per queue item
BATCH_DAYS = 7        # run records kept: a job re-run inside the window never sends twice
SEND_TIMEOUT = 600    # two send attempts, the wait between them, and slack
# the reminders window's daily edge, the reminders-changes run time: a late
# run covers the same 24 hours, so nothing falls between two emails
CUTOFF = time(6, 0)
QUICK_DONE = timedelta(minutes=3)   # completed this soon after being added: a mistaken add, left out
UNCONFIRMED = "Could not confirm this send. Check the Sent folder before sending again."
LOCK = threading.Lock()


def preset_default(kind):
    """A finance kind's preset before one is saved: every section, the
    DEFAULT_ON options on."""
    return {"sections": list(SECTIONS[kind]), **{k: k in DEFAULT_ON[kind] for k in OPTION_KEYS}}


def empty():
    return {"addresses": dict.fromkeys(PEOPLE, ""), "queues": {p: {} for p in PEOPLE},
            "presets": {kind: preset_default(kind) for kind in SECTIONS},
            "deliveries": {}, "batches": {}}


DATA = empty()
PREVIEWS = {}         # "person/kind" -> {status, settings, subject, text, built_at, error}


def store_path():
    return common.state_dir() / "family-emails.json"


def boot():
    global DATA
    DATA = json.loads(store_path().read_text()) if store_path().exists() else empty()
    for item in DATA["deliveries"].values():
        if item["status"] in ("building", "sending"):
            item.update(status="error", finished_at=common._now(),
                        note="The service stopped during this email. " + UNCONFIRMED)
    for batch in DATA["batches"].values():
        if batch["status"] == "running":
            batch.update(status="error", error="The service stopped during this run.")
    save()


def save():
    """Write the store, keeping HISTORY records per item and BATCH_DAYS of runs.
    Caller holds LOCK (or is boot)."""
    count, kept = {}, []
    for item in reversed(DATA["deliveries"].values()):
        key = item["person"] + "/" + item["kind"]
        count[key] = count.get(key, 0) + 1
        if count[key] <= HISTORY:
            kept.append(item)
    DATA["deliveries"] = {i["id"]: i for i in reversed(kept)}
    cutoff = (date.today() - timedelta(days=BATCH_DAYS)).isoformat()
    DATA["batches"] = {k: b for k, b in DATA["batches"].items() if k.split("/")[1] >= cutoff}
    common.write_json(store_path(), DATA)


def defaults(person, lists):
    """A new reminders item's settings: the lists named after the person
    (every list for Alex), Added and Completed."""
    mine = [n for n in lists if person == "Alex" or n == person or n.startswith(person + " ")]
    return {"lists": mine, "include": ["Added", "Completed"],
            "skip": True}


def valid_address(value):
    return (isinstance(value, str) and 0 < len(value) <= 254 and value.count("@") == 1
            and all(value.split("@")) and not any(c.isspace() or c in ",;<>" for c in value))


def recipients():
    """Every address an email may go to: the saved family addresses."""
    return list(dict.fromkeys(a.lower() for a in DATA["addresses"].values() if valid_address(a)))


def item_settings(person, kind):
    """What an item's email holds: the kind's preset for a finance item,
    the item's own settings for a reminders item. Caller holds LOCK."""
    if kind in SECTIONS:
        return DATA["presets"][kind]
    return DATA["queues"][person][kind]["settings"]


def next_runs():
    """kind -> the cron job's next run, None while the job is off in hermes."""
    out = {}
    for kind, name in JOBS.items():
        job = common.cron_job(name) or {}
        out[kind] = job.get("next_run_at") if job.get("enabled") else None
    return out


def state():
    runs = next_runs()
    lists = reminders.STATE.get("lists", [])
    with LOCK:
        people, enabled, issues, sent_at = [], 0, 0, []
        for person in PEOPLE:
            queues = []
            for kind, entry in DATA["queues"][person].items():
                own = [d for d in DATA["deliveries"].values() if d["person"] == person and d["kind"] == kind]
                last = own[-1] if own else None
                sent = [d for d in reversed(own) if d["status"] == "sent" and d["mode"] != "test"]
                sent_at += [d["finished_at"] for d in sent]
                preview = PREVIEWS.get(person + "/" + kind)
                enabled += entry["enabled"]
                issues += bool(last and last["status"] == "error")
                spec = item_settings(person, kind)
                queues.append({
                    "kind": kind, "enabled": entry["enabled"], "settings": spec,
                    "next": runs[kind] if entry["enabled"] else None,
                    "last": last and {k: last.get(k) for k in ("id", "status", "to", "mode", "finished_at", "note", "missing")},
                    "sent": [{"id": d["id"], "at": d["finished_at"]} for d in sent],
                    "preview": preview and {"status": preview["status"], "built_at": preview.get("built_at"),
                                            "error": preview.get("error"),
                                            "stale": preview["settings"] != spec}})
            people.append({"name": person, "address": DATA["addresses"][person], "queues": queues})
        return {"people": people, "lists": lists, "sections": SECTIONS, "options": OPTIONS,
                "presets": copy.deepcopy(DATA["presets"]), "content": CONTENT,
                "recipients": recipients(),
                "enabled": enabled, "issues": issues,
                "last_sent": max(sent_at, default=None),
                "next_send": min((q["next"] for p in people for q in p["queues"] if q["next"]), default=None)}


# ---------------------------------------------------------------- settings

def chosen(value, choices, label):
    if not isinstance(value, list) or not value or any(v not in choices for v in value):
        raise ValueError("Choose " + label + ".")
    return [c for c in choices if c in value]


def settings(kind, raw):
    """The saved form of a settings dict the page posted; ValueError on a bad one."""
    if not isinstance(raw, dict):
        raise ValueError("Bad settings.")
    if kind in SECTIONS:
        out = {"sections": chosen(raw.get("sections"), SECTIONS[kind], "at least one section")}
        for key in OPTION_KEYS:
            out[key] = key in OPTIONS[kind] and raw.get(key) is True
        return out
    lists = raw.get("lists")
    if not isinstance(lists, list) or not lists or not all(isinstance(n, str) and n for n in lists):
        raise ValueError("Choose at least one reminder list.")
    return {"lists": list(dict.fromkeys(lists)),
            "include": chosen(raw.get("include"), CONTENT, "at least one kind of reminder"),
            "skip": raw.get("skip") is not False}


def address(body):
    person, value = body.get("person"), body.get("address")
    if person not in PEOPLE or not isinstance(value, str):
        return 400, {"error": "Choose a person and an email address."}
    value = value.strip().lower()
    if value and not valid_address(value):
        return 400, {"error": "Enter one email address."}
    with LOCK:
        if not value and any(q["enabled"] for q in DATA["queues"][person].values()):
            return 400, {"error": "Pause " + person + "’s emails before removing the address."}
        DATA["addresses"][person] = value
        save()
    return 200, {}


def queue(body):
    person, kind, action = body.get("person"), body.get("kind"), body.get("action")
    if person not in PEOPLE or kind not in JOBS:
        return 400, {"error": "Choose a person and an email."}
    # read before the lock: a new reminders item starts with the person's
    # lists, and with the page closed nothing else has read them
    lists = reminders.lists() if action == "add" and kind not in SECTIONS else []
    with LOCK:
        items = DATA["queues"][person]
        entry = items.get(kind)
        if action == "add":
            if entry:
                return 409, {"error": "This email is already in the queue."}
            entry = items[kind] = ({"enabled": False, "reported": {}} if kind in SECTIONS
                                   else {"enabled": False, "settings": defaults(person, lists)})
            # on at once when enable would pass: a saved address and
            # settings that hold, which a reminders item with no list lacks
            try:
                settings(kind, item_settings(person, kind))
                entry["enabled"] = bool(DATA["addresses"][person])
            except ValueError:
                pass
        elif entry is None:
            return 404, {"error": "This email is not in the queue."}
        elif action == "remove":
            # the settings, the sent history and the preview go together
            del items[kind]
            DATA["deliveries"] = {i: d for i, d in DATA["deliveries"].items()
                                  if (d["person"], d["kind"]) != (person, kind)}
            PREVIEWS.pop(person + "/" + kind, None)
        elif action == "enable":
            if not DATA["addresses"][person]:
                return 400, {"error": "Save " + person + "’s email address first."}
            try:
                settings(kind, item_settings(person, kind))   # an added item may still hold no list
            except ValueError as e:
                return 400, {"error": str(e)}
            entry["enabled"] = True
        elif action == "disable":
            entry["enabled"] = False
        elif action == "settings":
            if kind in SECTIONS:
                return 400, {"error": "A finance email sends its kind's preset. Change it on finance reports."}
            try:
                entry["settings"] = settings(kind, body.get("settings"))
            except ValueError as e:
                return 400, {"error": str(e)}
        else:
            return 400, {"error": "Unknown queue action."}
        save()
    return 200, {}


def preset(body):
    """Finance reports' make-default key: a finance kind's sections and
    options, sent by every finance item of that kind from now on."""
    kind = body.get("kind")
    if kind not in SECTIONS:
        return 400, {"error": "Choose a finance email kind."}
    try:
        spec = settings(kind, body.get("settings"))
    except ValueError as e:
        return 400, {"error": str(e)}
    with LOCK:
        DATA["presets"][kind] = spec
        save()
    return 200, {}


# ---------------------------------------------------------------- content

def host_read(tool, args, field):
    ok, text = reminders._call(tool, {**args, "format": "json"})
    if not ok:
        raise ValueError(text)
    data = json.loads(text)
    if field == "reminders" and data["total"] > len(data[field]):
        raise ValueError("The reminder list is too large to read in full. No email was built.")
    return data[field]


def paused(kind, spec):
    """A 409 while the assistant's model runs are paused and the build would call the
    model (a finance email with Summary); None otherwise."""
    if kind in SECTIONS and "Summary" in spec["sections"] and (pause := llm_pause.read()):
        return 409, {"error": f"the assistant's model runs are paused until {pause['until']}"}
    return None


def finance_build(request):
    """One run of finance_email.py with its request; ValueError carries a
    failed build's stderr tail. The finance reports area builds through it."""
    run = subprocess.run([PYTHON, str(APP / "finance_email.py")], input=json.dumps(request),
                         capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    if run.returncode:
        raise ValueError(run.stderr.strip()[-ERR_TAIL:] or "The finance email failed to build.")
    return json.loads(run.stdout)


def last_month(today):
    """The month before today's, YYYY-MM: the monthly email's month."""
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def finance_content(kind, spec, now, reported, sample=False):
    today = now.date()
    return finance_build({"kind": kind, "settings": spec, "day": today.isoformat(),
                          "month": last_month(today), "reported": reported, "sample": sample})


class MissingLists(ValueError):
    """Chosen lists Reminders no longer has; a failed run keeps the names
    so the page can tell when the saved lists are fixed."""
    def __init__(self, names):
        super().__init__("Missing lists " + ", ".join(names) + ".")
        self.names = names


def quick_done(r):
    """Completed within QUICK_DONE of being added; both stamps are to the minute."""
    return bool(r["created_at"]) and (datetime.fromisoformat(r["done_at"])
                                      - datetime.fromisoformat(r["created_at"]) <= QUICK_DONE)


def reminder_rows(spec, start, end, today):
    """label -> the rows of every chosen list, read from the host app. start
    and end bound Added and Completed, as YYYY-MM-DDTHH:MM."""
    known = {item["name"] for item in host_read("get_lists", {}, "lists")}
    missing = [name for name in spec["lists"] if name not in known]
    if missing:
        raise MissingLists(missing)
    rows = {label: [] for label in CONTENT}
    for name in spec["lists"]:
        args = {"list_name": name, "limit": 10000}
        open_rows = host_read("search_reminders", args, "reminders")
        done_rows = ([r for r in host_read("search_reminders", {**args, "completed_on_or_after": start[:10]}, "reminders")
                      if not quick_done(r)]
                     if {"Added", "Completed"} & set(spec["include"]) else [])
        rows["Overdue"] += [r for r in open_rows if r["due"] and r["due"] < today]
        rows["Due soon"] += [r for r in open_rows if r["due"][:10] == today]
        rows["Completed"] += [r for r in done_rows if start <= r["done_at"] < end]
        seen = set()
        for r in done_rows + open_rows:
            ident = r["rid"] or (r["list"], r["name"], r["created_at"])
            if start <= r["created_at"] < end and ident not in seen:
                seen.add(ident)
                rows["Added"].append(r)
    return rows


# made-up reminders per section; due is days from today
SAMPLE_REMINDERS = {
    "Overdue": [{"name": "Call the dentist", "due": -3, "priority": "high", "notes": "Ask about the Tuesday slot."},
                {"name": "Renew the car registration", "due": -1}, {"name": "Return the library books", "due": -2},
                {"name": "Pay the water bill", "due": -1}],
    "Due soon": [{"name": "Water the plants", "due": 0, "repeat": "weekly"},
                 {"name": "Change the fridge water filter", "due": 0, "repeat": "monthly"},
                 {"name": "Send the odometer reading to the insurer", "due": 0}, {"name": "Buy batteries", "due": 0}],
    "Added": [{"name": "Buy printer paper"}, {"name": "Renew the library card"},
              {"name": "Sign up for the club event", "due": 3}, {"name": "Rotate the winter clothes"},
              {"name": "Clean the bike chain"}, {"name": "Print the insurance card for the car"}],
    "Completed": [{"name": "Take out the bins", "due": -1}, {"name": "Return the parcel"},
                  {"name": "Pick up the dry cleaning", "due": -1}, {"name": "Send the meeting notes"}],
}
SAMPLE_COUNTS = (3, 1, 2, 1, 0, 0, 0, 0)   # rows per list, one step on per section


def sample_reminder_rows(spec, today):
    """Made-up rows: each section gives about half the chosen lists one to
    three rows and skips the rest, as real days do; the first list shows in
    every section."""
    rows = {label: [] for label in CONTENT}
    for step, label in enumerate(CONTENT):
        pool = SAMPLE_REMINDERS[label]
        for i, name in enumerate(spec["lists"]):
            for _ in range(SAMPLE_COUNTS[(i + step) % len(SAMPLE_COUNTS)]):
                r = pool[len(rows[label]) % len(pool)]
                due = (today + timedelta(days=r["due"])).isoformat() if "due" in r else ""
                rows[label].append({**r, "list": name, "due": due})
    return rows


def reminder_content(spec, now, sample=False):
    """Subject, text and an empty flag. Overdue: due before today. Due soon:
    due today. Added and completed: the 24 hours up to the latest CUTOFF."""
    today = now.date()
    end = datetime.combine(today, CUTOFF)
    if end > now:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    rows = (sample_reminder_rows(spec, today) if sample
            else reminder_rows(spec, start.isoformat(timespec="minutes"),
                               end.isoformat(timespec="minutes"), today.isoformat()))
    title = f"Reminders daily — {today}" + (" (sample)" if sample else "")
    span = f"{start:%Y-%m-%d %H:%M} through {end:%Y-%m-%d %H:%M}"
    blocks = ["# " + title, f"Added and completed: {span}."]
    for label in spec["include"]:
        lines = ["## " + label]
        for name in spec["lists"]:
            matching = [r for r in rows[label] if r["list"] == name]
            if not matching:
                continue
            lines += ["", "### " + name]
            for r in matching:
                fields = [f"{key}: {r[key]}" for key in ("due", "priority", "repeat") if r.get(key)]
                lines.append("- " + r["name"] + (" · " + " · ".join(fields) if fields else ""))
                lines.extend("  " + line for line in r.get("notes", "").splitlines() if line)
        if len(lines) == 1:
            lines.append("None.")
        blocks.append("\n".join(lines))
    return {"subject": title, "text": "\n\n".join(blocks) + "\n",
            "empty": not any(rows[label] for label in spec["include"])}


def content(kind, spec, now, reported, sample=False):
    if kind in SECTIONS:
        return finance_content(kind, spec, now, reported, sample)
    return reminder_content(spec, now, sample)


# ---------------------------------------------------------------- previews and sends

def preview(body):
    person, kind = body.get("person"), body.get("kind")
    key = f"{person}/{kind}"
    with LOCK:
        entry = DATA["queues"].get(person, {}).get(kind)
        if not entry:
            return 404, {"error": "This email is not in the queue."}
        if PREVIEWS.get(key, {}).get("status") == "building":
            return 409, {"error": "This preview is already building."}
        if refused := paused(kind, item_settings(person, kind)):
            return refused
        item = PREVIEWS[key] = {"status": "building", "settings": copy.deepcopy(item_settings(person, kind))}
        reported = copy.deepcopy(entry.get("reported", {}))
    threading.Thread(target=build_preview, args=(item, kind, reported), daemon=True).start()
    return 202, {}


def build_preview(item, kind, reported):
    try:
        result = content(kind, item["settings"], datetime.now(), reported)
        out = {"status": "ready", "subject": result["subject"], "text": result["text"],
               "built_at": common._now()}
    except Exception as e:
        out = {"status": "error", "error": str(e)}
    with LOCK:
        item.update(out)


def record(person, kind, to, mode, **fields):
    """One delivery record, stored before anything is built or sent.
    Caller holds LOCK."""
    item = {"id": str(uuid.uuid4()), "person": person, "kind": kind, "to": to, "mode": mode,
            "started_at": common._now(), **fields}
    DATA["deliveries"][item["id"]] = item
    save()
    return item


def send_test(body):
    person, kind, to = body.get("person"), body.get("kind"), body.get("to")
    with LOCK:
        entry = DATA["queues"].get(person, {}).get(kind)
        built = PREVIEWS.get(f"{person}/{kind}")
        if not entry or not built or built["status"] != "ready":
            return 409, {"error": "Show a preview first."}
        if built["settings"] != item_settings(person, kind):
            return 409, {"error": "The settings changed. Show the preview again."}
        if to not in recipients():
            return 400, {"error": "Choose a saved email address."}
        if any(d["status"] == "sending" for d in DATA["deliveries"].values()):
            return 409, {"error": "An email is already being sent."}
        item = record(person, kind, to, "test", status="sending",
                      subject="[Test] " + built["subject"], text=built["text"])
    threading.Thread(target=deliver, args=(item,), daemon=True).start()
    return 202, {}


def deliver(item):
    """One send of a record in the sending state; the outcome lands on it.
    A failure keeps its cause in the note, with the check-Sent warning after
    it: only the sender itself knows whether nothing went out."""
    try:
        run = subprocess.run([MAIL_PYTHON, str(APP / "family_emails_send.py")],
                             input=json.dumps({k: item[k] for k in ("to", "subject", "text")}),
                             capture_output=True, text=True, timeout=SEND_TIMEOUT)
        if run.returncode:
            raise RuntimeError(run.stderr.strip()[-ERR_TAIL:] or "The send process failed.")
        result = json.loads(run.stdout)
    except Exception as e:
        result = {"status": "error", "note": f"{e} {UNCONFIRMED}".strip()}
    with LOCK:
        item.update(result, finished_at=common._now())
        reported = item.pop("reported", None)
        entry = DATA["queues"][item["person"]].get(item["kind"])
        if item["status"] == "sent" and item["mode"] != "test" and entry and reported is not None:
            entry["reported"] = reported
        save()


def sample(ident):
    """The text of a sample by its person/kind/sample id: what the item
    holds now, made-up rows, built now."""
    person, kind = (ident.split("/") + [""])[:2]
    with LOCK:
        entry = DATA["queues"].get(person, {}).get(kind)
        spec = copy.deepcopy(item_settings(person, kind)) if entry else None
    if not spec:
        return 404, {"error": "This email is not in the queue."}
    try:
        result = content(kind, spec, datetime.now(), {}, sample=True)
    except Exception as e:
        return 500, {"error": str(e)}
    return 200, {"subject": result["subject"], "text": result["text"]}


def text(params):
    """The exact text of a delivery record, of a preview by its person/kind
    key, or of a sample by its person/kind/sample id."""
    ident = params.get("id", "")
    if ident.endswith("/sample"):
        return sample(ident)
    with LOCK:
        item = DATA["deliveries"].get(ident) or PREVIEWS.get(ident)
        if not item or "text" not in item:
            return 404, {"error": "No text for this email."}
        return 200, {"subject": item["subject"], "text": item["text"], "to": item.get("to")}


# ---------------------------------------------------------------- runs

def build_and_send(item, spec, reported):
    """Build a record in the building state from what its item holds, then
    send it; every outcome lands on the record."""
    try:
        if not item["to"]:
            raise ValueError("No email address saved.")
        result = content(item["kind"], spec, datetime.now(), reported)
        with LOCK:
            item.update(subject=result["subject"], text=result["text"])
            if "reported" in result:
                item["reported"] = result["reported"]
            if result.get("empty") and spec.get("skip"):
                item.update(status="skipped", finished_at=common._now(), note="No matching reminders.")
            else:
                item["status"] = "sending"
            save()
    except Exception as e:
        with LOCK:
            item.update(status="error", note=str(e), finished_at=common._now())
            if isinstance(e, MissingLists):
                item["missing"] = e.names
            save()
    if item["status"] == "sending":
        deliver(item)


def run_item(body):
    """The user's run now: one item built and sent at once, paused or not. The
    schedule is not changed; its next run sends as usual."""
    person, kind = body.get("person"), body.get("kind")
    with LOCK:
        entry = DATA["queues"].get(person, {}).get(kind)
        if not entry:
            return 404, {"error": "This email is not in the queue."}
        if not DATA["addresses"][person]:
            return 400, {"error": "Save " + person + "’s email address first."}
        spec = copy.deepcopy(item_settings(person, kind))
        try:
            settings(kind, spec)   # an added item may still hold no list
        except ValueError as e:
            return 400, {"error": str(e)}
        if any(d["status"] in ("building", "sending") for d in DATA["deliveries"].values()):
            return 409, {"error": "An email is already being built or sent."}
        if refused := paused(kind, spec):
            return refused
        reported = copy.deepcopy(entry.get("reported", {}))
        item = record(person, kind, DATA["addresses"][person], "now", status="building")
    threading.Thread(target=build_and_send, args=(item, spec, reported), daemon=True).start()
    return 202, {}


def run(body):
    """The cron client's call: one run of a kind per day. A second call the
    same day answers with the stored run, unless that run failed — then a
    new run sends to the people the failed one did not reach."""
    kind = body.get("kind")
    if kind not in JOBS:
        return 400, {"error": "Unknown email kind."}
    batch_id = f"{kind}/{date.today().isoformat()}"
    with LOCK:
        batch = DATA["batches"].get(batch_id)
        if batch and batch["status"] != "error":
            return 200, copy.deepcopy(batch)
        batch = DATA["batches"][batch_id] = {"id": batch_id, "kind": kind, "status": "running",
                                             "started_at": common._now()}
        try:
            save()
        except OSError:
            del DATA["batches"][batch_id]
            raise
    threading.Thread(target=run_batch, args=(batch,), daemon=True).start()
    return 202, copy.deepcopy(batch)


def run_batch(batch):
    kind, errors = batch["kind"], []
    try:
        for person in PEOPLE:
            with LOCK:
                entry = DATA["queues"][person].get(kind)
                done = any(d.get("batch") == batch["id"] and d["person"] == person and d["status"] != "error"
                           for d in DATA["deliveries"].values())
                if not entry or not entry["enabled"] or done:
                    continue
                spec = copy.deepcopy(item_settings(person, kind))
                reported = copy.deepcopy(entry.get("reported", {}))
                item = record(person, kind, DATA["addresses"][person], "scheduled",
                              batch=batch["id"], status="building")
            build_and_send(item, spec, reported)
            if item["status"] == "error":
                errors.append(f"{person}: {item['note']}")
    except Exception as e:
        # a state write failed outside an item's own handling: the run ends
        # as an error instead of staying "running" until the next restart
        errors.append(f"The run stopped: {e}")
    with LOCK:
        batch.update(status="error" if errors else "done", error="; ".join(errors),
                     finished_at=common._now())
        save()


def status(params):
    """state() plus each kind's newest run, for the clients that are not the
    page: the page shows runs through each item's delivery records. Reads the
    reminder lists when none are known, since the page's polls may not have."""
    reminders.lists()
    out = state()
    with LOCK:
        newest = {}
        for batch in DATA["batches"].values():
            if batch["id"] > newest.get(batch["kind"], {"id": ""})["id"]:
                newest[batch["kind"]] = batch
        out["runs"] = copy.deepcopy(newest)
    return 200, out


def get_run(params):
    with LOCK:
        batch = DATA["batches"].get(params.get("id"))
        return (200, copy.deepcopy(batch)) if batch else (404, {"error": "Run not found."})


HANDLERS = {"/api/family-emails/address": address, "/api/family-emails/queue": queue,
            "/api/family-emails/preset": preset,
            "/api/family-emails/preview": preview, "/api/family-emails/send-test": send_test,
            "/api/family-emails/run": run, "/api/family-emails/run-item": run_item}
GET_HANDLERS = {"/api/family-emails/text": text, "/api/family-emails/run": get_run,
                "/api/family-emails/state": status}
