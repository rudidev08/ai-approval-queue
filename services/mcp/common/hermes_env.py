"""hermes_env — the KEY=VALUE pairs of ~/.hermes/.env, for the scripts that
run outside hermes (cron wrappers, the actions page's helpers): launchd and
cron carry none of that environment, so each script reads the file itself.
Plain parse: blank lines and # comments skipped, the first = splits, one
layer of surrounding quotes stripped. stdlib only.
"""

import os

PATH = "~/.hermes/.env"


def read(path=PATH):
    """{key: value} from path, another KEY=VALUE file when given; raises
    OSError when the file is missing."""
    values = {}
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values
