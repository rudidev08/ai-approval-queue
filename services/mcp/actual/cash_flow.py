#!/usr/bin/env python3
"""cash_flow — money in, money out, vs the monthly average (read-only).

Three plain-text views over the api-cache SQLite copy:

  build_report()                current-month status: so far, and once one
                                complete month exists, projection and streak
  build_report(month="2026-09") that month vs its average
  build_report(group="Variable")   adds that group's per-category rows
  history_report(6)             one line per complete month, newest first

The average is the plain mean per category over up to WINDOW complete months,
never earlier than FIRST_MONTH (the first full import month; the window grows
1 -> WINDOW on its own). Group numbers are sums of their categories.

Income is not averaged that way. Each payee inside the INCOME_SOURCE_CATEGORY
income category is one source, worth its standing payment times its payments
a year, over 12 — a year holds a whole number of payments and a month does
not, so monthly sums mis-state every rhythm that does not divide into one.
The standing payment is the latest one, judged against the two before it
(INCOME_TOLERANCE apart counts as different): a lone spike or dip is an
outlier and the previous payment stands in; two payments agreeing at a new
level are a confirmed change and count; the source's line says which. A
source's rhythm comes from vault/docs/finances/income-sources.md when set
there, otherwise from its own gaps once it has DETECT_MIN payments, and a
source with neither gets no figure. That file also carries a display name
and, for a payee covering more than one income, how many — such a payee uses
the mean of its last whole round of payments, since interleaved sizes make
consecutive comparison meaningless.
An entry with an amount is its own source: it claims every deposit of
exactly that value, whatever the payee, and its name is the display name;
old values stay listed so past deposits keep their source.
A source silent for STOPPED_INTERVALS of its own gaps leaves the total —
a payment's value is good for one cycle plus slack for a late arrival. The
status view prints the estimate and one line per source; a past month shows
the income that actually arrived instead, so no estimate appears there.

The projection counts that income estimate, NO_FORECAST_GROUPS at what was
actually spent (deliberate extra payments, made only when money is there, so
no future figure exists for them), and FIXED_GROUPS at their full monthly
figure (lumpy flows take no pace judgment); every other group runs at pace:
actual so far + average x remaining share of the month, even spread assumed.
It rests on the average, so until one complete month exists the status view
shows what has happened so far and nothing about month end. ledger_month()
returns one complete month's cents for the monthly-balance file that
finance_scan.py writes.
The status view groups under two labels: Estimated — the projection and the
income estimate — and Actual — so far, tracked categories, how long the
money lasts, debt.
The Actual side says how long the money lasts: CHECKING_ACCOUNT's
balance (every transaction in the account, transfers and starting balance
included) divided by the average monthly spending, income not counted —
until one complete month exists the balance shows alone.
The status view ends with the Debt section from debts.py — balances,
change and payoff math out of vault/docs/finances/debts.md — when that
file exists.
A green month is one whose income covered spending. Excluded everywhere: transfers,
off-budget accounts, starting-balance rows, EXCLUDED_GROUPS. Uncategorized
money always gets its own rows, split by sign; a deleted category's
transactions count as uncategorized. Whole dollars, minus before the dollar
sign; rows round independently of totals, so a $1 drift between them is
possible and left alone.

CLI: cash_flow.py [YYYY-MM] [--group NAME] | --history N
"""

import calendar
import glob
import os
import re
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import debts  # noqa: E402

FIRST_MONTH = "2026-08"    # first full import month; earlier months never count
WINDOW = 12                # complete months in the average, at most
EXCLUDED_GROUPS = ["Ignored"]   # group names left out of every number
FIXED_GROUPS = []          # groups projected at average, no pace judgment
NO_FORECAST_GROUPS = ["One-off"]   # spending counted as it happens, never projected
TRACKED_CATEGORIES = []    # category names always shown with their own row
CHECKING_ACCOUNT = "Acme Checking"     # account the runs-out section reads
OUTLIERS_N = 3
HISTORY_CAP = 24

API_CACHE = os.path.expanduser("~/Iris/services/actual/api-cache")

# --- income sources ---
# One payee inside the INCOME_SOURCE_CATEGORY category is one source — except
# deposits claimed by an entry's amount field, which form that entry's own
# source. A source's monthly value is its standing payment (_pick_payment:
# outliers skipped, confirmed changes adopted) times its payments a year,
# over 12: a year holds a whole number of payments and a month does not, so
# counting months mis-states every rhythm that does not divide into one (26
# paychecks a year is 2.167 a month, never 2 or 3). A payee carrying several
# incomes uses the mean of its last whole round instead.
SOURCES_FILE = os.path.expanduser(
    "~/Iris/vault/docs/finances/income-sources.md")
INCOME_SOURCE_CATEGORY = "Recurring"   # the other income category is one-off
INCOME_TOLERANCE = 0.10  # payments this fraction apart count as different
DETECT_MIN = 3           # payments needed before the dates may name a frequency
# silent for this many average gaps -> out of the total: a payment's value is
# good for one cycle, the extra quarter is slack for a late arrival
STOPPED_INTERVALS = 1.25
PER_YEAR = {"weekly": 52, "every 2 weeks": 26, "twice a month": 24,
            "monthly": 12, "quarterly": 4, "every 6 months": 2, "annual": 1}
# median gap in days -> frequency name, ranges inclusive. Fortnightly and
# twice-monthly overlap here (14 days against 15.2) and are split by the
# day-of-month test in _detect instead.
GAP_BUCKETS = [((5, 9), "weekly"), ((10, 20), "every 2 weeks"),
               ((21, 45), "monthly"), ((70, 115), "quarterly"),
               ((150, 215), "every 6 months"), ((300, 430), "annual")]
# one amount value: $ and cents optional, no commas inside a value. No zero
# and no leading zero, so the '000' left by writing '$2,000' is rejected
# loudly instead of parsing as $0.
AMOUNT_RE = r"\$?[1-9]\d*(\.\d{1,2})?"


# ---------------------------------------------------------------- months

def _idx(year, month):
    return year * 12 + month - 1


def _label(idx):
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _parse_month(text):
    m = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"month must be YYYY-MM, got {text!r}")
    return _idx(int(m.group(1)), int(m.group(2)))


def _bounds(idx):
    """(first, last) YYYYMMDD ints of the month; day 31 is safe for every
    month, no transaction date exceeds the real month length."""
    year, month = divmod(idx, 12)
    month += 1
    return year * 10000 + month * 100 + 1, year * 10000 + month * 100 + 31


def _days_in(idx):
    return calendar.monthrange(idx // 12, idx % 12 + 1)[1]


def _to_date(yyyymmdd):
    s = str(yyyymmdd)
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


# ---------------------------------------------------------------- dollars

def _money(cents):
    d = round(cents / 100)
    return f"-${-d:,.0f}" if d < 0 else f"${d:,.0f}"


def _signed(cents):
    d = round(cents / 100)
    if d < 0:
        return f"-${-d:,.0f}"
    return f"+${d:,.0f}" if d > 0 else "$0"


def _join_and(names):
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------- reading the copy

def _db():
    paths = glob.glob(os.path.join(API_CACHE, "*", "db.sqlite"))
    if len(paths) != 1:
        raise RuntimeError(f"expected one budget copy under {API_CACHE}, found {len(paths)}")
    conn = sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _structure(conn):
    """(cats, groups): cats = {category id: {name, grp}}, groups = ordered
    [{name, is_income}] in Actual's own order — both without EXCLUDED_GROUPS,
    hidden ones kept (real money)."""
    cats = {}
    for r in conn.execute(
            "SELECT c.id, c.name, g.name AS grp FROM categories c "
            "JOIN category_groups g ON g.id = c.cat_group "
            "WHERE c.tombstone = 0 AND g.tombstone = 0 "
            "ORDER BY g.sort_order, g.name, c.name COLLATE NOCASE"):
        if r["grp"] not in EXCLUDED_GROUPS:
            cats[r["id"]] = {"name": r["name"], "grp": r["grp"]}
    groups = [{"name": r["name"], "is_income": bool(r["is_income"])}
              for r in conn.execute(
                  "SELECT name, is_income FROM category_groups "
                  "WHERE tombstone = 0 ORDER BY sort_order, name")
              if r["name"] not in EXCLUDED_GROUPS]
    return cats, groups


def _fetch(conn, lo_idx, hi_idx):
    """Per-month per-category sums, cents: [{idx, cat, inc, spend, n}], cat
    None = uncategorized. Transfers, off-budget accounts and starting-balance
    rows are already gone here; split children count, split parents never."""
    rows = conn.execute(
        "SELECT t.date/100 AS ym, t.category AS cat, "
        "SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS inc, "
        "SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END) AS spend, "
        "COUNT(*) AS n "
        "FROM v_transactions t JOIN accounts a ON a.id = t.account "
        "WHERE t.is_parent = 0 AND t.transfer_id IS NULL "
        "AND t.starting_balance_flag = 0 AND a.offbudget = 0 "
        "AND t.date >= ? AND t.date <= ? "
        "GROUP BY ym, t.category",
        (_bounds(lo_idx)[0], _bounds(hi_idx)[1])).fetchall()
    out = []
    for r in rows:
        year, month = divmod(r["ym"], 100)
        out.append({"idx": _idx(year, month), "cat": r["cat"], "inc": r["inc"],
                    "spend": r["spend"], "n": r["n"]})
    return out


def _fetch_income(conn, lo_idx):
    """{payee: [(date, cents)]} for INCOME_SOURCE_CATEGORY payments from
    lo_idx's first day on, oldest first — the individual payments the source
    maths needs, where _fetch only keeps monthly sums. Same exclusions as
    _fetch; reading no earlier than lo_idx keeps a partial first import out of
    the gap measurements, where a missing payment would read as a longer
    rhythm."""
    rows = conn.execute(
        "SELECT COALESCE(p.name, '') AS payee, t.date, t.amount "
        "FROM v_transactions t "
        "JOIN accounts a ON a.id = t.account "
        "LEFT JOIN v_payees p ON p.id = t.payee "
        "JOIN categories c ON c.id = t.category "
        "JOIN category_groups g ON g.id = c.cat_group "
        "WHERE t.is_parent = 0 AND t.transfer_id IS NULL "
        "AND t.starting_balance_flag = 0 AND a.offbudget = 0 "
        "AND t.amount > 0 AND c.tombstone = 0 AND g.tombstone = 0 "
        "AND g.is_income = 1 AND c.name = ? AND t.date >= ? "
        "ORDER BY t.date, t.id",
        (INCOME_SOURCE_CATEGORY, _bounds(lo_idx)[0])).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["payee"] or "(no payee)", []).append(
            (_to_date(r["date"]), r["amount"]))
    return out


def _checking_balance(conn):
    """CHECKING_ACCOUNT's balance in cents — every transaction in the
    account counts, transfers and starting balance included, since the real
    balance holds them all. None when no single open account has that
    name."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE name = ? AND tombstone = 0 "
        "AND closed = 0", (CHECKING_ACCOUNT,))]
    if len(ids) != 1:
        return None
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM v_transactions "
        "WHERE account = ? AND is_parent = 0", (ids[0],)).fetchone()[0]


# ---------------------------------------------------------------- compute (pure)

def _month_data(rows, idx, cats, excluded_ids=frozenset()):
    """One month: {cats: {id: net cents}, uncat_inc, uncat_spend (positive),
    n_uncat}. excluded_ids (EXCLUDED_GROUPS categories) drop entirely; a
    category id in neither cats nor excluded_ids is a deleted category and
    counts as uncategorized, like the other servers display it."""
    d = {"cats": {}, "uncat_inc": 0, "uncat_spend": 0, "n_uncat": 0}
    for r in rows:
        if r["idx"] != idx or r["cat"] in excluded_ids:
            continue
        c = cats.get(r["cat"])
        if c is None:
            d["uncat_inc"] += r["inc"]
            d["uncat_spend"] += -r["spend"]
            d["n_uncat"] += r["n"]
        else:
            d["cats"][r["cat"]] = d["cats"].get(r["cat"], 0) + r["inc"] + r["spend"]
    return d


def _average(months):
    """Mean of the given month dicts, same shape, float cents; {} months is
    the caller's first-month case and never reaches here."""
    n = len(months)
    ids = {cid for m in months for cid in m["cats"]}
    return {"cats": {cid: sum(m["cats"].get(cid, 0) for m in months) / n
                     for cid in ids},
            "uncat_inc": sum(m["uncat_inc"] for m in months) / n,
            "uncat_spend": sum(m["uncat_spend"] for m in months) / n}


def _totals(d, cats, income_groups):
    """(income, spending) cents, spending positive. Categorized money counts
    net — a refund reduces its category's spending — uncategorized by sign."""
    income = d["uncat_inc"]
    spending = d["uncat_spend"]
    for cid, net in d["cats"].items():
        if cats[cid]["grp"] in income_groups:
            income += net
        else:
            spending += -net
    return income, spending


def _group_net(d, cats, grp):
    return sum(net for cid, net in d["cats"].items() if cats[cid]["grp"] == grp)


def _streak(leftovers):
    """Green months in a row at the end of the oldest-to-newest list."""
    run = 0
    for v in reversed(leftovers):
        if v < 0:
            break
        run += 1
    return run


# ---------------------------------------------------------------- income sources

SOURCES_HEADING = "## Sources"


def _read_sources(path=None):
    """({payee: {key: value}}, problem) from SOURCES_FILE. Only the
    SOURCES_HEADING section is read, so the notes above it are free prose and
    may use bullets of their own: inside it a top-level '- ' bullet names a
    payee and its indented 'key: value' bullets are that payee's fields
    (frequency, label, incomes). A missing file is no problem at all — every
    source then rests on its own dates — but a file with no such heading is,
    since entries written outside it would be read by nobody."""
    path = path or SOURCES_FILE   # read at call time; tests repoint the global
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        # a stray byte would otherwise take the whole cash-flow report down,
        # and this file is edited by hand and by the agent
        return {}, ""
    head = re.search(rf"^{re.escape(SOURCES_HEADING)}\s*$", text, re.M)
    if not head:
        return {}, (f"{os.path.basename(path)} has no '{SOURCES_HEADING}' "
                    "heading, so no source entries were read")
    out, cur = {}, None
    for line in text[head.end():].splitlines():
        if line.startswith("#"):
            break                       # the section ends at the next heading
        m = re.match(r"^( *)-\s+(.*\S)\s*$", line)
        if not m:
            continue
        body = m.group(2)
        if not m.group(1):
            cur = body
            out.setdefault(cur, {})
        elif cur is not None and ":" in body:
            key, value = body.split(":", 1)
            out[cur][key.strip().casefold()] = value.strip()
    return out, ""


def _per_year(text):
    """Payments a year for a frequency phrase — a PER_YEAR name or the
    'N per year' form. None when the phrase is not one of those."""
    t = " ".join(text.split()).casefold()
    if t in PER_YEAR:
        return PER_YEAR[t]
    m = re.fullmatch(r"(\d{1,3}) per year", t)
    if m and 1 <= int(m.group(1)) <= 366:
        return int(m.group(1))
    return None


def _detect(dates):
    """Frequency name read off payment dates (oldest first), or None when
    there are fewer than DETECT_MIN payments or the median gap fits no
    bucket. A fortnightly gap counts as 'twice a month' when the payments
    keep to two days of the month instead of drifting through it."""
    if len(dates) < DETECT_MIN:
        return None
    gaps = sorted((dates[i + 1] - dates[i]).days
                  for i in range(len(dates) - 1))
    med = gaps[len(gaps) // 2]
    name = next((n for (lo, hi), n in GAP_BUCKETS if lo <= med <= hi), None)
    if name == "every 2 weeks" and len(dates) >= 4 \
            and len({d.day for d in dates}) <= 2:
        return "twice a month"
    return name


def _same(a, b):
    return abs(a - b) <= INCOME_TOLERANCE * max(a, b)


def _pick_payment(amounts):
    """(cents, reason) — the standing payment for a single-income source,
    amounts oldest first. The latest payment counts, judged against the two
    before it: agreeing with the previous one confirms it (and when both sit
    at a level the third did not hold, that is a raise or drop worth saying);
    disagreeing with the previous but matching the one before means the
    middle payment was the outlier; disagreeing with both makes the latest
    the outlier, and the previous payment stands in. Fewer than 3 payments
    give nothing to judge against, so the latest counts as-is. reason '' =
    nothing to say."""
    c = amounts[-1]
    if len(amounts) < 3:
        return c, ""
    a, b = amounts[-3], amounts[-2]
    if _same(c, b):
        if _same(b, a):
            return c, ""
        word = "raise" if b > a else "drop"
        return c, f"last 2 consistent {word}, using new"
    if _same(c, a):
        return c, ""
    return b, "outlier, using previous"


def _round_mean(amounts, incomes):
    """(mean cents, payments used) for a multi-income payee: the mean of the
    last whole round — one payment per income. Fewer payments than incomes
    leaves the window whole, since there is no round to cut back to; the
    caller's line says 'N of M incomes averaged' there, which is how a
    leaning figure is marked."""
    window = amounts[-incomes:]
    return sum(window) / len(window), len(window)


def _entry_amounts(entries):
    """({entry: [cents]}, {entry: [note]}) for SOURCES_FILE entries carrying
    an amount field. Values are separated by commas or spaces, so a value
    holds no thousands separator — '$2,000' reads as '$2' and '000', and the
    note on '000' is how that mistake surfaces. A value another entry already
    listed is matched there (file order) and noted here."""
    amounts, notes, seen = {}, {}, {}
    for name, e in entries.items():
        if "amount" not in e:
            continue
        amounts[name], notes[name] = [], []
        for tok in re.split(r"[\s,]+", e["amount"].strip()):
            if not re.fullmatch(AMOUNT_RE, tok):
                notes[name].append(f"income-sources.md gives amount {tok!r}, "
                                   "which is not a dollar amount")
                continue
            cents = round(float(tok.lstrip("$")) * 100)
            if cents in seen:
                notes[name].append(f"amount {tok} is also on {seen[cents]} "
                                   "— matched there")
            else:
                seen[cents] = name
                amounts[name].append(cents)
    return amounts, notes


def _claim_amounts(payments, amounts):
    """The payments dict with amount-matched deposits moved out of their
    payee series into a series named by the claiming entry. A drained payee
    disappears; a claim landing on an existing name merges, oldest first."""
    owner = {c: n for n, cs in amounts.items() for c in cs}
    if not owner:
        return payments
    out, claimed = {}, {}
    for payee, pays in payments.items():
        kept = []
        for d, a in pays:
            if a in owner:
                claimed.setdefault(owner[a], []).append((d, a))
            else:
                kept.append((d, a))
        if kept:
            out[payee] = kept
    for name, pays in claimed.items():
        out[name] = sorted(out.get(name, []) + pays)
    return out


def _source_rows(ctx):
    """([row], [missing]) — one row per income source, biggest monthly
    first; missing = (name, by_amount, notes) for the SOURCES_FILE entries
    that matched nothing in the budget. A row carries name, label, freq,
    per_year, incomes, monthly, used, mean, latest, stopped and note;
    per_year None means no frequency is known, so the source gets no monthly
    figure and stays out of the total."""
    entries = ctx["source_file"]
    entry_amounts, amount_notes = _entry_amounts(entries)
    payments = _claim_amounts(ctx["payments"], entry_amounts)
    rows = []
    for payee, pays in payments.items():
        e = entries.get(payee, {})
        dates = [d for d, _ in pays]
        amounts = [a for _, a in pays]
        # every problem with this entry, not just the first
        notes = list(amount_notes.get(payee, []))
        incomes = 1
        if e.get("incomes"):
            if e["incomes"].isdigit() and int(e["incomes"]) >= 1:
                incomes = int(e["incomes"])
            else:
                notes.append(f"income-sources.md gives incomes "
                             f"{e['incomes']!r}, which is not a count")
        # several incomes under one payee interleave, so the gaps between
        # arrivals describe no single rhythm — two twice-yearly grants a month
        # apart leave gaps of 1 and 5 months, and a median of those means
        # nothing. Such a payee is read from its entry alone.
        detected = _detect(dates) if incomes == 1 else None
        want = e.get("frequency", "")
        set_year = _per_year(want) if want else None
        if want and set_year is None:
            notes.append(f"income-sources.md gives frequency {want!r}, "
                         "which is not a frequency I know")
        if set_year:
            freq, per_year = want, set_year
            # names may differ while the rate agrees ('quarterly' against
            # '4 per year'), so the check compares payments a year, not words
            if detected and PER_YEAR[detected] != set_year:
                notes.append(f"income-sources.md says {want}, "
                             f"the dates say {detected}")
        elif detected:
            freq, per_year = detected, PER_YEAR[detected]
        else:
            freq, per_year = "", None
        monthly = used = mean = None
        reason = ""
        if per_year:
            if incomes > 1:
                mean, used = _round_mean(amounts, incomes)
            else:
                mean, reason = _pick_payment(amounts)
            monthly = mean * per_year / 12
        latest = dates[-1]
        silent = (ctx["today"] - latest).days
        stopped = bool(per_year) and silent > STOPPED_INTERVALS * 365.25 / per_year
        rows.append({"name": payee, "label": e.get("label") or payee,
                     "freq": freq, "per_year": per_year, "incomes": incomes,
                     "monthly": monthly, "used": used, "mean": mean,
                     "reason": reason, "latest": latest, "silent": silent,
                     "stopped": stopped, "notes": notes, "n": len(pays)})
    # counted sources first and richest first; stopped and unestimable ones
    # gather at the bottom, where they read as exceptions rather than rows
    # somebody has to check against the total
    rows.sort(key=lambda r: (r["monthly"] is None or r["stopped"],
                             -(r["monthly"] or 0), r["label"]))
    missing = [(name, name in entry_amounts, amount_notes.get(name, []))
               for name in entries if name not in payments]
    return rows, missing


def _ago(days):
    if days < 60:
        return _plural(days, "day") + " ago"
    return _plural(round(days / 30.44), "month") + " ago"


def _income_section(ctx):
    """(estimate cents, lines) — the monthly income estimate and one entry
    per source. Counted are sources with a known frequency that have not gone
    silent; every other source still prints, with the reason instead of a
    number."""
    rows, missing = _source_rows(ctx)
    total = sum(r["monthly"] for r in rows
                if r["monthly"] is not None and not r["stopped"])
    lines = ["", f"Income estimate: {_money(total)} monthly", ""]
    if not rows and not missing:
        lines.append(f"- no payments yet in the {INCOME_SOURCE_CATEGORY} "
                     "income category")
    for r in rows:
        if r["stopped"]:
            lines.append(f"- {r['label']} ({r['freq']}): stopped")
            lines.append(f"    last payment {r['latest'].isoformat()}, "
                         f"{_ago(r['silent'])} — not counted")
        elif r["per_year"] is None:
            lines.append(f"- {r['label']}: not estimable")
            lines.append(f"    {_plural(r['n'], 'payment')}, latest "
                         f"{r['latest'].isoformat()} — set a frequency, or "
                         f"wait until it has {DETECT_MIN}")
        else:
            tail = f" ({r['reason']})" if r["reason"] else ""
            lines.append(f"- {r['label']} ({r['freq']}): "
                         f"{_money(r['monthly'])} monthly{tail}")
            if r["incomes"] > 1:
                if r["used"] < r["incomes"]:
                    count = f"{r['used']} of {r['incomes']} incomes averaged"
                else:
                    count = f"{_plural(r['used'], 'payment')} averaged"
                lines.append(f"    {count}, average ${r['mean'] / 100:,.2f}, "
                             f"latest {r['latest'].isoformat()}")
            else:
                lines.append(f"    payment ${r['mean'] / 100:,.2f}, "
                             f"latest {r['latest'].isoformat()}")
        for note in r["notes"]:
            lines.append(f"    check: {note}")
    for name, by_amount, notes in missing:
        if by_amount:
            lines.append(f"- {name}: no deposit matching its amount yet")
        else:
            lines.append(f"- {name}: not found in the budget — fix "
                         "income-sources.md")
        for note in notes:
            lines.append(f"    check: {note}")
    if ctx["source_problem"]:
        lines.append(f"- check: {ctx['source_problem']}")
    return total, lines


# ---------------------------------------------------------------- shared context

def _ctx(conn, today):
    cats, groups = _structure(conn)
    excluded_ids = frozenset(r["id"] for r in conn.execute(
        "SELECT c.id FROM categories c JOIN category_groups g "
        "ON g.id = c.cat_group WHERE c.tombstone = 0 AND g.tombstone = 0")
        if r["id"] not in cats)
    first = _parse_month(FIRST_MONTH)
    cur = _idx(today.year, today.month)
    if cur < first:
        raise ValueError(f"today is before FIRST_MONTH {FIRST_MONTH}")
    rows = _fetch(conn, first, cur)
    months = {i: _month_data(rows, i, cats, excluded_ids)
              for i in range(first, cur + 1)}
    income_groups = {g["name"] for g in groups if g["is_income"]}
    sources, problem = _read_sources()
    return {"cats": cats, "groups": groups, "income_groups": income_groups,
            "first": first, "cur": cur, "today": today, "months": months,
            "payments": _fetch_income(conn, first),
            "checking": _checking_balance(conn),
            "source_file": sources, "source_problem": problem}


def _window(ctx, upto):
    """Up to WINDOW complete months right before `upto`, oldest first."""
    return list(range(max(ctx["first"], upto - WINDOW), upto))


def _window_label(win):
    if not win:
        return "no average"
    span = _label(win[0]) if len(win) == 1 else f"{_label(win[0])}..{_label(win[-1])}"
    return f"average: {_plural(len(win), 'month')} ({span})"


def _leftovers(ctx, upto):
    """Leftover cents of every complete month from FIRST_MONTH to `upto`
    (exclusive), oldest first — streaks may run past the average window."""
    out = []
    for i in range(ctx["first"], upto):
        income, spending = _totals(ctx["months"][i], ctx["cats"], ctx["income_groups"])
        out.append(income - spending)
    return out


def _expense_groups(ctx):
    return [g["name"] for g in ctx["groups"] if not g["is_income"]]


def _resolve_group(ctx, name):
    want = name.strip()
    hit = [g["name"] for g in ctx["groups"] if g["name"] == want]
    if not hit:
        wf = want.casefold()
        hit = [g["name"] for g in ctx["groups"] if g["name"].casefold() == wf]
    if len(hit) == 1:
        return hit[0]
    valid = ", ".join(g["name"] for g in ctx["groups"])
    raise ValueError(f"unknown group {want!r} — groups: {valid}")


def _resolve_tracked(ctx, entry):
    """Category ids for one TRACKED_CATEGORIES entry — bare name, or
    'group: name' when the bare name is in several groups. [] = not found."""
    hit = [cid for cid, c in ctx["cats"].items()
           if entry in (c["name"], f"{c['grp']}: {c['name']}")]
    return hit if len(hit) == 1 else []


# ---------------------------------------------------------------- rows

def _cat_row(name, actual, avg, mode, elapsed):
    """One category or group line. mode: 'pace' (expected by now),
    'fixed' (of average), 'month' (average column), 'plain' (no average)."""
    if mode == "plain":
        return f"- {name}: {_money(actual)}"
    if mode == "fixed":
        return f"- {name}: {_money(actual)} of {_money(avg)} average"
    if mode == "pace":
        return f"- {name}: {_money(actual)} | expected by now {_money(avg * elapsed)}"
    return f"- {name}: {_money(actual)} | average {_money(avg)}"


def _uncat_amounts(d):
    parts = [f"{_money(d['uncat_spend'])} spending"]
    if round(d["uncat_inc"] / 100):
        parts.append(f"{_money(d['uncat_inc'])} income")
    return ", ".join(parts)


def _tracked_section(ctx, d, avg, mode, elapsed):
    if not TRACKED_CATEGORIES:
        return []
    lines = ["", "Tracked categories:"]
    for entry in TRACKED_CATEGORIES:
        ids = _resolve_tracked(ctx, entry)
        if not ids:
            lines.append(f"- {entry}: not found — fix TRACKED_CATEGORIES in cash_flow.py")
            continue
        cid = ids[0]
        c = ctx["cats"][cid]
        income_side = c["grp"] in ctx["income_groups"]
        actual = d["cats"].get(cid, 0) * (1 if income_side else -1)
        a = (avg["cats"].get(cid, 0) if avg else 0) * (1 if income_side else -1)
        row_mode = mode
        if mode == "pace" and c["grp"] in NO_FORECAST_GROUPS:
            row_mode = "plain"
        elif mode == "pace" and (income_side or c["grp"] in FIXED_GROUPS):
            row_mode = "fixed"
        lines.append(_cat_row(c["name"], actual, a, row_mode, elapsed))
    return lines


def _group_section(ctx, grp, d, avg, mode, elapsed):
    """Per-category rows of one group plus its total line."""
    income_side = grp in ctx["income_groups"]
    sign = 1 if income_side else -1
    if mode == "pace" and grp in NO_FORECAST_GROUPS:
        mode = "plain"
    elif mode == "pace" and (income_side or grp in FIXED_GROUPS):
        mode = "fixed"
    lines = ["", f"{grp} categories:"]
    total = a_total = 0.0
    for cid, c in ctx["cats"].items():
        if c["grp"] != grp:
            continue
        actual = d["cats"].get(cid, 0) * sign
        a = (avg["cats"].get(cid, 0) if avg else 0) * sign
        total += actual
        a_total += a
        if round(actual / 100) or round(a / 100):
            lines.append(_cat_row(c["name"], actual, a, mode, elapsed))
    lines.append(_cat_row("total", total, a_total, mode, elapsed))
    return lines


# ---------------------------------------------------------------- status view

def _projection(ctx, d, avg, win, elapsed, est):
    """The month-end block, average in hand: income from the sources,
    NO_FORECAST_GROUPS at what was actually spent, FIXED_GROUPS at their
    full average, every other group at pace."""
    cats = ctx["cats"]
    p_spending = d["uncat_spend"] + avg["uncat_spend"] * (1 - elapsed)
    for grp in _expense_groups(ctx):
        avg_g = -sum(net for cid, net in avg["cats"].items()
                     if cats[cid]["grp"] == grp)
        if grp in NO_FORECAST_GROUPS:
            p_spending += -_group_net(d, cats, grp)
        elif grp in FIXED_GROUPS:
            p_spending += avg_g
        else:
            p_spending += -_group_net(d, cats, grp) + avg_g * (1 - elapsed)
    pairs = [_totals(ctx["months"][i], cats, ctx["income_groups"]) for i in win]
    avg_income = sum(p[0] for p in pairs) / len(win)
    avg_spending = sum(p[1] for p in pairs) / len(win)
    p_leftover = est - p_spending
    run = _streak(_leftovers(ctx, ctx["cur"]))
    color = "green" if p_leftover >= 0 else "red"
    fixed = _join_and([g for g in _expense_groups(ctx) if g in FIXED_GROUPS])
    spent = _join_and([g for g in _expense_groups(ctx)
                       if g in NO_FORECAST_GROUPS])
    flat = "income from sources" + (f", {fixed} at average" if fixed else "") \
        + (f", {spent} as spent" if spent else "")
    return ["",
            f"Projected month end ({flat}, rest at pace):",
            f"- income: {_money(est)}",
            f"- spending: {_money(p_spending)} | average {_money(avg_spending)}",
            f"- leftover: {_signed(p_leftover)} | average "
            f"{_signed(avg_income - avg_spending)}",
            f"- streak: {_plural(run, 'green month')} in a row; this month "
            f"projected {color}"]


def _runs_out(ctx, win):
    """The runs-out block: CHECKING_ACCOUNT's balance divided by the
    window's average monthly spending — income deliberately not counted, so
    the answer is how long the money lasts if nothing more comes in."""
    bal = ctx["checking"]
    lines = ["", f"How long the money lasts ({CHECKING_ACCOUNT} against "
                 "average spending, income not counted):"]
    if bal is None:
        lines.append(f"- account {CHECKING_ACCOUNT!r} not found — fix "
                     "CHECKING_ACCOUNT in cash_flow.py")
        return lines
    lines.append(f"- balance: {_money(bal)}")
    if not win:
        lines.append("- first month: no average spending yet")
        return lines
    pairs = [_totals(ctx["months"][i], ctx["cats"], ctx["income_groups"])
             for i in win]
    avg_spending = sum(p[1] for p in pairs) / len(win)
    lines.append(f"- average spending: {_money(avg_spending)} monthly")
    if round(bal / 100) <= 0:
        lines.append("- already out")
    elif round(avg_spending / 100) <= 0:
        lines.append("- average spending is $0 — never runs out")
    else:
        months = bal / avg_spending
        when = ctx["today"] + timedelta(days=months * 30.44)
        lines.append(f"- lasts about {months:.1f} months — runs out around "
                     f"{when.year:04d}-{when.month:02d}")
    return lines


def _status(ctx, group=""):
    """Current month, grouped under Estimated (projection, income estimate)
    and Actual (so far, tracked, runs-out, debt). The projection and the
    streak need a complete month behind them, so the first month's Estimated
    block holds the income estimate alone."""
    today, cur, cats = ctx["today"], ctx["cur"], ctx["cats"]
    day, days = today.day, _days_in(cur)
    elapsed = day / days
    d = ctx["months"][cur]
    win = _window(ctx, cur)
    avg = _average([ctx["months"][i] for i in win]) if win else None
    income, spending = _totals(d, cats, ctx["income_groups"])
    est, income_lines = _income_section(ctx)

    head = _window_label(win) if win else "first month: no average yet"
    lines = [f"Budget status — {_label(cur)}, day {day} of {days} | {head}",
             "", "Estimated:"]
    if win:
        lines += _projection(ctx, d, avg, win, elapsed, est)
    lines += income_lines
    lines += ["", "Actual:"]
    lines += ["",
              "So far:",
              f"- income: {_money(income)}",
              f"- spending: {_money(spending)}",
              f"- income - spending: {_signed(income - spending)}"]
    for grp in _expense_groups(ctx):
        actual = -_group_net(d, cats, grp)
        avg_g = -sum(net for cid, net in avg["cats"].items()
                     if cats[cid]["grp"] == grp) if avg else 0
        if not round(actual / 100) and not round(avg_g / 100):
            continue
        if not avg or grp in NO_FORECAST_GROUPS:
            mode = "plain"
        elif grp in FIXED_GROUPS:
            mode = "fixed"
        else:
            mode = "pace"
        lines.append(_cat_row(grp, actual, avg_g, mode, elapsed))
    lines.append(f"- uncategorized: {_plural(d['n_uncat'], 'transaction')} "
                 f"({_uncat_amounts(d)})")
    mode = "pace" if avg else "plain"
    lines += _tracked_section(ctx, d, avg, mode, elapsed)
    if group:
        lines += _group_section(ctx, _resolve_group(ctx, group), d, avg,
                                mode, elapsed)
    lines += _runs_out(ctx, win)
    debt = debts.status_block()
    if debt:
        lines += ["", debt]
    return "\n".join(lines)


# ---------------------------------------------------------------- month view

def _month(ctx, m_idx, group=""):
    if m_idx == ctx["cur"]:
        return _status(ctx, group)
    if m_idx > ctx["cur"]:
        raise ValueError(f"{_label(m_idx)} is in the future")
    if m_idx < ctx["first"]:
        raise ValueError(f"data starts {FIRST_MONTH} — earlier months never count")
    cats = ctx["cats"]
    d = ctx["months"][m_idx]
    win = _window(ctx, m_idx)
    avg = _average([ctx["months"][i] for i in win]) if win else None
    income, spending = _totals(d, cats, ctx["income_groups"])
    header_tail = _window_label(win) if win else "first month: no average"
    mode = "month" if win else "plain"

    lines = [f"Month — {_label(m_idx)} | {header_tail}"]
    if win:
        pairs = [_totals(ctx["months"][i], cats, ctx["income_groups"]) for i in win]
        avg_income = sum(p[0] for p in pairs) / len(win)
        avg_spending = sum(p[1] for p in pairs) / len(win)
        lines += [f"- income: {_money(income)} | average {_money(avg_income)}",
                  f"- spending: {_money(spending)} | average {_money(avg_spending)}",
                  f"- leftover: {_signed(income - spending)} | average "
                  f"{_signed(avg_income - avg_spending)}"]
    else:
        lines += [f"- income: {_money(income)}",
                  f"- spending: {_money(spending)}",
                  f"- leftover: {_signed(income - spending)}"]

    lines += ["", "Groups:"]
    for g in ctx["groups"]:
        grp = g["name"]
        sign = 1 if g["is_income"] else -1
        actual = _group_net(d, cats, grp) * sign
        a = (sum(net for cid, net in avg["cats"].items()
                 if cats[cid]["grp"] == grp) * sign) if avg else 0
        if round(actual / 100) or round(a / 100):
            lines.append(_cat_row(grp, actual, a, mode, 1.0))
    uncat = f"- uncategorized: {_money(d['uncat_spend'])} spending"
    if avg:
        uncat += f" | average {_money(avg['uncat_spend'])}"
    if round(d["uncat_inc"] / 100) or (avg and round(avg["uncat_inc"] / 100)):
        uncat += f"; {_money(d['uncat_inc'])} income"
        if avg:
            uncat += f" | average {_money(avg['uncat_inc'])}"
    lines.append(uncat)

    lines += _tracked_section(ctx, d, avg, mode, 1.0)
    if group:
        lines += _group_section(ctx, _resolve_group(ctx, group), d, avg, mode, 1.0)

    if avg:
        deltas = []
        for cid, c in cats.items():
            sign = 1 if c["grp"] in ctx["income_groups"] else -1
            delta = (d["cats"].get(cid, 0) - avg["cats"].get(cid, 0)) * sign
            if abs(round(delta / 100)):
                deltas.append((abs(delta), delta, c["name"]))
        deltas.sort(reverse=True)
        if deltas:
            lines += ["", "Outliers vs average:"]
            for _, delta, name in deltas[:OUTLIERS_N]:
                word = "over" if delta > 0 else "under"
                lines.append(f"- {name}: {_money(abs(delta))} {word}")

    leftovers = _leftovers(ctx, m_idx + 1)
    green = leftovers[-1] >= 0
    lastn = leftovers[-WINDOW:]
    tally = (f"green: {sum(1 for v in lastn if v >= 0)} of last "
             f"{_plural(len(lastn), 'month')}")
    if green:
        lines += ["", f"Streak: green month — {_streak(leftovers)} in a row; {tally}."]
    else:
        lines += ["", f"Streak: red month; {tally}."]
    return "\n".join(lines)


# ---------------------------------------------------------------- history view

def _history(ctx, months):
    complete = list(range(ctx["first"], ctx["cur"]))
    if not complete:
        return (f"no complete months yet — first month {FIRST_MONTH} is "
                "still running")
    take = complete[-months:]
    lines = [f"History — last {_plural(len(take), 'complete month')}, newest first:"]
    for i in reversed(take):
        income, spending = _totals(ctx["months"][i], ctx["cats"],
                                   ctx["income_groups"])
        left = income - spending
        color = "green" if left >= 0 else "red"
        lines.append(f"- {_label(i)}: income {_money(income)} | spending "
                     f"{_money(spending)} | leftover {_signed(left)} | {color}")
    win = _window(ctx, ctx["cur"])
    pairs = [_totals(ctx["months"][i], ctx["cats"], ctx["income_groups"])
             for i in win]
    avg_income = sum(p[0] for p in pairs) / len(win)
    avg_spending = sum(p[1] for p in pairs) / len(win)
    lines.append(f"- {_window_label(win)}: income {_money(avg_income)} | "
                 f"spending {_money(avg_spending)} | leftover "
                 f"{_signed(avg_income - avg_spending)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- ledger

def _ledger_month(ctx, m_idx):
    """Cents for one complete month's monthly-balance line. The estimate is
    the source estimate as of the month's last day — payments after it are
    cut away — so a late backfill writes the same numbers a run on the 1st
    would have. recurring is the income that arrived in the
    INCOME_SOURCE_CATEGORY category; one_off is every other income,
    uncategorized deposits included."""
    end = date(m_idx // 12, m_idx % 12 + 1, _days_in(m_idx))
    payments = {}
    for p, pays in ctx["payments"].items():
        cut = [(d, a) for d, a in pays if d <= end]
        if cut:
            payments[p] = cut
    rows, _ = _source_rows({"payments": payments,
                            "source_file": ctx["source_file"], "today": end})
    estimate = sum(r["monthly"] for r in rows
                   if r["monthly"] is not None and not r["stopped"])
    d = ctx["months"][m_idx]
    income, spending = _totals(d, ctx["cats"], ctx["income_groups"])
    recurring = sum(net for cid, net in d["cats"].items()
                    if ctx["cats"][cid]["grp"] in ctx["income_groups"]
                    and ctx["cats"][cid]["name"] == INCOME_SOURCE_CATEGORY)
    return {"estimate": estimate, "recurring": recurring,
            "one_off": income - recurring, "spending": spending}


def ledger_month(month, today=None):
    """{estimate, recurring, one_off, spending} in cents for one complete
    month (YYYY-MM) — the numbers finance_scan.py caches in the
    monthly-balance file. Raises ValueError for the current month, a future
    one, or one before FIRST_MONTH."""
    ctx = _ctx(_db(), today or date.today())
    m_idx = _parse_month(month)
    if m_idx >= ctx["cur"]:
        raise ValueError(f"{month} is not a complete month yet")
    if m_idx < ctx["first"]:
        raise ValueError(f"data starts {FIRST_MONTH} — earlier months never count")
    return _ledger_month(ctx, m_idx)


# ---------------------------------------------------------------- entry points

def build_report(month="", group="", today=None):
    """Status (month empty) or one month (YYYY-MM) vs average; group expands
    that group into per-category rows. Raises ValueError on bad arguments."""
    ctx = _ctx(_db(), today or date.today())
    month = (month or "").strip()
    group = (group or "").strip()
    if month:
        return _month(ctx, _parse_month(month), group)
    return _status(ctx, group)


def history_report(months=6, today=None):
    """One line per complete month, newest first, plus the average line."""
    if not isinstance(months, int) or not 1 <= months <= HISTORY_CAP:
        raise ValueError(f"months must be 1-{HISTORY_CAP}")
    return _history(_ctx(_db(), today or date.today()), months)


def main():
    args = sys.argv[1:]
    try:
        if "--history" in args:
            i = args.index("--history")
            print(history_report(int(args[i + 1])))
            return
        month = group = ""
        while args:
            arg = args.pop(0)
            if arg == "--group":
                group = args.pop(0)
            else:
                month = arg
        print(build_report(month=month, group=group))
    except (ValueError, IndexError) as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
