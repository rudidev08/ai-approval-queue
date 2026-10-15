#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1", "pyyaml==6.0.3"]
# ///
"""Tests for services/ops/server.py — the plumbing: guards,
dispatch, /api/state assembly, the status-indicator count.

Handler tests run through http.client against a live ThreadingHTTPServer on
an ephemeral port, with the areas' stores pointed at a temp dir and
inbox._spawn recording (never a real execution thread: resolve runs under
inbox.LOCK, so a synchronous _spawn would deadlock here). The server
pulls in the mcp and pyyaml packages, so run through uv, which reads the
dependency header above (the same one server.py carries):
uv run --no-project test_server.py (from this directory).
"""

import http.client
import http.server
import json
import pathlib
import sys
import tempfile
import threading
import os
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import inbox
import finance
import chat_area
import research
import reminders
import server
import hermes_audit
import jobs
import system
import job_rules
import overview

CREATE_ARGS = {"calendar": "Work", "title": "Morning Fitness Group",
               "start": "2026-08-04 16:00", "end": "2026-08-04 17:00",
               "notes": "Meet: https://meet.google.com/new-link-111",
               "repeat": "weekly", "repeat_interval": 1,
               "repeat_until": "2026-08-31",
               "tz": "America/Phoenix", "all_day": False}


def row(kind, args):
    return {"id": "t1", "kind": kind, "args": args, "args_sha256": "",
            "label": "row label", "status": "pending", "status_text": ""}


def set_with_rows(email_ids, rows):
    return {"id": "s1", "title": "t", "rationale": "r",
            "email_ids": email_ids, "emails": [], "created_at": "",
            "created_by": {"job": "x", "session": ""},
            "state": "pending", "superseded_by": None, "rows": rows}


# ---------------------------------------------------------------- handler

class TestHandler(unittest.TestCase):
    H = {"X-Local-Request": "1"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        # every folder or file an area reads or writes, under the temp dir:
        # /api/state cannot see a real state file, run lock, report, topic
        # or budget copy
        self._env = mock.patch.dict(os.environ, {
            "ASSISTANT_OPS_STATE": str(tmp),
            "ASSISTANT_HERMES_AUDIT_STATE": str(tmp / "audit"),
            "ASSISTANT_AUDIT_EXPECTED": str(tmp / "expected.yaml"),
            "ASSISTANT_WEB_RESEARCH": str(tmp / "research"),
            "ASSISTANT_WEB_RESEARCH_STATE": str(tmp / "research-state"),
            "ASSISTANT_FINANCE_NOTES": str(tmp / "finances"),
            "ASSISTANT_BUDGET_API_CACHE": str(tmp / "api-cache"),
            "ASSISTANT_LLM_PAUSE": str(tmp / "assistant-llm-pause.json"),
            "ASSISTANT_LAUNCHD_SOURCE": str(tmp / "launchd")})
        self._env.start()
        self._saved = (inbox.STATE, system._STATUS, inbox._spawn,
                       set(server.ALLOWED_HOSTS), set(server.ALLOWED_ORIGINS),
                       inbox._CALENDARS, chat_area.STATE, common.CRON_JOBS,
                       jobs.JOBS_FILE, jobs.EXECUTIONS, jobs._ASSISTANT_STATES)
        chat_area.STATE = chat_area.empty_state()
        # the reminders and inbox areas read the host app on a hold-update
        # poll; pushed past any clock here so no test reaches the real host app
        self._rem = (reminders.STATE, reminders._NEXT_READ, inbox._CALENDARS_NEXT_TRY)
        reminders.STATE = reminders._empty("FAILED: no host app in tests")
        reminders._NEXT_READ = inbox._CALENDARS_NEXT_TRY = float("inf")
        # the jobs area reads hermes' jobs file and executions ledger, and
        # every other area reads the same jobs file through common for its
        # job stamps; pointed here, /api/state sees no jobs
        jobs.JOBS_FILE = tmp / "cron-jobs.json"
        jobs.EXECUTIONS = tmp / "executions.db"
        jobs.JOBS_FILE.write_text('{"jobs": []}')
        jobs._ASSISTANT_STATES = {}
        common.CRON_JOBS = jobs.JOBS_FILE
        system._STATUS = {"items": None, "checked_at": None}
        inbox._CALENDARS = []
        job_rules.boot()   # an empty rules file in the temp state folder
        self.spawns = []
        inbox._spawn = lambda set_id, row_id: self.spawns.append((set_id, row_id))
        inbox.STATE = inbox.empty_state()
        create = row("create_event", dict(CREATE_ARGS))
        create["id"] = "r1"
        s = set_with_rows(["E1"], [create])
        inbox.finalize_set(s)
        inbox.STATE["sets"][s["id"]] = s
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.server.server_address[1]
        server.ALLOWED_HOSTS.add(f"127.0.0.1:{self.port}")
        server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{self.port}")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        (inbox.STATE, system._STATUS, inbox._spawn) = self._saved[:3]
        inbox._CALENDARS, chat_area.STATE, common.CRON_JOBS = self._saved[5:8]
        jobs.JOBS_FILE, jobs.EXECUTIONS, jobs._ASSISTANT_STATES = self._saved[8:11]
        reminders.STATE, reminders._NEXT_READ, inbox._CALENDARS_NEXT_TRY = self._rem
        server.ALLOWED_HOSTS.clear()
        server.ALLOWED_HOSTS.update(self._saved[3])
        server.ALLOWED_ORIGINS.clear()
        server.ALLOWED_ORIGINS.update(self._saved[4])
        self._env.stop()
        self._tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Content-Type": "application/json", **(headers or {})})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        try:
            return res.status, json.loads(data)
        except ValueError:
            return res.status, {}

    def _sha(self, row_id, set_id="s1"):
        return next(r for r in inbox.STATE["sets"][set_id]["rows"]
                    if r["id"] == row_id)["args_sha256"]

    # ---- page files ----

    def test_the_two_pages_and_a_shared_file(self):
        for path in ("/", "/overview", "/bar.js", "/theme.css", "/actions-icon.png"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", path)
            res = conn.getresponse()
            res.read()
            conn.close()
            self.assertEqual(res.status, 200, path)

    def test_only_known_files_directly_in_pages(self):
        # source next to pages/, a path out of it, a hidden name, an unknown
        # type: none is served
        for path in ("/server.py", "/../server.py", "/pages/bar.js", "/.DS_Store",
                     "/actions", "/overview.html/x"):
            code, _ = self.call("GET", path)
            self.assertEqual(code, 404, path)

    def test_head_probes_a_page_file(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("HEAD", "/dev1.html")
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("HEAD", "/bar.js")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    # ---- guards ----

    def test_bad_host_403(self):
        code, _ = self.call("GET", "/api/state", headers={**self.H, "Host": "evil.com"})
        self.assertEqual(code, 403)

    def test_bad_origin_403(self):
        code, _ = self.call("GET", "/api/state", headers={**self.H, "Origin": "http://evil.com"})
        self.assertEqual(code, 403)

    def test_missing_local_header_403(self):
        code, _ = self.call("POST", "/api/inbox/resolve",
                            {"set_id": "s1", "row_id": "r1",
                             "decision": "approve", "args_sha256": "x"})
        self.assertEqual(code, 403)

    def test_an_api_get_without_the_local_header_403(self):
        for path in ("/api/state", "/api/soul-tests/result", "/demo/api/state"):
            self.assertEqual(self.call("GET", path), (403, {"error": "API calls need X-Local-Request: 1"}))

    def test_the_page_loads_without_the_local_header(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        res = conn.getresponse()
        res.read()
        conn.close()
        self.assertEqual(res.status, 200)

    # ---- dispatch ----

    def test_unknown_post_404(self):
        code, _ = self.call("POST", "/api/nope", {}, self.H)
        self.assertEqual(code, 404)

    def test_unknown_get_404(self):
        code, _ = self.call("GET", "/api/nope", headers=self.H)
        self.assertEqual(code, 404)

    def test_api_modules_are_dispatched(self):
        with mock.patch.dict(overview.GET_HANDLERS,
                             {"/api/overview/echo": lambda params: (200, params)}):
            self.assertEqual(self.call("GET", "/api/overview/echo?a=1", headers=self.H),
                             (200, {"a": "1"}))

    def test_a_raising_handler_answers_500_with_its_error(self):
        def boom(_):
            raise KeyError("id")
        with mock.patch.dict(overview.GET_HANDLERS, {"/api/overview/boom": boom}), \
                mock.patch.dict(job_rules.HANDLERS, {"/api/rules/boom": boom}), \
                mock.patch("traceback.print_exc"):
            self.assertEqual(self.call("GET", "/api/overview/boom", headers=self.H),
                             (500, {"error": "KeyError: 'id'"}))
            self.assertEqual(self.call("POST", "/api/rules/boom", {}, self.H),
                             (500, {"error": "KeyError: 'id'"}))

    def test_bad_json_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/inbox/resolve", "{not json",
                     {"Content-Type": "application/json", **self.H})
        res = conn.getresponse()
        res.read()
        conn.close()
        self.assertEqual(res.status, 400)

    # ---- /demo ----

    def test_demo_state_has_every_key_the_page_reads(self):
        code, d = self.call("GET", "/demo/api/state?hold-update", headers=self.H)
        self.assertEqual(code, 200)
        self.assertEqual(sorted(d), ["chat", "family_emails", "finance", "finance_reports",
                                     "gateways", "hermes_audit", "inbox", "jobs",
                                     "reminders", "rules",
                                     "status_checked_at", "status_issues", "system",
                                     "web_research"])
        # the real state has one set; the demo data is its own
        self.assertNotIn("s1", [x["id"] for x in d["inbox"]["sets"]])

    def test_demo_row_reads_and_unknown_paths(self):
        code, d = self.call("GET", "/demo/api/inbox/body?email_id=m-dentist", headers=self.H)
        self.assertEqual((code, "text" in d), (200, True))
        code, d = self.call("GET", "/demo/api/web_research/report?slug=daily-new-topic", headers=self.H)
        self.assertEqual((code, d["report"]), (200, None))
        self.assertEqual(self.call("GET", "/demo/api/nope", headers=self.H)[0], 404)

    def test_demo_post_is_a_202_that_does_nothing(self):
        code, _ = self.call("POST", "/demo/api/inbox/resolve",
                            {"set_id": "s1", "row_id": "r1", "decision": "approve",
                             "args_sha256": self._sha("r1")}, self.H)
        self.assertEqual(code, 202)
        self.assertEqual(self.spawns, [])
        self.assertEqual(inbox.STATE["sets"]["s1"]["rows"][0]["status"], "pending")
        # the guard still applies
        self.assertEqual(self.call("POST", "/demo/api/inbox/resolve", {})[0], 403)

    # ---- /api/state assembly ----

    def test_state_has_the_areas_and_no_status_until_first_check(self):
        code, d = self.call("GET", "/api/state", headers=self.H)
        self.assertEqual(code, 200)
        self.assertEqual(sorted(d), ["chat", "family_emails", "finance", "finance_reports",
                                     "hermes_audit", "inbox", "jobs",
                                     "reminders", "rules", "system", "web_research"])
        self.assertEqual([s["id"] for s in d["inbox"]["sets"]], ["s1"])

    def test_state_poll_with_hold_update_stamps_the_finance_page(self):
        finance._PAGE_SEEN = 0.0
        try:
            code, _ = self.call("GET", "/api/state?hold-update", headers=self.H)
            self.assertEqual(code, 200)
            self.assertGreater(finance._PAGE_SEEN, 0.0)
        finally:
            finance._PAGE_SEEN = 0.0

    def test_state_poll_without_the_flag_does_not_stamp(self):
        finance._PAGE_SEEN = 0.0
        code, _ = self.call("GET", "/api/state", headers=self.H)
        self.assertEqual(code, 200)
        self.assertEqual(finance._PAGE_SEEN, 0.0)

    def test_only_the_hold_update_poll_starts_the_host_app_reads(self):
        with mock.patch.object(reminders, "page_poll") as rem, \
             mock.patch.object(inbox, "page_poll") as col:
            self.call("GET", "/api/state", headers=self.H)
            rem.assert_not_called()
            col.assert_not_called()
            self.call("GET", "/api/state?hold-update", headers=self.H)
            rem.assert_called_once()
            col.assert_called_once()

    def test_clear_cache_drops_both_reads_and_checks_status_now(self):
        checked = threading.Event()
        with mock.patch.object(reminders, "clear") as rem, \
             mock.patch.object(inbox, "clear_calendars") as col, \
             mock.patch.object(server, "_status_once", checked.set):
            code, _ = self.call("POST", "/api/system/clear-cache", {}, self.H)
            self.assertTrue(checked.wait(2))   # the check runs on its own thread
        self.assertEqual(code, 200)
        rem.assert_called_once()
        col.assert_called_once()

    def test_clear_cache_is_mutating(self):
        code, _ = self.call("POST", "/api/system/clear-cache", {})
        self.assertEqual(code, 403)

    def test_state_carries_status_once_checked(self):
        # the count is the system area's problem items, -1 for a failed check
        system.set_status([{"name": "voice-audio", "state": "!!", "detail": ""},
                           {"name": "backup", "state": "!!", "detail": ""}],
                          "2026-08-11T00:00:00Z")
        code, d = self.call("GET", "/api/state", headers=self.H)
        self.assertEqual((d["status_issues"], d["status_checked_at"]),
                         (2, "2026-08-11T00:00:00Z"))
        system.set_status(None, "2026-08-11T00:01:00Z")
        code, d = self.call("GET", "/api/state", headers=self.H)
        self.assertEqual((d["status_issues"], d["status_checked_at"]),
                         (-1, "2026-08-11T00:01:00Z"))

    def test_state_carries_the_calendars_once_fetched(self):
        inbox._CALENDARS = [{"title": "Work", "color": "#83D754",
                              "editable": True, "note": ""}]
        code, d = self.call("GET", "/api/state", headers=self.H)
        self.assertEqual(d["inbox"]["calendar_colors"], {"Work": "#83d754"})
        self.assertEqual(d["inbox"]["calendars"], ["Work"])

    def test_state_carries_the_job_rules(self):
        code, d = self.call("GET", "/api/state", headers=self.H)
        self.assertEqual(d["rules"], {"inbox": {"rules": [], "suggestions": []}})

    # ---- email endpoints through dispatch (locking lives in the area) ----

    def test_sha_mismatch_409(self):
        code, _ = self.call("POST", "/api/inbox/resolve",
                            {"set_id": "s1", "row_id": "r1",
                             "decision": "approve", "args_sha256": "deadbeef"}, self.H)
        self.assertEqual(code, 409)

    def test_busy_approve_409(self):
        """One row executes at a time service-wide: an approve while another
        set's row is in_progress gets a 409 and spawns nothing."""
        r1 = inbox.STATE["sets"]["s1"]["rows"][0]
        r1["status"] = "in_progress"
        other = row("create_event", dict(CREATE_ARGS, title="Other"))
        other["id"] = "b1"
        sb = set_with_rows(["E2"], [other])
        sb["id"] = "sB"
        inbox.finalize_set(sb)
        inbox.STATE["sets"]["sB"] = sb
        code, d = self.call("POST", "/api/inbox/resolve",
                            {"set_id": "sB", "row_id": "b1",
                             "decision": "approve",
                             "args_sha256": self._sha("b1", "sB")}, self.H)
        self.assertEqual(code, 409)
        self.assertEqual(d["error"], "another row is executing")
        self.assertEqual(self.spawns, [])

    def test_duplicate_approve_200_never_reexecutes(self):
        body = {"set_id": "s1", "row_id": "r1",
                "decision": "approve", "args_sha256": self._sha("r1")}
        code, _ = self.call("POST", "/api/inbox/resolve", body, self.H)
        self.assertEqual(code, 202)
        code, d = self.call("POST", "/api/inbox/resolve", body, self.H)
        self.assertEqual((code, d["row"]["status"]), (200, "in_progress"))
        self.assertEqual(self.spawns, [("s1", "r1")])  # spawned exactly once

    def test_empty_emails_reset_400(self):
        code, _ = self.call("POST", "/api/inbox/reset", {"email_ids": []}, self.H)
        self.assertEqual(code, 400)

    def test_rescan_starts_the_job_and_touches_no_state(self):
        """The page's run-scan key only starts the agent run, so tapping it
        can never disturb a pending set."""
        class PopenStub:
            DEVNULL = inbox.subprocess.DEVNULL

            def __init__(self):
                self.calls = []

            def Popen(self, cmd, **kw):
                self.calls.append(cmd)

        stub = PopenStub()
        saved = inbox.subprocess
        inbox.subprocess = stub
        try:
            code, d = self.call("POST", "/api/inbox/rescan", {}, self.H)
        finally:
            inbox.subprocess = saved
        self.assertEqual((code, d), (200, {"started": "actions-inbox-scan"}))
        self.assertEqual(stub.calls, [["hermes", "cron", "run", "actions-inbox-scan"]])
        self.assertEqual(inbox.STATE["sets"]["s1"]["state"], "pending")

    def test_rescan_popen_failure_is_500(self):
        """A hermes binary that cannot be started (OSError from Popen)
        surfaces as a 500, not a crashed request."""
        class PopenStub:
            DEVNULL = inbox.subprocess.DEVNULL

            def Popen(self, cmd, **kw):
                raise OSError("No such file or directory: 'hermes'")

        saved = inbox.subprocess
        inbox.subprocess = PopenStub()
        try:
            code, d = self.call("POST", "/api/inbox/rescan", {}, self.H)
        finally:
            inbox.subprocess = saved
        self.assertEqual(code, 500)
        self.assertIn("could not start hermes", d["error"])


# ---------------------------------------------------------------- status count

class TestStatusCount(unittest.TestCase):
    def setUp(self):
        self._saved = (system.subprocess, jobs._ASSISTANT_STATES,
                       jobs._ASSISTANT_GENERATED, system._STATUS)
        jobs._ASSISTANT_STATES = {}
        jobs._ASSISTANT_GENERATED = None
        system._STATUS = {"items": None, "checked_at": None}

    def tearDown(self):
        (system.subprocess, jobs._ASSISTANT_STATES,
         jobs._ASSISTANT_GENERATED, system._STATUS) = self._saved

    def _run_with(self, stdout='{"generated": "2026-08-25T10:00:00-07:00", '
                               '"categories": []}',
                  returncode=0, raises=None):
        class Proc:
            pass
        p = Proc()
        p.stdout = stdout
        p.returncode = returncode

        class SubStub:
            def run(self, *a, **kw):
                if raises:
                    raise raises
                return p

        system.subprocess = SubStub()
        server._status_once()

    def test_counts_everything_not_ok_or_idle(self):
        item = lambda state, kind="service", name="x", detail="": {
            "state": state, "kind": kind, "name": name, "detail": detail}
        self._run_with(json.dumps({
            "generated": "2026-08-25T10:00:00-07:00", "categories": [
            {"name": "a", "items": [item("ok"), item("!!"),
                                    item("--"), item("?")]},
            {"name": "b", "items": [item("ok"),
                                    item("!!", "cron", "backup", "FAILED")]},
        ]}))
        self.assertTrue(system._STATUS["checked_at"])
        # the same pass hands the cron items' states to the jobs area
        self.assertEqual(jobs._ASSISTANT_STATES,
                         {"backup": {"state": "!!", "detail": "FAILED"}})
        # and the problem items to the system area, in report order
        self.assertEqual(system._STATUS["items"], [
            {"name": "x", "state": "!!", "detail": ""},
            {"name": "x", "state": "?", "detail": ""},
            {"name": "backup", "state": "!!", "detail": "FAILED"}])

    def test_unparseable_output_is_minus_one(self):
        self._run_with(stdout="not json")
        # a failed check tells the system area so, with the try's stamp
        self.assertIsNone(system._STATUS["items"])
        self.assertTrue(system._STATUS["checked_at"])

    def test_timeout_is_minus_one(self):
        self._run_with(raises=TimeoutError("timed out"))
        self.assertIsNone(system._STATUS["items"])
        self.assertTrue(system._STATUS["checked_at"])


if __name__ == "__main__":
    unittest.main()
