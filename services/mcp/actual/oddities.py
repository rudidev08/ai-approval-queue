#!/usr/bin/env python3
"""Odd-charge rules over the api-cache copy — one module behind two readers:
the daily report email's Outliers section (../../actions/finance_jobs.py)
and the actions page's oddities list in the finance area
(../../actions/finance.py). stdlib only.

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

  large        a single charge of LARGE_ABS_CENTS or more; or, for a payee
               with LARGE_MIN_HISTORY prior charges, a charge of at least
               LARGE_MIN_CENTS that is LARGE_RATIO times the payee's median
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

Charges in cash_flow.EXCLUDED_GROUPS (money the cash-flow report never
counts) and cash_flow.ONE_OFF_GROUPS (counted, but in no average) are left
out: both groups describe money that is not normal life. Deleting the
category or the group ends the exclusion.
"""

import pathlib
import statistics
import sys
from datetime import timedelta

sys.path.append(str(pathlib.Path(__file__).resolve().parent))
import cash_flow  # noqa: E402

ODD_WINDOW_DAYS = 7        # candidates come from the last 7 days
LARGE_ABS_CENTS = 50000    # any single charge of $500 or more
LARGE_RATIO = 3            # or 3x the payee's median charge ...
LARGE_MIN_CENTS = 10000    # ... when the charge is at least $100 ...
LARGE_MIN_HISTORY = 3      # ... and the payee has at least 3 prior charges
DUP_WITHIN_DAYS = 3        # same payee + same amount within 3 days
MONTHLY_MIN_HISTORY = 2    # prior charges before a payee counts as monthly
MONTHLY_DAY_SLACK = 2      # prior charges all within 2 days of one day of the month
TOO_SOON_DAYS = 25         # a monthly charge less than 25 days after the last
OFF_DAY_DAYS = 3           # or more than 3 days from the usual day


def _ordinal(day):
    suffix = "th" if day in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _day_gap(a, b):
    """Days between two days of the month, the short way round the month."""
    d = abs(a - b)
    return min(d, 31 - d)


def _excluded_rows_sql():
    """SQL tail that drops rows whose category sits in an excluded or one-off
    group. Deleting the category or the group stops the exclusion, so those
    rows count again — cash_flow does the same, reading them as
    uncategorized. The group names are this repo's own constants, so they
    go in as SQL literals."""
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
    "COALESCE(p.name, '') AS payee, a.name AS account "
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
    larges first. text names the charge (the email's line); reason is the
    same finding without it (the page prints the charge itself)."""
    large, dup, off, new = [], [], [], []
    seen_pairs = set()
    for s in _candidates(conn, today, ids, since):
        amt = -s["amount"]
        label = f"{s['payee'] or '(no payee)'} ${cash_flow._dollars(amt)} on {cash_flow._iso(s['date'])}"

        def flag(bucket, kind, reason, text):
            bucket.append({"kind": kind, "date": s["date"],
                           "transaction_id": s["id"], "payee": s["payee"],
                           "amount": s["amount"], "account": s["account"],
                           "text": text, "reason": reason})

        if amt >= LARGE_ABS_CENTS:
            flag(large, "large", "unusually large charge",
                 f"unusually large charge: {label}")
        elif s["payee_id"] and amt >= LARGE_MIN_CENTS:
            prior = [-r[0] for r in conn.execute(
                "SELECT amount FROM v_transactions "
                "WHERE payee = ? AND is_parent = 0 AND transfer_id IS NULL "
                "AND amount < 0 AND id != ?", (s["payee_id"], s["id"]))]
            if len(prior) >= LARGE_MIN_HISTORY \
                    and amt >= LARGE_RATIO * statistics.median(prior):
                median = cash_flow._dollars(statistics.median(prior))
                flag(large, "large",
                     f"unusually large charge for this payee (median ${median})",
                     f"unusually large charge for this payee: {label} "
                     f"(median ${median})")
        if not s["payee_id"]:
            continue
        d = cash_flow._to_date(s["date"])
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
    # identical new-payee lines collapsed — a new payee with two different
    # amounts still gets two lines
    deduped_new, seen = [], set()
    for n in new:
        if n["text"] not in seen:
            seen.add(n["text"])
            deduped_new.append(n)
    return large + dup + off + deduped_new
