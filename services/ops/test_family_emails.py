"""Family emails tests. Content reads, the send process and threads are stubbed.

Run with ~/.venvs/mail_tools/bin/python services/ops/test_family_emails.py
(the sender script imports the mail venv's packages).
"""

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import family_emails as mail
import family_emails_send as sender

FINANCE = {"sections": ["Summary", "New transactions"], "detailed": False, "combine": False,
           "show_new_payees": True}
REMINDERS = {"lists": ["Riley"], "include": ["Added"], "skip": True}


class FamilyEmailsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ASSISTANT_OPS_STATE": self.tmp.name,
                                           "ASSISTANT_LLM_PAUSE": os.path.join(self.tmp.name, "pause.json")})
        env.start()
        self.addCleanup(env.stop)
        self.old = mail.DATA, mail.PREVIEWS
        self.addCleanup(self.restore)
        mail.PREVIEWS = {}
        mail.boot()
        mail.DATA["addresses"]["Alex"] = "alex@example.com"
        mail.DATA["addresses"]["Jordan"] = "jordan@example.com"
        mock.patch.object(mail.common, "cron_job", return_value={"enabled": True, "next_run_at": "2026-09-15T07:00:00-07:00"}).start()
        mock.patch.object(mail.reminders, "STATE", {"lists": ["Riley", "Sam READY"]}).start()
        self.threads = mock.patch.object(mail.threading, "Thread").start()
        self.addCleanup(mock.patch.stopall)

    def restore(self):
        mail.DATA, mail.PREVIEWS = self.old

    def stored(self):
        return json.loads(mail.store_path().read_text())

    def add(self, person, kind, settings, enabled=True):
        """A queue item; a finance item's settings become its kind's preset."""
        if kind in mail.SECTIONS:
            mail.DATA["presets"][kind] = dict(settings)
            mail.DATA["queues"][person][kind] = {"enabled": enabled, "reported": {}}
        else:
            mail.DATA["queues"][person][kind] = {"enabled": enabled, "settings": dict(settings)}
        return mail.DATA["queues"][person][kind]

    def ready(self, person="Alex", kind="daily", settings=FINANCE):
        item = {"status": "ready", "settings": dict(settings), "subject": "Finance daily — 2026-09-14",
                "text": "Real built content <not HTML>", "built_at": "2026-09-14T17:00:00Z"}
        mail.PREVIEWS[f"{person}/{kind}"] = item
        return item

    # ---- addresses and queues

    def test_address_saved_lowercase_and_validated(self):
        self.assertEqual(mail.address({"person": "Riley", "address": " RILEY@Example.com "})[0], 200)
        self.assertEqual(self.stored()["addresses"]["Riley"], "riley@example.com")
        self.assertIn("riley@example.com", mail.recipients())
        for bad in ("two@x.com,other@x.com", "nope", "a b@x.com"):
            self.assertEqual(mail.address({"person": "Riley", "address": bad})[0], 400)
        self.assertEqual(mail.address({"person": "Nobody", "address": "x@y.z"})[0], 400)

    def test_address_stays_while_an_email_is_on(self):
        self.add("Alex", "daily", FINANCE)
        self.assertEqual(mail.address({"person": "Alex", "address": ""})[0], 400)
        self.assertEqual(mail.DATA["addresses"]["Alex"], "alex@example.com")

    def test_add_starts_on_with_the_persons_lists(self):
        mail.DATA["addresses"]["Sam"] = "sam@example.com"
        self.assertEqual(mail.queue({"person": "Sam", "kind": "reminders_daily", "action": "add"})[0], 200)
        entry = self.stored()["queues"]["Sam"]["reminders_daily"]
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["settings"], {"lists": ["Sam READY"], "include": ["Added", "Completed"], "skip": True})
        self.assertEqual(mail.queue({"person": "Sam", "kind": "reminders_daily", "action": "add"})[0], 409)

    def test_add_finance_starts_on_only_with_an_address(self):
        mail.queue({"person": "Alex", "kind": "daily", "action": "add"})
        mail.queue({"person": "Riley", "kind": "daily", "action": "add"})
        self.assertTrue(mail.DATA["queues"]["Alex"]["daily"]["enabled"])
        self.assertFalse(mail.DATA["queues"]["Riley"]["daily"]["enabled"])

    def test_remove_takes_the_history_and_the_preview_with_it(self):
        self.add("Alex", "daily", FINANCE)
        self.add("Alex", "weekly", FINANCE)
        self.ready()
        with mail.LOCK:
            gone = mail.record("Alex", "daily", "h", "test", status="sent", subject="s", text="t")
            kept = mail.record("Alex", "weekly", "h", "test", status="sent", subject="s", text="t")
        mail.queue({"person": "Alex", "kind": "daily", "action": "remove"})
        self.assertEqual(list(self.stored()["deliveries"]), [kept["id"]])
        self.assertNotIn("Alex/daily", mail.PREVIEWS)
        self.assertNotIn("daily", mail.DATA["queues"]["Alex"])

    def test_enable_needs_at_least_one_list(self):
        mail.DATA["addresses"]["Sam"] = "sam@example.com"
        mock.patch.object(mail.reminders, "STATE", {"lists": ["Riley"]}).start()
        mail.queue({"person": "Sam", "kind": "reminders_daily", "action": "add"})
        self.assertEqual(mail.DATA["queues"]["Sam"]["reminders_daily"]["settings"]["lists"], [])
        self.assertFalse(mail.DATA["queues"]["Sam"]["reminders_daily"]["enabled"])   # added paused
        code, body = mail.queue({"person": "Sam", "kind": "reminders_daily", "action": "enable"})
        self.assertEqual((code, body["error"]), (400, "Choose at least one reminder list."))
        self.assertFalse(mail.DATA["queues"]["Sam"]["reminders_daily"]["enabled"])

    def test_enable_needs_an_address(self):
        self.add("Riley", "daily", FINANCE, enabled=False)
        self.assertEqual(mail.queue({"person": "Riley", "kind": "daily", "action": "enable"})[0], 400)
        mail.DATA["addresses"]["Riley"] = "riley@example.com"
        self.assertEqual(mail.queue({"person": "Riley", "kind": "daily", "action": "enable"})[0], 200)
        self.assertTrue(self.stored()["queues"]["Riley"]["daily"]["enabled"])
        self.assertEqual(mail.queue({"person": "Riley", "kind": "daily", "action": "disable"})[0], 200)
        self.assertEqual(mail.queue({"person": "Riley", "kind": "daily", "action": "remove"})[0], 200)
        self.assertEqual(self.stored()["queues"]["Riley"], {})
        self.assertEqual(mail.queue({"person": "Riley", "kind": "daily", "action": "disable"})[0], 404)

    def test_a_finance_item_takes_no_settings_of_its_own(self):
        self.add("Alex", "daily", FINANCE)
        code, body = mail.queue({"person": "Alex", "kind": "daily", "action": "settings", "settings": FINANCE})
        self.assertEqual(code, 400)
        self.assertIn("finance reports", body["error"])
        self.assertEqual(mail.DATA["queues"]["Alex"]["daily"], {"enabled": True, "reported": {}})

    def test_default_preset_combines_personal_where_the_kind_takes_it(self):
        self.assertFalse(mail.preset_default("daily")["combine"])
        self.assertTrue(mail.preset_default("weekly")["combine"])
        self.assertTrue(mail.preset_default("monthly")["combine"])
        # only the daily flags new payees out of the box
        self.assertTrue(mail.preset_default("daily")["show_new_payees"])
        self.assertFalse(mail.preset_default("monthly")["show_new_payees"])

    def test_preset_is_checked_and_stored_in_a_fixed_order(self):
        for bad in ({"sections": []}, {"sections": ["made up"]}, "text", None):
            self.assertEqual(mail.preset({"kind": "daily", "settings": bad})[0], 400)
        self.assertEqual(mail.preset({"kind": "reminders_daily", "settings": FINANCE})[0], 400)
        code, _ = mail.preset({"kind": "daily", "settings": {"sections": ["New transactions", "Summary"],
                                                           "detailed": True, "show_new_payees": True}})
        self.assertEqual(code, 200)
        # the daily has no cash flow, so only show new payees holds
        self.assertEqual(self.stored()["presets"]["daily"],
                         {"sections": ["Summary", "New transactions"], "detailed": False,
                          "combine": False, "show_new_payees": True})
        # the weekly and monthly always print the categories, so no such option is kept;
        # the weekly has no charge list, so show new payees does not hold there
        on = {"sections": ["Cash flow"], "detailed": True, "categories": True, "combine": True,
              "show_new_payees": True}
        for kind, payees in (("weekly", False), ("monthly", True)):
            mail.preset({"kind": kind, "settings": on})
            self.assertEqual(mail.DATA["presets"][kind],
                             {"sections": ["Cash flow"], "detailed": True, "combine": True,
                              "show_new_payees": payees})

    def test_every_finance_item_of_a_kind_sends_its_preset(self):
        self.add("Alex", "weekly", FINANCE)
        self.add("Jordan", "weekly", FINANCE)
        weekly = {"sections": ["Cash flow"], "detailed": True, "combine": False, "show_new_payees": False}
        mail.preset({"kind": "weekly", "settings": weekly})
        self.assertEqual([q["settings"] for p in mail.state()["people"] for q in p["queues"]], [weekly, weekly])
        self.assertEqual(mail.state()["presets"]["weekly"], weekly)
        batch, build, send = self.run_batch("weekly", content={"return_value": {"subject": "s", "text": "t"}})
        self.assertEqual([c.args[1] for c in build.call_args_list], [weekly, weekly])

    def test_reminders_settings_are_checked_and_stored_in_a_fixed_order(self):
        base = {"person": "Alex", "kind": "reminders_daily", "action": "settings"}
        self.add("Alex", "reminders_daily", REMINDERS)
        for bad in ({"lists": [], "include": ["Added"]}, {"lists": ["Riley"], "include": ["Soon"]}):
            self.assertEqual(mail.queue({**base, "settings": bad})[0], 400)
        mail.queue({**base, "settings": {"lists": ["Riley", "Riley"], "include": ["Completed", "Overdue"], "skip": False}})
        self.assertEqual(mail.DATA["queues"]["Alex"]["reminders_daily"]["settings"],
                         {"lists": ["Riley"], "include": ["Overdue", "Completed"], "skip": False})

    # ---- state

    def test_state_summarizes_queues_previews_and_last_sends(self):
        self.add("Alex", "daily", FINANCE)
        self.add("Riley", "weekly", FINANCE, enabled=False)
        self.ready()["settings"]["sections"] = ["Summary"]
        with mail.LOCK:
            sent = mail.record("Alex", "daily", "alex@example.com", "scheduled", status="sent",
                               subject="s", text="t", finished_at="2026-09-14T14:00:00Z")
            mail.record("Alex", "daily", "riley@example.com", "test", status="sent",
                        subject="s", text="t", finished_at="2026-09-14T14:30:00Z")
            mail.record("Alex", "daily", "alex@example.com", "test", status="error",
                        subject="s", text="t", note="boom", finished_at="2026-09-14T15:00:00Z")
        state = mail.state()
        alex, riley = state["people"][0]["queues"][0], state["people"][1]["queues"][0]
        self.assertEqual((state["enabled"], state["issues"], state["last_sent"]), (1, 1, "2026-09-14T14:00:00Z"))
        self.assertEqual(state["next_send"], "2026-09-15T07:00:00-07:00")
        self.assertEqual(alex["next"], "2026-09-15T07:00:00-07:00")
        self.assertIsNone(riley["next"])
        self.assertEqual(alex["last"]["status"], "error")
        self.assertEqual(alex["last"]["note"], "boom")
        self.assertEqual([s["id"] for s in alex["sent"]], [sent["id"]])   # the test send is not listed
        self.assertTrue(alex["preview"]["stale"])
        self.assertEqual(state["lists"], ["Riley", "Sam READY"])
        self.assertEqual(state["recipients"], ["alex@example.com", "jordan@example.com"])

    def test_save_keeps_twenty_records_per_item_and_a_week_of_runs(self):
        with mail.LOCK:
            for i in range(25):
                mail.record("Alex", "daily", "h", "test", status="sent", subject="s", text=str(i))
            mail.record("Riley", "daily", "p", "test", status="sent", subject="s", text="riley")
            mail.DATA["batches"]["daily/" + (date.today() - timedelta(days=8)).isoformat()] = {"status": "done"}
            mail.DATA["batches"]["daily/" + date.today().isoformat()] = {"status": "done"}
            mail.save()
        stored = self.stored()
        alex = [d for d in stored["deliveries"].values() if d["person"] == "Alex"]
        self.assertEqual(len(alex), 20)
        self.assertEqual(alex[0]["text"], "5")
        self.assertEqual(len(stored["deliveries"]), 21)
        self.assertEqual(list(stored["batches"]), ["daily/" + date.today().isoformat()])

    def test_boot_marks_interrupted_work(self):
        with mail.LOCK:
            mail.record("Alex", "daily", "h", "test", status="sending", subject="s", text="t")
            batch = "daily/" + date.today().isoformat()
            mail.DATA["batches"][batch] = {"id": batch, "kind": "daily", "status": "running"}
            mail.save()
        mail.boot()
        item = next(iter(mail.DATA["deliveries"].values()))
        self.assertEqual(item["status"], "error")
        self.assertIn("Sent folder", item["note"])
        self.assertEqual(mail.DATA["batches"][batch]["status"], "error")

    # ---- previews and tests

    def test_preview_builds_from_the_saved_settings(self):
        self.add("Alex", "daily", FINANCE)["reported"] = {"old": "2026-09-13"}
        self.assertEqual(mail.preview({"person": "Riley", "kind": "daily"})[0], 404)
        self.assertEqual(mail.preview({"person": "Alex", "kind": "daily"})[0], 202)
        item, kind, reported = self.threads.call_args.kwargs["args"]
        self.assertEqual((kind, reported, item["settings"]), ("daily", {"old": "2026-09-13"}, FINANCE))
        self.assertEqual(mail.preview({"person": "Alex", "kind": "daily"})[0], 409)
        with mock.patch.object(mail, "content", return_value={"subject": "Finance", "text": "body"}) as build:
            mail.build_preview(item, kind, reported)
        self.assertEqual(build.call_args.args[:2], ("daily", FINANCE))
        self.assertEqual(item["status"], "ready")
        self.assertEqual(mail.text({"id": "Alex/daily"})[1]["text"], "body")
        with mock.patch.object(mail, "content", side_effect=ValueError("no budget")):
            mail.build_preview(item, kind, reported)
        self.assertEqual((item["status"], item["error"]), ("error", "no budget"))

    def test_a_pause_refuses_builds_that_call_the_model(self):
        self.add("Alex", "daily", FINANCE)
        self.add("Alex", "weekly", {**FINANCE, "sections": ["Cash flow"]})
        self.add("Alex", "reminders_daily", REMINDERS)
        mail.llm_pause.start(30, "other")
        for request in (mail.preview, mail.run_item):
            code, body = request({"person": "Alex", "kind": "daily"})
            self.assertEqual(code, 409)
            self.assertIn("paused until", body["error"])
        # no Summary, or a reminders email: no model call, so no refusal
        self.assertEqual(mail.preview({"person": "Alex", "kind": "weekly"})[0], 202)
        self.assertEqual(mail.run_item({"person": "Alex", "kind": "reminders_daily"})[0], 202)
        mail.llm_pause.clear()
        self.assertEqual(mail.preview({"person": "Alex", "kind": "daily"})[0], 202)

    def test_test_send_needs_a_current_preview_and_a_saved_recipient(self):
        self.add("Alex", "daily", FINANCE)
        request = {"person": "Alex", "kind": "daily", "to": "jordan@example.com"}
        self.assertEqual(mail.send_test(request)[0], 409)
        item = self.ready()
        item["status"] = "building"
        self.assertEqual(mail.send_test(request)[0], 409)
        item["status"] = "ready"
        item["settings"]["sections"] = ["Summary"]
        self.assertEqual(mail.send_test(request)[0], 409)
        item["settings"] = dict(FINANCE)
        for to in ("stranger@example.com", None):
            self.assertEqual(mail.send_test({**request, "to": to})[0], 400)
        self.threads.assert_not_called()

    def test_test_send_records_the_exact_preview_once(self):
        self.add("Alex", "daily", FINANCE)
        item = self.ready()
        request = {"person": "Alex", "kind": "daily", "to": "jordan@example.com"}
        self.assertEqual(mail.send_test(request)[0], 202)
        record = self.threads.call_args.kwargs["args"][0]
        self.assertEqual((record["subject"], record["text"], record["status"]),
                         ("[Test] " + item["subject"], item["text"], "sending"))
        self.assertEqual(self.stored()["deliveries"][record["id"]]["text"], item["text"])
        self.assertEqual(mail.send_test(request)[0], 409)   # one send at a time

    def test_deliver_sends_the_record_and_keeps_the_outcome(self):
        self.add("Alex", "daily", FINANCE)
        self.ready()
        mail.send_test({"person": "Alex", "kind": "daily", "to": "jordan@example.com"})
        record = self.threads.call_args.kwargs["args"][0]
        run = mock.Mock(returncode=0, stdout='{"status":"sent"}')
        with mock.patch.object(mail.subprocess, "run", return_value=run) as process:
            mail.deliver(record)
        process.assert_called_once()
        self.assertEqual(json.loads(process.call_args.kwargs["input"]),
                         {k: record[k] for k in ("to", "subject", "text")})
        self.assertEqual(self.stored()["deliveries"][record["id"]]["status"], "sent")
        self.assertEqual(mail.text({"id": record["id"]})[1]["to"], "jordan@example.com")

    def test_deliver_keeps_the_senders_error_before_the_warning(self):
        with mail.LOCK:
            record = mail.record("Alex", "daily", "h", "test", status="sending", subject="s", text="t")
        run = mock.Mock(returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'httpx'")
        with mock.patch.object(mail.subprocess, "run", return_value=run):
            mail.deliver(record)
        self.assertEqual(record["status"], "error")
        self.assertTrue(record["note"].startswith("ModuleNotFoundError: No module named 'httpx' Could not confirm"))

    def test_deliver_timeout_is_an_error_and_never_a_retry(self):
        with mail.LOCK:
            record = mail.record("Alex", "daily", "h", "test", status="sending", subject="s", text="t")
        with mock.patch.object(mail.subprocess, "run", side_effect=subprocess.TimeoutExpired("send", 180)) as process:
            mail.deliver(record)
        self.assertEqual(record["status"], "error")
        self.assertIn("Sent folder", record["note"])
        process.assert_called_once()

    def test_real_send_records_reported_ids_only_after_success(self):
        entry = self.add("Alex", "daily", FINANCE)
        with mail.LOCK:
            record = mail.record("Alex", "daily", "h", "now", status="sending", subject="s",
                                 text="t", reported={"t1": "2026-09-14"})
        with mock.patch.object(mail.subprocess, "run", return_value=mock.Mock(returncode=0, stdout='{"status":"error","note":"failed"}')):
            mail.deliver(record)
        self.assertEqual(entry["reported"], {})
        record.update(status="sending", reported={"t1": "2026-09-14"})
        with mock.patch.object(mail.subprocess, "run", return_value=mock.Mock(returncode=0, stdout='{"status":"sent"}')):
            mail.deliver(record)
        self.assertEqual(entry["reported"], {"t1": "2026-09-14"})
        self.assertNotIn("reported", self.stored()["deliveries"][record["id"]])

    # ---- content

    def test_finance_content_passes_settings_and_reported_ids(self):
        with mock.patch.object(mail.subprocess, "run", return_value=mock.Mock(returncode=0, stdout='{"subject":"Finance","text":"body"}')) as process:
            result = mail.content("monthly", FINANCE, datetime(2026, 9, 13, 7), {"a": "2026-09-10"})
        payload = json.loads(process.call_args.kwargs["input"])
        self.assertEqual((payload["settings"], payload["reported"], payload["sample"], payload["month"]),
                         (FINANCE, {"a": "2026-09-10"}, False, "2026-08"))
        self.assertEqual(result["text"], "body")
        with mock.patch.object(mail.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="no budget")):
            with self.assertRaisesRegex(ValueError, "no budget"):
                mail.content("daily", FINANCE, datetime(2026, 9, 13, 7), {})

    def test_reminders_use_selected_lists_and_the_cutoff_window(self):
        def row(rid, name, **kw):
            return {"rid": rid, "name": name, "list": "Riley", "due": "", "done_at": "", "created_at": "",
                    "notes": "", "priority": "", "repeat": "", **kw}
        added = row("new", "Added yesterday", created_at="2026-09-12T11:00")
        open_rows = [added, row("night", "Added before the cutoff", created_at="2026-09-13T06:00"),
                     row("early", "Added before the window", created_at="2026-09-12T07:29"),
                     row("today", "Added after the cutoff", created_at="2026-09-13T08:00"),
                     row("due", "Due today", due="2026-09-13"), row("late", "Late task", due="2026-09-12"),
                     row("next", "Tomorrow task", due="2026-09-14")]
        done_rows = [{**added, "done_at": "2026-09-12T12:00"},
                     row("d1", "Done at night", done_at="2026-09-13T07:29"),
                     row("d2", "Done at the cutoff", done_at="2026-09-13T07:30"),
                     row("oops", "Mistaken add", created_at="2026-09-12T09:00", done_at="2026-09-12T09:03"),
                     row("slow", "Done four minutes on", created_at="2026-09-12T09:00", done_at="2026-09-12T09:04")]

        def read(tool, args, field):
            if tool == "get_lists":
                return [{"name": "Riley"}, {"name": "Sam"}]
            self.assertEqual(args["list_name"], "Riley")
            return done_rows if "completed_on_or_after" in args else open_rows
        spec = {"lists": ["Riley"], "include": list(mail.CONTENT)}
        with mock.patch.object(mail, "host_read", side_effect=read):
            result = mail.content("reminders_daily", spec, datetime(2026, 9, 13, 9), {})
            # a run before the cutoff covers the window that ended at the last one
            early = mail.content("reminders_daily", spec, datetime(2026, 9, 13, 7, 29), {})
            with self.assertRaisesRegex(mail.MissingLists, "Missing lists Gone, Old.") as missing:
                mail.content("reminders_daily", {"lists": ["Gone", "Riley", "Old"], "include": ["Added"]},
                             datetime(2026, 9, 13, 9), {})
        self.assertEqual(missing.exception.names, ["Gone", "Old"])
        text = result["text"]
        self.assertFalse(result["empty"])
        self.assertIn("Added and completed: 2026-09-12 07:30 through 2026-09-13 07:30.\n", text)
        self.assertIn("Added and completed: 2026-09-11 07:30 through 2026-09-12 07:30.\n", early["text"])
        self.assertNotIn("Lists:", text)
        self.assertEqual(text.count("Added yesterday"), 2)  # once added, once completed
        self.assertEqual(text.count("Done four minutes on"), 2)
        for present in ("Added before the cutoff", "Done at night", "Due today", "Late task"):
            self.assertIn(present, text)
        for absent in ("Added before the window", "Added after the cutoff", "Done at the cutoff",
                       "Tomorrow task", "Mistaken add", "Sam"):
            self.assertNotIn(absent, text)

    def test_sample_reminders_read_nothing(self):
        with mock.patch.object(mail, "host_read") as read:
            result = mail.content("reminders_daily", {"lists": ["Riley", "Sam READY"], "include": list(mail.CONTENT)},
                                  datetime(2026, 9, 13, 9), {}, sample=True)
        read.assert_not_called()
        self.assertEqual(result["subject"], "Reminders daily — 2026-09-13 (sample)")
        self.assertIn("Added and completed: 2026-09-12 07:30 through 2026-09-13 07:30.\n", result["text"])
        self.assertFalse(result["empty"])
        for label in mail.CONTENT:
            self.assertIn("## " + label + "\n\n### Riley\n", result["text"])
        self.assertEqual(result["text"].count("### Sam READY"), 3)
        self.assertNotIn("None.", result["text"])

    def test_sample_text_builds_from_the_current_settings(self):
        self.assertEqual(mail.text({"id": "Alex/daily/sample"})[0], 404)
        self.add("Alex", "daily", FINANCE)
        with mock.patch.object(mail, "content", return_value={"subject": "s", "text": "made up"}) as build:
            self.assertEqual(mail.text({"id": "Alex/daily/sample"}), (200, {"subject": "s", "text": "made up"}))
        self.assertEqual(build.call_args.args[:2], ("daily", FINANCE))
        self.assertTrue(build.call_args.kwargs["sample"])
        with mock.patch.object(mail, "content", side_effect=ValueError("no budget")):
            self.assertEqual(mail.text({"id": "Alex/daily/sample"}), (500, {"error": "no budget"}))
        self.assertEqual(mail.PREVIEWS, {})

    def test_truncated_reminders_refuse_to_build(self):
        with mock.patch.object(mail.reminders, "_call", return_value=(True, '{"total":2,"reminders":[]}')):
            with self.assertRaisesRegex(ValueError, "read in full"):
                mail.host_read("search_reminders", {}, "reminders")

    # ---- scheduled runs

    def test_run_once_per_day_unless_it_failed(self):
        self.add("Alex", "reminders_daily", REMINDERS)
        code, batch = mail.run({"kind": "reminders_daily"})
        self.assertEqual((code, batch["status"]), (202, "running"))
        self.assertEqual(mail.run({"kind": "reminders_daily"})[0], 200)
        self.threads.assert_called_once()
        mail.DATA["batches"][batch["id"]]["status"] = "error"
        self.assertEqual(mail.run({"kind": "reminders_daily"})[0], 202)
        self.assertEqual(mail.run({"kind": "nope"})[0], 400)
        self.assertEqual(mail.get_run({"id": batch["id"]})[0], 200)

    def run_batch(self, kind="reminders_daily", **stubs):
        _, batch = mail.run({"kind": kind})
        stored = mail.DATA["batches"][batch["id"]]
        with mock.patch.object(mail, "content", **stubs.get("content", {})) as build, \
                mock.patch.object(mail, "deliver", side_effect=stubs.get("deliver")) as send:
            mail.run_batch(stored)
        return stored, build, send

    def test_paused_items_and_empty_reminders_send_nothing(self):
        self.add("Alex", "reminders_daily", REMINDERS, enabled=False)
        self.add("Riley", "reminders_daily", REMINDERS)
        mail.DATA["addresses"]["Riley"] = "riley@example.com"
        batch, build, send = self.run_batch(content={"return_value": {"subject": "s", "text": "None", "empty": True}})
        self.assertEqual(build.call_count, 1)
        send.assert_not_called()
        self.assertEqual(batch["status"], "done")
        record = next(iter(mail.DATA["deliveries"].values()))
        self.assertEqual((record["person"], record["status"], record["note"]), ("Riley", "skipped", "No matching reminders."))

    def test_a_failed_state_write_ends_the_run_as_an_error(self):
        self.add("Alex", "reminders_daily", REMINDERS)
        _, batch = mail.run({"kind": "reminders_daily"})
        stored = mail.DATA["batches"][batch["id"]]
        writes = iter([OSError("disk full")])

        def save_once_broken():
            problem = next(writes, None)
            if problem:
                raise problem
        with mock.patch.object(mail, "save", side_effect=save_once_broken), \
                mock.patch.object(mail, "content") as build:
            mail.run_batch(stored)
        build.assert_not_called()
        self.assertEqual((stored["status"], stored["error"]), ("error", "The run stopped: disk full"))
        # a re-run the same day is allowed again
        self.assertEqual(mail.run({"kind": "reminders_daily"})[0], 202)

    def test_a_first_save_failure_leaves_no_running_batch(self):
        with mock.patch.object(mail, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mail.run({"kind": "daily"})
        self.assertEqual(mail.DATA["batches"], {})

    def test_run_now_sends_one_item_outside_the_schedule(self):
        self.add("Alex", "reminders_daily", {**REMINDERS, "lists": []}, enabled=False)   # paused items run too
        self.add("Riley", "reminders_daily", REMINDERS)
        self.assertEqual(mail.run_item({"person": "Riley", "kind": "reminders_daily"})[0], 400)   # no address
        self.assertEqual(mail.run_item({"person": "Alex", "kind": "daily"})[0], 404)
        self.assertEqual(mail.run_item({"person": "Alex", "kind": "reminders_daily"}),
                         (400, {"error": "Choose at least one reminder list."}))
        self.add("Alex", "reminders_daily", REMINDERS, enabled=False)
        self.assertEqual(mail.run_item({"person": "Alex", "kind": "reminders_daily"})[0], 202)
        self.assertEqual(mail.run_item({"person": "Alex", "kind": "reminders_daily"})[0], 409)   # one at a time
        item, spec, reported = self.threads.call_args.kwargs["args"]
        self.assertEqual((item["person"], item["mode"], item["status"]), ("Alex", "now", "building"))
        with mock.patch.object(mail, "content", return_value={"subject": "s", "text": "t"}), \
                mock.patch.object(mail, "deliver", side_effect=lambda i: i.update(status="sent")) as send:
            mail.build_and_send(item, spec, reported)
        send.assert_called_once()
        self.assertEqual(mail.run_item({"person": "Alex", "kind": "reminders_daily"})[0], 202)   # again
        # the schedule is untouched: today's run still reaches Alex once the item is on
        mail.DATA["queues"]["Alex"]["reminders_daily"]["enabled"] = True
        for d in mail.DATA["deliveries"].values():
            d["status"] = "sent"
        batch, build, send = self.run_batch(content={"return_value": {"subject": "s", "text": "t"}})
        self.assertEqual([c.args[0]["person"] for c in send.call_args_list], ["Alex"])

    def test_a_missing_list_failure_keeps_the_names(self):
        self.add("Riley", "reminders_daily", REMINDERS)
        mail.DATA["addresses"]["Riley"] = "riley@example.com"
        self.run_batch(content={"side_effect": mail.MissingLists(["Gone"])})
        last = mail.state()["people"][1]["queues"][0]["last"]
        self.assertEqual((last["status"], last["note"], last["missing"]), ("error", "Missing lists Gone.", ["Gone"]))

    def test_a_person_without_an_address_fails_the_run(self):
        self.add("Riley", "reminders_daily", REMINDERS)
        batch, build, send = self.run_batch()
        build.assert_not_called()
        self.assertEqual(batch["status"], "error")
        self.assertIn("Riley: No email address saved.", batch["error"])

    def test_retry_of_a_failed_run_reaches_only_the_people_it_missed(self):
        self.add("Alex", "reminders_daily", REMINDERS)
        self.add("Riley", "reminders_daily", REMINDERS)
        mail.DATA["addresses"]["Riley"] = "riley@example.com"
        content = {"subject": "s", "text": "body", "empty": False}

        def sent(item):
            item["status"] = "sent"
        batch, build, send = self.run_batch(content={"side_effect": [content, ValueError("build failed")]}, deliver=sent)
        self.assertEqual(batch["status"], "error")
        self.assertEqual(batch["error"], "Riley: build failed")
        self.assertEqual(send.call_count, 1)
        batch, build, send = self.run_batch(content={"return_value": content}, deliver=sent)
        build.assert_called_once()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0]["person"], "Riley")
        self.assertEqual(batch["status"], "done")
        self.assertEqual(mail.run({"kind": "reminders_daily"})[0], 200)

    def test_scheduled_finance_record_carries_its_reported_ids(self):
        self.add("Alex", "daily", FINANCE)["reported"] = {"old": "2026-09-13"}
        batch, build, send = self.run_batch("daily", content={"return_value": {"subject": "s", "text": "t", "reported": {"old": "2026-09-13", "new": "2026-09-14"}}})
        self.assertEqual(build.call_args.args[3], {"old": "2026-09-13"})
        record = send.call_args.args[0]
        self.assertEqual(record["reported"], {"old": "2026-09-13", "new": "2026-09-14"})

    # ---- the sender script

    def test_sender_rechecks_recipients_and_makes_one_transport_call(self):
        mail.address({"person": "Riley", "address": "riley@example.com"})
        request = {"to": "riley@example.com", "subject": "[Test] s", "text": "exact content"}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(request))), \
                mock.patch.object(sys, "stdout", io.StringIO()) as output, \
                mock.patch.object(sender.send_mail, "_map_sender_tokens"), \
                mock.patch.object(sender.mail, "_send_allowed", create=True) as send:
            sender.main()
        self.assertEqual(json.loads(output.getvalue())["status"], "sent")
        send.assert_called_once_with([request["to"]], request["subject"], request["text"], mail.recipients())
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({**request, "to": "stranger@example.com"}))), \
                mock.patch.object(sys, "stdout", io.StringIO()) as output, \
                mock.patch.object(sender.mail, "_send_allowed", create=True) as send:
            sender.main()
        send.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "error")

    def test_sender_retries_a_mail_api_failure_once_and_never_a_refusal(self):
        mail.address({"person": "Riley", "address": "riley@example.com"})
        request = json.dumps({"to": "riley@example.com", "subject": "s", "text": "body"})

        def run(answers):
            with mock.patch.object(sys, "stdin", io.StringIO(request)), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as output, \
                    mock.patch.object(sender.send_mail, "_map_sender_tokens"), \
                    mock.patch.object(sender.time, "sleep") as sleep, \
                    mock.patch.object(sender.mail, "_send_allowed", create=True, side_effect=answers) as send:
                sender.main()
            return json.loads(output.getvalue()), send.call_count, sleep.call_args_list
        result, calls, sleeps = run([RuntimeError("The mail service returned HTTP 503."), "SUCCESS"])
        self.assertEqual((result["status"], calls, sleeps), ("sent", 2, [mock.call(sender.RETRY_WAIT)]))
        result, calls, sleeps = run([RuntimeError("A."), RuntimeError("B.")])
        self.assertEqual((result["status"], calls), ("error", 2))
        self.assertTrue(result["note"].startswith("A. Retried after 5 min: B. Could not confirm"))
        result, calls, sleeps = run([ValueError("subject is required")])
        self.assertEqual((result["status"], result["note"], calls, sleeps),
                         ("error", "subject is required", 1, []))

    def test_shared_transport_accepts_a_saved_recipient_but_the_mcp_tool_still_refuses(self):
        responses = [
            [["Identity/get", {"list": [{"id": "home", "email": "home@example.com"}]}, "i"]],
            [["Email/set", {"created": {"m": {"id": "email1"}}}, "m"]],
            [["EmailSubmission/set", {"created": {"s": {"id": "submission1"}}}, "s"]],
        ]
        session = {"username": "home@example.com", "primaryAccounts": {"urn:mail": "account1"}}
        with mock.patch.object(sender.mail, "_get_session", return_value=session), \
                mock.patch.object(sender.mail, "_get_mailbox_id", side_effect=lambda role: role), \
                mock.patch.object(sender.mail, "_api_send", side_effect=responses) as api:
            result = sender.mail._send_allowed(["jordan@example.com"], "[Test] Finance", "exact body", mail.recipients())
            self.assertTrue(result.startswith("SUCCESS:"))
            email = api.call_args_list[1].args[0][0][1]["create"]["m"]
            self.assertEqual(email["to"], [{"email": "jordan@example.com"}])
            self.assertEqual(email["bodyValues"]["body"]["value"], "exact body")
            with self.assertRaisesRegex(ValueError, "allowlist"):
                sender.mail._send(["jordan@example.com"], "s", "b")
            self.assertEqual(api.call_count, 3)

    def test_status_adds_each_kinds_newest_run(self):
        mail.DATA["batches"] = {
            "daily/2026-09-14": {"id": "daily/2026-09-14", "kind": "daily", "status": "done"},
            "daily/2026-09-15": {"id": "daily/2026-09-15", "kind": "daily", "status": "error",
                                 "error": "Alex: boom"},
            "monthly/2026-09-01": {"id": "monthly/2026-09-01", "kind": "monthly", "status": "done"}}
        code, body = mail.GET_HANDLERS["/api/family-emails/state"]({})
        self.assertEqual(code, 200)
        self.assertEqual({k: r["id"] for k, r in body["runs"].items()},
                         {"daily": "daily/2026-09-15", "monthly": "monthly/2026-09-01"})
        self.assertEqual(body["people"], mail.state()["people"])

    def test_add_and_status_read_the_lists_when_none_are_known(self):
        def read():
            mail.reminders.STATE = {"lists": ["Sam READY", "Riley"]}
        with mock.patch.object(mail.reminders, "STATE", {"lists": []}), \
                mock.patch.object(mail.reminders, "refresh", side_effect=read) as refresh:
            self.assertEqual(mail.queue({"person": "Sam", "kind": "reminders_daily",
                                         "action": "add"})[0], 200)
            self.assertEqual(mail.DATA["queues"]["Sam"]["reminders_daily"]["settings"]["lists"],
                             ["Sam READY"])
            self.assertEqual(mail.GET_HANDLERS["/api/family-emails/state"]({})[1]["lists"],
                             ["Sam READY", "Riley"])
        refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
