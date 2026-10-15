"""Build one finance email, or one part of it.

Reads the request on stdin and prints subject, text and the reported ids
the caller records once the email is sent. The request:

- kind: daily, weekly or monthly
- settings: the sections and options (family_emails.settings)
- day: the day the email is built as of (YYYY-MM-DD)
- month: the monthly's month (YYYY-MM), needed for a monthly; the
  current month builds the month so far
- reported: the daily's transaction ids already printed for one person
- sample: made-up rows instead of the budget; no budget read, no model call
- settings.show_new_payees: the new-payee flags print, in the outliers
  and in the new transactions' marks; off, they are left out
- full_window: no seen list and no printed ids, so the daily's outliers
  and new transactions print the whole 7-day window under a note
- part: one section label (or Links) built alone, with no title

One budget read and, with the Summary section in the build, one model
call; nothing is written. The reported ids out are, for the daily, every
fresh transaction of the window, whether the Summary commented on it or
the transaction section printed it.
"""
import calendar
import json
import sys
from datetime import date, timedelta

import finance_jobs as finance

SECTIONS = {"Summary": "summary", "Items to check": "check", "Unusual charges": "outliers",
            "New transactions": "new", "Cash flow": "cashflow"}
LINKS = "Links"   # a page-only part: no email carries it
ACCOUNTS = ["Credit card", "Checking", "Savings"]


def sample_data(today, kind, show_new_payees):
    """Made-up rows in the shapes read_all returns, dated from today, as
    many as a real email carries: the daily's transactions over three
    accounts, the current month with its category rows, or for the
    monthly, the month that just ended against its average.
    show_new_payees keeps the one new-payee flag, as read_all does."""
    end = today.replace(day=1) if kind == "monthly" else today   # rows fall in the reported month

    def day(n):
        return int((end - timedelta(days=n)).strftime("%Y%m%d"))

    def iso(n):
        return (end - timedelta(days=n)).isoformat()

    def month(n):
        """'Oct 2026' n months before today's month."""
        m = today.year * 12 + today.month - 1 - n
        return f"{calendar.month_abbr[m % 12 + 1]} {m // 12}"

    def row(tid, n, cents, payee, account, heading, mark=""):
        grp, cat = heading.split(": ")
        return {"id": tid, "date": day(n), "amount": cents, "sort_order": 0, "payee": payee,
                "account": account, "account_order": ACCOUNTS.index(account), "cat": cat, "grp": grp,
                "grp_order": 0, "mark": mark}
    new = [row("s1", 1, -7140, "Corner Market", "Credit card", "Variable: Groceries",
               "flag: above its usual range"),
           row("s2", 2, -9640, "Green Grocer", "Credit card", "Variable: Groceries"),
           row("s3", 2, -1820, "Burger Hut", "Credit card", "Variable: Eating Out"),
           row("s4", 2, -1475, "Taco Stand", "Credit card", "Variable: Eating Out"),
           row("s5", 1, -1549, "StreamCo", "Credit card", "Variable: Subscription"),
           row("s6", 3, -3800, "Fuel Stop", "Credit card", "Variable: Car"),
           row("s7", 1, 255000, "Employer", "Checking", "Income: Monthly",
               "routine: Employer, monthly, usual amount"),
           row("s8", 2, -12100, "City Power", "Checking", "Fixed: Bills"),
           row("s9", 1, 149, "Dividend", "Checking", "Income: One-off"),
           row("s10", 1, 15, "Dividend", "Savings", "Income: One-off"),
           row("s11", 3, -1240, "Hillside Bakery", "Credit card", "Variable: Eating Out",
               "flag: new payee" if show_new_payees else "")]
    odd = [{"kind": "amount", "date": day(1), "transaction_id": "s1",
            "text": f"above its usual range: Corner Market $71.40 on {iso(1)} (high $55.00 in the last year)"},
           {"kind": "duplicate", "date": day(3), "transaction_id": "s6",
            "text": f"possible duplicate charge: Fuel Stop $38.00 on {iso(3)} appears 2x within 3 days"}]
    if show_new_payees:
        odd.append({"kind": "new_payee", "date": day(3), "transaction_id": "s11",
                    "text": f"new payee: Hillside Bakery $12.40 on {iso(3)}"})
    check = [f"{name}: no {month(0)} balance — using {month(1)}"
             for name in ("Savings goal", "Retirement", "Loan")]
    def as_of(change):
        return f"({month(1)}, {change} from {month(4)})"
    assets = ["### Assets and Debt", "", "Assets:",
              f"- Retirement: $50,000 {as_of('+2%')}",
              "", "Low Interest Debt:", f"- Loan: $10,000 {as_of('-4%')}", "  - 5%"]
    if kind == "monthly":
        summary = [f"{month(1)} income of $7,400 beat its $6,300 average, thanks to $1,800 in one-off income.",
                   "Expenses came to $8,900 against an $8,400 average, with Medical $260 over "
                   "and a $600 one-off cost.",
                   "The month ended $1,500 short."]
        cash_flow = [
            f"for {month(1)} | averages calculated with data from {month(3)} (2 months)",
            "", "### Income and spending", "",
            "Income: $7,400 ($6,300 avg, +17%)",
            "- Monthly: $5,100 ($5,100 avg, +0%)", "- One-off: $1,800",
            "- Periodic: $0 ($1,200 avg, -100%)",
            "Expenses: $8,900 ($8,400 avg, +6%)",
            "- Fixed: $3,700 ($3,700 avg, +0%)",
            "  - Bills: $390 ($390 avg, +0%)", "  - Loan: $540 ($540 avg, +0%)",
            "  - Rent: $2,550 ($2,550 avg, +0%)", "  - Insurance: $220 ($220 avg, +0%)",
            "- Variable: $4,600 ($4,700 avg, -2%)",
            "  - Groceries: $1,200 ($1,350 avg, -11%)",
            "  - Eating Out: $380 ($560 avg, -32%)",
            "  - Medical: $660 ($400 avg, +65%)",
            "  - Car: $520 ($560 avg, -7%)",
            "  - Household: $430 ($450 avg, -4%)",
            "  - Subscription: $310 ($310 avg, +0%)",
            "  - Personal: $1,100 ($1,070 avg, +3%)",
            "- One-off: $600", "  - Laptop repair: $600",
            "Leftover: -$1,500",
            ""] + assets
    else:
        summary = [f"3 accounts have no {month(0)} balance in the finance files, so this report uses "
                   f"their {month(1)} balances instead.",
                   f"Corner Market showed up on {iso(1)} for $71.40, above its usual high of $55.00 "
                   "in the last year."]
        if kind == "weekly":
            summary = [
                "Income of $7,400 this month falls $30 short of $7,430 in expenses, even with $1,800 "
                "in one-off income; monthly income is $5,100.",
                "Variable spending is at $3,950 against $4,000 expected by now, with Medical over "
                "($700 vs $350) and Eating Out over ($520 vs $410).",
                f"Cash of $14,200 covers the $2,500 new laptop listed for {month(-1)}, and lasts "
                "about 3.5 months with monthly income only."]
        cash_flow = [
            f"for {month(0)}, day {today.day} of {calendar.monthrange(today.year, today.month)[1]}"
            f" | averages calculated with data from {month(2)} (2 months)",
            "", "### Income and spending so far", "",
            "Income: $7,400", "- Monthly: $5,100", "- One-off: $1,800", "- Periodic: $0",
            "Expenses: $7,430",
            "- Fixed: $3,480 ($3,700 month)",
            "  - Bills: $390 ($390 month)", "  - Loan: $540 ($540 month)",
            "  - Rent: $2,550 ($2,550 month)", "  - Insurance: $0 ($220 month)",
            "- Variable: $3,950 ($4,000 est, $4,700 month)",
            "  - Groceries: $1,100 ($1,150 est, $1,350 month)",
            "  - Eating Out: $520 ($410 est, $560 month)",
            "  - Medical: $700 ($350 est, $400 month)",
            "  - Car: $340 ($480 est, $560 month)",
            "  - Household: $280 ($390 est, $450 month)",
            "  - Subscription: $270 ($270 est, $310 month)",
            "  - Personal: $740 ($950 est, $1,070 month)",
            "", "### Estimated",
            "", "Income estimate: $6,300 monthly", "",
            "Monthly income: $5,100", "- Paycheck: $5,100 monthly",
            "Periodic income, spread per month: $1,200", "- Other income: $1,200 monthly",
            "- Bonus: no deposit yet",
            "", "Expense estimate: $8,400 monthly", "",
            "- Fixed: $3,700 monthly", "- Variable: $4,700 monthly",
            "", "Upcoming one-time expenses: $2,500", f"- New laptop: $2,500 ({month(-1)})",
            "", "### Forecast", "",
            "Cash today: $14,200 in Checking and Savings. Minus $2,500 listed upcoming expenses = $11,700 left.",
            "", "How long that cash lasts, with monthly income only:",
            f"- net spending $3,300 monthly, lasts about 3.5 months — runs out around {month(-3)}",
            "", "Same, with periodic income added:",
            f"- net spending $2,100 monthly, lasts about 5.6 months — runs out around {month(-5)}",
            ""] + assets
    return {"new": new, "odd": odd, "check": check, "cash_flow": "\n".join(cash_flow), "summary": summary}


def build(request):
    kind, spec = request["kind"], request["settings"]
    today = date.fromisoformat(request["day"])
    month = request["month"] if kind == "monthly" else ""
    sample, part = request.get("sample", False), request.get("part")
    full_window = request.get("full_window", False)
    labels = [part] if part else spec["sections"]
    if sample:
        data = sample_data(today, kind, spec["show_new_payees"])
    else:
        conn = finance.db()
        try:
            data = finance.read_all(conn, today, month, weekly=kind == "weekly",
                                    detailed=spec["detailed"], combine_personal=spec["combine"],
                                    full_window=full_window, show_new_payees=spec["show_new_payees"])
        finally:
            conn.close()
    reported = {} if full_window else request.get("reported", {})
    data = {**data, "new": [r for r in data["new"] if r["id"] not in reported]}
    summary = []
    if "Summary" in labels and sample:
        summary = data["summary"]
    elif "Summary" in labels:
        report = ("".join(finance.email_body(s, data, full_window=full_window)
                          for s in ("check", "outliers", "new")) if kind == "daily" else data["cash_flow"])
        summary = finance.build_sentences(finance.llm_report_sentences(
            report, finance.learned_notes(), month, kind == "weekly"))
    title = f"Finance {kind} — {finance.month_name(month) if month else today.isoformat()}" + (" (sample)" if sample else "")
    blocks = [] if part else ["# " + title]
    for label in labels:
        block = finance.email_body("links" if label == LINKS else SECTIONS[label], data, summary,
                                   month, full_window).strip()
        if block:
            blocks.append(block)
    if kind == "daily" and not sample and not full_window:
        reported = finance.merge_reported(reported, data["new"], today)
    return {"subject": title, "text": "\n\n".join(blocks) + "\n", "reported": reported}


if __name__ == "__main__":
    try:
        print(json.dumps(build(json.load(sys.stdin))))
    except Exception as e:
        sys.exit(str(e))
