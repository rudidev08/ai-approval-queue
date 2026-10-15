"""parcel_api — Parcel's (parcelapp.net) Premium web API, shared by the
parcel MCP server and the ops service's add_delivery rows. stdlib only.

carriers.json, Parcel's own carrier list, is vendored so a lookup never
waits on the network.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import hermes_env  # noqa: E402
from data_root import root  # noqa: E402

ADD_URL = "https://api.parcel.app/external/add-delivery/"
LIST_URL = "https://api.parcel.app/external/deliveries/?filter_mode="
CARRIERS_URL = "https://api.parcel.app/external/supported_carriers.json"
KEY_VAR = "PARCEL_KEY"
CARRIERS_MAX_AGE = 183 * 86400
TIMEOUT = 30
# the delivery's name in Parcel, like "Shop Desk Lamp"; a long one
# stands out in the list
DESCRIPTION_CAP = 40
STATUS = {0: "delivered", 1: "frozen", 2: "in transit", 3: "awaiting pickup",
          4: "out for delivery", 5: "not found", 6: "failed attempt",
          7: "exception", 8: "information received"}


def key():
    """The API key, None when ~/.hermes/.env lacks it or is missing."""
    try:
        return hermes_env.read().get(KEY_VAR) or None
    except OSError:
        return None


def _first_line(text):
    text = (text or "").strip()
    return text.splitlines()[0] if text else ""


def add(body):
    """One POST to the add-delivery API -> (ok, text). ok follows the API's
    own success flag; text is the error_message, or the HTTP status and
    first body line when the answer is not the documented JSON. Transport
    errors raise."""
    req = urllib.request.Request(
        ADD_URL, data=json.dumps(body).encode(), method="POST",
        headers={"api-key": key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {_first_line(e.read().decode(errors='replace'))}"
    try:
        out = json.loads(raw)
        return bool(out["success"]), out.get("error_message", "")
    except (ValueError, KeyError, TypeError):
        return False, f"unexpected answer: {_first_line(raw)}"


def deliveries(mode):
    """The deliveries Parcel lists for mode 'active' or 'recent' (one GET,
    a cached view on Parcel's side) -> list of the API's delivery dicts.
    Transport errors, a refused answer and an answer that is not the
    documented JSON raise RuntimeError."""
    req = urllib.request.Request(LIST_URL + mode, headers={"api-key": key()})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode()
    try:
        out = json.loads(raw)
        if not out.get("success"):
            raise RuntimeError(out.get("error_message") or "Parcel refused the listing")
        return out["deliveries"]
    except (ValueError, KeyError, AttributeError):
        raise RuntimeError(f"unexpected answer: {_first_line(raw)}") from None


def tracking_numbers():
    """Every tracking number Parcel lists, active and recent together -> set."""
    return {d["tracking_number"] for mode in ("active", "recent") for d in deliveries(mode)}


def carriers_file():
    """The vendored carrier list, rewritten when it ages out."""
    return str(root("ASSISTANT_PARCEL_CARRIERS"))


def fetch_carriers():
    """One GET of Parcel's public carrier list -> bytes."""
    with urllib.request.urlopen(CARRIERS_URL, timeout=TIMEOUT) as resp:
        return resp.read()


def _refresh_carriers():
    """Re-download the carrier list once the file is six months old: to a
    new name, then one rename over the old file, so a lookup never reads a
    half-written file. A failed fetch, or an answer that is not a
    non-empty {code: {name}} table, leaves the old list in place."""
    path = carriers_file()
    if time.time() - os.stat(path).st_mtime < CARRIERS_MAX_AGE:
        return
    try:
        data = fetch_carriers()
        table = json.loads(data)
        if not table or not all(isinstance(c, dict) and c.get("name") for c in table.values()):
            return
    except (OSError, ValueError, AttributeError):
        return
    new = path + ".new"
    with open(new, "wb") as f:
        f.write(data)
    os.replace(new, path)


def carriers():
    """{code: {name, extra_required?}} from carriers.json, refreshed first
    when the file is old."""
    _refresh_carriers()
    with open(carriers_file(), encoding="utf-8") as f:
        return json.load(f)


def carrier(name):
    """(code, canonical name) for a carrier named as an email names it,
    matched against the names on Parcel's list ignoring case: the exact
    name first, else the one name containing it ("DHL Express" for "dhl
    express"). Never against Parcel's internal codes: "EMS" is the code of
    EMS Russian Post, not China Post EMS. Several containing names, or
    none, raise ValueError naming the candidates. A carrier Parcel marks
    extra_required (a postcode or an email must ride along) is refused: an
    add without it fails and spends one of the day's 20 requests."""
    table = carriers()
    want = name.lower()
    hits = [(code, c["name"]) for code, c in table.items() if c["name"].lower() == want]
    if not hits:
        hits = [(code, c["name"]) for code, c in table.items() if want in c["name"].lower()]
    if len(hits) > 1:
        raise ValueError(f"carrier {name!r} matches several on Parcel's list — "
                         f"use one of: {', '.join(n for _, n in hits)}")
    if not hits:
        raise ValueError(f"carrier {name!r} is not on Parcel's carrier list — name "
                         "the carrier the tracking link's address or the "
                         "number's shape points to")
    code, canonical = hits[0]
    if table[code].get("extra_required"):
        raise ValueError(f"carrier {canonical!r} needs a postcode or an email "
                         "on Parcel, which cannot be sent along — drop it")
    return code, canonical
