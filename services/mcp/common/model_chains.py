"""model_chains — every model job of the assistant and the voice page as its
chain: the model that does the job, then each backup in the order it is
tried. The overview page shows the chains; the hermes audit checks that each
chain holds a model on this Mac and a model on a cloud service.

Read-only inputs, each owned by something else: hermes' config.yaml (parsed
by the caller) and cron jobs file, model_fallback.py's pins, the research
settings, the voice page's pins. stdlib only."""

import json
import pathlib
from urllib.parse import urlsplit

PROJECT = pathlib.Path(__file__).resolve().parents[3]
# A no-agent script job carries no model in jobs.json. These run the shared
# chain in model_fallback.py: Claude through the claude command, then the
# backup the job's own code names there (LOCAL or OPENROUTER). Job name ->
# role label and that backup. A script job absent here and not the research
# batch calls no model.
CHAIN_JOBS = {"finance-daily": ("finance", "LOCAL"),
              "finance-weekly": ("finance", "LOCAL"),
              "finance-monthly": ("finance", "LOCAL"),
              "finance-uncategorized": ("finance", "LOCAL"),
              "chat-attach-scan": ("messages", "OPENROUTER"),
              "rules-suggest": ("rules", "OPENROUTER"),
              "release-news": ("release news", "LOCAL"),
              "hermes-audit": ("audit", "OPENROUTER")}
# each backup's model, as model_fallback.py pins it, and its source
CHAIN_BACKUPS = {"LOCAL": ("LOCAL_MODEL", "lmstudio"),
                 "OPENROUTER": ("OPENROUTER_MODEL", "openrouter")}
MODEL_FALLBACK = PROJECT / "services/mcp/common/model_fallback.py"
# The research batch runs hermes with the model and provider its settings file
# pins; unset, hermes runs the profile's own main model.
RESEARCH_JOB = "web-research-batch"
RESEARCH_SETTINGS = PROJECT / "services/mcp/web_research/settings.env"
# the voice page pins its speech models and speech-out backup in its own code
VOICE = PROJECT / "services/voice/assistant.py"
# mlx-audio, the speech server on this Mac: hermes and the voice page both
# reach it here
SPEECH_URL = "http://127.0.0.1:8304/v1"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# sources that run on this Mac; every other source is a cloud service
LOCAL_SOURCES = {"lmstudio", "mlx-audio", "local"}


def pin(path, key):
    """The value a script pins for key, "" when the file or the key is gone.
    One reader for two shapes: `KEY=value` in the env files and `KEY = "value"`
    in model_fallback.py."""
    try:
        for line in path.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _netloc(url):
    """host:port with every loopback spelling folded to 127.0.0.1, so two
    URLs for one server compare equal."""
    u = urlsplit(url)
    host = "127.0.0.1" if u.hostname in LOOPBACK else u.hostname
    return "%s:%d" % (host, u.port) if u.port else host


def chains(cfg, jobs, *, include_paused=False):
    """{group: [(label, [(model, source), ...]), ...]} for "assistant" and
    "voice". cfg is hermes' parsed config.yaml, jobs the path of its cron
    jobs file. Paused cron jobs are left out unless include_paused."""
    return {"assistant": _assistant(cfg, jobs, include_paused), "voice": _voice(cfg)}


def _main_chain(cfg):
    return [(cfg["model"]["default"], cfg["model"].get("provider") or "")] + [
        (f["model"], f.get("provider") or "") for f in cfg.get("fallback_providers", [])]


def _assistant(cfg, jobs, include_paused):
    """hermes' model roles, its cron jobs and its speech."""
    main = _main_chain(cfg)
    out = [("main", main)]
    # a task pinned to a provider runs its own fallback_chain; an unpinned
    # task runs on the main model and falls back through fallback_providers
    for task, block in cfg.get("auxiliary", {}).items():
        if block.get("provider"):
            # no model: hermes runs the provider's default side-task model
            pairs = [(block.get("model") or f"{block['provider']} default", block["provider"])] + [
                (c.get("model") or f"{c['provider']} default", c["provider"])
                for c in block.get("fallback_chain", [])]
        else:
            pairs = main
        out.append((task, pairs))

    # cron jobs: an unpinned agent job runs cron.model, else model.default, on
    # cron.model_provider, else model.provider — the resolution
    # cron/scheduler.py does — and walks fallback_providers; a job with its
    # own model, provider or endpoint pin walks no backup (_job_route_pinned
    # there).
    # Jobs on the shared model fold into one "cron" chain; a pinned job gets
    # its own under a short label, its unpinned half taken from the shared
    # model. No jobs file, no cron chains.
    LABELS = {"actions-inbox-scan": "inbox"}
    cron_cfg = cfg.get("cron") or {}
    cron_chain = [(cron_cfg.get("model") or main[0][0],
                   cron_cfg.get("model_provider") or main[0][1])] + main[1:]
    try:
        job_list = json.loads(jobs.read_text())["jobs"]
    except OSError:
        job_list = []
    named, shared = {}, False
    for j in job_list:
        if not (j["enabled"] or include_paused):
            continue
        if j.get("no_agent"):
            # a script chain never folds into the shared one: it does not
            # follow cron.model
            if j["name"] in CHAIN_JOBS:
                label, backup = CHAIN_JOBS[j["name"]]
                named[label] = _script_chain(backup)
            elif j["name"] == RESEARCH_JOB:
                model = pin(RESEARCH_SETTINGS, "RESEARCH_MODEL")
                named["research"] = (
                    [(model, pin(RESEARCH_SETTINGS, "RESEARCH_PROVIDER"))] if model else main)
            continue
        if not any(j.get(k) for k in ("model", "provider", "base_url")):
            shared = True
        else:
            named[LABELS.get(j["name"], j["name"])] = [
                (j.get("model") or cron_chain[0][0], j.get("provider") or cron_chain[0][1])]
    out += sorted(named.items())
    if job_list and (shared or not named):
        out.append(("cron", cron_chain))

    # speech: the configured provider's model, and for speech in hermes'
    # local whisper when the provider is another one
    tts, stt = cfg.get("tts") or {}, cfg.get("stt") or {}
    out += _speech("speech out", tts, [])
    local = (stt.get("local") or {}).get("model")
    out += _speech("speech in", stt,
                   [(local, "local")] if local and stt.get("provider") != "local" else [])
    return out


def assistant_section(cfg, label):
    """The part of the assistant a chain label belongs to, for the overview page: chat,
    auxiliary (config.yaml's auxiliary tasks), speech, or scheduled jobs."""
    if label in ("main", "tests"):
        return "chat"
    if label in cfg.get("auxiliary", {}):
        return "auxiliary"
    if label in ("speech in", "speech out"):
        return "speech"
    return "scheduled jobs"


def _script_chain(backup):
    """model_fallback.chain's two steps for a caller that names backup."""
    key, source = CHAIN_BACKUPS[backup]
    return [(pin(MODEL_FALLBACK, "CLAUDE_MODEL"), "claude"), (pin(MODEL_FALLBACK, key), source)]


def _speech(label, block, backups):
    """The chain for a hermes tts or stt block; a model on the speech server
    has that server as its source."""
    spec = block.get(block.get("provider") or "") or {}
    if not spec.get("model"):
        return []
    on_speech_server = _netloc(spec.get("base_url") or "") == _netloc(SPEECH_URL)
    return [(label, [(spec["model"], "mlx-audio" if on_speech_server else block["provider"])]
             + backups)]


def _voice(cfg):
    """The voice page: its chat is the assistant's main chain through the web chat; dev
    mode runs model_fallback's chain with the LOCAL backup; speech in and
    speech out are pinned in its own code."""
    return [("chat", _main_chain(cfg)),
            ("dev", _script_chain("LOCAL")),
            ("speech in", [(pin(VOICE, "STT_MODEL"), "mlx-audio")]),
            ("speech out", [(pin(VOICE, "TTS_MODEL"), "mlx-audio"),
                            (pin(VOICE, "FALLBACK_MODEL"), "openrouter")])]
