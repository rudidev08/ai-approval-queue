#!/usr/bin/env python3
"""Call one tool on a server the Iris Host app runs, over HTTP.

The cron wrappers used to run a server's CLI directly on its venv python, which
made each cron job its own TCC principal needing its own Calendars/Reminders
grant. Going through the already-running server instead leaves the app as the
only holder of those grants.

    call_host_tool.py --port 4471 --tool get_lists
    call_host_tool.py --port 8355 --tool mirror_busy_events --args '{"days": 365}'

Success puts the tool's result on stdout and exits 0. A failed call (including
one past TOOL_TIMEOUT), or a result carrying the shared FAILED:/REJECTED:/PARTIAL:
marker, goes to stderr and exits 1 — so a wrapper can keep the usual
`out="$(...)" || { echo "$out" >&2; exit 1; }` shape.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_service import TOKEN_PATH  # noqa: E402

TOOL_TIMEOUT = 45
FAILURE_MARKERS = ("FAILED:", "REJECTED:", "PARTIAL:")


async def call_host_tool(port: int, tool: str, arguments: dict) -> tuple[str, bool]:
    token = Path(TOKEN_PATH).read_text(encoding="utf-8").strip()
    url = f"http://127.0.0.1:{port}/mcp"
    async with asyncio.timeout(TOOL_TIMEOUT), streamablehttp_client(
        url, headers={"Authorization": f"Bearer {token}"}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            text = "\n".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            ok = not result.isError and not text.lstrip().startswith(FAILURE_MARKERS)
            return text, ok


def call_sync(port: int, tool: str, arguments: dict) -> tuple[bool, str]:
    """(ok, text) from one call, for the actions areas: any exception (a
    refused connection, the timeout) becomes a FAILED: text."""
    try:
        text, ok = asyncio.run(call_host_tool(port, tool, arguments))
    except Exception as e:
        return False, f"FAILED: {type(e).__name__}: {e}"
    return ok, text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--args", default="{}", help="tool arguments as JSON")
    options = parser.parse_args()

    try:
        text, ok = asyncio.run(call_host_tool(options.port, options.tool, json.loads(options.args)))
    except Exception as e:
        print(f"{options.tool}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(text, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
