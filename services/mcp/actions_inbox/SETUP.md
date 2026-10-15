# Setup — actions_inbox

Thin MCP proxy to the ops service on 127.0.0.1:8300. State tools only: scan (new inbox emails + pending-set summaries, after two blocks outside the data fence: the user's approved rules for the scan, and the calendars a row may write), read_email (one of them in full), save_set (persist a proposed set, or mark emails ignored), and list_rules, save_rule, remove_rule, decide_suggestion (the scan's rules, from chat; the page's thumbs do the same). Sets are calendar changes, shipments to track or documents to file: every one carries a create_event, update_event, delete_event, add_delivery or copy row, with archive_email and open_email rows riding along. No execution tools exist here — approving a set's rows is a human action on the actions page.

## Pieces

- server.py — MCP stdio server `actions_inbox`; dependencies are PEP 723 inline (mcp pin + httpx), no venv to build, uv resolves them at launch
- talks only to the service at 127.0.0.1:8300 with the X-Local-Request header; no TCC, no credentials, no listening socket
- scan output is fenced as untrusted data (BEGIN/END ACTIONS INBOX DATA); fence-shaped lines inside email bodies are stripped
- set provenance is proxy-stamped ({"job": "actions-inbox-scan", "session": "cron"}) — whatever a caller passes as creator is ignored

## Depends on

- the ops service: launchd com.example.assistant.ops running ../../ops/server.py
- the actions-inbox-scan cron job (agent job, skill actions-inbox, pinned model) is what calls these tools unattended
  - its pre-run script, actions-inbox-scan-gate.sh, makes hermes skip the run while the assistant's model runs are paused (services/mcp/common/llm_pause.py)

## Register (assistant ~/.hermes/config.yaml)

```yaml
mcp_servers:
  actions_inbox:
    command: /usr/bin/env
    args: ["-u", "VIRTUAL_ENV", "-u", "PYTHONPATH", "-u", "CONDA_PREFIX", "/Users/demo/.local/bin/uv", "run", "--no-project", "/Users/demo/Assistant/services/mcp/actions_inbox/server.py"]
    enabled: true
    tools:
      resources: false
      prompts: false
```
