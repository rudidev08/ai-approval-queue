#!/usr/bin/env python3
"""audit_snoozes tests — snooze, unsnooze and expiry. The file lives in a
temp folder. Run: python3 services/mcp/common/test_audit_snoozes.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_snoozes  # noqa: E402


class AuditSnoozesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ, {"ASSISTANT_HERMES_AUDIT_STATE": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_no_file_snoozes_nothing(self):
        self.assertEqual(audit_snoozes.read(), {})

    def test_an_unreadable_file_snoozes_nothing(self):
        audit_snoozes.path().write_text("{ not json")
        self.assertEqual(audit_snoozes.read(), {})

    def test_snooze_lasts_two_weeks(self):
        until = audit_snoozes.snooze("agent release", "no stable tag")
        self.assertEqual(audit_snoozes.read(),
                         {"agent release": {"no stable tag": until}})
        left = datetime.fromisoformat(until) - datetime.now().astimezone()
        self.assertGreater(left, timedelta(days=13, hours=23))
        self.assertLessEqual(left, timedelta(days=14))

    def test_unsnooze_removes_the_finding_and_its_empty_category(self):
        audit_snoozes.snooze("agent release", "no stable tag")
        audit_snoozes.snooze("settings", "drift")
        audit_snoozes.unsnooze("agent release", "no stable tag")
        self.assertEqual(list(audit_snoozes.read()), ["settings"])
        self.assertNotIn("agent release",
                         json.loads(audit_snoozes.path().read_text()))

    def test_a_snooze_that_ran_out_is_left_out_and_dropped_on_write(self):
        past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
        audit_snoozes.path().write_text(json.dumps(
            {"settings": {"drift": past}}))
        self.assertEqual(audit_snoozes.read(), {})
        audit_snoozes.snooze("agent release", "no stable tag")
        self.assertEqual(list(json.loads(audit_snoozes.path().read_text())),
                         ["agent release"])


if __name__ == "__main__":
    unittest.main()
