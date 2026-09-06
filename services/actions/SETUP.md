# Setup — actions (service + page)

The page where the user approves things. Eight areas: emails (sets of cards derived from the inbox scan), messages (save proposals for attachments in watched Apple Messages chats), finance (the Actual Budget categorization queue), finance report (builds a report email — daily, weekly, or a finished month — whole or one part, and shows its text), web research (the vault's recurring research topics: run one now, delete its stored report, or delete the topic), audit (runs the deterministic Hermes audit and shows the pass as it happens), jobs (every enabled hermes cron job: how long the last 3 finished runs took, when the last week's failed runs happened, last and next run, current state, a chevron opening the schedule and the 7-day retry count with the newest retry message, and a run key per job), and system (a console with one row each for the hermes gateway and each hermes webui instance, then one saved-data row per area with a reset: emails, messages, audit). A grid of glance tiles on top shows each area's open count and shorthand status; a tile picks the view — the leading "queue" tile stacks emails, messages and finance, an area tile shows it alone. Each area opens with its control row, built like the system console's rows: the area's header on the left — icon, name and one status line carrying its scan state, then its counts in their shortest form — and every key of that area on the right, so the title row and the control row are one row.

## Pieces

- server.py — the service, run by launchd com.example.iris.actions with KeepAlive
- page.html — the page itself, polling the service
- finance.py — the finance area; finance_jobs.py fills it from the finance-uncategorized cron job
- messages.py — the messages area; messages_scan.py fills it from the messages-attach-scan cron job, reading the watched chats through the host app's macos_messages server, resolving senders through its macos_contacts server, and filing through the records server
- hermes_audit.py — the Hermes audit area; starts ../hermes-audit/audit.py and reads the files it leaves in ~/.local/state/hermes-audit/, plus the hermes-audit cron job's stamps from ~/.hermes/cron/ for the last-run and next-run indicators
- finance_report.py — the finance report area; runs finance_jobs.py in its write-free mode and keeps the text in memory; drafts or saves that text on request
- research.py — the web research area; reads the vault's research topics and reports (vault/research), and spawns services/mcp/research/driver.py for a run
- jobs.py — the jobs area; reads hermes' cron jobs file, executions ledger and the scripts' retry log (~/.hermes/logs/cron-retries.log), gets per-job states from the server's iris-status pass, and starts a job through `hermes cron run`
- system.py — the system area; restarts the hermes gateway and both hermes webui instances through launchctl kickstart, reads how long each has been up, and runs `hermes doctor --fix` for the audit's doctor-warning key
- state/ — live data, kept out of git: state.json, finance.json, finance-ask.json, finance-reported.json, finance-oddities.json, finance-oddities-seen.json, messages.json, research-cleared.json, decisions-YYYY.jsonl; directory 0700, files 0600
- finance.env — model and sampling settings for the finance scan
- messages.env — model, endpoint, and watched-chat settings for the messages scan

## How the parts divide

- The page is the only execution path: approve and deny there fire rows.
- The MCP server (actions_inbox) and the cron job only scan, propose, and record (categorize asks, intake intents, ping-mail finance card proposals) — execution never leaves the page.
- Finance cards apply through one write-helper run per call: the row's check key sends one card, the control row's submit-all key sends every valid card. One call runs at a time.
- Creating a category works from the page (the row's create form) and from Actual's web UI.

## Network

- Binds 127.0.0.1 and the tailscale IP, port 13727.
- tailscale serve maps the https root here, which is the primary URL.
- The direct http://mac-mini.your-tailnet.ts.net:13727 address keeps working for the iOS home-screen bookmark.

## Logs

- ~/.hermes/logs/actions.log holds the request lines the handler prints to stdout.
- ~/.hermes/logs/actions.error.log holds real errors only.
- newsyslog rotates both at 1 MB, keeping 2 (/etc/newsyslog.d/iris-actions.conf, mirrored in setup/newsyslog.d/).
  - Rotation SIGTERMs the pid in ~/.hermes/logs/actions.pid so KeepAlive restarts the service and it reopens the new files.

## Behavior worth knowing

- One key language on the whole page: same action, same icon, same slot.
  - The green check does it: approve, accept, save, seen, submit all, seen all.
  - The x does not: deny, skip, hide a proposal, close. It is amber only in a control row, where it acts area-wide; inside a card or row it is gray.
  - The circular arrow rescans (rebuilds the area's list now). Play runs a thing now: a job, a research topic, the audit, a report part. The rotate arrow restarts a process. The speech bubble is chat.
  - A control row reads circular arrow or play, check, x. A row's keys end check, x.
  - Words stay only for actions no icon covers: clear, retry, accept, stop, restore, delete, email draft, save a file, copy, clear failure, new category, reset.
- Everything finished leaves its area for the done section at the bottom of the view: resolved email sets as cards, newest first, then saved message rows and filed finance rows together in one card. The queue view's done section holds all three areas'; an area view's holds that area's.
  - The section exists only while it has something. Its header counts the items and carries the clear key, which posts the hide-all of every area with something there.
  - Items leave on their own anyway: a resolved set 24 hours after it resolved, a saved message row at the next messages scan, a filed finance row at the next scheduled finance scan.
  - Anything that still needs a decision stays in its area: pending rows, failed rows, stuck sets.
  - Every reset lives in the system area's saved-data rows, never in its own area.
- Every area carries its own keys in its control row, so a view is only which areas show: queue stacks emails, messages and finance, each with its own row, and an area view shows that one.
  - Web research is the exception, its header standing above its panel: it has one expandable row per topic.
  - System has no control row — its keys belong to its console rows: the process rows, then the saved-data rows.
- The glance tiles count the sets holding a row waiting for a decision (emails — a set is decided as a unit), the rows waiting for a decision (finance, messages), and the findings left (audit); the leading "queue" tile holds the emails + messages + finance total behind those three areas' icons, and its view stacks those three areas in that order — report, web research, audit, jobs and system open from their own tiles only. The picked view sticks in localStorage per device, and a chip strip pins under the top bar while the tiles are scrolled out of view.
  - The host app's menu badge counts emails the same way, one per set holding a pending row.
- Calendar dot colors come from a list_calendars call to the host app at boot, retried every 5 minutes until it lands.
- The emails scan key starts the actions-inbox-scan cron job; running state and last-run time come from hermes' cron ledger.
- Opening a member email row shows the email's own text (header block and body, as get_email renders them).
  - GET /api/emails/body?set_id&email_id reads it live; sets store the snapshot facts, never the body.
  - A set's member facts (subject, sender, date) are stamped by the service from the scan's inbox-listing cache — the scan agent passes email ids only, so a model typo can never corrupt them.
  - The page asks once per email and keeps the answer until reload.
  - An id the webmail child no longer knows — its session restarted, or an archive row moved the email out of the inbox — is re-registered by one mailbox-wide search on the subject, then fetched again. A failed fetch shows in the row as the tool's own message.
- A set card's head carries its keys next to chat, per state of the set: the x (skip) while rows wait, retry next to accept while the set is stuck, nothing once it is resolved.
  - Skip denies every row still waiting, one call each — the same deny as the row's own x key.
  - Hiding takes a finished card off the page before its 24-hour window ends; POST /api/emails/hide marks the record, and the denials, ledger entries and log lines stay as they are. The done section's clear key posts it with all: true, which takes resolved sets only.
  - A stuck set (nothing waiting, a row that failed keeps it pending) shows two keys — the choice is the user's:
    - Retry is POST /api/emails/reset for that set — the set is voided and its emails leave the ledger.
      - Nothing proposes them until a scan runs, so the heading row says to run a rescan until one lands. A scan only offers emails absent from the ledger, and a set member is in it, so a rescan without the retry never brings them back.
      - The reminder lives in the page, not the service: a reload drops it, and the next scheduled scan picks the emails up either way.
    - Accept settles the set as resolved with its failed rows standing as final, then hides it — moving on without a re-run. It is POST /api/emails/hide by set_id; the clear key never touches a stuck set.
  - Hiding is final — nothing brings a hidden set back.
  - The emails reset (the system area's saved-data row) is POST /api/emails/reset with all: true: every set, ledger entry and denial goes. The scan stamps, the listing cache and the intents stay.
- A failed or stale scan reads in the area's heading row, in amber — there are no banner strips.
- Emails, messages and finance each show one centered line where their cards would be when nothing is waiting: "inbox is clear", "no attachments waiting", "everything is categorized". A failed scan puts its own card there instead.
  - Emails: "last scan failed" from the scan's own status, or "no scan in over 11 hr" when the last batch is older than that (three missed scans at the every-3-hours cadence, plus 2 h slack so one late run does not trip it). The "last ran" stamp next to it belongs to the cron job, which can run without a batch landing.
  - Finance and messages: "last run failed" / "last scan failed", plus a panel card carrying the failing step and its message.
- Forwarding an email to iris@ saying "add to actions" queues it for the page: the chat agent's add_to_actions posts /api/emails/intake, which resolves sender + subject (+ optional received date) against the iris inbox — exactly one match required, or a rejection with the candidates.
  - A match records an intent in state.json; the next scan picks the email up like any new mail, under an "iris:"-prefixed id and with the intent's category (finance|general) as a hint for the scan agent.
  - The iris account is a separate Webmail account (the email platform's own mailbox), read through the service's own webmail child carrying only JMAP_TOKEN_READONLY_IRIS from ~/.hermes/.env — nothing moves or archives there. Without the token the endpoint 503s and a boot line in actions.log says intake is off.
  - Chat mail never enters the pipeline, only intent-matched ids. An intent is consumed when its email enters a set, dropped when the email leaves the iris inbox or after 14 days.
  - Sets reject archive_email rows on iris-source members — injected mail stays as chat history. The scan's trim drops state only when both inbox listings are provably complete (an empty iris inbox counts — it is the norm).
- The finance scan key first downloads a fresh budget copy, so new categories reach the picker, then starts the uncategorized scan; that run rebuilds the whole list.
- Two report emails go out, both built by finance_jobs.py and mailed from iris@ through send_mail.py.
  - The daily (finance-daily, 07:00): summary, Check (uncategorized money and finance-file problems, printed only when there is something to say), the outliers not yet marked seen on the finance tab, the week's new transactions. The summary sentences comment on those lists.
  - The weekly (finance-weekly, Mondays 07:00, --weekly): summary and the whole cash-flow block — Estimated, Actual with every group's category rows, Forecast, Assets and Debt, Check. No outliers, no new transactions. The summary sentences comment on the block.
  - A finished month (page builds only, --month): summary, the cash-flow block with the month's final numbers, the outliers over the whole month.
- The finance report area has one row per part — full (the whole body), then summary, cash flow, check, outliers, new and links — each with run, email-draft and save keys. A part the picked report does not carry greys its run key.
  - The header's control row holds the four settings every build takes: the report dropdown (daily, weekly, or a finished month back to cash_flow.FIRST_MONTH, read from the state's first_month), the details checkbox (finance_jobs.py --detailed: income sub-lines, history rows, debt payoff and interest math), the categories checkbox (--categories: every group's per-category rows under its group row) and the combine personal checkbox (--combine-personal).
    - The daily has no cash-flow rows, so its three checkboxes grey. The weekly always prints the category rows, so its categories checkbox shows checked and greyed.
    - Combine personal joins the categories named for a person (cash_flow.PERSONAL_CATEGORIES) into one "Personal" and one "Personal Subscription" row per group, and weighs them the same way in the outlier lines, so no per-person figure is named anywhere in the report.
    - Group totals are untouched, so the rows under a group still add up to it.
    - The finance-weekly job runs without it, so the weekly email names them as they are.
  - Run posts /api/finance-report/preview with the part and the four settings; the service runs finance_jobs.py --email --preview, the script's write-free mode: no assets/debts lines, no monthly-balance lines, no printed-transactions file, no card batch. The weekly adds --weekly, a past month --month; a component part adds --section <name>.
  - Nothing is emailed by a build. The reports the finance-daily and finance-weekly jobs send are unaffected.
  - Links is a page-only preview: no email carries it. Its row carries a grey "preview only" word.
  - The service caches each part's last build in memory with the settings it was built with; the row's meta line shows them. The caches live in the service's memory only, so a restart drops them all.
  - A full build takes a minute or two (one LLM call), so the run keys grey and the heading counts the build up and names the part; the page polls every 3 seconds while it runs. Every part but full and summary skips the LLM call and builds in seconds.
  - Email draft posts /api/finance-report/draft; the service puts the part's cached text — the text on the page, never a fresh build — into the hi@ Drafts folder through jmap_mail/draft_mail.py (one attempt), addressed to finance.env's REPORT_MAIL_TO, subject "Finance — <month or build date>", or "Finance weekly — <build date>" for the weekly. Nothing is sent; the user reviews and sends from his mail client. The outcome lands on the row: a drafted stamp, or a "draft failed" word carrying the error.
  - Save (two taps) posts /api/finance-report/save; the service writes the cached text to vault/docs/finances/reports/<month or build date>-finance[-weekly][-details][-categories][-personal][-<part>].md, making the folder if it is missing.
  - The state lists that folder with each file's settings read back from its name and its time; the row's meta line says when the header's combination was last saved, and names the file.
  - One build or draft at a time; a second tap gets a 409.
- The web research area has one expandable row per vault research topic (vault/research/topics): the topic's name, a meta line with its cadence prefix and the last successful run (from the driver's runs log), and its own run key. The heading line counts the topics, the runs in flight, and the failing ones, and shows the next batch time (the research-batch cron job's next_run_at) — the batch is one job, so the stamp belongs to the area, not a row.
  - A chevron key (or a tap on the row) opens it: the topic's full prompt and the stored report's text (GET /api/research/report), each in its own banded block, read from the service on the first open and re-read when the report's updated stamp changes, with the delete report and delete topic keys at the end.
  - While the driver's log shows consecutive failures for a topic, the row carries an amber "failed ×N" status word; the open row holds the two-tap clear key, and the flag comes back if the next run fails too. Cleared flags live in state/research-cleared.json, keyed by the failed entry's log stamp.
  - A row's run key spawns services/mcp/research/driver.py --topic <slug> detached, the same spawn the research MCP server's run_report makes; the row reads "started" for a few seconds, then a green "running" while the driver's in-flight marker (~/.local/state/research/running.json) holds a fresh stamp for the slug, and the finished report arrives by email like a batch run's. Marker entries older than the driver's run timeout are ignored, so a killed driver's leftover ages out.
  - The open row's delete report key removes that topic's report and failure dump, keeping the topic, so the next run starts from scratch (the research MCP server's delete_report semantics; undo = cloud-storage versioning). It greys while the topic has neither file.
  - The open row's delete topic key removes the topic file and, with it, its report and failure dump, so no orphan report stays behind (undo = cloud-storage versioning).
- The finance area has two control rows: Categorize (the categorization queue, its keys rescan, submit all and the x that hides every email proposal) and Oddities below it. The oddities block lists what the odd-charge rules flag: one row per transaction with its date, account, amount and reasons, newest first, and a seen key, the same two-tap green check as the inbox action rows; the Oddities control row's green check takes every row off at once, grey while none is listed. "nothing odd this week" when the list is empty.
  - The rules are services/mcp/actual/oddities.py, the same ones the daily email's Outliers section prints: unusually large charge, possible duplicate, a monthly payee charged too soon or off its usual day, new payee. Fixed thresholds, no model.
  - The daily email prints only the flags not yet seen here: finance_jobs.py reads state/finance-oddities-seen.json and drops those ids.
  - Every poll runs the rules over the last 7 days of spending plus every queued transaction. A flagged transaction that is neither queued nor seen joins the queue (state/finance-oddities.json, transaction ids only); a queued one stays until its seen key, or until the rules stop flagging it.
  - Seen (POST /api/finance/odd-seen, one id or all: true) moves the id into state/finance-oddities-seen.json with the transaction's date. Entries dated before the rules' 7-day window are dropped on each write, so a seen transaction never comes back: by then its date is outside the window the rules read.
  - The rules read the api-cache copy, which the 06:30 scan refreshes after the 04:00 bank sync; the rescan key refreshes it too.
  - The finance tile counts the oddities with the pending cards.
- The finance queue is the 10 newest uncategorized transactions, one card, one row each, no group labels.
  - Email cards hold cards proposed from a ping mail; they sit first in the card, each with a mail icon on its meta line: the email session's create_cards_from_email posts /api/finance/email-cards with (transaction_id, category) pairs it matched from the mail's chain; the service re-checks every id against the live uncategorized queue and the category against the budget, stamps the card's facts itself, and keeps at most 20 pending. The scan never adds or rebuilds these; prune, settle, skip and apply cover them like any card.
  - A transaction proposed both from email and by the scan shows one row, the email one; the finance status line counts the hidden duplicates.
  - Each email row carries a gray x, and the control row's amber x drops every email proposal; that key shows only while an email proposal is pending. Hiding drops the proposal only — the transaction stays uncategorized, and its scan row shows again at once.
  - A filed row moves to the done section; the clear key there takes every filed transaction off at once (POST /api/finance/hide). Filed means done or already handled — the transaction left the uncategorized queue, so no scan proposes it again. A failed row stays in the area with its keys for the retry.
- A scheduled finance scan only tops up open slots: pending cards stay, finished cards leave, new transactions fill the freed slots. Email cards take no slot.
- The daily finance email ends with the week's new transactions, grouped by account, then category.
  - Each transaction is printed exactly once: state/finance-reported.json tracks the printed ids, and only a successful email run writes it.
- A pending finance card drops on the next poll once the budget copy shows its transaction handled.
- A scheduled finance save is held while this page is open with cards still pending, so it never swaps the cards out mid-review. A list with nothing left to decide is replaced even while the page is open.
  - The page counts as open for 15 minutes after its last poll, and only the page's own poll counts: it carries the hold-update flag, so the menu-bar app's badge poll never holds a save.
  - The finance scan key is never held — it is a rebuild you asked for.
- A categorize ask emails numbered categorizing questions about uncategorized transactions to a helper (the finance-buddy flow); the helper's reply becomes approvable rows.
  - POST /api/finance/ask stores the ask (state/finance-ask.json; one open ask per recipient, at most 20 items) and returns the subject and numbered lines for the draft mail; the user sends the draft themselves.
  - The inbox scan marks a new email from an open ask's recipient finance_review (sender match, no subject tag) and attaches the whole thread; its pending_asks payload carries the open asks with per-item still-open flags and the valid category list.
  - The agent pairs the answers by number into categorize_transaction rows; the service stamps each row's display facts (args.transaction) from the ask store and validates the transaction against the ask.
  - Approving one writes category plus payee rule (skipped on update_rule=false) through the budget helper in services/mcp/actual/api_cache.py, one row at a time service-wide; a busy write is waited out (150 s), then the row fails — the ask stays open, so the next scan re-proposes it.
  - An ask is open while any of its items is uncategorized and it is younger than 14 days; the scan's payload build prunes dead ones. Open asks are counted in the finance status line, with who each went to and when in that line's tooltip, so an expiring ask never vanishes in silence.
- The messages area proposes attachments worth filing from the watched chats (Partner, Riley, Alex, and two group chats, set in messages.env).
  - One card is one attachment: clean filename, the model's records-location pick in a dropdown covering the whole catalog, and a one-line reason. Approving runs export_attachment_to_inbox into the records inbox, then save_file into the picked location.
  - The green submit-all key saves every waiting row to its dropdown's destination in one call (POST /api/messages/apply, the same call a row's check key makes with one item); the saves run one after another in a single thread, and the rows settle as each finishes. The key greys while no row is waiting or a save is running.
  - The sender shows as its contact name: scans resolve handles through macos_contacts and cache the mapping in messages.json — a number's owner does not change, so only misses re-check.
  - Scans also extract each candidate's text (export into the records inbox, read_file, delete the copy), so the model can file under the person named inside the document. The card's chevron shows the same text.
  - state/messages.json's ledger remembers every verdict for 30 days — long past the 72-hour scan lookback, so a scanned attachment is never proposed twice, and the file does not grow forever.
  - The messages rescan key starts the cron job on the spot. The messages reset (the system area's saved-data row) clears the ledger and drops every card, so the next scan re-proposes whatever is still in the window. The caches and scan stamps stay.
  - The control row's x denies every attachment still waiting, one call each (POST /api/messages/deny); a denied attachment never comes back.
  - A saved row moves to the done section; the clear key there takes every saved row off at once (POST /api/messages/hide). The next scan drops saved rows anyway, and the ledger keeps the verdict either way.
  - A failed save keeps its card: the error text shows and the check key retries. A card caught mid-approve by a service restart settles to the same retryable state at boot.
  - A failed scan posts an error record instead of cards, which the page shows as a "last scan failed" panel; the next good scan clears it.
- The Hermes audit area starts the same script the iris_ops tools start, so a run started in chat shows here too, and one started here shows there.
  - A pass can take an hour, so the run is detached: log rotation restarting this service does not touch it, and the audit's own files carry the state across.
  - One run at a time is the audit's own flock; the run key greys while a run holds it, and the stop key only exists while one does.
  - A category with findings gets a card as soon as it lands; clean categories are counted in the audit status line and named in its tooltip. Starting a run clears the previous pass's cards.
  - Each card's copy key puts that category's block on the clipboard, in the report's own wording.
  - A doctor finding carries a two-tap fix key (gray wrench chip): it posts /api/system/doctor-fix, one `hermes doctor --fix` for the shared install, which repairs what doctor can (the macOS TCC anchor on the venv python, the CA bundle, the hermes link). Every doctor finding's wrench shares one armed id, so they arm, go busy and settle together.
    - One run covers both profiles' copies of a warning; the findings stay on the card until the next pass re-runs doctor.
  - A doctor finding carries a second two-tap key (bell-off chip): expected from now on. It appends the warning line to doctor_known in ../hermes-audit/expected.yaml, so future runs stop flagging it.
  - A "started before the newest plugin deploy" finding carries a second two-tap key (gray rotate chip): restart. It posts the same /api/system/restart-<name> the system area's key posts, for the process the finding names.
    - The finding stays on the card: the record is the last pass's, and only the next pass re-reads the process. The system area's uptime line is what says the restart landed.
- The system area's process rows restart the hermes gateway and the hermes webui (the rotate-arrow key). Its saved-data rows hold the three resets — emails, messages, audit (the audit's is audit.py --reset: the next run is a first pass) — one amber reset key each, with a one-line description of what goes.
  - Both processes read the tool-approvals plugin code once, at start.
    - A plugin code deploy made after that is unenforced until they restart; rules.yaml and the tests need no restart.
    - The Hermes audit reports it as "started before the newest plugin deploy".
  - Each key runs launchctl kickstart -k on that agent.
    - kickstart never rewrites a plist, and the agent's KeepAlive starts the process again.
  - A refused kickstart shows launchctl's own text.
  - The line beside the heading says how long each process has been up, which is how a restart shows as landed.
    - Read per poll from the pid launchd holds for that label, so it follows the agent the key restarts.
    - An agent launchd holds no process for reads "not running".
  - Restarting the gateway drops a chat in flight.
  - Restarting the webui needs an open chat page reloaded.
- The wordmark carries the OK/WARN/OFFLINE status text; on this page it warns while the iris-status thread reports issues.
- Design testing: tapping the [iris] wordmark cycles iris, dev, demo.
  - dev swaps the app icons for tiles linking to dev1.html..dev5.html next to that page. Each server serves its own dev files, so dropping proposal pages in needs no page or server edit.
  - demo reloads the page with every /api call sent to /demo/api: demo.py answers the state and the per-row reads with invented data covering every area and row state, and takes every write as a 202 that does nothing. Nothing real is read or written. The public demo's screenshots come from this mode.
