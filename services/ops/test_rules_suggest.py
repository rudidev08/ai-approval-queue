#!/usr/bin/env python3
"""Tests for services/ops/rules_suggest.py — stdlib unittest, no network.

post and call_llm are stubbed, so no run ever reaches the ops service or a
model. Run: python3 services/ops/test_rules_suggest.py from the repo root.
"""

import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import rules_suggest as rs  # noqa: E402

AREA = {"area": "inbox", "about": "The inbox scan proposes events.",
        "evidence": [{"id": "s1/r1", "at": "2026-10-01T10:00:00Z",
                      "summary": 'Event "Dr": the user moved it from Work to Jordan. '
                                 "Email: Clinic <c@x.com>: Your visit"}],
        "rules": [{"id": "rule_1", "text": "Club notices go on Jordan.",
                   "reason": "", "added_at": ""}],
        "suggestions": [{"id": "sug_1", "change": "add", "rule_id": "",
                         "text": "Waiting rule.", "why": "w", "evidence": [],
                         "created_at": ""}],
        "rejected": [{"change": "add", "rule_id": "", "text": "Turned down.",
                      "reason": "too broad", "rejected_at": ""}],
        "through": "2026-10-01T10:00:00Z"}
SUGGESTION = {"change": "add", "rule_id": "", "text": "Jordan's doctor visits go on Jordan.",
              "why": "moved once, plain reason", "evidence": ["s1/r1"]}


class TestMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.posts = []
        self.areas = [AREA]

        def fake_post(url, payload, timeout):
            self.posts.append((url, payload))
            if url == rs.CANDIDATES_URL:
                return {"areas": self.areas}
            return {"added": len(payload["suggestions"]), "dropped": []}

        self.patches = (
            mock.patch.dict(os.environ, {"ASSISTANT_LLM_PAUSE": os.path.join(
                self._tmp.name, "assistant-llm-pause.json")}),
            mock.patch.object(rs, "post", fake_post),
            mock.patch.object(rs, "call_llm", lambda area: [dict(SUGGESTION)]),
            mock.patch.object(sys, "argv", ["rules_suggest.py"]),
            mock.patch.object(sys, "stderr", io.StringIO()))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def test_each_area_posts_its_suggestions_with_its_through_stamp(self):
        with mock.patch.object(sys, "stdout", io.StringIO()) as out:
            rs.main()
        self.assertEqual(out.getvalue(), "")   # silent on success
        self.assertEqual([u for u, _ in self.posts], [rs.CANDIDATES_URL, rs.BATCH_URL])
        self.assertEqual(self.posts[1][1], {"area": "inbox",
                                            "through": "2026-10-01T10:00:00Z",
                                            "suggestions": [SUGGESTION]})

    def test_no_new_evidence_posts_nothing_more(self):
        self.areas = []
        rs.main()
        self.assertEqual([u for u, _ in self.posts], [rs.CANDIDATES_URL])

    def test_dry_run_prints_and_posts_nothing_more(self):
        with mock.patch.object(sys, "argv", ["rules_suggest.py", "--dry-run"]), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            rs.main()
        self.assertIn("Jordan's doctor visits", out.getvalue())
        self.assertEqual([u for u, _ in self.posts], [rs.CANDIDATES_URL])

    def test_a_paused_run_posts_nothing(self):
        with mock.patch.object(rs.llm_pause, "read", lambda: {"until": "x"}):
            rs.main()
        self.assertEqual(self.posts, [])


class TestPrompt(unittest.TestCase):
    def test_prompt_carries_everything_and_fences_the_evidence(self):
        p = rs.build_prompt(AREA)
        self.assertIn("The inbox scan proposes events.", p)
        self.assertIn('"rule_id": "rule_1"', p)
        self.assertIn("Waiting rule.", p)
        self.assertIn("too broad", p)
        begin = p.index("===== BEGIN EVIDENCE DATA")
        self.assertIn("Clinic <c@x.com>", p[begin:])
        self.assertNotIn("Clinic <c@x.com>", p[:begin])


if __name__ == "__main__":
    unittest.main()
