#!/usr/bin/env python3
"""Tests for services/actions/server.py — the plumbing: guards,
dispatch, /api/state assembly, the status-indicator count.

Handler tests run through http.client against a live ThreadingHTTPServer on
an ephemeral port, with the areas' stores pointed at a temp dir and
emails._spawn recording (never a real execution thread: resolve runs under
emails.LOCK, so a synchronous _spawn would deadlock here). Run:
python3 -m pytest test_server.py -q (from this directory), or
python3 services/actions/test_server.py from the repo root.
"""

import http.client
import http.server
import json
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import emails
import finance
import messages
import research
import server
import hermes_audit
import jobs
import system

CREATE_ARGS = {"calendar": "Personal", "title": "Thursday Run Club",
               "start": "2026-08-04 16:00", "end": "2026-08-04 17:00",
               "notes": "Meet: https://meet.google.com/klm-nopq-rst",
               "repeat": "weekly", "repeat_interval": 1,
               "repeat_until": "2026-08-31",
               "tz": "America/Los_Angeles", "all_day": False}


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
    H = {"X-Actions-Local": "1"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self._saved = (emails.STATE, common.STATE_DIR, emails.STATE_FILE,
                       server._STATUS, emails._spawn,
                       set(server.ALLOWED_HOSTS), set(server.ALLOWED_ORIGINS),
                       emails._CALENDAR_COLORS, finance.STATE_FILE,
                       hermes_audit.LOCK, hermes_audit.RUN,
                       hermes_audit.ERR, messages.STATE_FILE, messages.STATE,
                       system.LABELS,
                       research.TOPICS, research.REPORTS, research.RUNS,
                       research.CLEARED, common.CRON_JOBS,
                       jobs.JOBS_FILE, jobs.EXECUTIONS, jobs._IRIS_STATES)
        common.STATE_DIR = tmp
        emails.STATE_FILE = tmp / "state.json"
        # bound at import from the real state dir, like emails.STATE_FILE;
        # server.AREAS includes finance, so /api/state reads it
        finance.STATE_FILE = tmp / "finance.json"
        messages.STATE_FILE = tmp / "messages.json"
        messages.STATE = messages.empty_state()
        # the audit area reads its own state dir; pointed here, /api/state
        # cannot see the real run lock or report
        hermes_audit.LOCK = tmp / "audit-lock"
        hermes_audit.RUN = tmp / "audit-run.json"
        hermes_audit.ERR = tmp / "audit-last-run.err"
        # no labels means system.state() runs no launchctl, so /api/state here
        # never reads the real launchd domain
        system.LABELS = {}
        # the jobs area reads hermes' jobs file and executions ledger, and
        # every other area reads the same jobs file through common for its
        # job stamps; pointed here, /api/state sees no jobs
        jobs.JOBS_FILE = tmp / "cron-jobs.json"
        jobs.EXECUTIONS = tmp / "executions.db"
        jobs.JOBS_FILE.write_text('{"jobs": []}')
        jobs._IRIS_STATES = {}
        common.CRON_JOBS = jobs.JOBS_FILE
        # the research area reads the vault's research tree, the driver's
        # runs log and its cleared-flags store; pointed at the temp dir,
        # /api/state sees no topics
        research.TOPICS = tmp / "research-topics"
        research.REPORTS = tmp / "research-reports"
        research.RUNS = tmp / "research-runs.jsonl"
        research.CLEARED = tmp / "research-cleared.json"
        server._STATUS = {}
        emails._CALENDAR_COLORS = {}
        self.spawns = []
        emails._spawn = lambda set_id, row_id: self.spawns.append((set_id, row_id))
        emails.STATE = emails.empty_state()
        create = row("create_event", dict(CREATE_ARGS))
        create["id"] = "r1"
        s = set_with_rows(["E1"], [create])
        emails.finalize_set(s)
        emails.STATE["sets"][s["id"]] = s
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.server.server_address[1]
        server.ALLOWED_HOSTS.add(f"127.0.0.1:{self.port}")
        server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{self.port}")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        (emails.STATE, common.STATE_DIR, emails.STATE_FILE,
         server._STATUS, emails._spawn) = self._saved[:5]
        emails._CALENDAR_COLORS = self._saved[7]
        finance.STATE_FILE = self._saved[8]
        hermes_audit.LOCK, hermes_audit.RUN, hermes_audit.ERR = self._saved[9:12]
        messages.STATE_FILE, messages.STATE = self._saved[12:14]
        system.LABELS = self._saved[14]
        research.TOPICS, research.REPORTS, research.RUNS = self._saved[15:18]
        research.CLEARED, common.CRON_JOBS = self._saved[18:20]
        jobs.JOBS_FILE, jobs.EXECUTIONS, jobs._IRIS_STATES = self._saved[20:23]
        server.ALLOWED_HOSTS.clear()
        server.ALLOWED_HOSTS.update(self._saved[5])
        server.ALLOWED_ORIGINS.clear()
        server.ALLOWED_ORIGINS.update(self._saved[6])
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
        return next(r for r in emails.STATE["sets"][set_id]["rows"]
                    if r["id"] == row_id)["args_sha256"]

    # ---- guards ----

    def test_bad_host_403(self):
        code, _ = self.call("GET", "/api/state", headers={"Host": "evil.com"})
        self.assertEqual(code, 403)

    def test_bad_origin_403(self):
        code, _ = self.call("GET", "/api/state", headers={"Origin": "http://evil.com"})
        self.assertEqual(code, 403)

    def test_missing_local_header_403(self):
        code, _ = self.call("POST", "/api/emails/resolve",
                            {"set_id": "s1", "row_id": "r1",
                             "decision": "approve", "args_sha256": "x"})
        self.assertEqual(code, 403)

    # ---- dispatch ----

    def test_unknown_post_404(self):
        code, _ = self.call("POST", "/api/nope", {}, self.H)
        self.assertEqual(code, 404)

    def test_unknown_get_404(self):
        code, _ = self.call("GET", "/api/nope")
        self.assertEqual(code, 404)

    def test_bad_json_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/emails/resolve", "{not json",
                     {"Content-Type": "application/json", **self.H})
        res = conn.getresponse()
        res.read()
        conn.close()
        self.assertEqual(res.status, 400)

    # ---- /demo ----

    def test_demo_state_has_every_key_the_page_reads(self):
        code, d = self.call("GET", "/demo/api/state?hold-update")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(d), ["emails", "finance", "finance_report",
                                     "hermes_audit", "jobs", "messages", "research",
                                     "status_checked_at", "status_issues",
                                     "system"])
        # the real state has one set; the demo data is its own
        self.assertNotIn("s1", [x["id"] for x in d["emails"]["sets"]])

    def test_demo_row_reads_and_unknown_paths(self):
        code, d = self.call("GET", "/demo/api/emails/body?email_id=m-mara")
        self.assertEqual((code, "text" in d), (200, True))
        code, d = self.call("GET", "/demo/api/research/report?slug=daily-new-topic")
        self.assertEqual((code, d["report"]), (200, None))
        self.assertEqual(self.call("GET", "/demo/api/nope")[0], 404)

    def test_demo_post_is_a_202_that_does_nothing(self):
        code, _ = self.call("POST", "/demo/api/emails/resolve",
                            {"set_id": "s1", "row_id": "r1", "decision": "approve",
                             "args_sha256": self._sha("r1")}, self.H)
        self.assertEqual(code, 202)
        self.assertEqual(self.spawns, [])
        self.assertEqual(emails.STATE["sets"]["s1"]["rows"][0]["status"], "pending")
        # the guard still applies
        self.assertEqual(self.call("POST", "/demo/api/emails/resolve", {})[0], 403)

    # ---- /api/state assembly ----

    def test_state_has_the_areas_and_no_status_until_first_check(self):
        code, d = self.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(d), ["emails", "finance", "finance_report",
                                     "hermes_audit", "jobs", "messages", "research", "system"])
        self.assertEqual([s["id"] for s in d["emails"]["sets"]], ["s1"])

    def test_state_poll_with_hold_update_stamps_the_finance_page(self):
        finance._PAGE_SEEN = 0.0
        try:
            code, _ = self.call("GET", "/api/state?hold-update")
            self.assertEqual(code, 200)
            self.assertGreater(finance._PAGE_SEEN, 0.0)
        finally:
            finance._PAGE_SEEN = 0.0

    def test_state_poll_without_the_flag_does_not_stamp(self):
        finance._PAGE_SEEN = 0.0
        code, _ = self.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(finance._PAGE_SEEN, 0.0)

    def test_state_carries_status_once_checked(self):
        server._STATUS = {"issues": 2, "checked_at": "2026-08-11T00:00:00Z"}
        code, d = self.call("GET", "/api/state")
        self.assertEqual((d["status_issues"], d["status_checked_at"]),
                         (2, "2026-08-11T00:00:00Z"))

    def test_state_carries_calendar_colors_once_fetched(self):
        emails._CALENDAR_COLORS = {"Personal": "#83d754"}
        code, d = self.call("GET", "/api/state")
        self.assertEqual(d["emails"]["calendar_colors"], {"Personal": "#83d754"})

    # ---- email endpoints through dispatch (locking lives in the area) ----

    def test_sha_mismatch_409(self):
        code, _ = self.call("POST", "/api/emails/resolve",
                            {"set_id": "s1", "row_id": "r1",
                             "decision": "approve", "args_sha256": "deadbeef"}, self.H)
        self.assertEqual(code, 409)

    def test_busy_approve_409(self):
        """One row executes at a time service-wide: an approve while another
        set's row is in_progress gets a 409 and spawns nothing."""
        r1 = emails.STATE["sets"]["s1"]["rows"][0]
        r1["status"] = "in_progress"
        other = row("mirror_kick", {"days": 365})
        other["id"] = "b1"
        sb = set_with_rows(["E2"], [other])
        sb["id"] = "sB"
        emails.finalize_set(sb)
        emails.STATE["sets"]["sB"] = sb
        code, d = self.call("POST", "/api/emails/resolve",
                            {"set_id": "sB", "row_id": "b1",
                             "decision": "approve",
                             "args_sha256": self._sha("b1", "sB")}, self.H)
        self.assertEqual(code, 409)
        self.assertEqual(d["error"], "another row is executing")
        self.assertEqual(self.spawns, [])

    def test_duplicate_approve_200_never_reexecutes(self):
        body = {"set_id": "s1", "row_id": "r1",
                "decision": "approve", "args_sha256": self._sha("r1")}
        code, _ = self.call("POST", "/api/emails/resolve", body, self.H)
        self.assertEqual(code, 202)
        code, d = self.call("POST", "/api/emails/resolve", body, self.H)
        self.assertEqual((code, d["row"]["status"]), (200, "in_progress"))
        self.assertEqual(self.spawns, [("s1", "r1")])  # spawned exactly once

    def test_empty_emails_reset_400(self):
        code, _ = self.call("POST", "/api/emails/reset", {"email_ids": []}, self.H)
        self.assertEqual(code, 400)

    def test_rescan_starts_the_job_and_touches_no_state(self):
        """The page's run-scan key only starts the agent run, so tapping it
        can never disturb a pending set."""
        class PopenStub:
            DEVNULL = emails.subprocess.DEVNULL

            def __init__(self):
                self.calls = []

            def Popen(self, cmd, **kw):
                self.calls.append(cmd)

        stub = PopenStub()
        saved = emails.subprocess
        emails.subprocess = stub
        try:
            code, d = self.call("POST", "/api/emails/rescan", {}, self.H)
        finally:
            emails.subprocess = saved
        self.assertEqual((code, d), (200, {"started": "actions-inbox-scan"}))
        self.assertEqual(stub.calls, [["hermes", "cron", "run", "actions-inbox-scan"]])
        self.assertEqual(emails.STATE["sets"]["s1"]["state"], "pending")

    def test_rescan_popen_failure_is_500(self):
        """A hermes binary that cannot be started (OSError from Popen)
        surfaces as a 500, not a crashed request."""
        class PopenStub:
            DEVNULL = emails.subprocess.DEVNULL

            def Popen(self, cmd, **kw):
                raise OSError("No such file or directory: 'hermes'")

        saved = emails.subprocess
        emails.subprocess = PopenStub()
        try:
            code, d = self.call("POST", "/api/emails/rescan", {}, self.H)
        finally:
            emails.subprocess = saved
        self.assertEqual(code, 500)
        self.assertIn("could not start hermes", d["error"])


# ---------------------------------------------------------------- status count

class TestStatusCount(unittest.TestCase):
    def setUp(self):
        self._saved = (server._STATUS, server.subprocess, jobs._IRIS_STATES,
                       jobs._IRIS_GENERATED)
        server._STATUS = {}
        jobs._IRIS_STATES = {}
        jobs._IRIS_GENERATED = None

    def tearDown(self):
        (server._STATUS, server.subprocess, jobs._IRIS_STATES,
         jobs._IRIS_GENERATED) = self._saved

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

        server.subprocess = SubStub()
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
        self.assertEqual(server._STATUS["issues"], 3)
        self.assertTrue(server._STATUS["checked_at"])
        # the same pass hands the cron items' states to the jobs area
        self.assertEqual(jobs._IRIS_STATES,
                         {"backup": {"state": "!!", "detail": "FAILED"}})

    def test_unparseable_output_is_minus_one(self):
        self._run_with(stdout="not json")
        self.assertEqual(server._STATUS["issues"], -1)

    def test_timeout_is_minus_one(self):
        self._run_with(raises=TimeoutError("timed out"))
        self.assertEqual(server._STATUS["issues"], -1)


if __name__ == "__main__":
    unittest.main()
