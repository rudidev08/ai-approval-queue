#!/usr/bin/env python3
"""Tests for services/actions/messages_scan.py — stdlib unittest, no network.

post and call_llm are stubbed, so no run ever reaches the actions service,
OpenRouter, or a model. Run: python3 -m pytest test_messages_scan.py -q (from
this directory), or python3 services/actions/test_messages_scan.py from the
repo root.
"""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import messages_scan as ms

CAND = {"attachment_id": 101, "sender": "+1555", "chat": "partner",
        "received_at": "2026-08-17 10:00:00", "original_name": "lab.pdf",
        "kind": "application/pdf", "context": "here you go"}
LOCS = [{"id": "partner-medical", "label": "Partner / Medical"},
        {"id": "alex-health", "label": "Alex / Health"}]


class TestMainFlow(unittest.TestCase):
    """One full main() run per test, with the candidates answer and the
    model's verdicts stubbed; the posted batch is the assertion surface."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        env = pathlib.Path(self._tmp.name) / "messages.env"
        env.write_text("MESSAGES_MODEL=test-model\n")
        self.posts = []
        self.llm_calls = []

        def fake_post(url, payload, timeout):
            self.posts.append((url, payload))
            if url == ms.CANDIDATES_URL:
                return {"candidates": self.cands, "locations": LOCS}
            return {"ok": True}

        def fake_llm(cfg, cands, locs):
            self.llm_calls.append(sorted(c["attachment_id"] for c in cands))
            return {c["attachment_id"]: self.verdicts[c["attachment_id"]]
                    for c in cands if c["attachment_id"] in self.verdicts}

        self.patches = (
            mock.patch.object(ms, "ENV_FILE", env),
            mock.patch.object(ms, "post", fake_post),
            mock.patch.object(ms, "call_llm", fake_llm),
            mock.patch.object(sys, "argv", ["messages_scan.py"]))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def run_scan(self, cands, verdicts):
        self.cands = cands
        self.verdicts = verdicts
        ms.main()
        batches = [p for u, p in self.posts if u == ms.BATCH_URL]
        self.assertEqual(len(batches), 1)
        return batches[0]

    def save_verdict(self, location_id="partner-medical", filename="lab.pdf"):
        return {101: {"attachment_id": 101, "verdict": "save",
                      "location_id": location_id, "filename": filename,
                      "reason": "keep"}}

    def test_good_save_becomes_a_card(self):
        batch = self.run_scan([CAND], self.save_verdict())
        self.assertEqual(len(batch["cards"]), 1)
        self.assertEqual(batch["cards"][0]["filename"], "lab.pdf")
        self.assertEqual(batch["ignored"], [])

    def test_card_carries_sender_name_and_text(self):
        cand = dict(CAND, sender_name="Partner Conner", text="annual labs")
        batch = self.run_scan([cand], self.save_verdict())
        card = batch["cards"][0]
        self.assertEqual(card["sender_name"], "Partner Conner")
        self.assertEqual(card["text"], "annual labs")

    def test_ignore_verdict_is_ledgered(self):
        batch = self.run_scan([CAND], {101: {"attachment_id": 101,
                                             "verdict": "ignore",
                                             "location_id": "",
                                             "filename": "", "reason": ""}})
        self.assertEqual(batch, {"cards": [], "ignored": [101]})

    def test_unanswered_candidate_is_not_ledgered(self):
        batch = self.run_scan([CAND], {})
        self.assertEqual(batch, {"cards": [], "ignored": []})

    def test_invented_location_is_dropped_not_ledgered(self):
        batch = self.run_scan([CAND], self.save_verdict(location_id="invented"))
        self.assertEqual(batch, {"cards": [], "ignored": []})

    def test_overlong_filename_is_dropped_not_ledgered(self):
        batch = self.run_scan([CAND], self.save_verdict(filename="x" * 201))
        self.assertEqual(batch, {"cards": [], "ignored": []})

    def test_one_llm_call_per_group(self):
        c2 = dict(CAND, attachment_id=202, sender="+1666")
        batch = self.run_scan([CAND, c2],
                              {**self.save_verdict(),
                               202: {"attachment_id": 202, "verdict": "ignore",
                                     "location_id": "", "filename": "",
                                     "reason": ""}})
        self.assertEqual(self.llm_calls, [[101], [202]])
        self.assertEqual(len(batch["cards"]), 1)
        self.assertEqual(batch["ignored"], [202])

    def test_a_failed_group_posts_cards_then_an_error_record(self):
        c2 = dict(CAND, attachment_id=202, sender="+1666")
        self.cands = [CAND, c2]
        self.verdicts = self.save_verdict()

        def flaky(cfg, cands, locs):
            if any(c["attachment_id"] == 202 for c in cands):
                raise RuntimeError("boom")
            return {c["attachment_id"]: self.verdicts[c["attachment_id"]]
                    for c in cands if c["attachment_id"] in self.verdicts}

        with mock.patch.object(ms, "call_llm", flaky):
            ms.main()
        batches = [p for u, p in self.posts if u == ms.BATCH_URL]
        self.assertEqual(len(batches), 2)
        # the good group's card posts first (clearing any old error)...
        self.assertEqual(len(batches[0]["cards"]), 1)
        self.assertNotIn("error", batches[0])
        # ...then the failure record, naming what was not proposed
        self.assertEqual(batches[1]["error"]["step"], "llm")
        self.assertIn("202", batches[1]["error"]["message"])

    def test_repeated_filename_into_the_same_folder_is_renamed(self):
        c2 = dict(CAND, attachment_id=202)
        verdicts = {**self.save_verdict(),
                    202: {"attachment_id": 202, "verdict": "save",
                          "location_id": "partner-medical",
                          "filename": "lab.pdf", "reason": "keep"}}
        batch = self.run_scan([CAND, c2], verdicts)
        names = [c["filename"] for c in batch["cards"]]
        self.assertEqual(names, ["lab.pdf", "lab (2).pdf"])
        # a different folder is not a collision
        verdicts[202]["location_id"] = "alex-health"
        verdicts[202]["filename"] = "lab.pdf"
        self.posts.clear()
        batch = self.run_scan([CAND, c2], verdicts)
        self.assertEqual([c["filename"] for c in batch["cards"]],
                         ["lab.pdf", "lab.pdf"])


class TestPrompt(unittest.TestCase):
    def test_sender_name_and_text_ride_along(self):
        cand = dict(CAND, sender_name="Partner Conner", text="annual labs")
        p = ms.build_prompt([cand], LOCS)
        self.assertIn("Partner Conner", p)
        self.assertIn("annual labs", p)

    def test_no_empty_text_key(self):
        p = ms.build_prompt([CAND], LOCS)
        self.assertNotIn('"text"', p)


class TestGrouping(unittest.TestCase):
    def cand(self, aid, ts, chat="partner", sender="+1555"):
        return {"attachment_id": aid, "sender": sender, "chat": chat,
                "received_at": ts, "original_name": "f.pdf",
                "kind": "application/pdf"}

    def test_burst_chains_into_one_group(self):
        # each gap under 10 minutes — the chain holds
        cands = [self.cand(1, "2026-08-16 10:00:00"),
                 self.cand(2, "2026-08-16 10:05:00"),
                 self.cand(3, "2026-08-16 10:14:59")]
        self.assertEqual([[c["attachment_id"] for c in g]
                          for g in ms.group_candidates(cands)], [[1, 2, 3]])

    def test_a_gap_over_ten_minutes_splits(self):
        cands = [self.cand(1, "2026-08-16 10:00:00"),
                 self.cand(2, "2026-08-16 10:11:00")]
        self.assertEqual(len(ms.group_candidates(cands)), 2)

    def test_sender_and_chat_split(self):
        cands = [self.cand(1, "2026-08-16 10:00:00"),
                 self.cand(2, "2026-08-16 10:00:00", sender="+1666"),
                 self.cand(3, "2026-08-16 10:00:00", chat="family")]
        self.assertEqual(len(ms.group_candidates(cands)), 3)


class TestCallLlm(unittest.TestCase):
    CFG = {"MESSAGES_MODEL": "m", "MESSAGES_URL": "http://x",
           "MESSAGES_KEY_ENV": "K", "MESSAGES_TEMPERATURE": "0.2",
           "MESSAGES_MAX_TOKENS": "100"}

    def answer(self, content, finish="stop"):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{"finish_reason": finish,
                          "message": {"content": content}}]})
        return resp

    def call(self, resp):
        with mock.patch("urllib.request.urlopen", return_value=resp), \
                mock.patch.object(ms, "api_key", lambda name: "k"):
            return ms.call_llm(self.CFG, [CAND], LOCS)

    def test_string_attachment_id_accepted(self):
        content = json.dumps({"verdicts": [{"attachment_id": "101",
                                            "verdict": "ignore",
                                            "location_id": "", "filename": "",
                                            "reason": ""}]})
        self.assertIn(101, self.call(self.answer(content)))

    def test_overflow_splits_the_group(self):
        c2 = dict(CAND, attachment_id=202)
        verdict = lambda i: {"attachment_id": i, "verdict": "ignore",
                             "location_id": "", "filename": "", "reason": ""}
        resps = [self.answer(None, finish="length"),
                 self.answer(json.dumps({"verdicts": [verdict(101)]})),
                 self.answer(json.dumps({"verdicts": [verdict(202)]}))]
        with mock.patch("urllib.request.urlopen", side_effect=resps) as calls, \
                mock.patch.object(ms, "api_key", lambda name: "k"):
            out = ms.call_llm(self.CFG, [CAND, c2], LOCS)
        self.assertEqual(sorted(out), [101, 202])
        self.assertEqual(calls.call_count, 3)

    def test_single_item_overflow_is_a_clear_error(self):
        with self.assertRaises(RuntimeError) as cm:
            self.call(self.answer(None, finish="length"))
        self.assertIn("overflowed", str(cm.exception))

    def test_truncated_content_with_length_is_overflow_not_bad_json(self):
        with self.assertRaises(RuntimeError) as cm:
            self.call(self.answer('{"verdicts": [{"att', finish="length"))
        self.assertIn("overflowed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
