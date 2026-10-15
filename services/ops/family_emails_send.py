"""One email send from the assistant, for family_emails.py only.

Runs in the mail venv (the shared mail sender lives there). The
request on stdin carries to, subject and text; the recipient must be one
of family_emails' own addresses, read from its store here. Two attempts at
most: a mail API failure or a lost answer is tried once more after RETRY_WAIT,
a refusal (a bad recipient, an empty subject) is not. The caller never
retries: a lost answer may still mean the email went out.
"""

import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mcp" / "mail_tools"))

import family_emails
import mail_tools as mail
import send_mail

RETRY_WAIT = 300   # seconds; a mail service outage at send time is usually shorter


def send(to, subject, text, allowed):
    try:
        return mail._send_allowed([to], subject, text, allowed)
    except RuntimeError as first:
        time.sleep(RETRY_WAIT)
        try:
            return mail._send_allowed([to], subject, text, allowed)
        except RuntimeError as second:
            raise RuntimeError(f"{first} Retried after {RETRY_WAIT // 60} min: {second}") from None


def main():
    try:
        request = json.load(sys.stdin)
        family_emails.DATA = json.loads(family_emails.store_path().read_text())
        allowed = family_emails.recipients()
        if request["to"] not in allowed or not request["text"].strip():
            raise ValueError("The recipient is not a saved address, or the email is empty.")
        send_mail._map_sender_tokens()
        send(request["to"], request["subject"], request["text"], allowed)
        result = {"status": "sent"}
    except (ValueError, KeyError, SystemExit) as e:
        result = {"status": "error", "note": str(e)}
    except Exception as e:
        # a refused or lost mail API request: the cause, then the warning
        result = {"status": "error", "note": f"{e} {family_emails.UNCONFIRMED}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
