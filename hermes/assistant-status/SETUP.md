# Setup — assistant-status

One-glance status of the assistant stack: services, hermes cron jobs, backup freshness, host resources. Doubles as live documentation — manifest.json is the expected list, anything missing or failing is flagged.

## Pieces

- hermes-assistant-status — the checker script
  - runs on system python (`/usr/bin/python3`), stdlib only
  - text by default; `--json` for machines
  - exit 0 = all ok, 1 = problems
- manifest.json — expected services and cron jobs, grouped by category
  - categories today: assistant, finance, files
  - new service or cron job → add one item here (that's the whole registration)
  - cron jobs in hermes but not in the manifest show up under an `unlisted` category as problems
- consumed by: the ops service's status report (the `status` tool in assistant_ops), the assistant overview page (`services/ops/overview.py`), the actions page's system area (`services/ops/server.py`)

## Manifest item fields

- type service: `launchd` label and/or `http` health URL; both checked when present
- type service with `callers`: a shared server (the speech server) that can run on this Mac or on another one
  - no fixed URL: `health` is the path probed at the server root, and `callers` lists where the callers on this Mac store its address, one `{file, key}` each
  - the reader follows the file ending: `.yaml`, `.json`, `.plist` read a dotted key path; `.sh` reads an `export VAR=` line
  - every distinct scheme://host:port across the callers gets its own row and probe, named after the service; the detail starts with the host
    - the stored URL's path (`/v1`) is dropped; `localhost`, `127.0.0.1`, `::1` count as one server and are probed at `127.0.0.1`
    - with more than one address, each row's detail names its callers
  - a caller file or key that cannot be read is flagged on the row; no readable address at all is one flagged row
  - the `launchd` label is checked only for a loopback address: a process on another Mac has no launchd job here
  - the manifest is the registry of callers: a new caller, or a retired one (a hermes profile included), is a manifest edit, or the check has a blind spot or a permanent flagged row
  - a plist caller is read as next-start configuration; a running job keeps its old environment until restarted
- type service with `event` true: short-lived launchd WatchPaths job, idle is its normal state
  - `restart_log` — the job's one-line-per-restart log, source of the "last restart" detail
- type service with `status_file`: a daemon launchd does not own, which writes its own pid, state and version to a JSON file
  - the `launchd` label on such an item is the watchdog that restarts the daemon, so it is idle between runs and only a non-zero last exit is a problem
- type service with `socket`: a server that detaches from the launchd job that starts it (a terminal server)
  - the `launchd` label must be loaded; its run state and last exit are not checked, since the job only starts the server
  - alive when the unix socket at `socket` accepts a connection
- type cron: name must match the hermes cron job name
  - optional `newest_files` (list of paths) + `max_file_age_h` — backup freshness check, catches "job reported ok but wrote nothing"
    - every path checked on its own; a missing or stale one is named as its own problem
    - skipped while the job is running (newest attempt in ~/.hermes/cron/executions.db still claimed/running) — mid-run the files are being rewritten; the detail says "run in progress"
- type service with `servers`: an app that runs MCP servers as its children (the Assistant Host app)
  - `servers` is the app's own child list (`servers.json`); only each entry's name and port are read, since the file also holds API keys
  - one `mcp` row per server under the app's row, so the check cannot drift from what the app runs
  - a down server is a problem, so both pages' WARN and the menu-bar badge count it
  - an unreadable list is one flagged row
- every item: `name`, `desc` (the live-documentation line)
- every service row in the JSON carries `address` (scheme://host:port it was probed at, null when there is none)

## Checks

- service: launchd loaded + pid + uptime; http probe counts any response as alive, ≥500 or no response = problem
- event service: loaded + last exit code + last restart from its log; not-running is not a problem, a non-zero last exit is
- status-file service: the file's `pid` must still be in the process table, and its `status` must read `connected`; the detail carries uptime and version
- socket service: launchd label loaded + the socket accepts a connection
- mcp row: its port on 127.0.0.1 accepts a connection
- cron (from ~/.hermes/cron/jobs.json): missing / disabled / last run failed / overdue past next_run_at (15 min grace) = problems; never-ran shows as `--`, not a problem
- host: CPU `top -l 2`, GPU ioreg, RAM (active+wired+compressed), pressure sysctl, free disk
- no temperature: Apple Silicon sensors need sudo

## Tests

- `python3 -m unittest hermes/assistant-status/test_status.py` — the readers, address dedup, the rows a shared server gets, and the Host app's server rows; temporary files only, no probe leaves the process
