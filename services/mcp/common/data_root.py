"""data_root — the one way code finds a folder or file it writes.

Each such path is one environment variable, set by the file that starts the
process: a launchd plist, a cron wrapper script, an MCP server's env block,
the Host app's server list. The code holds no real path, so a process that
was not started that way cannot write real data by accident.
"""

import os
import pathlib


def root(name):
    """pathlib.Path of the absolute path in the environment variable name.
    Missing, empty, or relative: RuntimeError naming the variable."""
    value = os.environ.get(name, "")
    if not os.path.isabs(value):
        raise RuntimeError(f"{name} is not set. The process start file sets it; "
                           "the code holds no real path.")
    return pathlib.Path(value)
