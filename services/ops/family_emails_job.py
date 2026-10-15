"""Cron entry point: ask Actions to run one email kind and wait for the result.

Started by hermes cron, the Jobs area's run key included. Writes no
files and prints nothing on success, so cron mails no second copy. A
failed run exits nonzero, so the job's normal failure alert goes out.
"""
import json
import pathlib
import sys
import time
import urllib.request
from urllib.parse import quote

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent / "mcp" / "common"))
import llm_pause  # noqa: E402

URL = "http://127.0.0.1:8300/api/family-emails/run"
# the finance kinds build their summary with a model call; the reminder
# kinds call none, so a pause never holds them
FINANCE_KINDS = ("daily", "weekly", "monthly")


def call(url, body=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json", "X-Local-Request": "1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    if sys.argv[1] in FINANCE_KINDS and llm_pause.read():
        return  # the assistant's model runs are paused; the kind is skipped today
    result = call(URL, {"kind": sys.argv[1]})
    deadline = time.monotonic() + 3600
    while result["status"] == "running":
        if time.monotonic() > deadline:
            raise RuntimeError("The email run is still going after an hour. Check Family emails.")
        time.sleep(2)
        result = call(URL + "?id=" + quote(result["id"], safe=""))
    if result["status"] != "done":
        raise RuntimeError(result.get("error") or "The email run failed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(str(e))
