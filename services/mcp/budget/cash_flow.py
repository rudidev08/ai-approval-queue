#!/usr/bin/env python3
"""cash_flow — money in, money out, vs the monthly average (read-only).

Two plain-text views over the api-cache SQLite copy, pulled fresh from the
server when its last download is older than 10 minutes (api_cache.pull_api_cache_if_stale):

  build_report()                current-month status: estimates, so far,
                                forecast
  build_report(month="2026-08") that finished month vs its average
  history_report(6)             one line per complete month, newest first
  check_report()                the current month's Check items alone, as
                                a list — the daily email's Check section

build_report also takes detailed — the report size. False (regular, the
default) prints income sources one line each, no history rows anywhere,
and debts at balance, rate and (high and medium) payoff month only; True
prints everything. categories
adds each group's per-category rows under its group row.

The average is the plain mean per category over up to WINDOW complete months,
never earlier than FIRST_MONTH (the first full import month; the window grows
1 -> WINDOW on its own). Group numbers are sums of their categories.

Income is not averaged that way. Two income categories hold the sources:
MONTHLY_CATEGORY (income that lands every month: paychecks,
pensions) and PERIODIC_CATEGORY (income that lands a few times a year:
bonuses, dividends). Each payee inside one of them is one source, worth its
standing payment times its payments a year, over 12 — a year holds a whole
number of payments and a month does not, so monthly sums mis-state every
rhythm that does not divide into one. The standing payment is the latest
one, judged against the two before it (INCOME_TOLERANCE apart counts as
different): a lone spike or dip is an outlier and the previous payment
stands in; two payments agreeing at a new level are a confirmed change and
count; the source's line says which. A source's rhythm comes from
cash-flow.md in the finance notes folder when set there (one section per
category, headed by its name), otherwise from its own gaps once it has
DETECT_MIN payments, and a source with neither gets no figure. That file
also carries a display name and, for a payee covering more than one income,
how many — such a payee uses the mean of its last whole round of payments,
since interleaved sizes make consecutive comparison meaningless.
An entry with an amount is its own source: it claims every deposit of
exactly that value, whatever the payee, and its name is the display name;
old values stay listed so past deposits keep their source.
An entry with no deposit yet prints "no deposit yet" and counts nothing;
the first real deposit gives it a figure.
A source silent for STOPPED_INTERVALS of its own gaps leaves the total. The
status view prints the estimate, a subtotal per category and one line per
source; a past month shows the income that actually arrived instead: the
file keeps no past rhythms to rebuild an estimate from.

The status view runs in ### sections: Income and spending so far
(Income and spending for a past month) — the income total with its
category rows, the expense total with its group rows (each with its
category rows when categories is set), tracked categories. In a detailed
report every Income and spending row carries a history row underneath
with the two previous months and the percent change against each
('  - Jul $x (+24%) · Jun $y (-8%)'; a month before FIRST_MONTH prints
'-', the percent is left out when the month is zero); Estimated — the
income estimate (monthly and periodic subtotals, one line per source),
the expense estimate (the average monthly spending, one line per group;
this month so far until one complete month exists) and the upcoming
one-time expenses; Forecast — how long the money lasts, two lines: the CASH_ACCOUNTS balance
(every transaction in those accounts, transfers and starting balance
included) minus the upcoming total, first against the expense estimate
minus the monthly income alone, then with the periodic income included —
never runs out while the income covers the expenses; the net spending
drops by a debt's payment from the month after its last payment; Assets
and Debt — asset balances and change out of assets.md, then per-tier
debt balances, rate, payoff and interest math out of debts.md (both in
the finance notes folder), when those files exist; Check — finance-file
problems and debt payment checks, printed only when there is something
to say.
A debts.md debt with a category field ties that budget category to the
debt: in the current month its average becomes the payment of every debt
in it still running (the payment field, else the category's own
average from the debt's first balance month on; with neither, the
category's plain average stays), so a paid-off debt's old payments leave the estimate at once.
A hidden category leaves the current month's estimate the same way.
Past months keep plain averages.
A past month is the same report with the month's final numbers: the
Income and spending rows carry their average and the percent off it,
plus a leftover total with no average; Estimated and Forecast are left
out, and Assets and Debt reads the files as of that month's end.
Upcoming one-time expenses are hand-listed in cash-flow.md's Upcoming
section (amount, optional best-guess month, display only); each prints in
the Estimated block and the total comes off the Forecast balance. The
entry is deleted by hand once paid — the payment itself goes to a
ONE_OFF_GROUPS category: that month's totals show it, no average ever does.
ONE_OFF_GROUPS and the ONE_OFF_INCOME category hold real outside-world
money that is not normal life (an asset bought to sell on, a planned
one-time payment, a one-time deposit): it counts in every
month's income and spending — the leftover and history tell the truth —
but stays out of every average, so the pace rows, the averages and the
runs-out divisor never see it. Excluded everywhere:
transfers, off-budget accounts, starting-balance rows, EXCLUDED_GROUPS.
Uncategorized money is counted in the totals; a deleted category's
transactions count as uncategorized. Whole dollars, minus before the dollar
sign; rows round independently of totals, so a $1 drift between them is
possible and left alone.

CLI: cash_flow.py [YYYY-MM] [--detailed] [--categories] [--combine-personal] | --history N
"""

import calendar
import os
import re
import sys
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import debts  # noqa: E402
from data_root import root  # noqa: E402
import api_cache  # noqa: E402

FIRST_MONTH = "2026-08"    # first full import month; earlier months never count
WINDOW = 12                # complete months in the average, at most
EXCLUDED_GROUPS = ["Ignored"]   # group names left out of every number
FIXED_GROUPS = ["Fixed"]   # groups shown at their full monthly figure, no pace judgment
ONE_OFF_GROUPS = ["One-off"]   # counted in month totals, in no average
ONE_OFF_INCOME = "One-off"     # the income category counted the same way
# category name -> the row its amount joins when a report is built with
# combine_personal: those categories print as one row under that label
# instead of their own, so no per-person figure is named. The group
# total is unchanged, so the rows under it still add up to it.
PERSONAL_CATEGORIES = {"Personal": "Personal (combined)",
                       "Personal (Alex)": "Personal (combined)",
                       "Personal (Sam)": "Personal (combined)",
                       "Recurring (Alex)": "Recurring (combined)",
                       "Recurring (Sam)": "Recurring (combined)"}
# accounts the Forecast balance sums — the money there is spendable, wherever
# it sits; moves between them are transfers and count nowhere else
CASH_ACCOUNTS = ["Main Checking", "Main Savings"]
HISTORY_CAP = 24

# --- income sources ---
# ONE_OFF_INCOME is never a source.


def cash_flow_file():
    """The hand-kept income-source file, in the finance notes folder."""
    return str(root("ASSISTANT_FINANCE_NOTES") / "cash-flow.md")


MONTHLY_CATEGORY = "Monthly"     # lands every month: paychecks, pensions
PERIODIC_CATEGORY = "Periodic"   # lands a few times a year: bonuses, dividends
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


# the YYYYMMDD integer the budget app stores a date as, both ways, and the bank's
# cents; shared with oddities.py, server.py and the ops service
def _to_date(yyyymmdd):
    s = str(yyyymmdd)
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def _day_int(d):
    return int(d.strftime("%Y%m%d"))


def _iso(yyyymmdd):
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _dollars(cents):
    return f"{(cents or 0) / 100:.2f}"


# ---------------------------------------------------------------- dollars

def _money(cents):
    d = round(cents / 100)
    return f"-${-d:,.0f}" if d < 0 else f"${d:,.0f}"


def _signed(cents):
    d = round(cents / 100)
    if d < 0:
        return f"-${-d:,.0f}"
    return f"+${d:,.0f}" if d > 0 else "$0"


def _vs_avg(actual, avg):
    """'($296 avg, +526%)' — the average and the percent `actual` is off
    it; the percent is left out when the average rounds to $0."""
    tail = f", {debts.pct(actual, avg)}" if round(avg / 100) else ""
    return f"({_money(avg)} avg{tail})"


def _join_and(names):
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------- reading the copy

def _structure(conn):
    """(cats, groups): cats = {category id: {name, grp, hidden}}, groups = ordered
    [{name, is_income}] in the budget app's own order — both without EXCLUDED_GROUPS,
    hidden ones kept and flagged (a hidden category or one in a hidden
    group): past months count them, the current month's estimate does not
    (_debt_average)."""
    cats = {}
    for r in conn.execute(
            "SELECT c.id, c.name, c.hidden OR g.hidden AS hidden, "
            "g.name AS grp FROM categories c "
            "JOIN category_groups g ON g.id = c.cat_group "
            "WHERE c.tombstone = 0 AND g.tombstone = 0 "
            "ORDER BY g.sort_order, g.name, c.name COLLATE NOCASE"):
        if r["grp"] not in EXCLUDED_GROUPS:
            cats[r["id"]] = {"name": r["name"], "grp": r["grp"],
                             "hidden": bool(r["hidden"])}
    groups = [{"name": r["name"], "is_income": bool(r["is_income"])}
              for r in conn.execute(
                  "SELECT name, is_income FROM category_groups "
                  "WHERE tombstone = 0 ORDER BY sort_order, name")
              if r["name"] not in EXCLUDED_GROUPS]
    return cats, groups


def _fetch(conn, lo_idx, hi_idx):
    """Per-month per-category sums, cents: [{idx, cat, inc, spend}], cat
    None = uncategorized. Transfers, off-budget accounts and starting-balance
    rows are already gone here; split children count, split parents never."""
    rows = conn.execute(
        "SELECT t.date/100 AS ym, t.category AS cat, "
        "SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS inc, "
        "SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END) AS spend "
        "FROM v_transactions t JOIN accounts a ON a.id = t.account "
        "WHERE t.is_parent = 0 AND t.transfer_id IS NULL "
        "AND t.starting_balance_flag = 0 AND a.offbudget = 0 "
        "AND t.date >= ? AND t.date <= ? "
        "GROUP BY ym, t.category",
        (_bounds(lo_idx)[0], _bounds(hi_idx)[1])).fetchall()
    out = []
    for r in rows:
        year, month = divmod(r["ym"], 100)
        out.append({"idx": _idx(year, month), "cat": r["cat"],
                    "inc": r["inc"], "spend": r["spend"]})
    return out


def _fetch_income(conn, lo_idx):
    """{category: {payee: [(date, cents)]}} for MONTHLY_CATEGORY and
    PERIODIC_CATEGORY payments from lo_idx's first day on, oldest first —
    the individual payments the source maths needs, where _fetch only keeps
    monthly sums. Same exclusions as _fetch; reading no earlier than lo_idx
    keeps a partial first import out of the gap measurements, where a
    missing payment would read as a longer rhythm."""
    rows = conn.execute(
        "SELECT c.name AS cat, COALESCE(p.name, '') AS payee, t.date, "
        "t.amount FROM v_transactions t "
        "JOIN accounts a ON a.id = t.account "
        "LEFT JOIN v_payees p ON p.id = t.payee "
        "JOIN categories c ON c.id = t.category "
        "JOIN category_groups g ON g.id = c.cat_group "
        "WHERE t.is_parent = 0 AND t.transfer_id IS NULL "
        "AND t.starting_balance_flag = 0 AND a.offbudget = 0 "
        "AND t.amount > 0 AND c.tombstone = 0 AND g.tombstone = 0 "
        "AND g.is_income = 1 AND c.name IN (?, ?) AND t.date >= ? "
        "ORDER BY t.date, t.id",
        (MONTHLY_CATEGORY, PERIODIC_CATEGORY, _bounds(lo_idx)[0])).fetchall()
    out = {MONTHLY_CATEGORY: {}, PERIODIC_CATEGORY: {}}
    for r in rows:
        out[r["cat"]].setdefault(r["payee"] or "(no payee)", []).append(
            (_to_date(r["date"]), r["amount"]))
    return out


def _cash_balance(conn):
    """(cents, missing) — the CASH_ACCOUNTS balance: every transaction in
    those accounts counts, transfers and starting balance included, since
    the real balance holds them all. missing lists the names no single open
    account carries; the balance covers the rest."""
    total, missing = 0, []
    for name in CASH_ACCOUNTS:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM accounts WHERE name = ? AND tombstone = 0 "
            "AND closed = 0", (name,))]
        if len(ids) != 1:
            missing.append(name)
            continue
        total += conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM v_transactions "
            "WHERE account = ? AND is_parent = 0", (ids[0],)).fetchone()[0]
    return total, missing


# ---------------------------------------------------------------- compute (pure)

def _month_data(rows, idx, cats, excluded_ids=frozenset()):
    """One month: {cats: {id: net cents}, uncat_inc, uncat_spend
    (positive)}. excluded_ids (EXCLUDED_GROUPS categories) drop entirely; a
    category id in neither cats nor excluded_ids is a deleted category and
    counts as uncategorized, like the other servers display it."""
    d = {"cats": {}, "uncat_inc": 0, "uncat_spend": 0}
    for r in rows:
        if r["idx"] != idx or r["cat"] in excluded_ids:
            continue
        c = cats.get(r["cat"])
        if c is None:
            d["uncat_inc"] += r["inc"]
            d["uncat_spend"] += -r["spend"]
        else:
            d["cats"][r["cat"]] = d["cats"].get(r["cat"], 0) + r["inc"] + r["spend"]
    return d


def _average(months, skip=frozenset()):
    """Mean of the given month dicts, same shape, float cents; skip
    (the one-off category ids, ctx["oneoff_ids"]) stays out — averages describe normal
    life. {} months is the caller's first-month case and never reaches
    here."""
    n = len(months)
    ids = {cid for m in months for cid in m["cats"]} - skip
    return {"cats": {cid: sum(m["cats"].get(cid, 0) for m in months) / n
                     for cid in ids},
            "uncat_inc": sum(m["uncat_inc"] for m in months) / n,
            "uncat_spend": sum(m["uncat_spend"] for m in months) / n}


def _totals(d, cats, income_groups, skip=frozenset()):
    """(income, spending) cents, spending positive. Categorized money counts
    net — a refund reduces its category's spending — uncategorized by sign.
    skip leaves those category ids out (the average flavor)."""
    income = d["uncat_inc"]
    spending = d["uncat_spend"]
    for cid, net in d["cats"].items():
        if cid in skip:
            continue
        if cats[cid]["grp"] in income_groups:
            income += net
        else:
            spending += -net
    return income, spending


def _avg_pair(ctx, win):
    """(income, spending) average cents over the window, the one-off
    categories left out."""
    pairs = [_totals(ctx["months"][i], ctx["cats"], ctx["income_groups"],
                     ctx["oneoff_ids"]) for i in win]
    n = len(win)
    return sum(p[0] for p in pairs) / n, sum(p[1] for p in pairs) / n


def _group_net(d, cats, grp):
    return sum(net for cid, net in d["cats"].items() if cats[cid]["grp"] == grp)


# ---------------------------------------------------------------- income sources

UPCOMING_HEADING = "## Upcoming"


def _heading(cat):
    """The cash-flow.md section holding one source category's entries,
    headed by the category's own name."""
    return f"## {cat}"


def _read_file():
    """cash-flow.md text, or None when it cannot be read — a missing file
    is a valid state, unlike a present one with no headings."""
    try:
        with open(cash_flow_file(), encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        # a stray byte would otherwise take the whole cash-flow report down,
        # and this file is edited by hand and by the agent
        return None


def _parse_section(text, heading):
    """{name: {key: value}} from one section of cash-flow.md, or None when
    the heading is absent. Only that section is read, so the notes elsewhere
    in the file are free prose and may use bullets of their own: inside the
    section a top-level '- ' bullet names an entry and its indented
    'key: value' bullets are that entry's fields."""
    head = re.search(rf"^{re.escape(heading)}\s*$", text, re.M)
    if not head:
        return None
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
    return out


def _read_sources():
    """({category: {payee: {key: value}}}, [problem]) from the two source
    sections of cash-flow.md, one per source category (_heading). A
    missing file is no problem at all — every source then rests on its own
    dates — but a file missing a heading is, since entries written outside
    it would be read by nobody."""
    text = _read_file()
    entries, problems = {}, []
    for cat in (MONTHLY_CATEGORY, PERIODIC_CATEGORY):
        found = _parse_section(text, _heading(cat)) if text is not None else {}
        if found is None:
            found = {}
            problems.append("cash-flow.md has no "
                            f"'{_heading(cat)}' heading, so no {cat} "
                            "entries were read")
        entries[cat] = found
    return entries, problems


def _read_upcoming():
    """{name: {key: value}} from the UPCOMING_HEADING section — the
    hand-listed one-time expenses ahead. A missing file or heading is
    simply no items."""
    return _parse_section(_read_file() or "", UPCOMING_HEADING) or {}


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
    """({entry: [cents]}, {entry: [note]}) for cash-flow.md entries carrying
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
                notes[name].append(f"cash-flow.md gives amount {tok!r}, "
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


def _source_rows(payments, entries, today):
    """[row] — one row per income source of one category, alphabetical by
    label: the payees with payments, plus the entries of that category's
    file section that matched nothing (n 0, no figure: "no deposit yet").
    A row carries name, label, freq, per_year, incomes, monthly, used,
    mean, reason, latest, silent, stopped, notes and n; per_year None
    means no frequency is known, so the source gets no monthly figure and
    stays out of the total."""
    entry_amounts, amount_notes = _entry_amounts(entries)
    payments = _claim_amounts(payments, entry_amounts)
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
                notes.append(f"cash-flow.md gives incomes "
                             f"{e['incomes']!r}, which is not a count")
        # several incomes under one payee interleave, so the gaps between
        # arrivals describe no single rhythm — two twice-yearly payments a month
        # apart leave gaps of 1 and 5 months, and a median of those means
        # nothing. Such a payee is read from its entry alone.
        detected = _detect(dates) if incomes == 1 else None
        freq, per_year, notes = _frequency(e, detected, notes)
        monthly = used = mean = None
        reason = ""
        if per_year:
            if incomes > 1:
                mean, used = _round_mean(amounts, incomes)
            else:
                mean, reason = _pick_payment(amounts)
            monthly = mean * per_year / 12
        latest = dates[-1]
        silent = (today - latest).days
        stopped = bool(per_year) and silent > STOPPED_INTERVALS * 365.25 / per_year
        rows.append({"name": payee, "label": e.get("label") or payee,
                     "freq": freq, "per_year": per_year, "incomes": incomes,
                     "monthly": monthly, "used": used, "mean": mean,
                     "reason": reason, "latest": latest, "silent": silent,
                     "stopped": stopped, "notes": notes, "n": len(pays)})
    for name, e in entries.items():
        if name in payments:
            continue
        freq, per_year, notes = _frequency(e, None, amount_notes.get(name, []))
        rows.append({"name": name, "label": e.get("label") or name,
                     "freq": freq, "per_year": per_year, "monthly": None,
                     "stopped": False, "notes": notes, "n": 0})
    rows.sort(key=lambda r: r["label"].lower())
    return rows


def _frequency(e, detected, notes):
    """(freq, per_year, notes) for one source: the entry's frequency when
    set and known, else the one detected from the dates, else none. A set
    rhythm beats the dates; when the two disagree on payments a year the
    notes say so. Names may differ while the rate agrees ('quarterly'
    against '4 per year'), so the check compares payments a year, not
    words."""
    notes = list(notes)
    want = e.get("frequency", "")
    set_year = _per_year(want) if want else None
    if want and set_year is None:
        notes.append(f"cash-flow.md gives frequency {want!r}, "
                     "which is not a frequency I know")
    if set_year:
        if detected and PER_YEAR[detected] != set_year:
            notes.append(f"cash-flow.md says {want}, "
                         f"the dates say {detected}")
        return want, set_year, notes
    if detected:
        return detected, PER_YEAR[detected], notes
    return "", None, notes


def _ago(days):
    if days < 60:
        return _plural(days, "day") + " ago"
    return _plural(round(days / 30.44), "month") + " ago"


def _source_lines(rows, detailed):
    """One entry per source row. Counted are sources with a known frequency
    that have not gone silent; every other source still prints, with the
    reason instead of a number. detailed=False is the regular report size:
    one line per source — label and monthly amount — with the sub-lines
    left out. The check notes print in both sizes; hiding a mistake would
    not fix it."""
    lines = []
    for r in rows:
        head = f"- {r['label']}"
        if detailed and r["freq"]:
            head += f" ({r['freq']})"
        if r["n"] == 0:
            lines.append(f"{head}: no deposit yet")
        elif r["stopped"]:
            lines.append(f"{head}: stopped")
            if detailed:
                lines.append(f"    last payment {r['latest'].isoformat()}, "
                             f"{_ago(r['silent'])} — not counted")
        elif r["per_year"] is None:
            lines.append(f"{head}: not estimable")
            if detailed:
                lines.append(f"    {_plural(r['n'], 'payment')}, latest "
                             f"{r['latest'].isoformat()} — set a frequency, "
                             f"or wait until it has {DETECT_MIN}")
        elif not detailed:
            lines.append(f"{head}: {_money(r['monthly'])} monthly")
        else:
            tail = f" ({r['reason']})" if r["reason"] else ""
            lines.append(f"{head}: {_money(r['monthly'])} monthly{tail}")
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
    return lines


def _income_section(ctx, detailed=True):
    """({category: estimate cents}, lines) — the monthly income estimate,
    then a subtotal and one entry per source for MONTHLY_CATEGORY and
    PERIODIC_CATEGORY in turn. The periodic subtotal is that income spread
    over 12 months, which its heading says."""
    totals, blocks = {}, []
    for cat in (MONTHLY_CATEGORY, PERIODIC_CATEGORY):
        rows = _source_rows(ctx["payments"][cat], ctx["source_file"][cat],
                            ctx["today"])
        totals[cat] = sum(r["monthly"] for r in rows
                          if r["monthly"] is not None and not r["stopped"])
        head = (f"{cat} income" if cat == MONTHLY_CATEGORY
                else f"{cat} income, spread per month")
        blocks.append(f"{head}: {_money(totals[cat])}")
        blocks += _source_lines(rows, detailed) or [
            f"- no payments yet in the {cat} income category"]
    lines = ["", f"Income estimate: {_money(sum(totals.values()))} monthly",
             ""] + blocks
    for problem in ctx["source_problems"]:
        lines.append(f"- check: {problem}")
    return totals, lines


# ---------------------------------------------------------------- upcoming

def _upcoming_rows(entries):
    """([row], total cents) — one row per Upcoming entry: name, cents
    (None when the amount is missing or not a dollar amount), month text,
    notes. The month is a best guess and prints only; no math reads it."""
    rows, total = [], 0
    for name, e in entries.items():
        notes = []
        cents = None
        a = e.get("amount")
        if a is None:
            notes.append("no amount — set one in cash-flow.md")
        elif not re.fullmatch(AMOUNT_RE, a.strip()):
            notes.append(f"cash-flow.md gives amount {a!r}, which is not "
                         "a dollar amount")
        else:
            cents = round(float(a.strip().lstrip("$")) * 100)
            total += cents
        month = e.get("month", "")
        if month:
            try:
                month = debts.month_name(_parse_month(month))
            except ValueError:
                notes.append(f"cash-flow.md gives month {month!r}, "
                             "not YYYY-MM")
                month = ""
        rows.append({"name": name, "cents": cents, "month": month,
                     "notes": notes})
    return rows, total


def _upcoming_section(rows, total):
    """The Estimated block's list of one-time expenses ahead; empty when
    the file lists none."""
    if not rows:
        return []
    lines = ["", f"Upcoming one-time expenses: {_money(total)}"]
    for r in rows:
        if r["cents"] is None:
            lines.append(f"- {r['name']}: no amount")
        else:
            tail = f" ({r['month']})" if r["month"] else ""
            lines.append(f"- {r['name']}: {_money(r['cents'])}{tail}")
        for note in r["notes"]:
            lines.append(f"    check: {note}")
    return lines


def _expense_figures(ctx, d, avg):
    """(rows, total, so_far) — the expense estimate's per-group figures:
    the average monthly spending, or the current month so far until one
    complete month exists. rows is [(name, cents)], uncategorized spending
    last. ONE_OFF_GROUPS never figure — no average exists for them, and a
    so-far figure would pass a one-time cost off as normal life; their
    expected costs are hand-listed under Upcoming."""
    src = d if avg is None else avg
    cats = ctx["cats"]
    rows, total = [], 0.0
    for grp in _estimate_groups(ctx):
        g = -sum(net for cid, net in src["cats"].items()
                 if cats[cid]["grp"] == grp)
        if round(g / 100):
            rows.append((grp, g))
            total += g
    if round(src["uncat_spend"] / 100):
        rows.append(("uncategorized", src["uncat_spend"]))
        total += src["uncat_spend"]
    return rows, total, avg is None


def _expense_estimate(ctx, d, avg):
    """The Estimated block's expense estimate, mirroring the income
    estimate: one line per expense group, the so-far flavor marked as
    such."""
    rows, total, so_far = _expense_figures(ctx, d, avg)
    if not rows:
        return []
    if so_far:
        head_tail, row_tail = " — so far this month, no average yet", ""
    else:
        head_tail = row_tail = " monthly"
    lines = ["", f"Expense estimate: {_money(total)}{head_tail}", ""]
    lines += [f"- {name}: {_money(g)}{row_tail}" for name, g in rows]
    return lines


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
    oneoff_ids = frozenset(
        cid for cid, c in cats.items() if c["grp"] in ONE_OFF_GROUPS
        or (c["grp"] in income_groups and c["name"] == ONE_OFF_INCOME))
    sources, problems = _read_sources()
    return {"cats": cats, "groups": groups, "income_groups": income_groups,
            "oneoff_ids": oneoff_ids,
            "first": first, "cur": cur, "today": today, "months": months,
            "payments": _fetch_income(conn, first),
            "cash": _cash_balance(conn),
            "source_file": sources, "source_problems": problems,
            "upcoming": _read_upcoming()}


def _window(ctx, upto):
    """Up to WINDOW complete months right before `upto`, oldest first."""
    return list(range(max(ctx["first"], upto - WINDOW), upto))


def _window_label(win):
    """The header's average span: its first month and its length, since it
    always ends with the last complete month before the report's month."""
    span = "1 year" if len(win) == 12 else _plural(len(win), "month")
    return f"averages calculated with data from {debts.month_name(win[0])} ({span})"


def _expense_groups(ctx):
    return [g["name"] for g in ctx["groups"] if not g["is_income"]]


def _estimate_groups(ctx):
    """The expense groups the expense estimate counts: no ONE_OFF_GROUPS."""
    return [g for g in _expense_groups(ctx) if g not in ONE_OFF_GROUPS]


# ---------------------------------------------------------------- rows

def _hist(ctx, upto, current, get):
    """The history row of one row: '  - Jul $x (+24%) · Jun $y (-8%)' —
    the two months before `upto`, newest first, '-' for a month before
    FIRST_MONTH (the row prints anyway, so a missing history is visible).
    Same shape as the history row on the Assets and Debt entries. Each
    percent compares `current` (cents) against that month and is left out
    when the month is missing or zero. get(month_data) returns the item's
    cents in that month."""
    parts = []
    for i in range(upto - 1, upto - 3, -1):
        label = calendar.month_abbr[i % 12 + 1]
        if i < ctx["first"]:
            parts.append(f"{label} -")
            continue
        v = get(ctx["months"][i])
        change = f" ({debts.pct(current, v)})" if v else ""
        parts.append(f"{label} {_money(v)}{change}")
    return "  - " + " · ".join(parts)


def _cat_row(name, actual, avg, mode, elapsed, hist=""):
    """The lines of one category or group row. mode: 'pace' (the estimate
    by today, 'est', plus the whole month), 'fixed' (the whole month
    alone), 'month' (the average and the percent off it), 'plain' (no
    average). hist, from _hist, prints underneath."""
    if mode == "plain":
        line = f"- {name}: {_money(actual)}"
    elif mode == "fixed":
        line = f"- {name}: {_money(actual)} ({_money(avg)} month)"
    elif mode == "pace":
        line = (f"- {name}: {_money(actual)} ({_money(avg * elapsed)} est, "
                f"{_money(avg)} month)")
    else:
        line = f"- {name}: {_money(actual)} {_vs_avg(actual, avg)}"
    return [line, hist] if hist else [line]


def _category_rows(ctx, grp, d, avg, mode, elapsed, hist_upto=None,
                   combine_personal=False):
    """One group's per-category rows, indented under its group row; a
    category prints when it or its average rounds to a dollar. mode and
    elapsed are the group row's. hist_upto (a month idx) adds the history
    row under every row. combine_personal joins this group's
    PERSONAL_CATEGORIES into one row per label, printed after the rest."""
    sign = 1 if grp in ctx["income_groups"] else -1
    lines = []
    joined = {}                 # label -> [category id], in first-seen order
    for cid, c in ctx["cats"].items():
        if c["grp"] != grp:
            continue
        if combine_personal and c["name"] in PERSONAL_CATEGORIES:
            joined.setdefault(PERSONAL_CATEGORIES[c["name"]], []).append(cid)
            continue
        actual = d["cats"].get(cid, 0) * sign
        a = (avg["cats"].get(cid, 0) if avg else 0) * sign
        if not round(actual / 100) and not round(a / 100):
            continue
        hist = ""
        if hist_upto is not None:
            hist = _hist(ctx, hist_upto, actual,
                         lambda md, cid=cid, sign=sign:
                         md["cats"].get(cid, 0) * sign)
        lines += _cat_row(c["name"], actual, a, mode, elapsed, hist)
    for label, ids in joined.items():
        actual = sum(d["cats"].get(cid, 0) for cid in ids) * sign
        a = (sum(avg["cats"].get(cid, 0) for cid in ids) if avg else 0) * sign
        if not round(actual / 100) and not round(a / 100):
            continue
        hist = ""
        if hist_upto is not None:
            hist = _hist(ctx, hist_upto, actual,
                         lambda md, ids=tuple(ids), sign=sign:
                         sum(md["cats"].get(cid, 0) for cid in ids) * sign)
        lines += _cat_row(label, actual, a, mode, elapsed, hist)
    return ["  " + line for line in lines]


# ---------------------------------------------------------------- debts

PAYMENT_GAP = 0.10   # a month paid this fraction off a listed payment is off


def _debt_average_of(ctx, win):
    """average(category name, first idx): a category's average monthly
    spending cents over the window months from first on (every window
    month when first is None), None when the budget has no such category
    or no month is left — where debts.py reads the payment of a debt with
    a category and no payment field. Months before a debt's first balance
    would pull its average down."""
    ids = {c["name"]: cid for cid, c in ctx["cats"].items()}

    def average(name, first):
        months = [i for i in win if first is None or i >= first]
        if name not in ids or not months:
            return None
        return -sum(ctx["months"][i]["cats"].get(ids[name], 0)
                    for i in months) / len(months)
    return average


def _running(plan, idx):
    """The debt still takes a payment in month idx."""
    return not plan["done"] and (plan["payoff"] is None or plan["payoff"] >= idx)


def _debt_average(ctx, avg, plans):
    """avg with every debt category set to the payments of its debts still
    running this month, and every hidden category set to zero: the
    averages the current month's estimate, pace rows and forecast read.
    A paid-off debt's old payments leave the estimate at once, where its
    category average would carry them for up to WINDOW months. A running
    debt with no known payment leaves its category's plain average."""
    if avg is None:
        return None
    running = {}
    for p in plans:
        if _running(p, ctx["cur"]) and p["monthly"] is None:
            continue            # payment unknown: the plain average stays
        paid = (p["monthly"] or 0) if _running(p, ctx["cur"]) else 0
        running[p["category"]] = running.get(p["category"], 0) + paid
    out = {**avg, "cats": dict(avg["cats"])}
    for cid, c in ctx["cats"].items():
        if c["hidden"]:
            out["cats"][cid] = 0
        elif c["name"] in running:
            out["cats"][cid] = -running[c["name"]]
    return out


def _debt_checks(ctx, plans):
    """Check items for the debts.md category fields: a category not in the
    budget outside EXCLUDED_GROUPS, and a listed payment missed by more than PAYMENT_GAP
    the same way in both of the last two complete months — a changed
    payment, a debt paid off early, or another debt's payments landing in
    its category. Months before the debt's first balance never count, and
    a debt at $0 or past its payoff month is not checked."""
    out = []
    ids = {c["name"]: cid for cid, c in ctx["cats"].items()}
    months = [i for i in (ctx["cur"] - 2, ctx["cur"] - 1) if i >= ctx["first"]]
    for p in plans:
        cid = ids.get(p["category"])
        if cid is None:
            out.append(f"{p['name']}: debts.md gives category "
                       f"{p['category']!r}, which is not a budget category "
                       f"outside {_join_and(EXCLUDED_GROUPS)}")
            continue
        if p["average"] or not p["monthly"] or len(months) < 2 \
                or p["first"] is None or months[0] < p["first"] \
                or not _running(p, months[1]):
            continue
        paid = [-ctx["months"][i]["cats"].get(cid, 0) for i in months]
        off = [(x - p["monthly"]) / p["monthly"] for x in paid]
        if all(o > PAYMENT_GAP for o in off) or all(o < -PAYMENT_GAP for o in off):
            out.append(f"{p['name']}: paid {_money(paid[0])} in "
                       f"{debts.month_name(months[0])} and {_money(paid[1])} in "
                       f"{debts.month_name(months[1])}, against "
                       f"{_money(p['monthly'])} monthly in debts.md")
    return out


def _payoff_steps(ctx, plans):
    """[(months from today, cents, first month without them)] — one step
    per payoff month, soonest first, holding the payments of every
    running debt paid off that month: they stop at the end of it. Only
    debts whose payment is in the estimate count: their category is in the
    budget, in a group the estimate counts (_estimate_groups), and not
    hidden."""
    counted = set(_estimate_groups(ctx))
    live = {c["name"] for c in ctx["cats"].values()
            if not c["hidden"] and c["grp"] in counted}
    ends = {}
    for p in plans:
        if _running(p, ctx["cur"]) and p["payoff"] is not None \
                and p["monthly"] and p["category"] in live:
            ends[p["payoff"]] = ends.get(p["payoff"], 0) + p["monthly"]
    left = 1 - ctx["today"].day / _days_in(ctx["cur"])
    return sorted((m - ctx["cur"] + left, cents, m + 1)
                  for m, cents in ends.items())


# ---------------------------------------------------------------- status view

def _forecast(ctx, income, exp_total, upcoming, steps):
    """The Forecast block: the CASH_ACCOUNTS balance, minus the upcoming
    total, against the expense estimate — first with the monthly income
    alone, then with the periodic income included. Never runs out while
    the income covers the expenses; otherwise the net spending eats the
    balance, month by month: each debt's payment (steps, from
    _payoff_steps) leaves the net spending once the debt is paid off."""
    bal, missing = ctx["cash"]
    lines = [""]
    for name in missing:
        lines.append(f"- account {name!r} not found — fix CASH_ACCOUNTS "
                     "in cash_flow.py")
    if missing:
        return lines
    head = f"Cash today: {_money(bal)} in {_join_and(CASH_ACCOUNTS)}."
    if upcoming:
        bal -= upcoming
        head += (f" Minus {_money(upcoming)} listed upcoming expenses = "
                 f"{_money(bal)} left.")
    lines.append(head)
    monthly = income[MONTHLY_CATEGORY]
    for label, est in (
            ("How long that cash lasts, with monthly income only:", monthly),
            ("Same, with periodic income added:",
             monthly + income[PERIODIC_CATEGORY])):
        burn = exp_total - est
        lines += ["", label]
        if round(burn / 100) <= 0:
            lines.append(f"- never runs out — {_money(-burn)} extra per month")
            continue
        if round(bal / 100) <= 0:
            lines.append("- not enough to cover expenses")
            continue
        line = f"- net spending {_money(burn)} monthly"
        t, left = 0.0, bal
        for at, cents, after in steps:
            if left <= burn * (at - t):
                break                   # the cash runs out before this payoff
            left -= burn * (at - t)
            t, burn = at, burn - cents
            if round(burn / 100) <= 0:
                break
            line += f", {_money(burn)} from {debts.month_name(after)}"
        if round(burn / 100) <= 0:
            line += (f", {_money(-burn)} extra per month from "
                     f"{debts.month_name(after)} — never runs out")
        else:
            months = t + left / burn
            when = ctx["today"] + timedelta(days=months * 30.44)
            line += (f", lasts about {months:.1f} months — runs out around "
                     f"{debts.month_name(_idx(when.year, when.month))}")
        lines.append(line)
    return lines


def _status(ctx, m_idx=None, detailed=False, categories=False,
            combine_personal=False):
    """The report for one month: the current one (m_idx None) or a past
    one."""
    today, cur, cats = ctx["today"], ctx["cur"], ctx["cats"]
    if m_idx is None:
        m_idx = cur
    if m_idx > cur:
        raise ValueError(f"{_label(m_idx)} is in the future")
    if m_idx < ctx["first"]:
        raise ValueError(f"data starts {FIRST_MONTH} — earlier months never count")
    current = m_idx == cur
    d = ctx["months"][m_idx]
    win = _window(ctx, m_idx)
    avg = _average([ctx["months"][i] for i in win],
                   ctx["oneoff_ids"]) if win else None
    income, spending = _totals(d, cats, ctx["income_groups"])
    hist_upto = m_idx if detailed else None
    as_of = today if current else \
        date(m_idx // 12, m_idx % 12 + 1, _days_in(m_idx))
    debt_average = _debt_average_of(ctx, win)
    plans = debts.plans(as_of, debt_average)

    if current:
        avg = _debt_average(ctx, avg, plans)
        day, days = today.day, _days_in(cur)
        elapsed = day / days
        head = _window_label(win) if win else "first month: no average yet"
        lines = [f"for {debts.month_name(cur)}, day {day} of {days} | {head}"]
        est, income_lines = _income_section(ctx, detailed)
        up_rows, up_total = _upcoming_rows(ctx["upcoming"])
        estimated = (["", "### Estimated"] + income_lines
                     + _expense_estimate(ctx, d, avg)
                     + _upcoming_section(up_rows, up_total))
        avg_income = avg_spending = None
    else:
        elapsed = 1.0
        head = _window_label(win) if win else "first month: no average"
        lines = [f"for {debts.month_name(m_idx)} | {head}"]
        avg_income, avg_spending = _avg_pair(ctx, win) if win else (None, None)

    def total(name, actual, average, get, fmt=_money):
        """One total line, the average on a past month with an average, the
        history row under it when detailed."""
        line = f"{name}: {fmt(actual)}"
        if average is not None:
            line += f" {_vs_avg(actual, average)}"
        lines.append(line)
        if detailed:
            lines.append(_hist(ctx, m_idx, actual, get))

    lines += ["", "### Income and spending so far" if current
              else "### Income and spending", ""]
    total("Income", income, avg_income,
          lambda md: _totals(md, cats, ctx["income_groups"])[0])
    income_mode = "month" if avg and not current else "plain"
    for cid, c in cats.items():
        if c["grp"] not in ctx["income_groups"]:
            continue
        actual = d["cats"].get(cid, 0)
        a = avg["cats"].get(cid, 0) if avg else 0
        if not round(actual / 100) and not round(a / 100):
            continue
        hist = _hist(ctx, m_idx, actual,
                     lambda md, cid=cid: md["cats"].get(cid, 0)) \
            if detailed else ""
        mode = "plain" if cid in ctx["oneoff_ids"] else income_mode
        lines += _cat_row(c["name"], actual, a, mode, elapsed, hist)
    total("Expenses", spending, avg_spending,
          lambda md: _totals(md, cats, ctx["income_groups"])[1])
    for grp in _expense_groups(ctx):
        actual = -_group_net(d, cats, grp)
        avg_g = -sum(net for cid, net in avg["cats"].items()
                     if cats[cid]["grp"] == grp) if avg else 0
        if not round(actual / 100) and not round(avg_g / 100):
            continue
        if not avg or grp in ONE_OFF_GROUPS:
            mode = "plain"
        elif not current:
            mode = "month"
        elif grp in FIXED_GROUPS:
            mode = "fixed"
        else:
            mode = "pace"
        hist = _hist(ctx, m_idx, actual,
                     lambda md, grp=grp:
                     -_group_net(md, cats, grp)) if detailed else ""
        lines += _cat_row(grp, actual, avg_g, mode, elapsed, hist)
        if categories:
            lines += _category_rows(ctx, grp, d, avg, mode, elapsed,
                                    hist_upto, combine_personal)
    if not current:
        # no average: a percent off a negative average reads backwards
        total("Leftover", income - spending, None,
              lambda md: (lambda t: t[0] - t[1])(
                  _totals(md, cats, ctx["income_groups"])),
              fmt=_signed)

    if current:
        exp_total = _expense_figures(ctx, d, avg)[1]
        lines += estimated
        lines += ["", "### Forecast"]
        lines += _forecast(ctx, est, exp_total, up_total,
                           _payoff_steps(ctx, plans))
    block, checks = debts.status_block(as_of, detailed, debt_average)
    if current:
        checks += _debt_checks(ctx, plans)
    if block:
        lines += ["", "### Assets and Debt", "", block]
    if checks:
        lines += ["", "### Check"] + [f"- {c}" for c in checks]
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
        lines.append(f"- {debts.month_name(i)}: income {_money(income)} | spending "
                     f"{_money(spending)} | leftover "
                     f"{_signed(income - spending)}")
    win = _window(ctx, ctx["cur"])
    avg_income, avg_spending = _avg_pair(ctx, win)
    span = debts.month_name(win[0]) if len(win) == 1 else \
        f"{debts.month_name(win[0])} to {debts.month_name(win[-1])}"
    lines.append(f"- average: {_plural(len(win), 'month')} ({span}): income {_money(avg_income)} | "
                 f"spending {_money(avg_spending)} | leftover "
                 f"{_signed(avg_income - avg_spending)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- entry points

def build_report(month="", today=None, detailed=False, categories=False,
                 combine_personal=False):
    """The current month's status (month empty or the current one) or a
    past month (YYYY-MM) vs its average. combine_personal joins
    PERSONAL_CATEGORIES into one row each, so no per-person figure is
    named. Raises ValueError on bad arguments."""
    ctx = _ctx(api_cache.db(), today or date.today())
    month = (month or "").strip()
    m_idx = _parse_month(month) if month else None
    return _status(ctx, m_idx, detailed, categories, combine_personal)


def check_report(today=None):
    """The current month's Check items alone — the daily email's Check
    section: the finance-file problems and the debt payment checks. []
    when there is nothing to say."""
    ctx = _ctx(api_cache.db(), today or date.today())
    plans = debts.plans(ctx["today"],
                        _debt_average_of(ctx, _window(ctx, ctx["cur"])))
    return (debts.status_block(ctx["today"], False)[1]
            + _debt_checks(ctx, plans))


def history_report(months=6, today=None):
    """One line per complete month, newest first, plus the average line."""
    if not isinstance(months, int) or not 1 <= months <= HISTORY_CAP:
        raise ValueError(f"months must be 1-{HISTORY_CAP}")
    return _history(_ctx(api_cache.db(), today or date.today()), months)


def main():
    args = sys.argv[1:]
    try:
        if "--history" in args:
            i = args.index("--history")
            print(history_report(int(args[i + 1])))
            return
        month = ""
        detailed = categories = combine_personal = False
        for arg in args:
            if arg == "--detailed":
                detailed = True
            elif arg == "--categories":
                categories = True
            elif arg == "--combine-personal":
                combine_personal = True
            else:
                month = arg
        print(build_report(month=month, detailed=detailed,
                           categories=categories,
                           combine_personal=combine_personal))
    except (ValueError, IndexError) as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
