"""Tests for hermes-assistant-status: the caller readers, the address dedup, the
rows a shared server gets, and the Host app's server rows. Everything runs on
temporary files; no probe leaves the process (_http and _listening are
replaced).

    python3 -m unittest hermes/assistant-status/test_status.py
"""

import importlib.machinery
import importlib.util
import json
import os
import plistlib
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
loader = importlib.machinery.SourceFileLoader(
    "assistant_status", os.path.join(HERE, "hermes-assistant-status"))
spec = importlib.util.spec_from_loader("assistant_status", loader)
st = importlib.util.module_from_spec(spec)
loader.exec_module(st)

CONFIG = """\
# a hermes config
providers:
  lmstudio:
    base_url: http://192.168.1.20:1234/v1  # another Mac
    api_key: test-key
tts:
  provider: openai
  openai:
    base_url: "http://localhost:1234/v1"
stt:
  openai:
    base_url: http://127.0.0.1:1234/v1
"""


class Readers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name, text=None, data=None):
        p = os.path.join(self.dir.name, name)
        if data is not None and name.endswith(".plist"):
            with open(p, "wb") as f:
                plistlib.dump(data, f)
        elif data is not None:
            with open(p, "w") as f:
                json.dump(data, f)
        else:
            with open(p, "w") as f:
                f.write(text)
        return p

    def test_yaml_walks_the_key_path(self):
        p = self.path("config.yaml", CONFIG)
        # same leaf name under two parents: the path decides, and a trailing
        # comment or quotes around the value are dropped
        self.assertEqual(st._yaml_path(p, "providers.lmstudio.base_url"),
                         "http://192.168.1.20:1234/v1")
        self.assertEqual(st._yaml_path(p, "tts.openai.base_url"),
                         "http://localhost:1234/v1")
        self.assertEqual(st._yaml_path(p, "stt.openai.base_url"),
                         "http://127.0.0.1:1234/v1")
        with self.assertRaises(KeyError):
            st._yaml_path(p, "providers.openai.base_url")

    def test_sh_export(self):
        p = self.path("run.sh", 'cd x\nexport APP_LLM_URL="http://a:1234/v1"'
                                '   # LM Studio\n')
        self.assertEqual(st._sh_export(p, "APP_LLM_URL"), "http://a:1234/v1")
        with self.assertRaises(KeyError):
            st._sh_export(p, "OTHER")

    def test_plist_and_json_key_paths(self):
        plist = self.path("voice.plist", data={
            "EnvironmentVariables": {"ASSISTANT_AUDIO_URL": "http://a:8305"}})
        conf = self.path("config.json", data={"stt": {"url": "http://a:1234/v1"}})
        self.assertEqual(st._caller_value(
            {"file": plist, "key": "EnvironmentVariables.ASSISTANT_AUDIO_URL"}),
            "http://a:8305")
        self.assertEqual(st._caller_value({"file": conf, "key": "stt.url"}),
                         "http://a:1234/v1")
        with self.assertRaises(KeyError):
            st._caller_value({"file": conf, "key": "stt.model"})

    def test_addresses_dedup_and_notes(self):
        yaml = self.path("config.yaml", CONFIG)
        conf = self.path("config.json", data={"stt": {"url": "http://192.168.1.20:1234/v1"},
                                              "bad": {"url": "not a url"}})
        item = {"name": "lmstudio", "callers": [
            {"file": yaml, "key": "providers.lmstudio.base_url"},
            {"file": yaml, "key": "tts.openai.base_url"},
            {"file": yaml, "key": "stt.openai.base_url"},
            {"file": conf, "key": "stt.url"},
            {"file": conf, "key": "bad.url"},
            {"file": conf, "key": "missing.url"},
            {"file": os.path.join(self.dir.name, "gone.json"), "key": "x"}]}
        found, notes = st._addresses(item)
        # the path is dropped, localhost and 127.0.0.1 are one server
        self.assertEqual(found, {
            "http://192.168.1.20:1234": ["config.yaml", "config.json"],
            "http://127.0.0.1:1234": ["config.yaml", "config.yaml"]})
        self.assertEqual(len(notes), 3)
        self.assertIn("cannot read bad.url from config.json", notes[0])
        self.assertIn("cannot read missing.url from config.json", notes[1])
        self.assertIn("cannot read x from gone.json", notes[2])


class SharedRows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.probed = []
        self.orig = st._http
        st._http = lambda url: (self.probed.append(url), (200, None))[1]
        self.addCleanup(setattr, st, "_http", self.orig)
        self.conf = os.path.join(self.dir.name, "c.json")

    def item(self, **values):
        with open(self.conf, "w") as f:
            json.dump(values, f)
        return {"name": "voice-audio", "launchd": "com.x.audio",
                "health": "/v1/models",
                "callers": [{"file": self.conf, "key": k} for k in values]}

    def test_remote_address_is_http_only(self):
        problems = []
        rows = st._check_shared(self.item(a="http://192.168.1.20:8305"),
                                {}, problems)
        self.assertEqual(rows, [("ok", "192.168.1.20:8305 · http 200",
                                 "http://192.168.1.20:8305")])
        self.assertEqual(self.probed, ["http://192.168.1.20:8305/v1/models"])
        self.assertEqual(problems, [])

    def test_loopback_address_checks_launchd_too(self):
        problems = []
        rows = st._check_shared(self.item(a="http://localhost:8305"), {},
                                problems)
        self.assertEqual(rows[0][0], "!!")
        self.assertIn("not loaded in launchd", rows[0][1])
        self.assertEqual(rows[0][2], "http://127.0.0.1:8305")
        self.assertEqual(problems, ["voice-audio: not loaded in launchd"])

    def test_two_addresses_name_their_callers(self):
        rows = st._check_shared(self.item(a="http://a:8305", b="http://b:8305"),
                                {}, [])
        self.assertEqual([r[2] for r in rows], ["http://a:8305", "http://b:8305"])
        self.assertIn("used by c.json", rows[0][1])

    def test_no_address_is_one_flagged_row(self):
        problems = []
        rows = st._check_shared(self.item(a="nope"), {}, problems)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "!!")
        self.assertIsNone(rows[0][2])
        self.assertIn("no caller names an address", rows[0][1])
        self.assertIn("cannot read a from c.json", rows[0][1])
        self.assertEqual(self.probed, [])
        self.assertEqual(len(problems), 2)


class ServerRows(unittest.TestCase):
    """The Host app's MCP servers: one row each from its child list."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "servers.json")
        self.up = {8351}
        orig = st._listening
        st._listening = lambda port: port in self.up
        self.addCleanup(setattr, st, "_listening", orig)

    def rows(self):
        problems = []
        return st._check_servers({"name": "host", "servers": self.path}, problems), problems

    def test_one_row_per_server_and_a_closed_port_is_a_problem(self):
        with open(self.path, "w") as f:
            json.dump({"servers": [{"name": "macos_calendar", "port": 8351, "env": {"K": "secret"}},
                                   {"name": "weather", "port": 8355}]}, f)
        rows, problems = self.rows()
        self.assertEqual(rows, [
            ("macos_calendar", "ok", "listening", "http://127.0.0.1:8351"),
            ("weather", "!!", "not listening", "http://127.0.0.1:8355")])
        self.assertEqual(problems, ["weather: not listening on :8355"])

    def test_an_unreadable_list_is_one_flagged_row(self):
        rows, problems = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "!!")
        self.assertIn("server list unreadable", rows[0][2])
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
