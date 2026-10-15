#!/usr/bin/env python3
"""debts — the Assets and Debt section at the end of the cash-flow status
view.

Reads two hand-kept files of the same shape: one top-level bullet per
entry, indented `key: value` fields, and a balances list with one
`YYYY-MM: $amount` line per month, oldest first. `?` marks a value not
known yet; `(text)` after a value is a note the report prints.

  - assets.md (finance notes folder) — one `## assets` heading; savings
    and retirement accounts; note field only.
  - debts.md (finance notes folder) — three tier headings (## high,
    ## medium, ## low); rate, bank, payment, category and note fields.

The bank field is read and checked but never printed — it says where an
entry is held, which the report has no line for. The rate field is one
percent number, or a promotional rate in the form 'X% until Mon YYYY,
then Y%' (X for the months before Mon YYYY). The category field names
the budget category that holds this debt's payments and nothing else.

A debt's monthly payment is its payment field ('$N monthly'), else the
average monthly spending of its category over the report's average
months from the debt's first balance on, which the caller works out
(average). Its payoff month is the month of the last payment: the
newest balance counts as the balance at the start of its month, before
that month's payment, and each month adds that month's interest, then
takes the payment.

status_block(today, detailed, average) returns (text, checks) — the
section body and the check items kept apart, so the report files them
under its own headings (('', []) when neither file exists):

  - "Assets:" first, then one section per debt tier (High Interest
    Debt, Medium Interest Debt, Low Interest Debt). An entry is a
    name-and-balance row, the balance followed by its month and the
    percent it changed from the balance before it ('(Oct 2026, -3% from
    Jul 2026)'), then — detailed only — a history row, then one facts
    row. The history row shows the two most recent quarter months,
    newest first, each with the percent the current balance changed
    against it, like the Income and spending rows; a quarter month with
    no balance typed in yet prints '-'. An asset's facts row holds its
    notes alone, in both sizes. A debt's facts row starts with its rate;
    high and medium debts add the payoff month ('never paid off at
    ~$137 monthly' when the payment never clears the balance; '~' marks
    a payment read from the category average). Detailed adds the
    payment, what clears a promotional balance before the rate jumps,
    the interest at this month's rate/12 x balance, and the notes. An
    entry with no real balance yet gets a placeholder line with its note
    instead.
  - checks: parse problems and each entry whose newest balance line is
    an unfilled `?` — the cron's quarterly populate is what asks

plans(today, average) returns one dict per debt with a category field —
its monthly payment and payoff month — for the cash-flow estimate and
forecast.

validate() returns every problem in both files for the
validate_finance_files tool. populate_month(today), in January, April,
July and October only, inserts a `- YYYY-MM: ?` line into every balances
list missing the current month (both files) and returns the names
touched — the finance-uncategorized cron job (finance_jobs.py) calls it,
so a new quarter asks for its numbers by itself.

Cents inside, whole dollars printed, like cash_flow.py.
"""

import calendar
import math
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from data_root import root  # noqa: E402


def debts_file():
    return str(root("ASSISTANT_FINANCE_NOTES") / "debts.md")


def assets_file():
    return str(root("ASSISTANT_FINANCE_NOTES") / "assets.md")


TIERS = ["high", "medium", "low"]
ASSET_TIERS = ["assets"]
# the printed section subtitles; the file headings stay the bare tier names
TIER_TITLES = {"assets": "Assets", "high": "High Interest Debt",
               "medium": "Medium Interest Debt", "low": "Low Interest Debt"}
FIELD_KEYS = {"rate", "bank", "payment", "category", "note"}


def _label(idx):
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _mon(idx):
    return calendar.month_abbr[idx % 12 + 1]


def month_name(idx):
    """'Oct 2026' — how every report prints a month."""
    return f"{_mon(idx)} {idx // 12}"


def _money(cents):
    d = round(cents / 100)
    return f"-${-d:,.0f}" if d < 0 else f"${d:,.0f}"


def pct(cur, was):
    """'+24%' — the percent `cur` changed against `was` (nonzero), whole
    numbers; one decimal when that rounds to 0 but the change does not
    ('-0.4%'); a change under 0.05% is '+0%'."""
    p = (cur - was) / abs(was) * 100
    return f"{p:+.1f}%" if round(p) == 0 and round(p, 1) else f"{round(p):+d}%"


def _split_note(value):
    m = re.fullmatch(r"(.*?)\s*\((.*)\)", value)
    return (m.group(1), m.group(2)) if m else (value, "")


def _cents(value):
    m = re.fullmatch(r"\$(\d[\d,]*)(?:\.(\d{2}))?", value)
    if not m:
        return None
    return int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)


def _rates(fields):
    """[(until idx, percent), ..., (None, percent)] — the rate field's rates
    in order: one for a plain rate, two for 'X% until Mon YYYY, then Y%',
    the first applying to the months before Mon YYYY. [] when the field
    has no leading percent number."""
    m = re.match(r"(\d+(?:\.\d+)?)\s*%(?:\s+until\s+([A-Z][a-z]{2})\s+(\d{4}),"
                 r"\s*then\s+(\d+(?:\.\d+)?)\s*%)?", fields.get("rate", ""))
    if not m:
        return []
    if m.group(2) not in calendar.month_abbr[1:]:
        return [(None, float(m.group(1)))]
    until = int(m.group(3)) * 12 + list(calendar.month_abbr).index(m.group(2)) - 1
    return [(until, float(m.group(1))), (None, float(m.group(4)))]


def _rate_at(rates, idx):
    return next(pct for until, pct in rates if until is None or idx < until)


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
                value = body.split(":", 1)[1].strip()
                cur["fields"][key] = value
                problems += _field_problems(cur["name"], key, value)
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
    shared = {}
    for d in debts:
        if d["fields"].get("category"):
            shared.setdefault(d["fields"]["category"], []).append(d["name"])
    problems += [f"category {c!r} is on {' and '.join(n)} — one debt per "
                 "category" for c, n in shared.items() if len(n) > 1]
    return debts, problems


def _field_problems(name, key, value):
    """A rate or payment value the maths cannot read: it would fall back
    silently (a promotional rate read as permanent, a payment replaced by
    the category average), so it is a parse problem the report's Check
    section shows."""
    if value == "?":
        return []
    if key == "rate" and not _rates({"rate": value}):
        return [f"{name}: rate {value!r} has no leading percent number"]
    if key == "rate" and "until" in value.lower() \
            and len(_rates({"rate": value})) < 2:
        return [f"{name}: rate {value!r} is not 'X% until Mon YYYY, then Y%'"]
    if key == "payment" and _payment({"payment": value}) is None:
        return [f"{name}: payment {value!r} does not start with '$N monthly'"]
    return []


# ---------------------------------------------------------------- maths

def _derive(d, kind, idx, average):
    """latest and previous (idx, cents), and for a debt: rates, payment
    (cents, None when unknown), average (the payment came from the
    category average), interest at month idx's rate, payoff idx, never
    (the payment never clears the balance) and clears ((cents, until idx):
    the payment that clears the balance before a promotional rate ends)."""
    real = [(b["idx"], b["cents"]) for b in d["balances"]
            if b["cents"] is not None]
    v = {"latest": real[-1] if real else None,
         "previous": real[-2] if len(real) >= 2 else None,
         "first": real[0][0] if real else None,
         "rates": _rates(d["fields"]), "payment": _payment(d["fields"]),
         "average": False, "interest": None, "payoff": None, "never": False,
         "clears": None}
    if kind != "debt":
        return v
    category = d["fields"].get("category")
    paid = average(category, v["first"]) \
        if v["payment"] is None and category and average else None
    if paid and paid > 0:
        v["payment"], v["average"] = round(paid), True
    if v["latest"] is None or v["latest"][1] <= 0:
        return v
    start, bal = v["latest"]
    rates = v["rates"]
    if rates:
        v["interest"] = bal * _rate_at(rates, idx) / 1200
    if v["payment"]:
        v["payoff"] = _payoff(start, bal, v["payment"], rates)
        v["never"] = v["payoff"] is None
    if len(rates) == 2 and rates[0][0] > idx:
        n, r = rates[0][0] - start, rates[0][1] / 1200
        need = bal / n if r == 0 else bal * r / (1 - (1 + r) ** -n)
        v["clears"] = (need, rates[0][0])
    return v


def _payoff(idx, bal, pay, rates):
    """Month idx of the last payment, or None when the payment never clears
    the balance. bal is the balance at the start of month idx, before that
    month's payment. Promotional months step one at a time; the last rate
    uses the closed form."""
    for until, pct in rates[:-1]:
        while idx < until:
            bal = bal * (1 + pct / 1200) - pay
            if bal <= 0:
                return idx
            idx += 1
    r = (rates[-1][1] if rates else 0) / 1200
    if r == 0:
        return idx + math.ceil(bal / pay) - 1
    if pay <= bal * r:
        return None
    return idx + math.ceil(-math.log(1 - r * bal / pay) / math.log(1 + r)) - 1


# ---------------------------------------------------------------- report

def _hist_line(d, v, months):
    """'  - Jul $100,000 (+2%) · Apr -' — the balance at each of the given
    months (the two most recent quarter months), newest first, with the
    percent the current balance changed against it, like the history row
    on the Income and spending rows; the row always shows, so the report's shape does
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
        change = f" ({pct(cur, was)})" if was and i != cur_idx else ""
        parts.append(f"{_mon(i)} {_money(was)}{change}")
    return "  - " + " · ".join(parts)


def _entry_lines(d, v, detail, months, kind="debt", detailed=True):
    """The report rows for an entry with a known balance: `- name:
    balance (Mon YYYY, +N% from Mon YYYY)`, the month the balance was
    typed in and the percent it changed from the balance before it (left
    out when there is no earlier balance or it was zero), then — detailed
    only — the month history on its own row, then one row holding the
    facts, left out when there are none. A debt's facts start with its
    rate; detail (high and medium) adds the payoff; detailed adds the
    payment, what clears a promotional balance, the interest and the
    notes. An asset's facts are its notes, in both sizes."""
    idx, cents = v["latest"]
    when = month_name(idx)
    if v["previous"] and v["previous"][1]:
        p_idx, p_cents = v["previous"]
        when += f", {pct(cents, p_cents)} from {month_name(p_idx)}"
    lines = [f"- {d['name']}: {_money(cents)} ({when})"]
    if detailed:
        hist = _hist_line(d, v, months)
        if hist:
            lines.append(hist)
    facts = []
    if kind == "debt":
        rate = d["fields"].get("rate", "")
        if rate and rate != "?":
            facts.append(rate)
        pay = v["payment"] and \
            f"{'~' if v['average'] else ''}{_money(v['payment'])} monthly"
        if detail and detailed and pay:
            facts.append(f"pays {pay}")
        if detail and v["payoff"] is not None:
            facts.append(f"paid off ~{month_name(v['payoff'])}")
        elif detail and v["never"]:
            facts.append("never paid off at this pace" if detailed
                         else f"never paid off at {pay}")
        if detail and detailed and v["clears"]:
            need, until = v["clears"]
            facts.append(f"{_money(need)} monthly clears it by "
                         f"{month_name(until)}")
        if detail and detailed and v["interest"] is not None:
            facts.append(f"interest ~{_money(v['interest'])} monthly")
    if kind == "asset" or detailed:
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


def status_block(today=None, detailed=True, average=None):
    """(text, checks) — the section body and the check items; ('', [])
    when neither file exists and there is nothing to say. today pins the
    report date (tests, a past month's report) and picks the history
    months: the two most recent quarter months before or at it.
    average(category name, first balance idx) gives the average monthly
    spending cents of a category from that month on (None when unknown),
    where a debt with a category and no payment field reads its payment."""
    today = today or date.today()
    idx = today.year * 12 + today.month - 1
    q0 = idx - idx % 3
    months = (q0, q0 - 3)
    lines, checks = [], []
    for path, tiers, kind in ((assets_file(), ASSET_TIERS, "asset"),
                              (debts_file(), TIERS, "debt")):
        entries, problems = _parse(path, tiers)
        if problems is None:
            continue
        checks += problems
        # a past month's report reads the files as of that month: later
        # balance lines are cut away
        for e in entries:
            e["balances"] = [b for b in e["balances"] if b["idx"] <= idx]
        derived = {e["name"]: _derive(e, kind, idx, average)
                   for e in entries}
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
                              f"{month_name(e['balances'][-1]['idx'])} balance"
                              f" — using {month_name(v['latest'][0])}")
    if not lines and not checks:
        return "", []
    return "\n".join(lines).lstrip("\n"), checks


def plans(today, average):
    """One dict per debts.md debt with a category field, as of today: name,
    category, monthly (payment cents, None when unknown), average (read
    from the category average), payoff (idx of the last payment, None
    when unknown or never), done (the newest balance is $0 or less) and
    first (idx of the first real balance, None when none). [] when the
    file does not exist."""
    idx = today.year * 12 + today.month - 1
    out = []
    for e in _parse(debts_file(), TIERS)[0]:
        if not e["fields"].get("category"):
            continue
        e["balances"] = [b for b in e["balances"] if b["idx"] <= idx]
        v = _derive(e, "debt", idx, average)
        out.append({"name": e["name"], "category": e["fields"]["category"],
                    "monthly": v["payment"], "average": v["average"],
                    "payoff": v["payoff"], "first": v["first"],
                    "done": v["latest"] is not None and v["latest"][1] <= 0})
    return out


# ---------------------------------------------------------------- validate

def validate():
    """Every problem in assets.md and debts.md, [] when both are clean —
    the hand-kept-file half of the validate_finance_files tool."""
    out = []
    for path, tiers, label in ((assets_file(), ASSET_TIERS, "assets.md"),
                               (debts_file(), TIERS, "debts.md")):
        entries, problems = _parse(path, tiers)
        if problems is None:
            out.append(f"{label}: not found")
            continue
        out += [f"{label}: {p}" for p in problems]
        out += [f"{label}: {e['name']}: no balances list"
                for e in entries if not e["has_balances"]]
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
    return (_populate_file(assets_file(), ASSET_TIERS, idx)
            + _populate_file(debts_file(), TIERS, idx))


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
