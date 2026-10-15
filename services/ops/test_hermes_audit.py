#!/usr/bin/env python3
"""Tests for services/ops/hermes_audit.py — stdlib unittest, no live data.

Every path the module reads is pointed at a temp directory and Popen is
recorded, so nothing here starts an audit run or touches the real
the folder ASSISTANT_HERMES_AUDIT_STATE names. The run lock is a real flock: locks belong to the
open file description, so a second open in this process conflicts with the
first exactly like another process would. Run:
python3 -m pytest test_hermes_audit.py -q (from this directory), or
python3 services/ops/test_hermes_audit.py from the repo root.
"""

import fcntl
import json
import pathlib
import subprocess
import sys
import tempfile
import os
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common
import audit_snoozes  # services/mcp/common, which common puts on the path
import hermes_audit
import system


def stamp(minutes_ago):
    """audit.py writes local wall-clock stamps; the stderr check compares them
    against a file mtime, so a fixture's stamps have to be real times."""
    return (datetime.now()
            - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def finding(text, **act):
    """One finding as audit.py records it."""
    return {"text": text, "note": "a note", **({"act": act} if act else {})}


def record(labels_states):
    """A run record: (label, state) pairs, findings on every warn category."""
    return {"step": 4, "total": 9, "category": "tool servers",
            "step_started": stamp(12), "llm": None,
            "llm_started": None, "started_at": stamp(30),
            "finished_at": stamp(5), "accepted": False,
            "categories": [{"label": label, "state": state,
                            "findings": [finding("a finding")] if state == "warn" else [],
                            "notes": []} for label, state in labels_states]}


class Fake:
    """A recorded Popen: never a real child."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return self

    def poll(self):
        return None


class HermesAuditTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "ASSISTANT_HERMES_AUDIT_STATE": str(tmp),
            "ASSISTANT_AUDIT_EXPECTED": str(tmp / "expected.yaml"),
            "ASSISTANT_AUDIT_REPORT": str(tmp / "report.md"),
            "ASSISTANT_OPS_STATE": str(tmp),
            "ASSISTANT_LLM_PAUSE": str(tmp / "assistant-llm-pause.json"),
            "ASSISTANT_LAUNCHD_SOURCE": str(tmp / "launchd")})
        self._env.start()
        self._saved = (hermes_audit._child, common.CRON_JOBS,
                       subprocess.Popen, subprocess.run,
                       system.GATEWAY_PLIST, system.plist_written_at)
        hermes_audit._child = None
        # no test may read the real gateway plist, ~/Library/LaunchAgents or
        # cron files
        system.plist_written_at = lambda: None
        system.GATEWAY_PLIST = tmp / "live" / "ai.hermes.gateway.plist"
        common.CRON_JOBS = tmp / "no-jobs.json"
        self.held = None

    def tearDown(self):
        if self.held is not None:
            self.held.close()
        (hermes_audit._child, common.CRON_JOBS,
         subprocess.Popen, subprocess.run,
         system.GATEWAY_PLIST, system.plist_written_at) = self._saved
        self._env.stop()
        self._tmp.cleanup()

    def hold_lock(self):
        """Take the audit's run lock, as a run in progress would."""
        self.held = open(hermes_audit.lock_file(), "w")
        fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def write_run(self, rec):
        hermes_audit.run_file().write_text(json.dumps(rec))

    # ---- state ----

    # state() carries the cron job's stamps too; with the stand-in files
    # absent they read as Nones
    NO_JOB = {"job_last_run_at": None, "job_next_run_at": None,
              "job_last_failed": False}

    def test_state_before_any_run(self):
        self.assertEqual(hermes_audit.state(),
                         {"running": False, "run": None, "error": None,
                          "handled": {}, "open": 0, **self.NO_JOB})

    def test_state_returns_the_finished_record(self):
        rec = record([("tool approvals", "ok"), ("tool servers", "warn")])
        self.write_run(rec)
        got = hermes_audit.state()
        self.assertEqual((got["running"], got["error"]), (False, None))
        self.assertEqual(got["run"], rec)

    def test_state_is_running_while_its_own_run_starts_up(self):
        hermes_audit._child = Fake()   # poll() None: alive, lock not taken yet
        self.assertTrue(hermes_audit.state()["running"])
        self.assertEqual(hermes_audit.start()[0], 409)

    def test_state_is_running_while_the_lock_is_held(self):
        self.hold_lock()
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = None
        self.write_run(rec)
        got = hermes_audit.state()
        # unfinished plus running is a run in progress, never a failure
        self.assertEqual((got["running"], got["error"]), (True, None))

    def test_state_reports_a_run_that_died_without_a_report(self):
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = None
        self.write_run(rec)
        hermes_audit.err_file().write_text("AUDIT FAILED: cannot read expected.yaml\n")
        got = hermes_audit.state()
        self.assertEqual(got["running"], False)
        self.assertIn("cannot read expected.yaml", got["error"])

    def test_state_reports_a_run_that_never_started(self):
        # a finished record plus newer stderr: the start died before audit.py
        # wrote anything, so the record is the pass before it
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = "2020-01-01T00:00:00"
        self.write_run(rec)
        hermes_audit.err_file().write_text("error: Failed to spawn: `audit.py`\n")
        got = hermes_audit.state()
        self.assertEqual(got["running"], False)
        self.assertIn("Failed to spawn", got["error"])
        self.assertEqual(got["run"]["finished_at"], "2020-01-01T00:00:00")

    def test_state_ignores_stderr_older_than_the_pass(self):
        # the same files, the other way round: the pass came after that error
        hermes_audit.err_file().write_text("an error from an older run\n")
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = "2099-01-01T00:00:00"
        self.write_run(rec)
        self.assertIsNone(hermes_audit.state()["error"])

    def test_state_survives_an_unreadable_record(self):
        hermes_audit.run_file().write_text("{ not json")
        self.assertEqual(hermes_audit.state(),
                         {"running": False, "run": None, "error": None,
                          "handled": {}, "open": 0, **self.NO_JOB})

    def test_state_survives_a_record_that_is_not_an_object(self):
        # one bad file must not throw inside /api/state and blank every area
        hermes_audit.run_file().write_text("[1, 2, 3]")
        self.assertEqual(hermes_audit.state(),
                         {"running": False, "run": None, "error": None,
                          "handled": {}, "open": 0, **self.NO_JOB})

    # ---- the accepted memory review, the fixed gateway plist, the open count ----

    def test_an_accepted_memory_review_handles_its_findings(self):
        rec = record([("settings", "warn"), ("memory", "warn")])
        rec["categories"][1]["findings"] = [finding("LLM note: a", accept=True),
                                            finding("LLM note: b", accept=True)]
        self.write_run(rec)
        self.assertEqual(hermes_audit.state()["open"], 3)
        rec["accepted"] = True
        self.write_run(rec)
        got = hermes_audit.state()
        self.assertEqual(got["handled"], {"memory": {
            "LLM note: a": {"as": "accepted"}, "LLM note: b": {"as": "accepted"}}})
        self.assertEqual(got["open"], 1)

    def test_a_plist_written_after_the_pass_is_fixed(self):
        rec = record([("tool approvals", "warn")])
        stale = "hermes gateway status: Service definition is stale"
        rec["categories"][0]["findings"] = [finding(stale, fix_plist=True)]
        self.write_run(rec)
        system.plist_written_at = lambda: stamp(1)
        self.assertEqual(hermes_audit.state()["handled"],
                         {"tool approvals": {stale: {"as": "fixed"}}})
        system.plist_written_at = lambda: stamp(60)
        self.assertEqual(hermes_audit.state()["open"], 1)

    def test_ok_categories_never_count(self):
        self.write_run(record([("tool approvals", "ok"), ("settings", "warn")]))
        self.assertEqual(hermes_audit.state()["open"], 1)

    # ---- cron job stamps ----

    def test_job_fields_read_the_job_record(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "hermes-audit", "id": "x",
             "last_run_at": "2026-08-16T08:10:00-07:00",
             "next_run_at": "2026-08-23T08:10:00-07:00",
             "last_status": "ok"},
            {"name": "other-job", "last_status": "error"}]}))
        out = hermes_audit._job_fields()
        self.assertEqual(out["job_last_run_at"], "2026-08-16T08:10:00-07:00")
        self.assertEqual(out["job_next_run_at"], "2026-08-23T08:10:00-07:00")
        self.assertFalse(out["job_last_failed"])

    def test_job_fields_flag_a_failed_run(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": [
            {"name": "hermes-audit", "last_run_at": "2026-08-16T08:10:00",
             "next_run_at": None, "last_status": "error"}]}))
        self.assertTrue(hermes_audit._job_fields()["job_last_failed"])

    def test_job_fields_before_the_job_exists(self):
        common.CRON_JOBS.write_text(json.dumps({"jobs": []}))
        self.assertEqual(hermes_audit._job_fields(),
                         {"job_last_run_at": None, "job_next_run_at": None,
                          "job_last_failed": False})

    # ---- starting a run ----

    def test_start_spawns_the_script_detached(self):
        fake = Fake()
        subprocess.Popen = fake
        code, body = hermes_audit.start()
        self.assertEqual((code, body), (200, {"started": True}))
        argv, kw = fake.calls[0]
        self.assertEqual(argv[:3], [str(hermes_audit.UV), "run", "--no-project"])
        self.assertTrue(argv[3].endswith("hermes-audit/audit.py"))
        self.assertIs(kw["start_new_session"], True)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)

    def test_start_refuses_while_a_run_holds_the_lock(self):
        self.hold_lock()
        fake = Fake()
        subprocess.Popen = fake
        code, body = hermes_audit.start()
        self.assertEqual(code, 409)
        self.assertIn("already in progress", body["error"])
        self.assertEqual(fake.calls, [])

    def test_start_refuses_while_the_pause_stands(self):
        hermes_audit.llm_pause.start(30, "other")
        fake = Fake()
        subprocess.Popen = fake
        code, body = hermes_audit.start()
        self.assertEqual(code, 409)
        self.assertIn("paused until", body["error"])
        self.assertEqual(fake.calls, [])

    # ---- stop and reset ----

    def test_control_relays_the_scripts_message(self):
        def fake_run(argv, **kw):
            self.argv = argv
            return subprocess.CompletedProcess(argv, 0, "audit run 42 stopped", "")
        subprocess.run = fake_run
        code, body = hermes_audit.control("--stop", "hermes_audit_stopped")
        self.assertEqual((code, body), (200, {"result": "audit run 42 stopped"}))
        self.assertEqual(self.argv[-1], "--stop")

    def test_control_reports_a_refusal_as_an_error(self):
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 1, "", "AUDIT FAILED: no audit run in progress")
        subprocess.run = fake_run
        code, body = hermes_audit.control("--stop", "hermes_audit_stopped")
        self.assertEqual(code, 500)
        self.assertIn("no audit run in progress", body["error"])

    def test_control_reports_a_timeout_instead_of_raising(self):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 120)
        subprocess.run = fake_run
        code, body = hermes_audit.control("--reset", "hermes_audit_reset")
        self.assertEqual(code, 500)
        self.assertIn("TimeoutExpired", body["error"])

    def test_accept_changes_runs_the_memory_accept_and_starts_no_pass(self):
        def fake_run(argv, **kw):
            self.argv = argv
            return subprocess.CompletedProcess(argv, 0, "accepted the memory review", "")
        subprocess.run = fake_run
        fake = Fake()
        subprocess.Popen = fake
        code, body = hermes_audit._h_accept({})
        self.assertEqual((code, body), (200, {"result": "accepted the memory review"}))
        self.assertEqual(self.argv[-1], "--accept")
        self.assertEqual(fake.calls, [])

    def test_accept_changes_refused_is_an_error(self):
        subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, "", "AUDIT FAILED: an audit run holds the lock")
        code, body = hermes_audit._h_accept({})
        self.assertEqual(code, 500)
        self.assertIn("holds the lock", body["error"])

    # ---- acknowledging a finding into expected.yaml ----

    YAML = ('# comment above\n'
            'doctor_known:\n'
            '  - "⚠ browser (system dependency not met)"\n'
            '\n'
            'platform_tools:\n'
            '  cli:\n'
            '    tools:\n'
            '      - b_tool  # kept with its comment\n'
            '      - d_tool\n'
            '    mcp: []\n'
            'own_skill_edits: []\n')

    def acked(self, *findings):
        """A finished pass whose tools card carries `findings`, and a small
        expected.yaml."""
        rec = record([("tool approvals", "ok"), ("tools and skills", "warn")])
        rec["categories"][1]["findings"] = list(findings)
        self.write_run(rec)
        hermes_audit.expected_file().write_text(self.YAML)
        return rec

    def ack(self, text):
        return hermes_audit.acknowledge({"category": "tools and skills",
                                         "finding": text})

    def edits(self, *edits):
        return {"edits": [{"path": p, "add": a, "remove": r} for p, a, r in edits],
                "label": "write"}

    def test_acknowledge_writes_every_edit_and_keeps_the_rest(self):
        self.acked(finding("tools changed", acknowledge=self.edits(
            (["platform_tools", "cli", "tools"], ["a_tool", "c_tool"], ["d_tool"]),
            (["platform_tools", "cli", "mcp"], ["files"], []),
            (["doctor_known"], ['assistant: ⚠ x "y"'], []))))
        self.assertEqual(self.ack("tools changed"), (200, {"acknowledged": True}))
        self.assertEqual(hermes_audit.expected_file().read_text(), (
            '# comment above\n'
            'doctor_known:\n'
            '  - "assistant: ⚠ x \\"y\\""\n'
            '  - "⚠ browser (system dependency not met)"\n'
            '\n'
            'platform_tools:\n'
            '  cli:\n'
            '    tools:\n'
            '      - a_tool\n'
            '      - b_tool  # kept with its comment\n'
            '      - c_tool\n'
            '    mcp:\n'
            '      - files\n'
            'own_skill_edits: []\n'))

    def test_an_acknowledged_finding_is_handled(self):
        self.acked(finding("skill kept", acknowledge=self.edits(
            (["own_skill_edits"], ["pdf"], []))), finding("a finding"))
        self.assertEqual(hermes_audit.state()["open"], 2)
        self.ack("skill kept")
        got = hermes_audit.state()
        self.assertEqual(got["handled"], {"tools and skills": {
            "skill kept": {"as": "acknowledged"}}})
        self.assertEqual(got["open"], 1)

    def test_removing_the_last_item_leaves_an_empty_list(self):
        self.acked(finding("gone", acknowledge=self.edits(
            (["platform_tools", "cli", "tools"], [], ["b_tool", "d_tool"]))))
        self.ack("gone")
        self.assertIn("    tools: []\n", hermes_audit.expected_file().read_text())

    def test_acknowledge_refuses_a_finding_without_the_key(self):
        self.acked(finding("a finding"))
        code, body = self.ack("a finding")
        self.assertEqual(code, 400)
        self.assertEqual(hermes_audit.expected_file().read_text(), self.YAML)

    def test_acknowledge_refuses_a_finding_not_in_the_pass(self):
        self.acked(finding("a finding"))
        code, body = self.ack("another finding")
        self.assertEqual(code, 404)
        self.assertIn("reload the page", body["error"])

    def test_acknowledge_of_a_missing_list_changes_nothing(self):
        self.acked(finding("x", acknowledge=self.edits(
            (["doctor_known"], ["a"], []), (["bundled_skills"], ["b"], []))))
        code, body = self.ack("x")
        self.assertEqual(code, 500)
        self.assertIn("bundled_skills", body["error"])
        self.assertEqual(hermes_audit.expected_file().read_text(), self.YAML)

    # ---- restoring a skill hermes stopped updating ----

    def test_restore_skill_runs_the_hermes_reset(self):
        self.acked(finding("skill", restore_skill="pdf"))
        calls = []
        subprocess.run = lambda argv, **kw: calls.append(argv) or \
            subprocess.CompletedProcess(argv, 0, "restored", "")
        code, body = hermes_audit.restore_skill(
            {"category": "tools and skills", "finding": "skill"})
        self.assertEqual((code, body), (200, {"restored": "pdf"}))
        self.assertEqual(calls[0][1:], ["skills", "reset", "pdf", "--restore", "--yes"])

    def test_restore_skill_refuses_a_finding_without_the_key(self):
        self.acked(finding("a finding"))
        subprocess.run = lambda argv, **kw: self.fail("ran hermes")
        code, _ = hermes_audit.restore_skill(
            {"category": "tools and skills", "finding": "a finding"})
        self.assertEqual(code, 400)

    # ---- snoozing a finding ----

    def test_snooze_takes_the_finding_out_of_the_count(self):
        self.write_run(record([("agent release", "warn")]))
        code, body = hermes_audit.HANDLERS["/api/hermes-audit/snooze"](
            {"category": "agent release", "finding": "a finding"})
        self.assertEqual(code, 200)
        got = hermes_audit.state()
        self.assertEqual(got["handled"], {"agent release": {"a finding": {
            "as": "snoozed", "until": body["until"]}}})
        self.assertEqual(got["open"], 0)

    def test_unsnooze_puts_it_back(self):
        self.write_run(record([("agent release", "warn")]))
        audit_snoozes.snooze("agent release", "a finding")
        self.assertEqual(hermes_audit.HANDLERS["/api/hermes-audit/unsnooze"](
            {"category": "agent release", "finding": "a finding"}), (200, {}))
        self.assertEqual(hermes_audit.state()["open"], 1)

    def test_snooze_refuses_a_finding_not_in_the_pass(self):
        self.write_run(record([("agent release", "warn")]))
        code, body = hermes_audit.snooze(
            {"category": "agent release", "finding": "another finding"})
        self.assertEqual(code, 404)
        self.assertEqual(audit_snoozes.read(), {})

    # ---- copying a live plist over its setup/launchd copy ----

    DIFFERS = ("plist ai.hermes.gateway.plist differs from the live copy — "
               "hermes rewrote the live plist; check it, then copy it to "
               "setup/launchd")

    def plisted(self, *texts):
        """A finished pass whose local files category carries `texts`, the
        gateway plist one with its copy key, a live plist, and an older copy
        of it."""
        rec = record([("tool approvals", "ok"), ("local files", "warn")])
        rec["categories"][1]["findings"] = [
            finding(t, **({"copy_plist": True} if t == self.DIFFERS else {}))
            for t in texts]
        self.write_run(rec)
        for d, text in ((system.GATEWAY_PLIST.parent, "new"),
                        (hermes_audit.launchd_source(), "old")):
            d.mkdir()
            (d / "ai.hermes.gateway.plist").write_text(text)
        return hermes_audit.launchd_source() / "ai.hermes.gateway.plist"

    def test_copy_plist_copies_the_live_plist(self):
        copy = self.plisted(self.DIFFERS)
        self.assertEqual(hermes_audit.state()["open"], 1)
        code, body = hermes_audit.copy_plist(
            {"category": "local files", "finding": self.DIFFERS})
        self.assertEqual((code, body),
                         (200, {"copied": "ai.hermes.gateway.plist"}))
        self.assertEqual(copy.read_text(), "new")
        self.assertEqual(hermes_audit.state()["handled"], {"local files": {
            self.DIFFERS: {"as": "copied"}}})

    def test_copy_plist_refuses_a_finding_not_in_the_pass(self):
        copy = self.plisted("another finding")
        code, body = hermes_audit.copy_plist(
            {"category": "local files", "finding": self.DIFFERS})
        self.assertEqual(code, 404)
        self.assertIn("reload the page", body["error"])
        self.assertEqual(copy.read_text(), "old")

    def test_copy_plist_refuses_another_finding(self):
        other = "live plist x.plist has no source in ~/Assistant/setup/launchd"
        copy = self.plisted(other)
        code, body = hermes_audit.copy_plist(
            {"category": "local files", "finding": other})
        self.assertEqual(code, 400)
        self.assertEqual(copy.read_text(), "old")

    def test_copy_plist_reports_a_missing_live_plist(self):
        self.plisted(self.DIFFERS)
        system.GATEWAY_PLIST.unlink()
        code, body = hermes_audit.copy_plist(
            {"category": "local files", "finding": self.DIFFERS})
        self.assertEqual(code, 500)
        self.assertIn("could not copy", body["error"])

    # ---- area interface ----

    def test_handlers_cover_the_nine_endpoints(self):
        self.assertEqual(sorted(hermes_audit.HANDLERS),
                         ["/api/hermes-audit/accept-changes",
                          "/api/hermes-audit/acknowledge",
                          "/api/hermes-audit/copy-plist",
                          "/api/hermes-audit/reset",
                          "/api/hermes-audit/restore-skill",
                          "/api/hermes-audit/run", "/api/hermes-audit/snooze",
                          "/api/hermes-audit/stop",
                          "/api/hermes-audit/unsnooze"])

    # ---- the text answers ----

    def status(self):
        return hermes_audit.GET_HANDLERS["/api/hermes-audit/status"]({})[1]["text"]

    def report(self):
        return hermes_audit.GET_HANDLERS["/api/hermes-audit/report"]({})[1]["text"]

    def test_status_before_any_run(self):
        self.assertEqual(self.status(), "No audit run recorded yet.")

    def test_status_while_running_names_the_step_and_the_llm_call(self):
        self.hold_lock()
        rec = record([])
        rec.update(finished_at=None, llm="tool servers review", llm_started=stamp(3))
        self.write_run(rec)
        text = self.status()
        self.assertTrue(text.startswith("Audit run in progress, running 30 min."), text)
        self.assertIn("step 4/9: tool servers, 12 min on this step", text)
        self.assertIn("LLM call in flight: tool servers review, running 3 min", text)

    def test_status_while_starting_up_ignores_the_previous_pass(self):
        self.hold_lock()
        self.write_run(record([("tool approvals", "ok")]))   # finished: the last pass
        self.assertEqual(self.status(), "Audit run in progress. Not finished — no "
                         "report for it yet.\nstarting up (no step written yet)")

    def test_status_of_a_clean_pass(self):
        rec = record([("tool approvals", "ok")])
        self.write_run(rec)
        self.assertEqual(self.status(), f"Last pass finished {rec['finished_at']}: "
                                        "every category clean, no findings.")

    def test_status_names_the_categories_with_findings(self):
        rec = record([("tool approvals", "ok"), ("tool servers", "warn"),
                      ("backups", "warn")])
        self.write_run(rec)
        self.assertEqual(self.status(), f"Last pass finished {rec['finished_at']}: "
                                        "2 of 3 categories carry open findings — "
                                        "tool servers, backups.")

    def test_status_leaves_out_snoozed_findings(self):
        rec = record([("tool servers", "warn"), ("backups", "warn")])
        self.write_run(rec)
        audit_snoozes.snooze("backups", "a finding")
        self.assertEqual(self.status(), f"Last pass finished {rec['finished_at']}: "
                                        "1 of 2 categories carry open findings — "
                                        "tool servers. 1 other finding handled "
                                        "or snoozed on the actions page.")
        audit_snoozes.snooze("tool servers", "a finding")
        self.assertEqual(self.status(), f"Last pass finished {rec['finished_at']}: "
                                        "no open findings. 2 other findings "
                                        "handled or snoozed on the actions page.")

    def test_status_counts_a_repeated_snoozed_finding_as_handled(self):
        rec = record([("tool servers", "warn")])
        rec["categories"][0]["findings"] = [finding("a finding"), finding("a finding")]
        self.write_run(rec)
        audit_snoozes.snooze("tool servers", "a finding")
        self.assertEqual(self.status(), f"Last pass finished {rec['finished_at']}: "
                                        "no open findings. 2 other findings "
                                        "handled or snoozed on the actions page.")

    def test_status_of_a_run_that_died_mid_pass(self):
        rec = record([])
        rec["finished_at"] = None
        self.write_run(rec)
        hermes_audit.err_file().write_text("Traceback: boom\n")
        text = self.status()
        self.assertTrue(text.startswith("FAILED: " + hermes_audit.NO_REPORT), text)
        self.assertTrue(text.endswith("Traceback: boom"), text)

    def test_status_of_a_start_that_never_ran(self):
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = stamp(60)
        self.write_run(rec)
        hermes_audit.err_file().write_text("uv: no such file\n")
        self.assertEqual(self.status(), "FAILED: " + hermes_audit.NEVER_STARTED
                                        + "\nuv: no such file")

    def test_report_returns_the_file(self):
        self.write_run(record([("tool approvals", "ok")]))
        hermes_audit.report_file().write_text("# Audit\nall clean\n")
        self.assertEqual(self.report(), "# Audit\nall clean")

    def test_report_is_withheld_while_a_run_is_going(self):
        self.hold_lock()
        self.write_run(record([]))
        hermes_audit.report_file().write_text("# old pass\n")
        text = self.report()
        self.assertTrue(text.startswith("A run is in progress"), text)
        self.assertNotIn("old pass", text)

    def test_report_without_a_file(self):
        self.assertTrue(self.report().startswith("FAILED: no report at "))

    def test_report_after_a_start_that_never_ran_says_it_is_older(self):
        rec = record([("tool approvals", "ok")])
        rec["finished_at"] = stamp(60)
        self.write_run(rec)
        hermes_audit.report_file().write_text("# Audit\n")
        hermes_audit.err_file().write_text("uv: no such file\n")
        self.assertEqual(self.report(), "The last audit run never started, so this "
                         f"is the pass before it ({rec['finished_at']}), not a "
                         "fresh one:\n\n# Audit")


if __name__ == "__main__":
    unittest.main()
