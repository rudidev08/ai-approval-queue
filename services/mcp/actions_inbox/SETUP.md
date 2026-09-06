# Setup — actions_inbox

Thin MCP proxy to the actions service on 127.0.0.1:13727. Five state tools only: scan (new inbox emails + pending-set summaries + open categorize asks), save_set (persist a proposed set, or mark emails ignored), ask_categorize (store a categorize ask, get the numbered draft lines), add_to_actions (queue an iris-inbox email for the next scan), create_cards_from_email (finance suggestion cards from one chat-inbox mail's content). No execution tools exist here — approving a set's rows is a human action on the actions page.

## Pieces

- server.py — MCP stdio server `actions_inbox`; dependencies are PEP 723 inline (mcp pin + httpx), no venv to build, uv resolves them at launch
- talks only to the service at 127.0.0.1:13727 with the X-Actions-Local header; no TCC, no credentials, no listening socket
- scan output is fenced as untrusted data (BEGIN/END ACTIONS INBOX DATA); fence-shaped lines inside email bodies are stripped
- set provenance is proxy-stamped ({"job": "actions-inbox-scan", "session": "cron"}) — whatever a caller passes as creator is ignored

## Depends on

- the actions service: launchd com.example.iris.actions running ../../actions/server.py — service, page, state and cron job documented in ~/Iris/hermes/settings.md (actions section)
- the actions-inbox-scan cron job (agent job, skill actions-inbox, pinned model) is what calls these tools unattended

## Register (iris ~/.hermes/config.yaml)

```yaml
mcp_servers:
  actions_inbox:
    command: /usr/bin/env
    args: ["-u", "VIRTUAL_ENV", "-u", "PYTHONPATH", "-u", "CONDA_PREFIX", "/Users/me/.local/bin/uv", "run", "--no-project", "/Users/me/Iris/services/mcp/actions_inbox/server.py"]
    enabled: true
    tools:
      resources: false
      prompts: false
```

## rules.yaml (tool-approvals plugin)

- all four tools: unscoped allow — unattended cron runs must reach scan/save_set (cron_mode deny refuses prompts); on email the catch-all blocks scan/save_set/ask_categorize and the email section allows add_to_actions (records an intent only) and create_cards_from_email (finance card proposals)
