# Setup — iris-status

One-glance status of the assistant stack: services, hermes cron jobs, backup freshness, host resources. Doubles as live documentation — manifest.json is the expected list, anything missing or failing is flagged.

## Pieces

- hermes-iris-status — the checker script
  - runs on system python (`/usr/bin/python3`), stdlib only
  - text by default; `--json` for machines
  - exit 0 = all ok, 1 = problems
- manifest.json — expected services and cron jobs, grouped by category
  - categories today: iris, finance, vault
  - new service or cron job → add one item here (that's the whole registration)
  - cron jobs in hermes but not in the manifest show up under an `unlisted` category as problems
- consumed by: `status` tool in iris_ops MCP, the Iris dashboard page (`app/server.py`)

## Manifest item fields

- type service: `launchd` label and/or `http` health URL; both checked when present
- type service with `event` true: short-lived launchd WatchPaths job, idle is its normal state
  - `restart_log` — the job's one-line-per-restart log, source of the "last restart" detail
- type service with `status_file`: a daemon launchd does not own, which writes its own pid, state and version to a JSON file
  - the `launchd` label on such an item is the watchdog that restarts the daemon, so it is idle between runs and only a non-zero last exit is a problem
- type cron: name must match the hermes cron job name
  - optional `newest_files` (list of paths) + `max_file_age_h` — backup freshness check, catches "job reported ok but wrote nothing"
    - every path checked on its own; a missing or stale one is named as its own problem
    - skipped while the job is running (newest attempt in ~/.hermes/cron/executions.db still claimed/running) — mid-run the files are being rewritten; the detail says "run in progress"
- every item: `name`, `desc` (the live-documentation line)

## Checks

- service: launchd loaded + pid + uptime; http probe counts any response as alive, ≥500 or no response = problem
- event service: loaded + last exit code + last restart from its log; not-running is not a problem, a non-zero last exit is
- status-file service: the file's `pid` must still be in the process table, and its `status` must read `connected`; the detail carries uptime and version
- cron (from ~/.hermes/cron/jobs.json): missing / disabled / last run failed / overdue past next_run_at (15 min grace) = problems; never-ran shows as `--`, not a problem
- host: CPU `top -l 2`, GPU ioreg, RAM (active+wired+compressed, matches Stats.app), pressure sysctl, free disk
- model: oMLX `/health` on port 2130 — active model, memory, loaded count
- no temperature: Apple Silicon sensors need sudo

## Ports (from the services' own configuration)

- webui 35422, actual 60195, omlx 2130, dashboard 30655
