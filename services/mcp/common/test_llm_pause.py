#!/usr/bin/env python3
"""llm_pause tests — the pause file's read, start, clear and expiry. The
file lives in a temp folder. Run: python3 services/mcp/common/test_llm_pause.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_pause  # noqa: E402


class LlmPauseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file = os.path.join(self._tmp.name, "assistant-llm-pause.json")
        self._env = mock.patch.dict(os.environ, {"ASSISTANT_LLM_PAUSE": self.file})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_no_file_reads_as_no_pause(self):
        self.assertIsNone(llm_pause.read())

    def test_start_writes_a_pause_read_gives_it_back(self):
        written = llm_pause.start(30, "other")
        self.assertEqual(llm_pause.read(), written)
        self.assertEqual(written["source"], "other")
        since = datetime.fromisoformat(written["since"])
        until = datetime.fromisoformat(written["until"])
        self.assertEqual(until - since, timedelta(minutes=30))
        self.assertIsNotNone(until.tzinfo)

    def test_start_replaces_the_pause_in_effect(self):
        llm_pause.start(30, "other")
        second = llm_pause.start(120, "ops")
        self.assertEqual(llm_pause.read(), second)

    def test_expired_file_is_deleted_and_reads_as_no_pause(self):
        past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
        with open(self.file, "w") as f:
            json.dump({"since": past, "until": past, "source": "ops"}, f)
        self.assertIsNone(llm_pause.read())
        self.assertFalse(os.path.exists(self.file))

    def test_unreadable_file_is_deleted(self):
        with open(self.file, "w") as f:
            f.write("not json")
        self.assertIsNone(llm_pause.read())
        self.assertFalse(os.path.exists(self.file))

    def test_naive_until_is_local_time(self):
        until = (datetime.now() + timedelta(hours=1)).isoformat()
        with open(self.file, "w") as f:
            json.dump({"since": until, "until": until, "source": "ops"}, f)
        self.assertIsNotNone(llm_pause.read())

    def test_start_refuses_a_pause_over_the_cap(self):
        llm_pause.start(llm_pause.MAX_HOURS * 60, "other")
        with self.assertRaises(ValueError):
            llm_pause.start(llm_pause.MAX_HOURS * 60 + 1, "other")

    def test_until_past_the_cap_is_deleted(self):
        now = datetime.now().astimezone()
        until = now + timedelta(hours=llm_pause.MAX_HOURS, minutes=1)
        with open(self.file, "w") as f:
            json.dump({"since": now.isoformat(), "until": until.isoformat(),
                       "source": "other"}, f)
        self.assertIsNone(llm_pause.read())
        self.assertFalse(os.path.exists(self.file))

    def test_clear_removes_the_file(self):
        llm_pause.start(60, "ops")
        llm_pause.clear()
        self.assertIsNone(llm_pause.read())
        llm_pause.clear()  # nothing to remove is fine

    def test_unset_variable_raises(self):
        with mock.patch.dict(os.environ, {"ASSISTANT_LLM_PAUSE": ""}):
            with self.assertRaises(RuntimeError):
                llm_pause.read()


if __name__ == "__main__":
    unittest.main()
