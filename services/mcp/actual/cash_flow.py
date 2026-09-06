#!/usr/bin/env python3
"""cash_flow — money in, money out, vs the monthly average (read-only).

Two plain-text views over the api-cache SQLite copy, pulled fresh from the
server when its last download is older than 10 minutes (api_cache.pull_api_cache_if_stale):

  build_report()                current-month status: so far, and once one
                                complete month exists, projection and streak
  build_report(month="2026-08") that finished month vs its average
  history_report(6)             one line per complete month, newest first
  check_report()                the current month's Check items alone, as
                                a list — the daily email's Check section

build_report also takes detailed — the report size. False (regular, the
default) prints income sources one line each, no history rows anywhere,
and debts at balance and rate only; True prints everything. categories
adds each group's per-category rows under its group row.

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
source's rhythm comes from vault/docs/finances/cash-flow.md when set
there, otherwise from its own gaps once it has DETECT_MIN payments, and a
source with neither gets no figure. That file also carries a display name
and, for a payee covering more than one income, how many — such a payee uses
the mean of its last whole round of payments, since interleaved sizes make
consecutive comparison meaningless.
An entry with an amount is its own source: it claims every deposit of
exactly that value, whatever the payee, and its name is the display name;
old values stay listed so past deposits keep their source.
An entry with an estimate uses that value as a stand-in payment while it
has no deposits: the value times its file-set frequency, printed as a
placeholder; the first real deposit replaces it.
A source silent for STOPPED_INTERVALS of its own gaps leaves the total —
a payment's value is good for one cycle plus slack for a late arrival. The
status view prints the estimate and one line per source; a past month shows
the income that actually arrived instead, so no estimate appears there.

The projection counts that income estimate, ONE_OFF_GROUPS at what was
actually spent (one-time flows, so no future figure exists for them), and
FIXED_GROUPS at their full monthly figure (lumpy flows take no pace
judgment); every other group runs at pace:
actual so far + average x remaining share of the month, even spread assumed.
It rests on the average, so until one complete month exists the status view
shows what has happened so far and nothing about month end. ledger_month()
returns one complete month's cents for the monthly-balance file that
finance_jobs.py writes.
The status view runs in ### sections: Estimated — the projection, the
income estimate, the expense estimate (the average monthly spending, one
line per group; this month so far until one complete month exists) and the
upcoming one-time expenses; Actual — the income total with its category
rows, the expense total with its group rows (each with its category rows
when categories is set), tracked categories. In a
detailed report every Actual row carries a history row
underneath with the two previous
months and the percent change against each ('  - Jul $x (+24%) · Jun $y
(-8%)'; a month before FIRST_MONTH prints '-', the percent is left out
when the month is zero); Forecast —
how long the money lasts: CHECKING_ACCOUNT's balance (every transaction in
the account, transfers and starting balance included) against the burn
(expense estimate minus income estimate) — never runs out while the
estimate covers itself; Assets and Debt —
asset balances and change out of vault/docs/finances/assets.md, then
per-tier debt balances, change, payoff and interest math out of
vault/docs/finances/debts.md, when those files exist; Check — uncategorized
money and finance-file problems, printed only when there is something to
say.
A past month is the same report with the month's final numbers: the Actual
rows carry the average column instead of the pace, a leftover total, the
OUTLIERS_N categories furthest from their average and the streak; Estimated
and Forecast are left out, and Assets and Debt reads the files as of that
month's end.
Upcoming one-time expenses are hand-listed in CASH_FLOW_FILE's Upcoming
section (amount, optional best-guess month, display only); each prints in
the Estimated block. The entry is
deleted by hand once paid — the payment itself goes to a ONE_OFF_GROUPS
category: that month's totals show it, no average ever does.
A green month is one whose income covered spending. ONE_OFF_GROUPS hold
real outside-world money that is not normal life (an asset bought to sell
on, a planned one-time payment): it counts in every month's income and
spending — green/red and history tell the truth — but stays out of every
average, so the pace rows, the average columns, the outliers and the
runs-out divisor never see it. Excluded everywhere: transfers,
off-budget accounts, starting-balance rows, EXCLUDED_GROUPS. Uncategorized
money is counted in the totals and named in the Check section, split by
sign; a deleted category's
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
import debts  # noqa: E402
import api_cache  # noqa: E402

FIRST_MONTH = "2026-08"    # first full import month; earlier months never count
WINDOW = 12                # complete months in the average, at most
EXCLUDED_GROUPS = ["Ignored"]   # group names left out of every number
FIXED_GROUPS = ["Fixed"]   # groups projected at average, no pace judgment
ONE_OFF_GROUPS = ["One-off"]   # counted in month totals, in no average; projected as spent
# category name -> the row its amount joins when a report is built with
# combine_personal: those categories print as one row under that label
# instead of their own, so no per-person figure is named. The group
# total is unchanged, so the rows under it still add up to it.
PERSONAL_CATEGORIES = {"Morgan One-off": "Personal",
                       "Riley One-off": "Personal",
                       "Alex": "Personal",
                       "Morgan": "Personal",
                       "Riley": "Personal",
                       "Morgan Recurring": "Personal Subscription",
                       "Riley Recurring": "Personal Subscription"}
CHECKING_ACCOUNT = "StarOne Checking"   # account the runs-out section reads
OUTLIERS_N = 3
HISTORY_CAP = 24

# --- income sources ---
# One payee inside the INCOME_SOURCE_CATEGORY category is one source — except
# deposits claimed by an entry's amount field, which form that entry's own
# source. A source's monthly value is its standing payment (_pick_payment:
# outliers skipped, confirmed changes adopted) times its payments a year,
# over 12: a year holds a whole number of payments and a month does not, so
# counting months mis-states every rhythm that does not divide into one (26
# paychecks a year is 2.167 a month, never 2 or 3). A payee carrying several
# incomes uses the mean of its last whole round instead.
CASH_FLOW_FILE = os.path.expanduser("~/Iris/vault/docs/finances/cash-flow.md")
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


# the YYYYMMDD integer Actual stores a date as, both ways, and the bank's
# cents; shared with oddities.py, server.py and the actions service
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


def _join_and(names):
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------- reading the copy

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


def _average(months, skip=frozenset()):
    """Mean of the given month dicts, same shape, float cents; skip
    (the ONE_OFF_GROUPS category ids) stays out — averages describe normal
    life. {} months is the caller's first-month case and never reaches
    here."""
    n = len(months)
    ids = {cid for m in months for cid in m["cats"]} - skip
    return {"cats": {cid: sum(m["cats"].get(cid, 0) for m in months) / n
                     for cid in ids},
            "uncat_inc": sum(m["uncat_inc"] for m in months) / n,
            "uncat_spend": sum(m["uncat_spend"] for m in months) / n}


def _totals(d, cats, income_groups, skip_groups=()):
    """(income, spending) cents, spending positive. Categorized money counts
    net — a refund reduces its category's spending — uncategorized by sign.
    skip_groups leaves those groups out (the average flavor)."""
    income = d["uncat_inc"]
    spending = d["uncat_spend"]
    for cid, net in d["cats"].items():
        if cats[cid]["grp"] in skip_groups:
            continue
        if cats[cid]["grp"] in income_groups:
            income += net
        else:
            spending += -net
    return income, spending


def _avg_pair(ctx, win):
    """(income, spending) average cents over the window, ONE_OFF_GROUPS left
    out — the average describes normal life, which they are not."""
    pairs = [_totals(ctx["months"][i], ctx["cats"], ctx["income_groups"],
                     ONE_OFF_GROUPS) for i in win]
    n = len(win)
    return sum(p[0] for p in pairs) / n, sum(p[1] for p in pairs) / n


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
UPCOMING_HEADING = "## Upcoming"


def _read_file():
    """CASH_FLOW_FILE text, or None when it cannot be read — a missing file
    is a valid state, unlike a present one with no headings."""
    try:
        with open(CASH_FLOW_FILE, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        # a stray byte would otherwise take the whole cash-flow report down,
        # and this file is edited by hand and by the agent
        return None


def _parse_section(text, heading):
    """{name: {key: value}} from one section of CASH_FLOW_FILE, or None when
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
    """({payee: {key: value}}, problem) from the SOURCES_HEADING section.
    A missing file is no problem at all — every source then rests on its own
    dates — but a file with no such heading is, since entries written
    outside it would be read by nobody."""
    text = _read_file()
    if text is None:
        return {}, ""
    entries = _parse_section(text, SOURCES_HEADING)
    if entries is None:
        return {}, (f"{os.path.basename(CASH_FLOW_FILE)} has no "
                    f"'{SOURCES_HEADING}' heading, so no source entries "
                    "were read")
    return entries, ""


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
    """({entry: [cents]}, {entry: [note]}) for CASH_FLOW_FILE entries carrying
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


def _placeholder_row(name, e, notes):
    """Row for an entry with an estimate field and no payments yet: the
    estimate value stands in for a payment until the first deposit arrives.
    The frequency must come from the file — there are no dates to read one
    from — so without a valid one the row prints unestimable."""
    notes = list(notes)
    value = e["estimate"].strip()
    freq = e.get("frequency", "")
    per_year = _per_year(freq) if freq else None
    if freq and per_year is None:
        notes.append(f"cash-flow.md gives frequency {freq!r}, "
                     "which is not a frequency I know")
    monthly = None
    if not re.fullmatch(AMOUNT_RE, value):
        notes.append(f"cash-flow.md gives estimate {value!r}, "
                     "which is not a dollar amount")
    elif not freq:
        notes.append("estimate set but no frequency — set one in "
                     "cash-flow.md")
    elif per_year:
        monthly = round(float(value.lstrip("$")) * 100) * per_year / 12
    return {"name": name, "label": e.get("label") or name,
            "freq": freq if per_year else "", "per_year": per_year,
            "monthly": monthly, "stopped": False, "placeholder": True,
            "notes": notes}


def _source_rows(ctx):
    """([row], [missing]) — one row per income source, alphabetical by
    label; missing = (name, by_amount, notes) for the CASH_FLOW_FILE entries
    that matched nothing in the budget and carry no estimate — an entry with
    one becomes a placeholder row instead. A row carries name, label, freq,
    per_year, incomes, monthly, used, mean, latest, stopped and note
    (a placeholder row only label, freq, per_year, monthly, stopped, notes);
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
                notes.append(f"cash-flow.md gives incomes "
                             f"{e['incomes']!r}, which is not a count")
        # several incomes under one payee interleave, so the gaps between
        # arrivals describe no single rhythm — two twice-yearly grants a month
        # apart leave gaps of 1 and 5 months, and a median of those means
        # nothing. Such a payee is read from its entry alone.
        detected = _detect(dates) if incomes == 1 else None
        want = e.get("frequency", "")
        set_year = _per_year(want) if want else None
        if want and set_year is None:
            notes.append(f"cash-flow.md gives frequency {want!r}, "
                         "which is not a frequency I know")
        if set_year:
            freq, per_year = want, set_year
            # names may differ while the rate agrees ('quarterly' against
            # '4 per year'), so the check compares payments a year, not words
            if detected and PER_YEAR[detected] != set_year:
                notes.append(f"cash-flow.md says {want}, "
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
                     "stopped": stopped, "placeholder": False,
                     "notes": notes, "n": len(pays)})
    missing = []
    for name, e in entries.items():
        if name in payments:
            continue
        if "estimate" in e:
            rows.append(_placeholder_row(name, e, amount_notes.get(name, [])))
        else:
            missing.append((name, name in entry_amounts,
                            amount_notes.get(name, [])))
    rows.sort(key=lambda r: r["label"].lower())
    return rows, missing


def _ago(days):
    if days < 60:
        return _plural(days, "day") + " ago"
    return _plural(round(days / 30.44), "month") + " ago"


def _income_section(ctx, detailed=True):
    """(estimate cents, lines) — the monthly income estimate and one entry
    per source. Counted are sources with a known frequency that have not gone
    silent; every other source still prints, with the reason instead of a
    number. detailed=False is the regular report size: one line per source —
    label and monthly amount, '(est)' on a placeholder — with the sub-lines
    left out. The check notes print in both sizes; hiding a mistake would
    not fix it."""
    rows, missing = _source_rows(ctx)
    total = sum(r["monthly"] for r in rows
                if r["monthly"] is not None and not r["stopped"])
    lines = ["", f"Income estimate: {_money(total)} monthly", ""]
    if not rows and not missing:
        lines.append(f"- no payments yet in the {INCOME_SOURCE_CATEGORY} "
                     "income category")
    for r in rows:
        if r["placeholder"]:
            if r["monthly"] is None:
                lines.append(f"- {r['label']}: not estimable")
            elif not detailed:
                lines.append(f"- {r['label']}: {_money(r['monthly'])} "
                             "monthly (est)")
            else:
                lines.append(f"- {r['label']} ({r['freq']}): "
                             f"{_money(r['monthly'])} monthly (placeholder)")
                lines.append("    no payments yet — the estimate field "
                             "stands in until the first deposit")
        elif r["stopped"]:
            if not detailed:
                lines.append(f"- {r['label']}: stopped")
            else:
                lines.append(f"- {r['label']} ({r['freq']}): stopped")
                lines.append(f"    last payment {r['latest'].isoformat()}, "
                             f"{_ago(r['silent'])} — not counted")
        elif r["per_year"] is None:
            lines.append(f"- {r['label']}: not estimable")
            if detailed:
                lines.append(f"    {_plural(r['n'], 'payment')}, latest "
                             f"{r['latest'].isoformat()} — set a frequency, "
                             f"or wait until it has {DETECT_MIN}")
        elif not detailed:
            lines.append(f"- {r['label']}: {_money(r['monthly'])} monthly")
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
                         "cash-flow.md")
        for note in notes:
            lines.append(f"    check: {note}")
    if ctx["source_problem"]:
        lines.append(f"- check: {ctx['source_problem']}")
    return total, lines


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
                _parse_month(month)
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
    for grp in _expense_groups(ctx):
        if grp in ONE_OFF_GROUPS:
            continue
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
    oneoff_ids = frozenset(cid for cid, c in cats.items()
                           if c["grp"] in ONE_OFF_GROUPS)
    sources, problem = _read_sources()
    return {"cats": cats, "groups": groups, "income_groups": income_groups,
            "oneoff_ids": oneoff_ids,
            "first": first, "cur": cur, "today": today, "months": months,
            "payments": _fetch_income(conn, first),
            "checking": _checking_balance(conn),
            "source_file": sources, "source_problem": problem,
            "upcoming": _read_upcoming()}


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
        pct = f" ({round((current - v) / abs(v) * 100):+d}%)" if v else ""
        parts.append(f"{label} {_money(v)}{pct}")
    return "  - " + " · ".join(parts)


def _cat_row(name, actual, avg, mode, elapsed, hist=""):
    """The lines of one category or group row. mode: 'pace' (expected by
    now plus the whole month), 'fixed' (the whole month alone), 'month'
    (average column), 'plain' (no average). hist is the history row from
    _hist, printed underneath."""
    if mode == "plain":
        line = f"- {name}: {_money(actual)}"
    elif mode == "fixed":
        line = f"- {name}: {_money(actual)} | month {_money(avg)}"
    elif mode == "pace":
        line = (f"- {name}: {_money(actual)} | expected by now "
                f"{_money(avg * elapsed)} month {_money(avg)}")
    else:
        line = f"- {name}: {_money(actual)} | average {_money(avg)}"
    return [line, hist] if hist else [line]


def _uncat_amounts(d):
    parts = [f"{_money(d['uncat_spend'])} spending"]
    if round(d["uncat_inc"] / 100):
        parts.append(f"{_money(d['uncat_inc'])} income")
    return ", ".join(parts)


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


# ---------------------------------------------------------------- status view

def _projection(ctx, d, avg, win, elapsed, est):
    """The month-end block, average in hand: income from the sources,
    ONE_OFF_GROUPS at what was actually spent, FIXED_GROUPS at their
    full average, every other group at pace."""
    cats = ctx["cats"]
    p_spending = d["uncat_spend"] + avg["uncat_spend"] * (1 - elapsed)
    for grp in _expense_groups(ctx):
        avg_g = -sum(net for cid, net in avg["cats"].items()
                     if cats[cid]["grp"] == grp)
        if grp in ONE_OFF_GROUPS:
            p_spending += -_group_net(d, cats, grp)
        elif grp in FIXED_GROUPS:
            p_spending += avg_g
        else:
            p_spending += -_group_net(d, cats, grp) + avg_g * (1 - elapsed)
    avg_income, avg_spending = _avg_pair(ctx, win)
    p_leftover = est - p_spending
    run = _streak(_leftovers(ctx, ctx["cur"]))
    color = "green" if p_leftover >= 0 else "red"
    fixed = _join_and([g for g in _expense_groups(ctx) if g in FIXED_GROUPS])
    spent = _join_and([g for g in _expense_groups(ctx)
                       if g in ONE_OFF_GROUPS])
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


def _forecast(ctx, est, exp_total):
    """The Forecast block: CHECKING_ACCOUNT against the estimated income and
    expenses. Never runs out while the estimate covers itself; otherwise the
    net spending eats the balance."""
    bal = ctx["checking"]
    if bal is None:
        return ["", f"How long money lasts with current balance and "
                    "estimated income and expenses:",
                f"- account {CHECKING_ACCOUNT!r} not found — fix "
                "CHECKING_ACCOUNT in cash_flow.py"]
    burn = exp_total - est
    lines = ["", "How long money lasts with current balance and estimated "
                 "income and expenses:"]
    if round(burn / 100) <= 0:
        lines.append(f"- never runs out — {_money(-burn)} extra per month")
    elif round(bal / 100) <= 0:
        lines.append("- already out")
    else:
        months = bal / burn
        when = ctx["today"] + timedelta(days=months * 30.44)
        lines.append(f"- net spending {_money(burn)} monthly, balance "
                     f"{_money(bal)} lasts about {months:.1f} months — "
                     f"runs out around {when.year:04d}-{when.month:02d}")
    return lines


def _status(ctx, m_idx=None, detailed=False, categories=False,
            combine_personal=False):
    """The report for one month — the current one (m_idx None) or a past
    one — in ### sections. Current month: Estimated (projection, income
    estimate, expense estimate, upcoming), Actual, Forecast (how long the
    money lasts against the estimated income and expenses), Assets and
    Debt (debts.py), Check. The projection and the streak need a complete
    month behind them; until then the expense estimate falls back to this
    month so far, marked as such. Past month: the Actual section carries
    the month's final numbers with the average column, then the categories
    furthest over and under their average and the streak; Estimated and
    Forecast are left out (nothing is ahead of a finished month), and
    Assets and Debt shows the files as of that month's end.

    Actual: the income total with its category rows, the expense total
    with its group rows, tracked categories; a past month adds the
    leftover. categories adds each group's per-category rows under its
    group row; combine_personal then joins the per-person ones into a
    "Personal" and a "Personal Subscription" row.
    detailed=False is the regular report size: income sources
    one line each, no history rows anywhere, debts at balance and rate
    only."""
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

    if current:
        day, days = today.day, _days_in(cur)
        elapsed = day / days
        head = _window_label(win) if win else "first month: no average yet"
        lines = [f"{_label(cur)}, day {day} of {days} | {head}"]
        est, income_lines = _income_section(ctx, detailed)
        up_rows, up_total = _upcoming_rows(ctx["upcoming"])
        lines += ["", "### Estimated"]
        if win:
            lines += _projection(ctx, d, avg, win, elapsed, est)
        lines += income_lines
        lines += _expense_estimate(ctx, d, avg)
        lines += _upcoming_section(up_rows, up_total)
        avg_income = avg_spending = None
    else:
        elapsed = 1.0
        head = _window_label(win) if win else "first month: no average"
        lines = [f"Month — {_label(m_idx)} | {head}"]
        avg_income, avg_spending = _avg_pair(ctx, win) if win else (None, None)

    def total(name, actual, average, get, fmt=_money):
        """One total line, the average column on a past month with an
        average, the history row under it when detailed."""
        line = f"{name}: {fmt(actual)}"
        if average is not None:
            line += f" | average {fmt(average)}"
        lines.append(line)
        if detailed:
            lines.append(_hist(ctx, m_idx, actual, get))

    lines += ["", "### Actual", ""]
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
        lines += _cat_row(c["name"], actual, a, income_mode, elapsed, hist)
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
        left_avg = avg_income - avg_spending if avg else None
        total("Leftover", income - spending, left_avg,
              lambda md: (lambda t: t[0] - t[1])(
                  _totals(md, cats, ctx["income_groups"])),
              fmt=_signed)
    if not current:
        lines += _outliers_section(ctx, d, avg, combine_personal)
        lines += ["", _streak_line(ctx, m_idx)]

    if current:
        exp_total = _expense_figures(ctx, d, avg)[1]
        lines += ["", "### Forecast"]
        lines += _forecast(ctx, est, exp_total)
        as_of = today
    else:
        as_of = date(m_idx // 12, m_idx % 12 + 1, _days_in(m_idx))
    block, file_checks = debts.status_block(as_of, detailed)
    if block:
        lines += ["", "### Assets and Debt", "", block]
    checks = _checks(d, file_checks)
    if checks:
        lines += ["", "### Check"] + [f"- {c}" for c in checks]
    return "\n".join(lines)


def _checks(d, file_checks):
    """The Check items: uncategorized money, then the finance-file
    problems debts.status_block found."""
    checks = []
    if d["n_uncat"]:
        checks.append(f"uncategorized: {_plural(d['n_uncat'], 'transaction')}"
                      f" ({_uncat_amounts(d)})")
    return checks + file_checks


def _outliers_section(ctx, d, avg, combine_personal=False):
    """A past month's categories furthest from their average, the OUTLIERS_N
    largest gaps; nothing without an average. ONE_OFF_GROUPS categories
    have no average, so no over/under. combine_personal weighs the
    per-person categories as one entry per PERSONAL_CATEGORIES label, named
    by that label, so no per-person figure is named here either."""
    if not avg:
        return []
    cats = ctx["cats"]
    deltas = []
    joined = {}                 # label -> its categories' summed gap
    for cid, c in cats.items():
        if cid in ctx["oneoff_ids"]:
            continue
        sign = 1 if c["grp"] in ctx["income_groups"] else -1
        delta = (d["cats"].get(cid, 0) - avg["cats"].get(cid, 0)) * sign
        if combine_personal and c["name"] in PERSONAL_CATEGORIES:
            label = PERSONAL_CATEGORIES[c["name"]]
            joined[label] = joined.get(label, 0) + delta
        elif abs(round(delta / 100)):
            deltas.append((abs(delta), delta, c["name"]))
    for label, delta in joined.items():
        if abs(round(delta / 100)):
            deltas.append((abs(delta), delta, label))
    deltas.sort(reverse=True)
    if not deltas:
        return []
    lines = ["", "Outliers vs average:"]
    for _, delta, name in deltas[:OUTLIERS_N]:
        word = "over" if delta > 0 else "under"
        lines.append(f"- {name}: {_money(abs(delta))} {word}")
    return lines


def _streak_line(ctx, m_idx):
    """A past month's green/red verdict, the streak it ends, and the green
    tally over the last WINDOW complete months up to it."""
    leftovers = _leftovers(ctx, m_idx + 1)
    lastn = leftovers[-WINDOW:]
    tally = (f"green: {sum(1 for v in lastn if v >= 0)} of last "
             f"{_plural(len(lastn), 'month')}")
    if leftovers[-1] >= 0:
        return f"Streak: green month — {_streak(leftovers)} in a row; {tally}."
    return f"Streak: red month; {tally}."


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
    avg_income, avg_spending = _avg_pair(ctx, win)
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
    month (YYYY-MM) — the numbers finance_jobs.py caches in the
    monthly-balance file. Raises ValueError for the current month, a future
    one, or one before FIRST_MONTH."""
    ctx = _ctx(api_cache.db(), today or date.today())
    m_idx = _parse_month(month)
    if m_idx >= ctx["cur"]:
        raise ValueError(f"{month} is not a complete month yet")
    if m_idx < ctx["first"]:
        raise ValueError(f"data starts {FIRST_MONTH} — earlier months never count")
    return _ledger_month(ctx, m_idx)


# ---------------------------------------------------------------- entry points

def build_report(month="", today=None, detailed=False, categories=False,
                 combine_personal=False):
    """The current month's status (month empty or the current one) or a
    past month (YYYY-MM) vs its average. detailed is the report size:
    False (regular) hides the income sub-lines, every history row and the
    debt facts. categories adds each group's per-category rows;
    combine_personal joins the per-person ones (PERSONAL_CATEGORIES) into
    one row each, so no per-person figure is named. Raises ValueError on
    bad arguments."""
    ctx = _ctx(api_cache.db(), today or date.today())
    month = (month or "").strip()
    m_idx = _parse_month(month) if month else None
    return _status(ctx, m_idx, detailed, categories, combine_personal)


def check_report(today=None):
    """The current month's Check items alone — the daily email's Check
    section: uncategorized money and finance-file problems. [] when there
    is nothing to say."""
    today = today or date.today()
    ctx = _ctx(api_cache.db(), today)
    _, file_checks = debts.status_block(today, False)
    return _checks(ctx["months"][ctx["cur"]], file_checks)


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
