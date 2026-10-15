#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1", "pyyaml==6.0.3"]
# ///
"""Render tests for the pages in services/ops/pages/. The actions page, in its demo mode,
drawn by the Mac's own WebKit (pagecheck.swift) against a live server on an
ephemeral port. Demo mode sends every /api call to /demo/api, which demo.py
answers with canned data covering every area and every row kind — so a row
kind the page cannot draw (a BUILD entry missing, a builder that throws)
lands in the page's own error strip and fails here, before a real set does
it on the phone. Nothing real is read or written: demo.py invents its
data, the server's state paths are never touched, and the WebKit storage
is non-persistent. The server pulls in the mcp and pyyaml packages, so
run through uv, which reads the dependency header above:
uv run --no-project test_page.py (from this directory).
"""

import http.server
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import demo
import inbox
import overview
import server

WAIT_S = 4   # the page polls at start; the first render lands well inside this


class TestPageRenders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.checker = pathlib.Path(cls._tmp.name) / "pagecheck"
        subprocess.run(["swiftc", "-O", str(HERE / "pagecheck.swift"),
                        "-o", str(cls.checker)], check=True)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.server.server_address[1]
        cls._hosts = (set(server.ALLOWED_HOSTS), set(server.ALLOWED_ORIGINS))
        server.ALLOWED_HOSTS.add(f"127.0.0.1:{cls.port}")
        server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{cls.port}")
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        out = subprocess.run([str(cls.checker), f"http://127.0.0.1:{cls.port}/",
                              str(WAIT_S), "demo"], capture_output=True, text=True,
                             check=True, timeout=60)
        cls.page = json.loads(out.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        server.ALLOWED_HOSTS.clear()
        server.ALLOWED_HOSTS.update(cls._hosts[0])
        server.ALLOWED_ORIGINS.clear()
        server.ALLOWED_ORIGINS.update(cls._hosts[1])
        cls._tmp.cleanup()

    def test_demo_covers_every_row_kind(self):
        """A kind the service accepts must be in the demo data, or the render
        test below never exercises it."""
        kinds = {r["kind"] for s in demo._inbox()["sets"] for r in s["rows"]}
        self.assertEqual(kinds, inbox.ROW_KINDS)

    def test_no_script_error(self):
        self.assertEqual(self.page["errors"], [])
        self.assertEqual(self.page["page_error"], "")

    def test_audit_area_draws_handled_findings(self):
        """demo.py marks one plist copied, one skill acknowledged and one
        finding snoozed; the snoozed one shows on the snoozed card under its
        category's name."""
        for word in ("Copied", "Acknowledged", "tool servers: files: search"):
            self.assertIn(word, self.page["text"])

    def test_audit_findings_carry_their_notes(self):
        for c in demo._hermes_audit()["run"]["categories"]:
            for f in c["findings"]:
                if c["label"] != "tool servers":   # snoozed: no note on its row
                    self.assertTrue(f["text"] + f["note"] in self.page["text"],
                                    f"{f['text']!r} and its note not drawn")

    def test_family_emails_area_is_drawn(self):
        for text in ("4 emails on", "finance · weekly", "reminders · daily", "sent 3h ago (run now)"):
            self.assertTrue(text in self.page["text"], f"{text!r} not drawn")

    def test_finance_reports_area_is_drawn(self):
        for text in ("what the daily email holds", "same as the emails' default", "whole email",
                     "Items to check", "page only", "drafted 1 hr ago to ada@example.com"):
            self.assertTrue(text in self.page["text"], f"{text!r} not drawn")

    def test_reminders_area_is_drawn(self):
        r = demo._reminders()
        for text in (r["groups"][0]["label"], r["groups"][0]["lists"][0]["items"][0]["name"],
                     f'{r["groups"][0]["lists"][0]["due"]} due'):
            self.assertTrue(text in self.page["text"], f"{text!r} not drawn")

    def test_gateways_area_is_drawn(self):
        for text in ("Assistant#4821 from demo_user", "connected",
                     "@assistant_demo_bot from @demo_user",
                     "uptime 3d",
                     "last chat 4h ago", "Plant watering · Added a reminder for Thursday.",
                     "last chat 30m ago", "Bakery hours"):
            self.assertTrue(text in self.page["text"], f"{text!r} not drawn")

    def test_dashboard_area_is_drawn(self):
        for text in ("ada@example.com", "finance daily", "no emails", "Gateways", "Discord"):
            self.assertTrue(text in self.page["text"], f"{text!r} not drawn")

    def test_stale_plist_card_has_the_fix_key(self):
        # the card head reads its title, then copy, then the fix key
        text = "tool approvals copyfix"
        self.assertTrue(text in self.page["text"], f"{text!r} not drawn")

    def test_every_set_and_tracking_number_is_drawn(self):
        sets = demo._inbox()["sets"]
        for s in sets:
            # not assertIn: its failure message would dump the whole page text
            self.assertTrue(s["title"] in self.page["text"], f"{s['title']!r} not drawn")
        tracks = [r["args"]["tracking_number"] for s in sets for r in s["rows"]
                  if r["kind"] == "add_delivery"]
        self.assertTrue(tracks)
        # each number is drawn twice: on the meta line and in the detail area
        self.assertEqual(sorted(set(self.page["tracks"])), sorted(tracks))


# the overview page's data, canned: every block the page draws, a bad and an
# idle service, a backup model, an excluded session surface, and a tool
# call whose arguments hold markup
SNAPSHOT = {
    "host": {"cpu": 12, "gpu": 3, "ram": 61, "ramGb": 39.0, "diskUsedGb": 410, "disk": 44},
    "issues": 1,
    "models": {"assistant": [
        {"name": "model-a", "label": "chat", "provider": "lmstudio", "section": "chat"},
        {"name": "model-b", "label": "chat backup", "provider": "openrouter", "section": "chat"}],
        "voice": [{"name": "model-c", "label": "reply", "provider": "claude", "section": ""}]},
    "services": [
        {"name": "ops", "port": ":8300", "state": "ok", "detail": "up 3d"},
        {"name": "web-chat", "port": ":8302", "state": "bad",
         "detail": "down: connection refused"},
        {"name": "speech", "port": "", "state": "idle", "detail": "–"}],
    "mcpServers": [{"name": "macos_calendar", "port": ":8351", "state": "ok", "detail": "up",
                    "ago": "5m"}],
    "links": [{"name": "actions", "url": "https://assistant.example.net/"}],
    "llmPause": None,
    "catalog": {"hermes": ["memory"], "macos_calendar": ["list_events"]},
    "recentTools": [{"server": "macos_calendar", "tool": "list_events", "n": 2, "ago": "5m",
                     "ts": 0, "p": "query: <b>not markup</b>"}],
    "tools1h": [["macos_calendar", "list_events", 2]],
    "tools24h": [["macos_calendar", "list_events", 2]],
    "tools7d": [["macos_calendar", "list_events", 9]],
    "models1h": [], "models24h": [{"name": "model-a", "sessions": 3}],
    "models7d": [{"name": "model-a", "sessions": 8}],
    "surfaces1h": [], "surfaces24h": [{"name": "web-chat", "sessions": 3}],
    "surfaces7d": [{"name": "web-chat", "sessions": 8}, {"name": "tool", "sessions": 4}],
}


class TestOverviewRenders(unittest.TestCase):
    """The overview page against a server whose snapshot is canned: drawn
    without a script error, every block present, data drawn as text."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.checker = pathlib.Path(cls._tmp.name) / "pagecheck"
        subprocess.run(["swiftc", "-O", str(HERE / "pagecheck.swift"),
                        "-o", str(cls.checker)], check=True)
        cls._snapshot = overview.snapshot
        overview.snapshot = lambda: SNAPSHOT
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.server.server_address[1]
        cls._hosts = (set(server.ALLOWED_HOSTS), set(server.ALLOWED_ORIGINS))
        server.ALLOWED_HOSTS.add(f"127.0.0.1:{cls.port}")
        server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{cls.port}")
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        out = subprocess.run([str(cls.checker), f"http://127.0.0.1:{cls.port}/overview",
                              str(WAIT_S)], capture_output=True, text=True,
                             check=True, timeout=60)
        cls.page = json.loads(out.stdout)

    @classmethod
    def tearDownClass(cls):
        overview.snapshot = cls._snapshot
        cls.server.shutdown()
        cls.server.server_close()
        server.ALLOWED_HOSTS.clear()
        server.ALLOWED_HOSTS.update(cls._hosts[0])
        server.ALLOWED_ORIGINS.clear()
        server.ALLOWED_ORIGINS.update(cls._hosts[1])
        cls._tmp.cleanup()

    def test_no_script_error(self):
        self.assertEqual(self.page["errors"], [])
        self.assertEqual(self.page["page_error"], "")

    def test_every_block_is_drawn(self):
        for text in ("model-a", "model-b", "down: connection refused", "mac: calendar",
                     "list_events", "https://assistant.example.net/", "410 GB", "WARN"):
            self.assertTrue(text in self.page["text"], f"{text!r} not drawn")

    def test_data_is_drawn_as_text(self):
        self.assertTrue("query: <b>not markup</b>" in self.page["text"])


if __name__ == "__main__":
    unittest.main()
