#!/usr/bin/env python3
"""rules — the budget app's rules over the api-cache copy, in the shape the assistant writes.
One module behind every reader and writer: list_rules and the rule writes
(server.py), the finance page's cards and apply path
(../../ops/finance.py), and the odd-charge checks (oddities.py).
stdlib only.

The shape the assistant writes (parse() reads it back; any other rule is "web
interface only"):
  match    payee is X, or imported payee contains T (the bank's own payee
           text, before the budget app renames it)
  amount   optional: is N, or is approximately N (the budget app's 7.5% window)
  repeat   optional: date is approximately every month or year from a start
           date (interval 1, no patterns, no end)
  notes    optional: notes contain T
  action   set category
  stage    'post' when a condition is "contains", else none

Why post: the budget app runs every matching rule and the last one to set the
category wins. It orders rules by stage (pre, none, post), then by a score
(OP_SCORES summed, doubled when every condition is is/isapprox), then by id.
"contains" scores 0, so without the post stage a plain "payee is X" rule
(score 20) would beat "payee is X and notes contain T" (score 10) on every
charge they share. Checked against the budget app's API package (computeScore,
rankRules); re-check on a budget app update.

check() runs before every rule write and names what is wrong, so the caller
can propose a fix: a monthly repeat starting on day 29-31 or a yearly one on
Feb 29 (the budget app skips months without that day), a spending amount set to an
income category, and conflicts with the rules already there — the same
conditions with another category, a new rule that would take over or lose
to an existing one, two rules the budget app ranks the same, or a web-interface-only
rule on the same payee that sets another category (its rank is not worked
out). Two different contains-texts are taken never to meet in one charge.
"""

import json
import math
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parent))
import cash_flow  # noqa: E402

APPROX = 0.075   # the budget app's "is approximately" window on an amount
OP_SCORES = {"is": 10, "isNot": 10, "oneOf": 9, "notOneOf": 9, "isapprox": 5,
             "isbetween": 5, "gt": 1, "gte": 1, "lt": 1, "lte": 1}
DOUBLED = {"is", "isNot", "isapprox", "oneOf", "notOneOf"}
STAGES = {"pre": 0, None: 1, "post": 2}
# the copy stores some fields under their database names
FIELDS = {"description": "payee", "imported_description": "imported payee",
          "imported_payee": "imported payee", "acct": "account"}


# ---------------------------------------------------------------- reading

def load(conn):
    """Every live rule: [{id, stage, op, conditions, actions, rule}]; rule is
    the parsed shape, None for a rule outside it."""
    out = []
    for r in conn.execute("SELECT id, stage, conditions_op, conditions, actions "
                          "FROM rules WHERE tombstone = 0"):
        entry = {"id": r[0], "stage": r[1] or None, "op": r[2] or "and",
                 "conditions": json.loads(r[3] or "[]"),
                 "actions": json.loads(r[4] or "[]")}
        entry["rule"] = parse(entry)
        out.append(entry)
    return out


def parse(entry):
    """The shape dict {payee, imported, amount, repeat, notes, category,
    stage} of one rule, or None when the rule has parts the assistant cannot write.
    An amount with the budget app's inflow/outflow option becomes signed cents."""
    acts = entry["actions"]
    if entry["op"] != "and" or entry["stage"] not in (None, "post") \
            or len(acts) != 1 or acts[0].get("op") != "set" \
            or acts[0].get("field") != "category" or not acts[0].get("value"):
        return None
    rule = {"payee": None, "imported": None, "amount": None, "repeat": None,
            "notes": None, "category": acts[0]["value"], "stage": entry["stage"]}
    for c in entry["conditions"]:
        f, op, v = c.get("field"), c.get("op"), c.get("value")
        matched = rule["payee"] or rule["imported"]
        if f in ("description", "payee") and op == "is" and v and not matched:
            rule["payee"] = v
        elif f in ("imported_description", "imported_payee") and op == "contains" \
                and isinstance(v, str) and v and not matched:
            rule["imported"] = v
        elif f == "amount" and op in ("is", "isapprox") \
                and isinstance(v, (int, float)) and rule["amount"] is None:
            opts = c.get("options") or {}
            cents = (-abs(int(v)) if opts.get("outflow")
                     else abs(int(v)) if opts.get("inflow") else int(v))
            rule["amount"] = (op, cents)
        elif f == "date" and op == "isapprox" and isinstance(v, dict) \
                and v.get("frequency") in ("monthly", "yearly") \
                and v.get("interval", 1) == 1 and not v.get("patterns") \
                and v.get("endMode", "never") == "never" \
                and not v.get("skipWeekend") and rule["repeat"] is None:
            rule["repeat"] = (v["frequency"], v["start"])
        elif f == "notes" and op == "contains" and isinstance(v, str) and v \
                and rule["notes"] is None:
            rule["notes"] = v
        else:
            return None
    return rule if rule["payee"] or rule["imported"] else None


def match_of(entry):
    """("payee", id), ("imported", text), or None: what a rule matches on,
    read from its raw conditions — rules outside the shape included. Only
    "payee is" and "imported payee contains" count; a web-interface rule
    naming the payee another way ("is one of", "is not", "imported payee
    is") gets None, so check() and the finance cards do not see it."""
    for c in entry["conditions"]:
        if c.get("field") in ("description", "payee") and c.get("op") == "is":
            return ("payee", c.get("value"))
        if c.get("field") in ("imported_description", "imported_payee") \
                and c.get("op") == "contains":
            return ("imported", c.get("value"))
    return None


def texts(conn):
    """{payee id: [each bank payee text its transactions carry]}, or [its
    name] for a payee with none yet: what an imported-payee rule's text is
    tested against."""
    out = {}
    for pid, text in conn.execute("SELECT DISTINCT payee, imported_payee FROM v_transactions "
                                  "WHERE payee IS NOT NULL AND imported_payee IS NOT NULL"):
        out.setdefault(pid, []).append(text)
    for pid, name in conn.execute("SELECT id, name FROM v_payees"):
        out.setdefault(pid, [name or ""])
    return out


def names(conn):
    """({payee id: name}, {category id: (name, is income)})."""
    payees = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM v_payees")}
    cats = {r[0]: (r[1], bool(r[2])) for r in conn.execute(
        "SELECT id, name, is_income FROM categories WHERE tombstone = 0")}
    return payees, cats


# ---------------------------------------------------------------- describing

def _money(cents):
    return f"-${cash_flow._dollars(-cents)}" if cents < 0 else f"${cash_flow._dollars(cents)}"


def _condition_text(c, payees):
    f, op, v = c.get("field"), c.get("op"), c.get("value")
    label = FIELDS.get(f, f)
    if label == "payee" and op == "is":
        return f"payee is {payees.get(v, v)}"
    if f == "amount" and isinstance(v, (int, float)):
        opts = c.get("options") or {}
        cents = (-abs(int(v)) if opts.get("outflow")
                 else abs(int(v)) if opts.get("inflow") else int(v))
        return f"amount is {'about ' if op == 'isapprox' else ''}{_money(cents)}"
    if f == "date" and isinstance(v, dict) and v.get("frequency"):
        return f"repeats {v['frequency']} from {v.get('start')}"
    if op == "contains":
        return f"{label} contain{'' if label == 'notes' else 's'} '{v}'"
    return f"{label} {op} {json.dumps(v)}"


def describe(entry, payees, cats):
    """One plain line: 'payee is X and notes contain 'T' → Category'."""
    joiner = " or " if entry["op"] == "or" else " and "
    conds = joiner.join(_condition_text(c, payees) for c in entry["conditions"])
    acts = []
    for a in entry["actions"]:
        if a.get("op") == "set" and a.get("field") == "category":
            acts.append(cats.get(a.get("value"), (a.get("value"),))[0])
        else:
            acts.append(f"{a.get('op')} {FIELDS.get(a.get('field'), a.get('field'))} "
                        f"{json.dumps(a.get('value'))}")
    return f"{conds or 'every transaction'} → {', '.join(acts)}"


def line(entry, payees, cats):
    """list_rules' line for one rule, the page's too."""
    text = describe(entry, payees, cats)
    if entry["rule"] is None:
        text += " (web interface only)"
    return text


# ---------------------------------------------------------------- building

def build(category, payee=None, imported=None, amount=None, repeat=None, notes=None):
    """A new rule's shape; the stage follows its conditions."""
    contains = bool(imported or notes)
    return {"payee": payee, "imported": imported, "amount": amount,
            "repeat": repeat, "notes": notes, "category": category,
            "stage": "post" if contains else None}


def from_transaction(row, category, amount_mode, repeat_mode):
    """The rule the categorize path writes: payee is the transaction's payee;
    amount_mode any|approx|exact and repeat_mode any|monthly|yearly anchor on
    its amount and date. row needs payee, amount, date (YYYYMMDD int)."""
    amount = None if amount_mode == "any" else \
        ("isapprox" if amount_mode == "approx" else "is", row["amount"])
    repeat = None if repeat_mode == "any" else \
        (repeat_mode, cash_flow._iso(row["date"]))
    return build(category, payee=row["payee"], amount=amount, repeat=repeat)


def to_api(rule):
    """The rule as the budget app's createRule/updateRule take it (no id)."""
    conds = []
    if rule["payee"]:
        conds.append({"field": "payee", "op": "is", "value": rule["payee"], "type": "id"})
    if rule["imported"]:
        conds.append({"field": "imported_payee", "op": "contains",
                      "value": rule["imported"], "type": "string"})
    if rule["amount"]:
        conds.append({"field": "amount", "op": rule["amount"][0],
                      "value": rule["amount"][1], "type": "number"})
    if rule["repeat"]:
        conds.append({"field": "date", "op": "isapprox", "type": "date", "value": {
            "frequency": rule["repeat"][0], "start": rule["repeat"][1],
            "interval": 1, "patterns": [], "skipWeekend": False,
            "weekendSolveMode": "after", "endMode": "never"}})
    if rule["notes"]:
        conds.append({"field": "notes", "op": "contains", "value": rule["notes"],
                      "type": "string"})
    return {"stage": rule["stage"], "conditionsOp": "and", "conditions": conds,
            "actions": [{"field": "category", "op": "set",
                         "value": rule["category"], "type": "id"}]}


def entry_of(rule, rule_id=None):
    """A built rule in load()'s entry form, so describe() reads it."""
    api = to_api(rule)
    return {"id": rule_id, "stage": rule["stage"], "op": "and",
            "conditions": api["conditions"], "actions": api["actions"],
            "rule": rule}


# ---------------------------------------------------------------- checking

def rank(entry):
    """(stage, score) the way the budget app orders rules (computeScore, rankRules);
    higher runs later, so wins. Equal ranks fall to the rule id."""
    ops = [c.get("op") for c in entry["conditions"]]
    score = sum(OP_SCORES.get(o, 0) for o in ops)
    return (STAGES.get(entry["stage"], 1),
            score * 2 if all(o in DOUBLED for o in ops) else score)


def by_priority(entries):
    """entries in the budget app's order, highest priority first (equal ranks by
    rule id): a charge gets the category of the first one it matches that
    sets a category."""
    return sorted(entries, key=lambda e: (rank(e), e["id"] or ""), reverse=True)


def approx_slack(cents):
    """The budget app's "is approximately" window in cents: Math.round(|N| * 7.5%),
    which rounds halves up where Python's round() rounds them to even."""
    return math.floor(abs(cents) * APPROX + 0.5)


def _window(rule):
    if not rule["amount"]:
        return None
    op, cents = rule["amount"]
    slack = approx_slack(cents) if op == "isapprox" else 0
    return cents - slack, cents + slack


def _within(small, big):
    return big.casefold() in small.casefold()


def _match_covers(a, b, texts, test=any):
    """True when rule a's payee match takes in rule b's. Against a payee, an
    imported-payee text must be in its bank texts (texts()): in some of them
    for an overlap (test=any), in every one for a cover (test=all)."""
    if a["payee"]:
        return a["payee"] == b["payee"]
    return bool(b["imported"] and _within(b["imported"], a["imported"])
                or b["payee"] and test(_within(t, a["imported"])
                                       for t in texts.get(b["payee"], ())))


def _covers(a, b, texts):
    """True when rule a matches every charge rule b matches."""
    wa, wb = _window(a), _window(b)
    return (_match_covers(a, b, texts, all)
            and (wa is None or wb is not None and wa[0] <= wb[0] and wb[1] <= wa[1])
            and (a["repeat"] is None or a["repeat"] == b["repeat"])
            and (a["notes"] is None or b["notes"] is not None
                 and _within(b["notes"], a["notes"])))


def _overlap(a, b, texts):
    """True when one charge could match both rules. Two repeats always count
    as overlapping. Two different contains-texts count only when one holds
    the other: a charge holding both ('June rent' for 'june' and 'rent') is
    taken not to happen, since refusing every such pair would refuse every
    split of one payee by notes text."""
    wa, wb = _window(a), _window(b)
    return ((_match_covers(a, b, texts) or _match_covers(b, a, texts))
            and (wa is None or wb is None or wa[0] <= wb[1] and wb[0] <= wa[1])
            and (a["notes"] is None or b["notes"] is None
                 or _within(a["notes"], b["notes"]) or _within(b["notes"], a["notes"])))


def _same(a, b):
    fold = lambda t: t.casefold() if t else t  # noqa: E731
    return (a["payee"] == b["payee"] and fold(a["imported"]) == fold(b["imported"])
            and a["amount"] == b["amount"] and a["repeat"] == b["repeat"]
            and fold(a["notes"]) == fold(b["notes"]))


def income_refusal(cats, category, cents):
    """Refusal text when spending (cents < 0) would land in an income
    category, else None. Income may go to any category (a refund)."""
    name, income = cats.get(category, (category, False))
    if income and cents < 0:
        return (f"{name} is an income category and takes only income, but the "
                f"amount is spending ({_money(cents)}). Pick a spending category")
    return None


def check(rule, entries, payees, cats, texts, exclude=None):
    """(refusal, covered): refusal is the text that says why rule must not be
    written and what to do instead, None when it may; covered is an
    existing rule with the same conditions, stage and category, None when
    there is none. Finding it ends the check: nothing will be written, so
    nothing is refused. A narrower rule of the same category is
    written: it changes no category, and an amount rule is what marks charges
    expected (oddities.py). exclude is the id of the rule being replaced."""
    problems = []
    if rule["repeat"]:
        freq, start = rule["repeat"]
        if freq == "monthly" and int(start[8:10]) > 28:
            problems.append(f"a monthly repeat starting on day {int(start[8:10])} "
                            "misses every month without that day, because the budget app "
                            "skips those months. Use no repeat, or a rule from "
                            "create_rule with repeat_start on day 28 or earlier "
                            "(a repeat also matches charges up to 2 days off)")
        if freq == "yearly" and start[5:10] == "02-29":
            problems.append("a yearly repeat starting on Feb 29 only occurs in "
                            "leap years. Start it on Feb 28 or Mar 1")
    if rule["amount"]:
        refusal = income_refusal(cats, rule["category"], rule["amount"][1])
        if refusal:
            problems.append(refusal)
    new_rank = rank(entry_of(rule))
    for e in entries:
        old = e["rule"]
        if e["id"] == exclude:
            continue
        said = f"rule {e['id']} ({describe(e, payees, cats)})"
        if old is None:
            m = match_of(e)
            sets = {a.get("value") for a in e["actions"]
                    if a.get("op") == "set" and a.get("field") == "category"}
            other = build(None, payee=m[1] if m and m[0] == "payee" else None,
                          imported=m[1] if m and m[0] == "imported" else None)
            if m and sets - {rule["category"]} and (
                    _match_covers(rule, other, texts) or _match_covers(other, rule, texts)):
                problems.append(f"{said} can match the same payee and sets another "
                                "category, and it has parts the rule tools cannot "
                                "rank, so one of the two may stop applying. Change "
                                "or delete it in the budget app's web interface first")
            continue
        same_cat = old["category"] == rule["category"]
        if _same(rule, old):
            if same_cat and old["stage"] == rule["stage"]:
                return None, e
            problems.append(f"{said} has the same conditions. Update rule "
                            f"{e['id']} instead (update_rule also resets its priority)")
            continue
        if not _overlap(rule, old, texts):
            continue
        if same_cat:
            continue
        old_rank = rank(e)
        how = "the same" if new_rank == old_rank else "higher"
        if _covers(rule, old, texts) and new_rank >= old_rank:
            problems.append(f"the new rule matches every charge {said} matches, "
                            f"and the budget app ranks the new rule {how}, so rule "
                            f"{e['id']} would stop applying. Make the new rule "
                            "narrower (notes text or an amount), or delete rule "
                            f"{e['id']} first if it is no longer wanted")
        elif _covers(old, rule, texts) and old_rank >= new_rank:
            problems.append(f"{said} matches every charge the new rule matches, "
                            f"and the budget app ranks it {'the same' if old_rank == new_rank else 'higher'}, "
                            f"so the new rule would never apply. Update rule "
                            f"{e['id']} instead, or delete it and write narrower rules")
        elif new_rank == old_rank:
            problems.append(f"{said} can match the same charges, and the budget app "
                            "ranks both rules the same, so the rule id picks the "
                            "winner. Add an exact amount or notes text so the two "
                            f"cannot match one charge, or update rule {e['id']} instead")
    return ("; ".join(problems), None) if problems else (None, None)
