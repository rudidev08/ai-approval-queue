#!/usr/bin/env python3
"""model_fallback tests — what each call is sent, when the backup runs, and
that every failure is raised. urlopen and subprocess.run are replaced; no network, no
claude run, no key file.
Run: python3 services/mcp/common/test_model_fallback.py
"""

import http.client
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model_fallback as mf  # noqa: E402

BODY = {"model": "primary", "max_tokens": 10, "reasoning": {"effort": "low"},
        "messages": [{"role": "system", "content": "be brief"},
                     {"role": "user", "content": "hi"}]}
ANSWER = {"choices": [{"message": {"content": "ok"}}]}


def http_error(code, message=""):
    body = json.dumps({"error": {"message": message}}).encode() if message else b""
    return urllib.error.HTTPError("https://primary/v1/chat/completions", code,
                                  "err", {}, io.BytesIO(body))


def response(payload):
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(payload)
    return resp


def claude_run(result=None, returncode=0, is_error=False, structured=None):
    out = {"result": result, "is_error": is_error}
    if structured is not None:
        out["structured_output"] = structured
    return subprocess.CompletedProcess([], returncode, json.dumps(out), "")


class TestModelCalls(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(mf, "api_key", lambda name: "key-" + name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, http=(), claude=(), body=BODY, chain=False, backup=mf.LOCAL):
        with mock.patch("urllib.request.urlopen", side_effect=list(http)) as urlopen, \
                mock.patch("subprocess.run", side_effect=list(claude)) as run:
            if chain:
                out = mf.chain(body, 5, backup)
            else:
                out = mf.chat("https://primary/v1", "PRIMARY_KEY", body, 5)
        return out, urlopen.call_args_list, run.call_args_list

    def test_chat_posts_to_the_callers_model(self):
        out, posts, runs = self.call(http=[response(ANSWER)])
        self.assertEqual(out, ANSWER)
        self.assertEqual((len(posts), len(runs)), (1, 0))
        req = posts[0].args[0]
        self.assertEqual(req.full_url, "https://primary/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer key-PRIMARY_KEY")

    def test_chat_raises_every_http_error_with_its_status(self):
        for code in (400, 401, 403, 429, 503):
            with self.subTest(code=code):
                with self.assertRaises(mf.ProviderError) as cm:
                    self.call(http=[http_error(code, "bad key")])
                self.assertEqual(cm.exception.code, code)
                self.assertIn(f"https://primary/v1: HTTP {code}: bad key", str(cm.exception))

    def test_chat_raises_an_unreachable_or_broken_answer(self):
        broken = mock.MagicMock()
        broken.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"{")
        reset = mock.MagicMock()
        reset.__enter__.return_value.read.side_effect = ConnectionResetError("reset")
        html = mock.MagicMock()
        html.__enter__.return_value.read.return_value = b"<html>gateway</html>"
        for failed in (urllib.error.URLError("refused"), TimeoutError(), broken, reset, html):
            with self.subTest(failed=failed):
                with self.assertRaises(mf.ProviderError) as cm:
                    self.call(http=[failed])
                self.assertIsNone(cm.exception.code)

    def test_error_bodies_of_any_shape_keep_the_status(self):
        for body in (b'"Service Unavailable"', b'{"error": "down"}',
                     b'{"error": {"message": {"code": 1}}}', b'[1]', b'not json'):
            with self.subTest(body=body):
                err = urllib.error.HTTPError("https://primary/v1/chat/completions", 503,
                                             "err", {}, io.BytesIO(body))
                with self.assertRaises(mf.ProviderError) as cm:
                    self.call(http=[err])
                self.assertIn("HTTP 503", str(cm.exception))

    def test_chain_runs_claude_first(self):
        out, posts, runs = self.call(claude=[claude_run("Blue.")], chain=True)
        self.assertEqual(out["choices"][0]["message"]["content"], "Blue.")
        self.assertEqual((len(posts), len(runs)), (0, 1))
        argv = runs[0].args[0]
        self.assertEqual(argv[0], mf.CLAUDE)
        for flag, value in (("--model", "opus"), ("--effort", "high"),
                            ("--system-prompt", "be brief"), ("--output-format", "json"),
                            ("--tools", "")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--safe-mode", argv)
        self.assertNotIn("--json-schema", argv)
        self.assertEqual(runs[0].kwargs["input"], "hi")
        self.assertNotEqual(runs[0].kwargs["cwd"], os.getcwd())

    def test_schema_reaches_claude_and_its_structured_answer_is_the_content(self):
        body = {**BODY, "response_format": {"type": "json_schema", "json_schema": {
            "name": "x", "strict": True, "schema": {"type": "object"}}}}
        out, posts, runs = self.call(body=body, chain=True,
                                     claude=[claude_run('{"a": 1}', structured={"a": 1})])
        argv = runs[0].args[0]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), {"type": "object"})
        self.assertEqual(json.loads(out["choices"][0]["message"]["content"]), {"a": 1})

    def test_several_turns_are_rendered_with_their_roles(self):
        body = {**BODY, "messages": [{"role": "user", "content": "a"},
                                     {"role": "assistant", "content": "b"},
                                     {"role": "user", "content": "c"}]}
        out, posts, runs = self.call(body=body, claude=[claude_run("d")], chain=True)
        self.assertEqual(runs[0].kwargs["input"], "user: a\n\nassistant: b\n\nuser: c")
        self.assertNotIn("--system-prompt", runs[0].args[0])

    def test_every_failed_claude_run_goes_to_the_backup(self):
        for failed in (claude_run("boom", returncode=1), claude_run("Not logged in", is_error=True),
                       FileNotFoundError("no claude"),
                       subprocess.TimeoutExpired(["claude"], 5),
                       subprocess.CompletedProcess([], 0, "not json", "")):
            with self.subTest(failed=failed):
                out, posts, runs = self.call(claude=[failed], http=[response(ANSWER)], chain=True)
                self.assertEqual(out, ANSWER)
                self.assertEqual((len(posts), len(runs)), (1, 1))

    def test_local_backup_gets_the_callers_request_without_its_reasoning(self):
        body = {**BODY, "response_format": {"type": "json_schema", "json_schema": {
            "name": "x", "schema": {"type": "object"}}}}
        out, posts, runs = self.call(body=body, claude=[claude_run("x", returncode=1)],
                                     http=[response(ANSWER)], chain=True)
        req = posts[0].args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer key-LM_API_TOKEN")
        self.assertEqual(json.loads(req.data), {
            "model": mf.LOCAL_MODEL, "messages": BODY["messages"],
            "response_format": body["response_format"], "max_tokens": 10})

    def test_openrouter_backup_asks_for_zero_retention_providers(self):
        out, posts, runs = self.call(claude=[claude_run("x", returncode=1)],
                                     http=[response(ANSWER)], chain=True, backup=mf.OPENROUTER)
        req = posts[0].args[0]
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer key-OPENROUTER_API_KEY")
        sent = json.loads(req.data)
        self.assertEqual(sent["model"], mf.OPENROUTER_MODEL)
        self.assertEqual(sent["provider"], {"zdr": True, "require_parameters": True})
        self.assertNotIn("reasoning", sent)

    def test_both_failures_are_raised_together(self):
        with self.assertRaises(RuntimeError) as cm:
            self.call(claude=[claude_run("Not logged in", is_error=True)],
                      http=[http_error(503, "down")], chain=True)
        self.assertIn("claude opus", str(cm.exception))
        self.assertIn("then http://127.0.0.1:1234/v1: HTTP 503: down", str(cm.exception))

    def test_a_missing_backup_key_keeps_claudes_error(self):
        def no_key(name):
            raise RuntimeError(f"{name} missing")
        with mock.patch.object(mf, "api_key", no_key), self.assertRaises(RuntimeError) as cm:
            self.call(claude=[claude_run("Not logged in", is_error=True)], chain=True)
        self.assertIn("claude opus", str(cm.exception))
        self.assertIn("then LM_API_TOKEN missing", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
