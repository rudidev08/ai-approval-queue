#!/usr/bin/env python3
"""Odd-charge rules over the api-cache copy — one module behind two readers:
the daily report email's Outliers section (../../ops/finance_jobs.py)
and the actions page's oddities list in the finance area
(../../ops/finance.py). stdlib only.

Fixed rules with explicit thresholds; every flag prints, no model keep/drop
(when a model was asked to keep or drop flags it dropped them at random). A
noisy rule is fixed here, with a number.

Every rule reads one charge against its payee's history, never against
today: the window only picks the candidates (spending dated in the last
ODD_WINDOW_DAYS days, or a whole past month for that month's report), and
`ids` adds charges outside it. A charge in the
page's queue is therefore re-read on every poll and gets the same flags
until the history itself changes — a twin deleted, the row parked in an
excluded group — at which point the flags go and the page drops it.

Rules, in output order:

  amount       a charge off its payee's own pattern; no dollar floor or
               ceiling, the payee's past is the only measure. The past is
               the payee's charges of the AMOUNT_WINDOW_DAYS days before
               the charge's date — never the same day, so two same-day
               charges are judged alike and a queued charge re-read
               later still sees the same past. Its long history
               picks the mode; its newest charge sets the bar.
               Steady payee (at least half of the steps from one past
               charge to the next stay within AMOUNT_SLACK): a charge
               more than AMOUNT_SLACK away from the newest past charge,
               up or down. A changed level flags once: the next charge
               at the new level is judged against it, and one odd step
               in a long steady history does not change the mode.
               Any other payee: a charge above its highest past charge
               by more than AMOUNT_SLACK. Drops never flag here
  duplicate    same payee, same amount, DUP_WITHIN_DAYS days apart — the
               pair is reported once, on its newest charge
  off_schedule a monthly payee (MONTHLY_MIN_HISTORY prior charges, all
               within MONTHLY_DAY_SLACK days of one day of the month, the
               first and last at least TOO_SOON_DAYS apart)
               charged again less than TOO_SOON_DAYS after its last charge,
               or more than OFF_DAY_DAYS from its usual day. Skipped when
               the duplicate rule already flagged the charge
  new_payee    the payee's first charge ever is within ODD_WINDOW_DAYS days
               before this one; identical lines collapse (a new payee with
               two different amounts still gets two)

income_marks judges the window's deposits in the income source categories
(cash_flow.MONTHLY_CATEGORY, PERIODIC_CATEGORY) with the cash-flow
report's own source maths, one mark per deposit for the daily email's
transaction lines: "routine: ..." when the deposit is the source's usual
amount (within cash_flow.INCOME_TOLERANCE of its standing payment) or the
source has no standing payment yet, "flag: ..." when it is off that amount
or is a payee's only deposit. A payee carrying several incomes gets no
amount judgment: its standing figure is a mean of different incomes.

Charges in cash_flow.EXCLUDED_GROUPS (money the cash-flow report never
counts) and cash_flow.ONE_OFF_GROUPS (counted, but in no average) are left
out: both groups describe money that is not normal life. Deleting the
category or the group ends the exclusion.

The budget app's rules say what is expected. A rule in the shape rules.py reads
(payee is X or imported payee contains T, optionally notes contain T, an
amount, and a monthly or yearly repeat — the rule tools, the finance area's
rule options, or the budget app's web UI) that has an amount condition makes
matching charges expected. A charge matching such a rule's payee or
imported payee text, its notes text (any case), its amount (and its date,
when it has one, within the budget app's own windows: 7.5% on the amount, one day
early to two days late on the date) is expected: the amount, off_schedule
and new_payee rules skip it, the duplicate rule still runs. A charge
matching all but the date is flagged off_schedule ("off its expected
date"); the other rules skip it too. The amount's sign follows the budget app: a
rule with the inflow or outflow option compares the magnitude on that side
of zero, any other value is signed. Rules of another shape, or with no
amount condition, say nothing here.
"""

import pathlib
import statistics
import sys
from datetime import date, timedelta

sys.path.append(str(pathlib.Path(__file__).resolve().parent))
import cash_flow  # noqa: E402
import rules  # noqa: E402

ODD_WINDOW_DAYS = 7        # candidates come from the last 7 days
AMOUNT_SLACK = 0.05        # 5%: a step this small is steady; a change past it flags
AMOUNT_WINDOW_DAYS = 365   # the past a charge is judged against: the last year
DUP_WITHIN_DAYS = 3        # same payee + same amount within 3 days
MONTHLY_MIN_HISTORY = 2    # prior charges before a payee counts as monthly
MONTHLY_DAY_SLACK = 2      # prior charges all within 2 days of one day of the month
TOO_SOON_DAYS = 25         # a monthly charge less than 25 days after the last
OFF_DAY_DAYS = 3           # or more than 3 days from the usual day
# The budget app's own "is approximately" window on a date recurrence: each occurrence
# sits at noon, and the budget app searches from two days before the charge's midnight
# to two days after it, so a charge matches an occurrence from one day before
# it to two days after it
RULE_DATE_EARLY_DAYS = 1
RULE_DATE_LATE_DAYS = 2


def _ordinal(day):
    suffix = "th" if day in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _day_gap(a, b):
    """Days between two days of the month, the short way round the month."""
    d = abs(a - b)
    return min(d, 31 - d)


def _excluded_rows_sql():
    """SQL tail that drops rows whose category sits in an excluded or one-off
    group. The group names are this repo's own constants, so they go in as
    SQL literals."""
    groups = cash_flow.EXCLUDED_GROUPS + cash_flow.ONE_OFF_GROUPS
    if not groups:
        return ""
    names = ", ".join("'" + n.replace("'", "''") + "'" for n in groups)
    return (" AND (t.category IS NULL OR t.category NOT IN "
            "(SELECT xc.id FROM categories xc "
            "JOIN category_groups xg ON xg.id = xc.cat_group "
            "WHERE xc.tombstone = 0 AND xg.tombstone = 0 "
            f"AND xg.name IN ({names})))")


_SPEND_SQL = (
    "SELECT t.id, t.date, t.amount, t.payee AS payee_id, "
    "COALESCE(p.name, '') AS payee, a.name AS account, "
    "t.imported_payee, t.notes "
    "FROM v_transactions t "
    "JOIN accounts a ON a.id = t.account "
    "LEFT JOIN v_payees p ON p.id = t.payee "
    "WHERE t.is_parent = 0 AND t.transfer_id IS NULL "
    "AND t.starting_balance_flag = 0 AND a.offbudget = 0 "
    "AND t.amount < 0 AND ")


def _candidates(conn, today, ids, since=None):
    """The window's spending rows — dated since..today, since defaulting to
    ODD_WINDOW_DAYS before today — plus the rows named by ids, newest
    first, each once."""
    win = cash_flow._day_int(since or today - timedelta(days=ODD_WINDOW_DAYS))
    rows = [dict(r) for r in conn.execute(
        _SPEND_SQL + "t.date >= ? AND t.date <= ?" + _excluded_rows_sql()
        + " ORDER BY t.date DESC", (win, cash_flow._day_int(today)))]
    ids = [i for i in ids if i not in {r["id"] for r in rows}]
    if ids:
        qmarks = ",".join("?" * len(ids))
        rows += [dict(r) for r in conn.execute(
            _SPEND_SQL + f"t.id IN ({qmarks})" + _excluded_rows_sql(), ids)]
        rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def _expected_rules(conn):
    """The shape of every rule with an amount condition (rules.parse), the
    repeat's start as a date."""
    out = []
    for e in rules.load(conn):
        r = e["rule"]
        if r and r["amount"]:
            if r["repeat"]:
                r = dict(r, repeat=(r["repeat"][0], date.fromisoformat(r["repeat"][1])))
            out.append(r)
    return out


def _on_repeat(repeat, d):
    """True when d falls inside the budget app's window around an occurrence of the
    repeat (monthly or yearly from its start date, the start included; a
    month without the start's day has no occurrence)."""
    freq, start = repeat
    if d < start - timedelta(days=RULE_DATE_EARLY_DAYS):
        return False
    near = []
    for k in (-1, 0, 1):
        try:
            if freq == "yearly":
                near.append(start.replace(year=d.year + k))
            else:
                m = d.month + k
                y = d.year + (m - 1) // 12
                near.append(start.replace(year=y, month=(m - 1) % 12 + 1))
        except ValueError:
            continue
    return any(-RULE_DATE_LATE_DAYS <= (n - d).days <= RULE_DATE_EARLY_DAYS
               and n >= start for n in near)


def _rule_verdict(expected, s, d):
    """Expected-by-rule check for one charge row s: "expected" when a rule's
    payee or imported payee text, notes text, amount and (if any) date all
    match; ("off_date", repeat) when all but the date match; None when no
    rule matches. Amounts are signed cents, as the copy stores them."""
    off = None
    fold = lambda t: (t or "").casefold()  # noqa: E731
    for r in expected:
        if r["payee"] and r["payee"] != s["payee_id"] \
                or r["imported"] and fold(r["imported"]) not in fold(s["imported_payee"]) \
                or r["notes"] and fold(r["notes"]) not in fold(s["notes"]):
            continue
        op, cents = r["amount"]
        if op == "is" and s["amount"] != cents:
            continue
        if op == "isapprox" and abs(s["amount"] - cents) > rules.approx_slack(cents):
            continue
        if r["repeat"] is None or _on_repeat(r["repeat"], d):
            return "expected"
        off = off or ("off_date", r["repeat"])
    return off


def _repeat_text(repeat):
    freq, start = repeat
    if freq == "yearly":
        return f"yearly, usually around {start.strftime('%b')} {start.day}"
    return f"monthly, usually the {_ordinal(start.day)}"


def _monthly_day(prior_dates):
    """The day of the month a monthly payee charges on, or None when the
    prior charges do not agree on one."""
    days = [d.day for d in prior_dates]
    if len(days) < MONTHLY_MIN_HISTORY \
            or (prior_dates[-1] - prior_dates[0]).days < TOO_SOON_DAYS:
        return None
    usual = statistics.median_low(days)
    if all(_day_gap(d, usual) <= MONTHLY_DAY_SLACK for d in days):
        return usual
    return None


def odd_candidates(conn, today, ids=(), since=None):
    """The flags for the window's spending (since..today; the last
    ODD_WINDOW_DAYS days by default — a past month's report passes its
    first and last day) plus the charges named by ids:
    [{kind, date, transaction_id, payee, amount, account, text, reason}],
    amounts first. text names the charge (the email's line); reason is the
    same finding without it (the page prints the charge itself)."""
    amount, dup, off, new = [], [], [], []
    seen_pairs = set()
    expected = _expected_rules(conn)
    for s in _candidates(conn, today, ids, since):
        amt = -s["amount"]
        label = f"{s['payee'] or '(no payee)'} ${cash_flow._dollars(amt)} on {cash_flow._iso(s['date'])}"

        def flag(bucket, kind, reason, text):
            bucket.append({"kind": kind, "date": s["date"],
                           "transaction_id": s["id"], "payee": s["payee"],
                           "amount": s["amount"], "account": s["account"],
                           "text": text, "reason": reason})

        if not s["payee_id"]:
            continue
        d = cash_flow._to_date(s["date"])
        verdict = _rule_verdict(expected, s, d)
        if verdict is None:
            since = cash_flow._day_int(d - timedelta(days=AMOUNT_WINDOW_DAYS))
            past = [-r[0] for r in conn.execute(
                "SELECT amount FROM v_transactions "
                "WHERE payee = ? AND is_parent = 0 AND transfer_id IS NULL "
                "AND amount < 0 AND date >= ? AND date < ? "
                "ORDER BY date DESC", (s["payee_id"], since, s["date"]))]
            steps = list(zip(past, past[1:]))   # (newer, older)
            steady = sum(abs(a - b) <= AMOUNT_SLACK * a for a, b in steps)
            if steps and steady * 2 >= len(steps):
                level = past[0]
                if abs(amt - level) > AMOUNT_SLACK * level:
                    was = cash_flow._dollars(level)
                    flag(amount, "amount", f"changed amount (was ${was})",
                         f"changed amount: {label} (was ${was})")
            elif past and amt > max(past) * (1 + AMOUNT_SLACK):
                high = cash_flow._dollars(max(past))
                flag(amount, "amount",
                     f"above its usual range (high ${high} in the last year)",
                     f"above its usual range: {label} "
                     f"(high ${high} in the last year)")
        twins = conn.execute(
            "SELECT COUNT(*) FROM v_transactions "
            "WHERE payee = ? AND amount = ? AND is_parent = 0 "
            "AND transfer_id IS NULL AND date >= ? AND date <= ?",
            (s["payee_id"], s["amount"],
             cash_flow._day_int(d - timedelta(days=DUP_WITHIN_DAYS)),
             cash_flow._day_int(d + timedelta(days=DUP_WITHIN_DAYS)))).fetchone()[0]
        duplicate = twins > 1
        if duplicate:
            pair = (s["payee_id"], s["amount"])
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                flag(dup, "duplicate",
                     f"possible duplicate charge: appears {twins}x within "
                     f"{DUP_WITHIN_DAYS} days",
                     f"possible duplicate charge: {label} appears "
                     f"{twins}x within {DUP_WITHIN_DAYS} days")
        if verdict is not None:
            if verdict != "expected":
                rhythm = _repeat_text(verdict[1])
                flag(off, "off_schedule", f"off its expected date ({rhythm})",
                     f"off its expected date: {label} ({rhythm})")
            continue
        if not duplicate:
            prior_dates = [cash_flow._to_date(r[0]) for r in conn.execute(
                "SELECT date FROM v_transactions "
                "WHERE payee = ? AND is_parent = 0 AND transfer_id IS NULL "
                "AND amount < 0 AND date < ? ORDER BY date",
                (s["payee_id"], s["date"]))]
            usual = _monthly_day(prior_dates)
            if usual is not None:
                gap = (d - prior_dates[-1]).days
                rhythm = f"monthly, usually the {_ordinal(usual)}"
                if gap < TOO_SOON_DAYS:
                    flag(off, "off_schedule",
                         f"too soon: {gap} days after the last charge ({rhythm})",
                         f"too soon: {label}, {gap} days after the last "
                         f"charge ({rhythm})")
                elif _day_gap(d.day, usual) > OFF_DAY_DAYS:
                    flag(off, "off_schedule",
                         f"off its usual day ({rhythm})",
                         f"off its usual day: {label} ({rhythm})")
        first = conn.execute(
            "SELECT MIN(date) FROM v_transactions "
            "WHERE payee = ? AND is_parent = 0", (s["payee_id"],)).fetchone()[0]
        if first is not None \
                and first >= cash_flow._day_int(d - timedelta(days=ODD_WINDOW_DAYS)):
            flag(new, "new_payee", "new payee", f"new payee: {label}")
    deduped_new, seen = [], set()
    for n in new:
        if n["text"] not in seen:
            seen.add(n["text"])
            deduped_new.append(n)
    return amount + dup + off + deduped_new


_DEPOSIT_SQL = (
    "SELECT t.id, t.amount, COALESCE(p.name, '') AS payee, c.name AS cat "
    "FROM v_transactions t "
    "JOIN accounts a ON a.id = t.account "
    "LEFT JOIN v_payees p ON p.id = t.payee "
    "JOIN categories c ON c.id = t.category "
    "JOIN category_groups g ON g.id = c.cat_group "
    "WHERE t.is_parent = 0 AND t.transfer_id IS NULL "
    "AND t.starting_balance_flag = 0 AND a.offbudget = 0 "
    "AND t.amount > 0 AND c.tombstone = 0 AND g.tombstone = 0 "
    "AND g.is_income = 1 AND c.name IN (?, ?) "
    "AND t.date >= ? AND t.date <= ?")


def income_marks(conn, today, since=None):
    """{transaction id: mark} for the window's deposits (since..today, the
    last ODD_WINDOW_DAYS days by default) in the income source categories.
    Each deposit is matched to its source the way the cash-flow report does
    — by payee, or by an entry's listed amount — and marked:
      routine: <label>, <frequency>, usual amount
      flag: <label> deposit off its usual $N
      flag: new payee                      (the payee's only deposit)
      routine: <label>, N deposits so far  (no frequency known yet)
      routine: <label>, <frequency>        (several incomes under one payee)
    """
    win = cash_flow._day_int(since or today - timedelta(days=ODD_WINDOW_DAYS))
    deposits = conn.execute(
        _DEPOSIT_SQL, (cash_flow.MONTHLY_CATEGORY, cash_flow.PERIODIC_CATEGORY,
                       win, cash_flow._day_int(today))).fetchall()
    if not deposits:
        return {}
    payments = cash_flow._fetch_income(
        conn, cash_flow._parse_month(cash_flow.FIRST_MONTH))
    entries, _ = cash_flow._read_sources()
    out = {}
    for cat in (cash_flow.MONTHLY_CATEGORY, cash_flow.PERIODIC_CATEGORY):
        amounts, _ = cash_flow._entry_amounts(entries[cat])
        owner = {a: name for name, vals in amounts.items() for a in vals}
        sources = {r["name"]: r for r in cash_flow._source_rows(
            payments[cat], entries[cat], today)}
        for t in deposits:
            if t["cat"] != cat:
                continue
            src = sources.get(owner.get(t["amount"], t["payee"] or "(no payee)"))
            if src is None:
                continue
            label = src["label"]
            if src.get("mean") is None:
                mark = ("flag: new payee" if src["n"] == 1
                        else f"routine: {label}, {src['n']} deposits so far")
            elif src["incomes"] > 1:
                mark = f"routine: {label}, {src['freq']}"
            elif cash_flow._same(t["amount"], src["mean"]):
                mark = f"routine: {label}, {src['freq']}, usual amount"
            else:
                mark = (f"flag: {label} deposit off its usual "
                        f"${cash_flow._dollars(src['mean'])}")
            out[t["id"]] = mark
    return out
