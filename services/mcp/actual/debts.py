#!/usr/bin/env python3
"""debts — the Assets and Debt section at the end of the cash-flow status
view.

Reads two hand-kept files of the same shape: one top-level bullet per
entry, indented `key: value` fields, and a balances list with one
`YYYY-MM: $amount` line per month, oldest first. `?` marks a value not
known yet; `(text)` after a value is a note the report prints.

  - vault/docs/finances/assets.md — one `## assets` heading; home values
    and retirement accounts; note field only.
  - vault/docs/finances/debts.md — three tier headings (## high,
    ## medium, ## low); rate, bank, payment and note fields.

The bank field is read and checked but never printed — it says where an
entry is held, which the report has no line for.

status_block(today, detailed) returns (text, checks) — the section body
and the check items kept apart, so the report files them under its own
headings (('', []) when neither file exists):

  - "Assets:" first, then one section per debt tier (High Interest
    Debt, Medium Interest Debt, Low Interest Debt). An entry is a
    name-and-balance row; detailed adds a history row and one row
    holding every remaining fact. The history row shows the two most
    recent quarter months, newest first, each with the percent the
    current balance changed against it, like the Actual rows; a quarter
    month with no balance typed in yet prints '-'. An asset carries its
    monthly change (the two newest balances spread over the months
    between them — so a quarterly entry still reads as a monthly
    figure) in both sizes; only its history row needs detailed. A
    detailed debt carries the same change math (worded paid down / paid
    off), plus payoff month and interest at rate/12 x balance for the
    high and medium tiers; payoff comes from amortization when the rate
    and a '$N monthly' payment are set, else from the pace. Low debts
    get balances only. A regular-size debt is the balance row alone —
    the rate rides in its name. An entry with no real balance yet gets
    a placeholder line with its note instead.
  - checks: parse problems and each entry whose newest balance line is
    an unfilled `?` — the cron's quarterly populate is what asks

validate() returns every problem in both files for the
validate_finance_files tool. populate_month(today), in January, April,
July and October only, inserts a `- YYYY-MM: ?` line into every balances
list missing the current month (both files) and returns the names
touched — the finance-daily cron calls it, so a new quarter asks for its
numbers by itself.

Cents inside, whole dollars printed, like cash_flow.py.
"""

import calendar
import math
import os
import re
from datetime import date

DEBTS_FILE = os.path.expanduser("~/Iris/vault/docs/finances/debts.md")
ASSETS_FILE = os.path.expanduser("~/Iris/vault/docs/finances/assets.md")
TIERS = ["high", "medium", "low"]
ASSET_TIERS = ["assets"]
# the printed section subtitles; the file headings stay the bare tier names
TIER_TITLES = {"assets": "Assets", "high": "High Interest Debt",
               "medium": "Medium Interest Debt", "low": "Low Interest Debt"}
FIELD_KEYS = {"rate", "bank", "payment", "note"}


def _label(idx):
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _mon(idx):
    return calendar.month_abbr[idx % 12 + 1]


def _money(cents):
    d = round(cents / 100)
    return f"-${-d:,.0f}" if d < 0 else f"${d:,.0f}"


def _split_note(value):
    m = re.fullmatch(r"(.*?)\s*\((.*)\)", value)
    return (m.group(1), m.group(2)) if m else (value, "")


def _cents(value):
    m = re.fullmatch(r"\$(\d[\d,]*)(?:\.(\d{2}))?", value)
    if not m:
        return None
    return int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)


def _rate(fields):
    m = re.match(r"(\d+(?:\.\d+)?)\s*%", fields.get("rate", ""))
    return float(m.group(1)) if m else None


def _payment(fields):
    m = re.match(r"\$(\d[\d,]*)(?:\.(\d{2}))?\s+monthly",
                 fields.get("payment", ""))
    if not m:
        return None
    return int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)


# ---------------------------------------------------------------- parse

def _parse(path, tiers):
    """([entry], [problem]); ([], None) when the file does not exist. An
    entry is {name, tier, fields, has_balances, balances: [{idx, cents,
    note}]}, cents None for `?`. Bad lines land in problems and are
    skipped, so one typo never hides the rest of the file."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return [], None
    debts, problems, seen = [], [], set()
    tier, cur, in_balances = None, None, False
    for line in text.splitlines():
        h = re.match(r"^##\s+(.*\S)\s*$", line)
        if h:
            name = h.group(1).strip().casefold()
            tier = name if name in tiers else None
            if tier is None:
                problems.append(f"heading '## {h.group(1)}' is not a tier "
                                f"({' / '.join(tiers)})")
            cur, in_balances = None, False
            continue
        m = re.match(r"^( *)-\s+(.*\S)\s*$", line)
        if not m or tier is None:
            continue
        indent, body = len(m.group(1)), m.group(2)
        if indent == 0:
            if body in seen:
                problems.append(f"debt {body!r} appears twice")
            seen.add(body)
            cur = {"name": body, "tier": tier, "fields": {},
                   "has_balances": False, "balances": [], "last_mon": None}
            debts.append(cur)
            in_balances = False
        elif cur is None:
            problems.append(f"{tier}: indented line before any debt: {body!r}")
        elif indent < 4:
            if ":" not in body:
                problems.append(f"{cur['name']}: not a 'key: value' line: "
                                f"{body!r}")
                continue
            key = body.split(":", 1)[0].strip().casefold()
            in_balances = key == "balances"
            if in_balances:
                cur["has_balances"] = True
            elif key not in FIELD_KEYS:
                problems.append(f"{cur['name']}: unknown field {key!r}")
            else:
                cur["fields"][key] = body.split(":", 1)[1].strip()
        elif in_balances:
            if ":" not in body:
                problems.append(f"{cur['name']}: not a 'YYYY-MM: value' "
                                f"line: {body!r}")
                continue
            mon, value = (s.strip() for s in body.split(":", 1))
            ym = re.fullmatch(r"(\d{4})-(\d{2})", mon)
            if not ym or not 1 <= int(ym.group(2)) <= 12:
                problems.append(f"{cur['name']}: {mon!r} is not a YYYY-MM "
                                "month")
                continue
            idx = int(ym.group(1)) * 12 + int(ym.group(2)) - 1
            if cur["last_mon"] is not None and idx <= cur["last_mon"]:
                problems.append(f"{cur['name']}: {mon} is not after "
                                f"{_label(cur['last_mon'])} — keep months "
                                "oldest first")
                continue
            cur["last_mon"] = idx
            value, note = _split_note(value)
            cents = None
            if value != "?":
                cents = _cents(value)
                if cents is None:
                    problems.append(f"{cur['name']}: {mon}: not a $ amount "
                                    f"or '?': {value!r}")
                    continue
            cur["balances"].append({"idx": idx, "cents": cents, "note": note})
        else:
            problems.append(f"{cur['name']}: balance-style line outside a "
                            f"balances list: {body!r}")
    return debts, problems


# ---------------------------------------------------------------- maths

def _derive(d, kind="debt"):
    """latest (idx, cents), monthly pace (debt: positive = paying down;
    asset: positive = growing), rate, payment, interest and payoff idx for
    one entry; None where the file gives too little. Assets get no rate
    math: payoff and interest stay None."""
    real = [(b["idx"], b["cents"]) for b in d["balances"]
            if b["cents"] is not None]
    v = {"latest": real[-1] if real else None, "pace": None,
         "rate": _rate(d["fields"]), "payment": _payment(d["fields"]),
         "interest": None, "payoff": None}
    if len(real) >= 2:
        (i0, c0), (i1, c1) = real[-2], real[-1]
        v["pace"] = (c0 - c1) / (i1 - i0) if kind == "debt" \
            else (c1 - c0) / (i1 - i0)
    if kind == "debt":
        if v["latest"] and v["rate"] is not None:
            v["interest"] = v["latest"][1] * v["rate"] / 1200
        v["payoff"] = _payoff(v)
    return v


def _payoff(v):
    """Month idx the balance reaches zero, or None. Amortization when a
    monthly payment is set (a payment not covering the interest never pays
    off); otherwise the pace, when it points down."""
    if v["latest"] is None:
        return None
    idx, bal = v["latest"]
    if bal <= 0:
        return None
    p = v["payment"]
    if p:
        r = (v["rate"] or 0) / 1200
        if r == 0:
            return idx + math.ceil(bal / p)
        if p <= bal * r:
            return None
        return idx + math.ceil(-math.log(1 - r * bal / p) / math.log(1 + r))
    if v["pace"] and v["pace"] > 0:
        return idx + math.ceil(bal / v["pace"])
    return None


def _pace_text(pace, kind="debt"):
    d = round(pace / 100)
    if d == 0:
        return "no change"
    if kind == "asset":
        return f"up {_money(pace)} monthly" if d > 0 \
            else f"down {_money(-pace)} monthly"
    return f"paid down {_money(pace)} monthly" if d > 0 \
        else f"up {_money(-pace)} monthly"


# ---------------------------------------------------------------- report

def _hist_line(d, v, months):
    """'  - Jul $100,000 (+2%) · Apr -' — the balance at each of the given
    months (the two most recent quarter months), newest first, with the
    percent the current balance changed against it, like the history row
    on the Actual rows; the row always shows, so the report's shape does
    not depend on how many balances are typed in. A month with no balance
    typed in yet prints '-'. The percent is left out for a zero balance
    and for the month the current balance itself came from. None when
    there is no real balance at all (the placeholder line says it)."""
    by_idx = {b["idx"]: b["cents"] for b in d["balances"]
              if b["cents"] is not None}
    if not by_idx:
        return None
    cur_idx, cur = v["latest"]
    parts = []
    for i in months:
        if i not in by_idx:
            parts.append(f"{_mon(i)} -")
            continue
        was = by_idx[i]
        pct = (f" ({round((cur - was) / abs(was) * 100):+d}%)"
               if was and i != cur_idx else "")
        parts.append(f"{_mon(i)} {_money(was)}{pct}")
    return "  - " + " · ".join(parts)


def _entry_lines(d, v, detail, months, kind="debt", detailed=True):
    """The report rows for an entry with a known balance: `- name (rate):
    balance`, then — detailed only — the month history on its own row, then
    one row holding every extra fact. The rate shows for debts only. The
    facts are the pace for every entry, payoff and interest for debts when
    detail is on (high and medium), then any notes; that row is left out
    when there are none. A regular-size debt is the balance row alone; a
    regular-size asset keeps its facts row. The change against an earlier
    month is not a fact here — the history row's percents say it."""
    name = d["name"]
    rate = d["fields"].get("rate", "")
    if kind == "debt" and rate and rate != "?":
        name += f" ({rate})"
    lines = [f"- {name}: {_money(v['latest'][1])}"]
    if detailed:
        hist = _hist_line(d, v, months)
        if hist:
            lines.append(hist)
    if kind == "debt" and not detailed:
        return lines
    facts = []
    if detail:
        if v["pace"] is not None:
            facts.append(_pace_text(v["pace"], kind))
        if v["payoff"] is not None:
            facts.append(f"paid off ~{_label(v['payoff'])}")
        if v["interest"] is not None:
            facts.append(f"interest ~{_money(v['interest'])} monthly")
    if d["fields"].get("note"):
        facts.append(d["fields"]["note"])
    latest_note = next((b["note"] for b in reversed(d["balances"])
                        if b["cents"] is not None), "")
    if latest_note:
        facts.append(latest_note)
    if facts:
        lines.append("  - " + " · ".join(facts))
    return lines


def _placeholder_line(d):
    """`- name: no balance yet — note` for an entry whose balances are all
    `?`; the note is the newest balance note, else the field note."""
    note = next((b["note"] for b in reversed(d["balances"])
                 if b["note"]), "") or d["fields"].get("note", "")
    return (f"- {d['name']}: no balance yet"
            + (f" — {note}" if note else ""))


def status_block(today=None, detailed=True):
    """(text, checks) — the Assets and Debt section body (assets first,
    then one section per debt tier) and the check items, so the report
    files them under its own headings. ('', []) when neither file exists
    and there is nothing to say. Broken entries surface in checks instead
    of vanishing. today pins the report date (tests, a past month's
    report); balance lines after it are left out,
    and the history months are the two most recent quarter months
    before or at it. detailed=False is the regular report size: no
    history rows, and debts show only balance and rate."""
    today = today or date.today()
    idx = today.year * 12 + today.month - 1
    q0 = idx - idx % 3
    months = (q0, q0 - 3)
    lines, checks = [], []
    for path, tiers, kind in ((ASSETS_FILE, ASSET_TIERS, "asset"),
                              (DEBTS_FILE, TIERS, "debt")):
        entries, problems = _parse(path, tiers)
        if problems is None:
            continue
        checks += problems
        # a past month's report reads the files as of that month: later
        # balance lines are cut away
        for e in entries:
            e["balances"] = [b for b in e["balances"] if b["idx"] <= idx]
        derived = {e["name"]: _derive(e, kind) for e in entries}
        for tier in tiers:
            members = [e for e in entries if e["tier"] == tier]
            if not members:
                continue
            lines += ["", f"{TIER_TITLES[tier]}:"]
            for e in members:
                v = derived[e["name"]]
                if v["latest"] is None:
                    lines.append(_placeholder_line(e))
                else:
                    lines += _entry_lines(e, v, detail=tier != "low",
                                          months=months, kind=kind,
                                          detailed=detailed)
        # the ask: an entry whose newest balance line is an unfilled `?`
        # (the cron's quarterly populate put it there); a real newest line
        # is never flagged, however old — the next quarter's populate asks
        # again
        for e in entries:
            v = derived[e["name"]]
            if v["latest"] is not None and e["balances"] \
                    and e["balances"][-1]["cents"] is None:
                checks.append(f"{e['name']}: no "
                              f"{_label(e['balances'][-1]['idx'])} balance"
                              f" — using {_label(v['latest'][0])}")
    if not lines and not checks:
        return "", []
    return "\n".join(lines).lstrip("\n"), checks


# ---------------------------------------------------------------- validate

def validate():
    """Every problem in assets.md and debts.md, [] when both are clean —
    the hand-kept-file half of the validate_finance_files tool. The
    rate/payment field checks apply to debts only; assets carry none."""
    out = []
    for path, tiers, kind, label in (
            (ASSETS_FILE, ASSET_TIERS, "asset", "assets.md"),
            (DEBTS_FILE, TIERS, "debt", "debts.md")):
        entries, problems = _parse(path, tiers)
        if problems is None:
            out.append(f"{label}: not found")
            continue
        out += [f"{label}: {p}" for p in problems]
        for e in entries:
            f = e["fields"]
            if not e["has_balances"]:
                out.append(f"{label}: {e['name']}: no balances list")
            if kind != "debt":
                continue
            if f.get("rate") and f["rate"] != "?" and _rate(f) is None:
                out.append(f"{label}: {e['name']}: rate {f['rate']!r} has "
                           "no leading percent number")
            if f.get("payment") and f["payment"] != "?" \
                    and _payment(f) is None:
                out.append(f"{label}: {e['name']}: payment {f['payment']!r}"
                           " does not start with '$N monthly'")
    return out


# ---------------------------------------------------------------- populate

def populate_month(today):
    """Insert a `- YYYY-MM: ?` line for today's month into every balances
    list that lacks one, in assets.md and debts.md; returns the entry
    names touched (assets first), [] when nothing was missing or neither
    file exists. Quarter months only (January, April, July, October) —
    the ask comes every 3 months, so other months add nothing. The only
    write this module makes, and only added lines — the rest of each file
    is kept byte for byte."""
    idx = today.year * 12 + today.month - 1
    if idx % 3 != 0:
        return []
    return (_populate_file(ASSETS_FILE, ASSET_TIERS, idx)
            + _populate_file(DEBTS_FILE, TIERS, idx))


def _populate_file(path, tiers, idx):
    """The one-file half of populate_month: the names whose balances list
    got the month-idx `?` line, in file order."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return []
    tier, cur = None, None
    found = []          # [{name, insert_at, months}]
    for i, raw in enumerate(lines):
        line = raw.rstrip("\n")
        h = re.match(r"^##\s+(.*\S)\s*$", line)
        if h:
            name = h.group(1).strip().casefold()
            tier = name if name in tiers else None
            cur = None
            continue
        m = re.match(r"^( *)-\s+(.*\S)\s*$", line)
        if not m or tier is None:
            continue
        indent, body = len(m.group(1)), m.group(2)
        if indent == 0:
            cur = {"name": body, "insert_at": None, "months": set()}
            found.append(cur)
        elif cur is not None and indent < 4:
            if body.split(":", 1)[0].strip().casefold() == "balances":
                cur["insert_at"] = i + 1
        elif cur is not None and cur["insert_at"] is not None:
            ym = re.match(r"(\d{4})-(\d{2}):", body)
            if ym:
                cur["months"].add(int(ym.group(1)) * 12
                                  + int(ym.group(2)) - 1)
            cur["insert_at"] = i + 1
    inserts = [(d["insert_at"], d["name"]) for d in found
               if d["insert_at"] is not None and idx not in d["months"]]
    if not inserts:
        return []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for at, _ in sorted(inserts, reverse=True):
        lines.insert(at, f"    - {_label(idx)}: ?\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return [name for _, name in sorted(inserts)]
