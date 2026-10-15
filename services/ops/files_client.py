"""The files server client and the filing folders, shared by the filing
areas (messages, emails).

The files server is an Assistant Host child; calls go over its port with the host
token (call_host_tool). The filing folders are walked here, on the disk:
every folder under Papers and SharedPapers outside the top-level `ignore`
folder and hidden folders."""

import os
import pathlib
import sys

PROJECT = pathlib.Path(__file__).resolve().parent.parent.parent
FILES_PORT = 8360   # host app's files server
TOOL_TIMEOUT = 120  # a copy or text recognition can run long

sys.path.append(str(PROJECT / "services" / "mcp" / "common"))
import call_host_tool  # noqa: E402
from data_root import root  # noqa: E402
from file_paths import IGNORED  # noqa: E402
from file_types import FILENAME_CAP, file_ext, stored_name  # noqa: E402,F401


def call_files(tool, args):
    """(ok, text) from one call to the files server."""
    return call_host_tool.call_sync(FILES_PORT, tool, args, TOOL_TIMEOUT)


def filing_folders():
    """The full path of every folder a file may be filed in, sorted."""
    out = []
    for var in ("ASSISTANT_PAPERS", "ASSISTANT_SHARED_PAPERS"):
        top = str(root(var))
        for dirpath, dirs, _ in os.walk(top):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and not (dirpath == top and d.casefold() == IGNORED)]
            if dirpath != top:
                out.append(dirpath)
    return sorted(out, key=str.casefold)


def file_warning(name, source):
    """The page's warning for a file filed from source under name, or '':
    source has no extension, or the files server adds it to a name that
    already ends in another. The user decides whether the name is right."""
    source = source.strip(". ")
    if not file_ext(source):
        return f"'{source}' has no file type"
    stored = stored_name(name, source)
    if stored != name and file_ext(name):
        return f"extension changes: '{source}' saves as '{stored}'"
    return ""
