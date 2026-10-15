"""llm_pause — the pause file that holds the assistant's model runs.

While the file ASSISTANT_LLM_PAUSE names exists and its "until" stamp has not
passed, every run that would call a model returns early and does nothing:
the cron scripts release-news, finance-uncategorized, chat-attach-scan,
rules-suggest, hermes-audit and web-research-batch (each checks first thing, whoever
started it — cron, the actions page, an assistant_ops tool), the three finance
family emails, a research topic run, and the actions-inbox-scan agent job
(its pre-run gate, hermes-home/scripts/actions-inbox-scan-gate.sh, tells
hermes to skip the agent). A finance email build with Summary started by
hand — family emails' show and run now, the assistant's send-now tool, a finance
reports build from real data — is refused with the pause's end time. The assistant's
own chats are not held.

The file is JSON: {"since": iso, "until": iso, "source": the writer, "ops"
or another local tool}, local time with offset. The actions page's system
area writes it (1 to 8 hours) and clears it; other local tools may write
one for as long as they need the machine. read() deletes an expired file, so nothing has to run at the
expiry. No pause may last more than MAX_HOURS: start() refuses a longer one,
and read() deletes a file whose until is further away than that. stdlib only.
"""

import json
from datetime import datetime, timedelta

from data_root import root

MAX_HOURS = 24  # catches a writer bug, such as an until years away


def path():
    return root("ASSISTANT_LLM_PAUSE")


def read():
    """The pause in effect, or None. A file whose until has passed, is more
    than MAX_HOURS away, or cannot be read as a pause, is deleted."""
    try:
        data = json.loads(path().read_text())
        until = datetime.fromisoformat(data["until"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        data, until = None, None
    if until is not None and until.tzinfo is None:
        until = until.astimezone()
    now = datetime.now().astimezone()
    if until is None or until <= now or until > now + timedelta(hours=MAX_HOURS):
        path().unlink(missing_ok=True)
        return None
    return data


def start(minutes, source):
    """Write a pause of the given length, replacing any pause in effect."""
    if minutes > MAX_HOURS * 60:
        raise ValueError(f"pause of {minutes} minutes is longer than {MAX_HOURS} hours")
    now = datetime.now().astimezone().replace(microsecond=0)
    data = {"since": now.isoformat(),
            "until": (now + timedelta(minutes=minutes)).isoformat(),
            "source": source}
    path().write_text(json.dumps(data) + "\n")
    return data


def clear():
    path().unlink(missing_ok=True)
