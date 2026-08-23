#!/usr/bin/env python3
"""debts — the Debt section at the end of the cash-flow status view.

Reads vault/docs/finances/debts.md, a hand-kept file: three tier headings
(## high, ## medium, ## low), one top-level bullet per debt, indented
`key: value` fields (rate, lender, payment, note), and a balances list with
one `YYYY-MM: $amount` line per month, oldest first. `?` marks a value not
known yet; `(text)` after a value is a note the report prints.

status_block() returns the section text ('' without the file):

  - one line per tier: total of each debt's last known balance; high and
    medium also carry the change, worded "paid down" / "up", summed from
    each debt's own pace (its two newest balances, spread over the months
    between them — so a quarterly entry still reads as a monthly figure)
  - one line per high and medium debt: balance, pace, payoff month, and
    interest at rate/12 x balance. Payoff comes from amortization when the
    rate and a '$N monthly' payment are set, else from the pace.
  - one line per low debt: balance only — payoff time matters least there
  - Check: parse problems, debts with no balance at all, and balances older
    than the file's newest month line (a `?` line counts, so the cron's
    populate makes a new month flag every unfilled debt)

validate() returns every problem in the file for the validate_finance_files
tool. populate_month(today) inserts a `- YYYY-MM: ?` line into every
balances list missing the current month and returns the names touched — the
finance-daily cron calls it, so a new month asks for its numbers by itself.

Cents inside, whole dollars printed, like cash_flow.py.
"""

import math
import os
import re

DEBTS_FILE = os.path.expanduser("~/Iris/vault/docs/finances/debts.md")
TIERS = ["high", "medium", "low"]
FIELD_KEYS = {"rate", "lender", "payment", "note"}


def _label(idx):
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


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

def _parse(path=None):
    """([debt], [problem]); ([], None) when the file does not exist. A debt
    is {name, tier, fields, has_balances, balances: [{idx, cents, note}]},
    cents None for `?`. Bad lines land in problems and are skipped, so one
    typo never hides the rest of the file."""
    path = path or DEBTS_FILE   # read at call time; tests repoint the global
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
            tier = name if name in TIERS else None
            if tier is None:
                problems.append(f"heading '## {h.group(1)}' is not a tier "
                                "(high / medium / low)")
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

def _derive(d):
    """latest (idx, cents), monthly pace (positive = paying down), rate,
    payment, interest and payoff idx for one debt; None where the file gives
    too little."""
    real = [(b["idx"], b["cents"]) for b in d["balances"]
            if b["cents"] is not None]
    v = {"latest": real[-1] if real else None, "pace": None,
         "rate": _rate(d["fields"]), "payment": _payment(d["fields"]),
         "interest": None}
    if len(real) >= 2:
        (i0, c0), (i1, c1) = real[-2], real[-1]
        v["pace"] = (c0 - c1) / (i1 - i0)
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


def _pace_text(pace):
    d = round(pace / 100)
    if d == 0:
        return "no change"
    return f"paid down {_money(pace)} monthly" if d > 0 \
        else f"up {_money(-pace)} monthly"


# ---------------------------------------------------------------- report

def _debt_lines(d, v, detail):
    """The report lines for one debt: the balance line (with pace, payoff
    and interest when detail is on), then any notes indented under it."""
    name = d["name"]
    rate = d["fields"].get("rate", "")
    if rate and rate != "?":
        name += f" ({rate})"
    parts = [f"- {name}: {_money(v['latest'][1])}"]
    if detail:
        if v["pace"] is not None:
            parts.append(_pace_text(v["pace"]))
        if v["payoff"] is not None:
            parts.append(f"paid off ~{_label(v['payoff'])}")
        if v["interest"] is not None:
            parts.append(f"interest ~{_money(v['interest'])} monthly")
    out = [" | ".join(parts)]
    if d["fields"].get("note"):
        out.append(f"    {d['fields']['note']}")
    latest_note = next((b["note"] for b in reversed(d["balances"])
                        if b["cents"] is not None), "")
    if latest_note:
        out.append(f"    {latest_note}")
    return out


def status_block():
    """The Debt section, or '' when there is no debts file and nothing to
    say. Broken entries surface in the Check list instead of vanishing."""
    debts, problems = _parse()
    if problems is None:
        return ""
    checks = list(problems)
    derived = {d["name"]: _derive(d) for d in debts}
    newest_any = max((b["idx"] for d in debts for b in d["balances"]),
                     default=None)
    newest_real = max((v["latest"][0] for v in derived.values()
                       if v["latest"]), default=None)

    lines = [f"Debt — {_label(newest_real)}:"
             if newest_real is not None else "Debt:"]
    total, total_missing, any_valued = 0, [], False
    for tier in TIERS:
        members = [d for d in debts if d["tier"] == tier]
        if not members:
            continue
        valued = [d for d in members if derived[d["name"]]["latest"]]
        missing = [d["name"] for d in members
                   if not derived[d["name"]]["latest"]]
        total_missing += missing
        if not valued:
            lines.append(f"- {tier}: no balance yet — {', '.join(missing)}")
            continue
        any_valued = True
        tier_total = sum(derived[d["name"]]["latest"][1] for d in valued)
        total += tier_total
        row = f"- {tier}: {_money(tier_total)}"
        if missing:
            row += f" (without {', '.join(missing)})"
        if tier != "low":
            paces = [derived[d["name"]]["pace"] for d in valued
                     if derived[d["name"]]["pace"] is not None]
            if paces:
                row += f" | {_pace_text(sum(paces))}"
        lines.append(row)
    if any_valued:
        row = f"- total: {_money(total)}"
        if total_missing:
            row += f" (without {', '.join(total_missing)})"
        lines.append(row)

    hm = [d for d in debts if d["tier"] != "low" and derived[d["name"]]["latest"]]
    if hm:
        lines += ["", "High and medium:"]
        for d in hm:
            lines += _debt_lines(d, derived[d["name"]], detail=True)
    low = [d for d in debts if d["tier"] == "low" and derived[d["name"]]["latest"]]
    if low:
        lines += ["", "Low:"]
        for d in low:
            lines += _debt_lines(d, derived[d["name"]], detail=False)

    for d in debts:
        v = derived[d["name"]]
        if v["latest"] is None:
            note = next((b["note"] for b in reversed(d["balances"])
                         if b["note"]), "") or d["fields"].get("note", "")
            checks.append(f"{d['name']}: no balance yet"
                          + (f" — {note}" if note else ""))
        elif newest_any is not None and v["latest"][0] < newest_any:
            checks.append(f"{d['name']}: no {_label(newest_any)} balance — "
                          f"using {_label(v['latest'][0])}")
    if checks:
        lines += ["", "Check:"] + [f"- {c}" for c in checks]
    if not debts and not checks:
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------- validate

def validate():
    """Every problem in debts.md, [] when clean — the debt half of the
    validate_finance_files tool."""
    debts, problems = _parse()
    if problems is None:
        return ["debts.md: not found"]
    out = [f"debts.md: {p}" for p in problems]
    for d in debts:
        f = d["fields"]
        if not d["has_balances"]:
            out.append(f"debts.md: {d['name']}: no balances list")
        if f.get("rate") and f["rate"] != "?" and _rate(f) is None:
            out.append(f"debts.md: {d['name']}: rate {f['rate']!r} has no "
                       "leading percent number")
        if f.get("payment") and f["payment"] != "?" and _payment(f) is None:
            out.append(f"debts.md: {d['name']}: payment {f['payment']!r} "
                       "does not start with '$N monthly'")
    return out


# ---------------------------------------------------------------- populate

def populate_month(today):
    """Insert a `- YYYY-MM: ?` line for today's month into every balances
    list that lacks one, after that list's last line; returns the debt names
    touched, [] when nothing was missing or there is no file. The only write
    this module makes, and only added lines — the rest of the file is kept
    byte for byte."""
    idx = today.year * 12 + today.month - 1
    try:
        with open(DEBTS_FILE, encoding="utf-8") as f:
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
            tier = name if name in TIERS else None
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
    with open(DEBTS_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return [name for _, name in sorted(inserts)]
