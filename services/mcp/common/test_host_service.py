#!/usr/bin/env python3
"""host_service.py tests — reject_unknown_args and the helpers it calls
directly. HTTP serving, the auth token check, and the worker thread pool
are untested here.

No network, no real MCP client: builds a plain FastMCP server in-process
with throwaway tool functions, then calls reject_unknown_args and the
wrapped call_tool it installs directly.

Importing host_service.py pulls in mcp.server.fastmcp, which is not in the
base Python environment. Run under uv with the mcp version the real
servers pin (mcp==1.28.1):
    uv run --python 3.13 --with pytest --with mcp==1.28.1 python -m pytest -q test_host_service.py
    uv run --python 3.13 --with pytest --with mcp==1.28.1 python services/mcp/common/test_host_service.py
"""

import asyncio
import unittest

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from host_service import reject_unknown_args


class Base(unittest.TestCase):

    def setUp(self):
        self.mcp = FastMCP("test")

        def greet(name: str, times: int = 1) -> str:
            return name * times

        def kw_tool(a: str, **rest: str) -> str:
            return f"{a} {sorted(rest)}"

        self.mcp.add_tool(greet)
        self.mcp.add_tool(kw_tool)
        reject_unknown_args(self.mcp)

    def call(self, name, arguments):
        return asyncio.run(self.mcp._tool_manager.call_tool(name, arguments))

    def text(self, result):
        """Pulls the text out of either shape convert_result can return: a
        plain content list, or the (content, structured) tuple a tool with
        a return-type annotation gets."""
        content = result[0] if isinstance(result, tuple) else result
        return content[0].text


class TestValidCalls(Base):

    def test_declared_args_pass_through(self):
        self.assertEqual(self.call("greet", {"name": "x", "times": 3}), "xxx")

    def test_missing_optional_arg_uses_the_default(self):
        self.assertEqual(self.call("greet", {"name": "x"}), "x")


class TestUnknownArgument(Base):

    def test_unknown_argument_name_is_rejected(self):
        result = self.call("greet", {"name": "x", "bogus": 1})
        self.assertEqual(
            self.text(result),
            "REJECTED: bad argument: bogus — check the tool's schema and call again",
        )

    def test_missing_required_argument_named_as_missing(self):
        result = self.call("greet", {"times": 2})
        self.assertEqual(
            self.text(result),
            "REJECTED: missing argument: name — check the tool's schema and call again",
        )

    def test_unknown_and_missing_together_both_named(self):
        result = self.call("greet", {"bogus": 1})
        self.assertEqual(
            self.text(result),
            "REJECTED: bad argument: bogus; missing argument: name"
            " — check the tool's schema and call again",
        )

    def test_kwargs_signature_still_forbids_true_unknowns(self):
        # **rest becomes one pydantic field named "rest"; a key beyond that
        # and the declared "a" is still an unknown key, still rejected
        text = self.text(self.call("kw_tool", {"a": "x", "b": "y"}))
        self.assertTrue(text.startswith("REJECTED: bad argument:"))
        self.assertIn("b", text)


class TestNonArgumentErrors(Base):

    def test_unknown_tool_name_is_not_swallowed(self):
        # ToolError here has no ValidationError cause, so reject_unknown_args
        # re-raises it instead of turning it into a REJECTED result
        with self.assertRaises(ToolError):
            self.call("no_such_tool", {})


class TestSchemaAndRepeat(Base):

    def test_schema_forbids_additional_properties(self):
        schema = self.mcp._tool_manager.get_tool("greet").parameters
        self.assertEqual(schema["additionalProperties"], False)

    def test_calling_twice_does_not_break_it(self):
        reject_unknown_args(self.mcp)  # arg_model is already forbid: skipped
        text = self.text(self.call("greet", {"name": "x", "bogus": 1}))
        self.assertEqual(
            text,
            "REJECTED: bad argument: bogus — check the tool's schema and call again",
        )


if __name__ == "__main__":
    unittest.main()
