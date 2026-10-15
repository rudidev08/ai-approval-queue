#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1"]
# ///
"""Tests for services/ops/job_rules.py — stdlib unittest. The state file and the
decisions log live in a temp folder. job_rules imports inbox, which pulls in
the mcp package, so run through uv: uv run --no-project test_job_rules.py (from
this directory).
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common  # noqa: E402
import job_rules  # noqa: E402


def stamp(days_ago):
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def resolved(set_id, ts, calendar="Jordan", proposed="Work", outcome="done"):
    """A create_event row_resolved line as inbox._work_item writes it."""
    return {"ts": ts, "event": "row_resolved", "set_id": set_id, "row_id": "r1",
            "kind": "create_event", "args": {}, "outcome": outcome, "result": "",
            "title": "Dr appointment", "calendar": calendar,
            "proposed_calendar": proposed,
            "emails": [{"subject": "Your visit", "from": "Clinic <c@x.com>"}]}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"ASSISTANT_OPS_STATE": self._tmp.name})
        self._env.start()
        job_rules.boot()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def log(self, *lines):
        """Lines into their year's decisions file, as common._log writes them."""
        for line in lines:
            path = common.state_dir() / f"decisions-{line['ts'][:4]}.jsonl"
            with open(path, "a") as f:
                f.write(json.dumps(line) + "\n")

    def suggest(self, *suggestions, through=None):
        code, out = job_rules.batch({"area": "inbox", "through": through or stamp(0),
                                 "suggestions": list(suggestions)})
        self.assertEqual(code, 200)
        return out

    def add(self, text="Jordan's doctor visits go on Jordan.", evidence=("s1/r1",)):
        return {"change": "add", "rule_id": "", "text": text, "why": "moved twice",
                "evidence": list(evidence)}

    def emails_area(self):
        return job_rules.STATE["areas"]["inbox"]


class TestCandidates(RulesTest):
    def test_new_evidence_is_handed_over_once(self):
        self.log(resolved("s1", stamp(3)), resolved("s2", stamp(2), proposed="Jordan"),
                 resolved("s3", stamp(1), outcome="denied"),
                 {"ts": stamp(1), "event": "set_created"})
        code, out = job_rules.candidates({})
        self.assertEqual(code, 200)
        [area] = out["areas"]
        self.assertEqual(area["area"], "inbox")
        self.assertEqual([e["id"] for e in area["evidence"]], ["s1/r1", "s2/r1"])
        self.assertIn("moved it from Work to Jordan", area["evidence"][0]["summary"])
        self.assertEqual(area["through"], area["evidence"][1]["at"])
        self.suggest(through=area["through"])
        self.assertEqual(job_rules.candidates({})[1]["areas"], [])
        # one new item hands over the whole window again
        self.log(resolved("s4", stamp(0)))
        [area] = job_rules.candidates({})[1]["areas"]
        self.assertEqual([e["id"] for e in area["evidence"]], ["s1/r1", "s2/r1", "s4/r1"])

    def test_evidence_older_than_the_window_is_left_out(self):
        self.log(resolved("old", stamp(job_rules.EVIDENCE_DAYS + 1)), resolved("new", stamp(1)))
        [area] = job_rules.candidates({})[1]["areas"]
        self.assertEqual([e["id"] for e in area["evidence"]], ["new/r1"])

    def test_no_evidence_no_area(self):
        self.assertEqual(job_rules.candidates({})[1]["areas"], [])


class TestBatch(RulesTest):
    def setUp(self):
        super().setUp()
        self.log(resolved("s1", stamp(2)), resolved("s2", stamp(1)))

    def test_good_suggestions_are_stored_with_the_services_own_evidence(self):
        out = self.suggest(self.add(evidence=["s1/r1", "s2/r1", "s1/r1"]),
                           through="2026-10-01T00:00:00Z")
        self.assertEqual(out, {"added": 1, "dropped": []})
        [s] = self.emails_area()["suggestions"]
        self.assertEqual([e["id"] for e in s["evidence"]], ["s1/r1", "s2/r1"])
        self.assertTrue(s["evidence"][0]["summary"].startswith('Event "Dr appointment"'))
        self.assertEqual(self.emails_area()["evidence_through"], "2026-10-01T00:00:00Z")
        self.assertTrue(job_rules.state_file().exists())

    def test_bad_suggestions_are_dropped_and_named(self):
        rule = job_rules._add_rule(self.emails_area(), "Old rule.", "")
        bad = [dict(self.add(), change="merge"),
               dict(self.add(), evidence=["nope/r9"]),
               dict(self.add(), evidence=[]),
               dict(self.add(), text="two\nlines"),
               dict(self.add(), text="x" * (job_rules.TEXT_CAP + 1)),
               dict(self.add(), why=""),
               {"change": "edit", "rule_id": "rule_gone", "text": "x", "why": "y",
                "evidence": ["s1/r1"]},
               "not an object"]
        good = {"change": "remove", "rule_id": rule["id"], "text": "",
                "why": "contradicted", "evidence": ["s2/r1"]}
        out = self.suggest(*bad, good)
        self.assertEqual(out["added"], 1)
        self.assertEqual(len(out["dropped"]), len(bad))
        self.assertEqual(self.emails_area()["suggestions"][0]["change"], "remove")

    def test_refusals(self):
        self.assertEqual(job_rules.batch({"area": "nope", "through": "x", "suggestions": []})[0], 400)
        self.assertEqual(job_rules.batch({"area": "inbox", "suggestions": []})[0], 400)
        self.assertEqual(job_rules.batch({"area": "inbox", "through": "x",
                                      "suggestions": "x"})[0], 400)


class TestDecide(RulesTest):
    def setUp(self):
        super().setUp()
        self.log(resolved("s1", stamp(1)))

    def decide(self, decision, **kw):
        sid = self.emails_area()["suggestions"][0]["id"]
        return job_rules.decide({"area": "inbox", "id": sid, "decision": decision, **kw})

    def test_thumbs_up_on_an_add_makes_a_rule_with_the_edited_text(self):
        self.suggest(self.add())
        self.assertEqual(self.decide("up", text="Jordan's visits go on Jordan.",
                                     reason="only some visits")[0], 200)
        a = self.emails_area()
        self.assertEqual(a["suggestions"], [])
        [rule] = a["rules"]
        self.assertEqual((rule["text"], rule["reason"]),
                         ("Jordan's visits go on Jordan.", "only some visits"))
        self.assertEqual(job_rules.listing({"area": "inbox"})[1]["rules"], [rule])
        self.assertEqual(job_rules.state()["inbox"]["rules"], [rule])

    def test_thumbs_down_rejects_with_the_reason(self):
        self.suggest(self.add())
        # the wording box goes along on a down vote too, unused and unchecked
        self.assertEqual(self.decide("down", reason="only some visits",
                                     text="x" * (job_rules.TEXT_CAP + 1))[0], 200)
        a = self.emails_area()
        self.assertEqual((a["rules"], a["suggestions"]), ([], []))
        self.assertEqual([(r["text"], r["reason"]) for r in a["rejected"]],
                         [("Jordan's doctor visits go on Jordan.", "only some visits")])

    def test_edit_and_remove_apply_to_their_rule(self):
        rule = job_rules._add_rule(self.emails_area(), "Old rule.", "first")
        self.suggest({"change": "edit", "rule_id": rule["id"], "text": "New rule.",
                      "why": "w", "evidence": ["s1/r1"]})
        self.decide("up")
        self.assertEqual((rule["text"], rule["reason"]), ("New rule.", "first"))
        self.suggest({"change": "remove", "rule_id": rule["id"], "text": "",
                      "why": "w", "evidence": ["s1/r1"]})
        self.decide("up")
        self.assertEqual(self.emails_area()["rules"], [])

    def test_a_suggestion_whose_rule_is_gone_cannot_be_applied(self):
        rule = job_rules._add_rule(self.emails_area(), "Old rule.", "")
        self.suggest({"change": "edit", "rule_id": rule["id"], "text": "New rule.",
                      "why": "w", "evidence": ["s1/r1"]})
        job_rules.remove({"area": "inbox", "rule_id": rule["id"]})
        self.assertEqual(self.decide("up")[0], 409)
        self.assertEqual(self.decide("down")[0], 200)

    def test_refusals(self):
        self.suggest(self.add())
        self.assertEqual(self.decide("maybe")[0], 400)
        self.assertEqual(self.decide("up", reason="a\nb")[0], 400)
        self.assertEqual(job_rules.decide({"area": "inbox", "id": "sug_x",
                                       "decision": "up"})[0], 404)
        self.assertEqual(job_rules.decide({"area": "nope", "decision": "up"})[0], 400)


class TestSaveAndRemove(RulesTest):
    def test_save_adds_then_replaces(self):
        code, out = job_rules.save({"area": "inbox", "text": "A rule.", "reason": "r"})
        self.assertEqual(code, 200)
        rid = out["rule"]["id"]
        code, out = job_rules.save({"area": "inbox", "text": "B rule.", "rule_id": rid})
        self.assertEqual((code, out["rule"]["text"], out["rule"]["reason"]),
                         (200, "B rule.", "r"))
        self.assertEqual(len(self.emails_area()["rules"]), 1)
        self.assertEqual(job_rules.save({"area": "inbox", "text": "x", "rule_id": "rule_x"})[0], 404)
        self.assertEqual(job_rules.save({"area": "inbox", "text": ""})[0], 400)

    def test_remove(self):
        rid = job_rules.save({"area": "inbox", "text": "A rule."})[1]["rule"]["id"]
        self.assertEqual(job_rules.remove({"area": "inbox", "rule_id": rid})[0], 200)
        self.assertEqual(job_rules.remove({"area": "inbox", "rule_id": rid})[0], 404)
        self.assertEqual(self.emails_area()["rules"], [])

    def test_state_survives_a_restart(self):
        job_rules.save({"area": "inbox", "text": "A rule."})
        job_rules.boot()
        self.assertEqual([r["text"] for r in self.emails_area()["rules"]], ["A rule."])


if __name__ == "__main__":
    unittest.main()
