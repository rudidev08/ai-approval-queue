# Setup — actions (service + page)

The page where the user approves things. Seven areas: emails (sets of cards derived from the inbox scan), messages (save proposals for attachments in watched Apple Messages chats), finance (the Actual Budget categorization queue), finance report (builds the daily report email, whole or one part, and shows its text), research (the vault's recurring web-research topics: run one now, or delete its stored report), audit (runs the deterministic Hermes audit and shows the pass as it happens), and system (a console with one row each for the hermes gateway and the hermes webui). A grid of glance tiles on top shows each area's open count and shorthand status; a tile picks the view — the leading "queue" tile stacks emails, messages and finance, an area tile shows it alone. Each area opens with its control row, built like the system console's rows: the area's header on the left — icon, name and one status line carrying its scan state, then its counts in their shortest form — and every key of that area on the right, so the title row and the control row are one row.

## Pieces

- server.py — the service, run by launchd com.example.iris.actions with KeepAlive
- page.html — the page itself, polling the service
- finance.py — the finance area; finance_scan.py fills it from the finance-uncategorized cron job
- messages.py — the messages area; messages_scan.py fills it from the messages-attach-scan cron job, reading the watched chats through the host app's macos_messages server, resolving senders through its macos_contacts server, and filing through the records server
- hermes_audit.py — the Hermes audit area; starts ../hermes-audit/audit.py (not part of this repo slice) and reads the files it leaves in ~/.local/state/hermes-audit/, plus the hermes-audit cron job's stamps from ~/.hermes/cron/ for the last-run and next-run indicators
- finance_report.py — the finance report area; runs finance_scan.py in its write-free mode and keeps the text in memory (this public copy stubs the run with canned sample text)
- research.py — the research area; reads the vault's research topics and reports (vault/research), and spawns services/mcp/research/driver.py for a run (not part of this repo slice)
- system.py — the system area; restarts the hermes gateway and the hermes webui through launchctl kickstart, and reads how long each has been up
- state/ — live data, kept out of git: state.json, finance.json, finance-ask.json, finance-reported.json, messages.json, hermes-audit-dismissed.json, research-cleared.json, decisions-YYYY.jsonl; directory 0700, files 0600
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
- newsyslog rotates both at 1 MB, keeping 2 (/etc/newsyslog.d/iris-actions.conf, mirrored in setup/newsyslog.d/ — not part of this repo slice).
  - Rotation SIGTERMs the pid in ~/.hermes/logs/actions.pid so KeepAlive restarts the service and it reopens the new files.

## Behavior worth knowing

- Every area carries its own keys in its control row, so a view is only which areas show: queue stacks emails, messages and finance, each with its own row, and an area view shows that one.
  - Report and research are the exceptions, their headers standing above their panels: report's keys pick report parts, so it has two rows named by hand (unsorted, report), and research has one expandable row per topic.
  - System has no control row — its keys belong to its console rows.
- The glance tiles count the rows waiting for a decision (emails, finance, messages) and the findings left (audit); the leading "queue" tile holds the emails + messages + finance total behind those three areas' icons, and its view stacks those three areas in that order — report, research, audit and system open from their own tiles only. The picked view sticks in localStorage per device, and a chip strip pins under the top bar while the tiles are scrolled out of view.
  - The host app's menu badge counts emails one per set holding a pending row, not one per row: a set is decided as a unit.
- Calendar dot colors come from a list_calendars call to the host app at boot, retried every 5 minutes until it lands.
- The emails scan key starts the actions-inbox-scan cron job; running state and last-run time come from hermes' cron ledger.
- Opening a member email row shows the email's own text (header block and body, as get_email renders them).
  - GET /api/emails/body?set_id&email_id reads it live; sets store the snapshot facts, never the body.
  - The page asks once per email and keeps the answer until reload.
  - An id the webmail child no longer knows — its session restarted, or an archive row moved the email out of the inbox — is re-registered by one mailbox-wide search on the subject, then fetched again. A failed fetch shows in the row as the tool's own message.
- A set card's head carries one key next to chat, one per state of the set: skip all, hide, or retry.
  - Skip all denies every row still waiting, one call each — the same deny as the row's own x key.
  - Hide takes a finished card off the page before its 24-hour window ends; POST /api/emails/hide marks the record, and the denials, ledger entries and log lines stay as they are.
  - Retry shows on a set that is stuck: nothing waiting, and a row that failed keeps the set pending. It is POST /api/emails/reset for that set — the set is voided and its emails leave the ledger.
    - Nothing proposes them until a scan runs, so the heading row says to run a rescan until one lands. A scan only offers emails absent from the ledger, and a set member is in it, so a rescan without the retry never brings them back.
    - The reminder lives in the page, not the service: a reload drops it, and the next scheduled scan picks the emails up either way.
  - The emails hide-finished key hides every finished set at once; it greys while no finished set is on the page. Hiding is final — nothing brings a hidden set back.
- A failed or stale scan reads in the area's heading row, in amber — there are no banner strips.
- Emails, messages and finance each show one centered line where their cards would be when nothing is waiting: "inbox is clear", "no attachments waiting", "everything is categorized". A failed scan puts its own card there instead.
  - Emails: "last scan failed" from the scan's own status, or "no scan in over 11 hr" when the last batch is older than that (three missed scans at the every-3-hours cadence, plus 2 h slack so one late run does not trip it). The "last ran" stamp next to it belongs to the cron job, which can run without a batch landing.
  - Finance and messages: "last run failed" / "last scan failed", plus a panel card carrying the failing step and its message.
- Forwarding an email to iris@ saying "add to actions" queues it for the page: the chat agent's add_to_actions posts /api/emails/intake, which resolves sender + subject (+ optional received date) against the iris inbox — exactly one match required, or a rejection with the candidates.
  - A match records an intent in state.json; the next scan picks the email up like any new mail, under an "iris:"-prefixed id and with the intent's category (finance|general) as a hint for the scan agent.
  - The iris account is a separate webmail account (the email platform's own mailbox), read through the service's own webmail child carrying only JMAP_TOKEN_READONLY_IRIS from ~/.hermes/.env — nothing moves or archives there. Without the token the endpoint 503s and a boot line in actions.log says intake is off.
  - Chat mail never enters the pipeline, only intent-matched ids. An intent is consumed when its email enters a set, dropped when the email leaves the iris inbox or after 14 days.
  - Sets reject archive_email rows on iris-source members — injected mail stays as chat history. The scan's trim drops state only when both inbox listings are provably complete (an empty iris inbox counts — it is the norm).
- The finance scan key first downloads a fresh budget copy, so new categories reach the picker, then starts the uncategorized scan; that run rebuilds the whole list.
- The finance report area has part keys picking a part of the daily report email — "report" holds full next to the green create-preview key, "unsorted" holds summary, cash flow, lenses, odd, new and links. That key builds the picked part, for developing a part at a time.
  - The picked part key carries a blue outline.
  - The service caches each part's last build in memory, so a part key switches the panel to that part's cached text with no new run — for comparing parts side by side.
  - The create-preview key runs finance_scan.py --email --today <today>, which is the script's write-free mode: no debts lines, no monthly-balance lines, no printed-transactions file, no card batch. A part other than full adds --section <name>.
  - Nothing is emailed. The report the 07:00 finance-daily job sends is unaffected. The full body opens with the script's own test-data line; a part prints bare.
  - A full build takes a minute or two (one local LLM call), so the key greys and the heading counts the build up and names the part; the page polls every 3 seconds while it runs. The cash flow, new, and links parts skip the LLM call and build in seconds.
  - The caches live in the service's memory only, so a restart drops them all.
  - One build at a time; a second tap gets a 409.
- The research area has one expandable row per vault research topic (vault/research/topics): the topic's name, a meta line with its cadence prefix and the last successful run (from the driver's runs log), and its own run and delete keys. The heading line counts the topics, the runs in flight, and the failing ones, and shows the next batch time (the research-batch cron job's next_run_at) — the batch is one job, so the stamp belongs to the area, not a row.
  - A chevron key (or a tap on the row) opens it: the topic's full prompt and the stored report's text (GET /api/research/report), each in its own banded block, read from the service on the first open and re-read when the report's updated stamp changes.
  - While the driver's log shows consecutive failures for a topic, the row carries an amber "failed ×N" status word; the open row holds the two-tap clear key, and the flag comes back if the next run fails too. Cleared flags live in state/research-cleared.json, keyed by the failed entry's log stamp.
  - A row's run key spawns services/mcp/research/driver.py --topic <slug> detached, the same spawn the research MCP server's run_report makes; the row reads "started" for a few seconds, then a green "running" while the driver's in-flight marker (~/.local/state/research/running.json) holds a fresh stamp for the slug, and the finished report arrives by email like a batch run's. Marker entries older than the driver's run timeout are ignored, so a killed driver's leftover ages out.
  - A row's delete key removes that topic's report and failure dump, keeping the topic, so the next run starts from scratch (the research MCP server's delete_report semantics; undo = cloud-storage versioning). It greys while the topic has neither file.
- The finance queue comes in two groups of 5: the newest uncategorized transactions, and 5 random older ones that are never any of the newest 5.
  - The page shows each group as its own card, headed "last 5" and "random 5".
  - A third group, "from email", holds cards proposed from a ping mail: the email session's run_action posts /api/finance/email-cards with (transaction_id, category) pairs it matched from the mail's chain; the service re-checks every id against the live uncategorized queue and the category against the budget, stamps the card's facts itself, and keeps at most 20 pending. The scan never adds or rebuilds these; prune, settle, skip and apply cover them like any card.
  - A transaction proposed both from email and by the scan shows one row, under "from email"; the finance status line counts the hidden duplicates.
  - Each email row carries a hide key, and the group label a hide-all key. Hiding drops the proposal only — the transaction stays uncategorized, and its scan row shows again at once.
  - A filed row keeps a hide key, and the control row's hide-finished key takes every filed transaction off at once (POST /api/finance/hide); both grey while nothing is filed. Filed means done or already handled — the transaction left the uncategorized queue, so no scan proposes it again. A failed row keeps its keys for the retry.
- A scheduled finance scan only tops up open slots, counted per group: pending cards stay, finished cards leave, new transactions fill the freed slots of their own group.
- The daily finance email ends with the week's new transactions, grouped by account, then category.
  - Each transaction is printed exactly once: state/finance-reported.json tracks the printed ids, and only a successful email run writes it.
- A pending finance card drops on the next poll once the budget copy shows its transaction handled.
- A scheduled finance save is held while this page is open, so it never swaps the cards out mid-review.
  - The page counts as open for 15 minutes after its last poll; a tab left open holds the batch until it closes.
  - The finance scan key is never held — it is a rebuild you asked for.
- A categorize ask emails numbered categorizing questions about uncategorized transactions to a helper (the finance-buddy flow); the helper's reply becomes approvable rows.
  - POST /api/finance/ask stores the ask (state/finance-ask.json; one open ask per recipient, at most 20 items) and returns the subject and numbered lines for the draft mail; the user sends the draft by hand.
  - The inbox scan marks a new email from an open ask's recipient finance_review (sender match, no subject tag) and attaches the whole thread; its pending_asks payload carries the open asks with per-item still-open flags and the valid category list.
  - The agent pairs the answers by number into categorize_transaction rows; the service stamps each row's display facts (args.transaction) from the ask store and validates the transaction against the ask.
  - Approving one writes category plus payee rule (skipped on update_rule=false) through the finance area's write helper, one row at a time service-wide; a busy write is waited out (150 s), then the row fails — the ask stays open, so the next scan re-proposes it.
  - An ask is open while any of its items is uncategorized and it is younger than 14 days; the scan's payload build prunes dead ones. Open asks are counted in the finance status line, with who each went to and when in that line's tooltip, so an expiring ask never vanishes in silence.
- The messages area proposes attachments worth filing from the watched chats (the partner and family chats, set in messages.env).
  - One card is one attachment: clean filename, the model's records-location pick in a dropdown covering the whole catalog, and a one-line reason. Approving runs export_attachment into the records inbox, then save_file into the picked location.
  - The sender shows as its contact name: scans resolve handles through macos_contacts and cache the mapping in messages.json — a number's owner does not change, so only misses re-check.
  - Scans also extract each candidate's text (export into the records inbox, read_file, delete the copy), so the model can file under the person named inside the document. The card's chevron shows the same text.
  - state/messages.json's ledger remembers every verdict for 30 days — long past the 72-hour scan lookback, so a scanned attachment is never proposed twice, and the file does not grow forever.
  - The messages rescan key starts the cron job on the spot; the amber reset key clears the ledger, so the next scan re-proposes whatever is still in the window.
  - The skip-all key denies every attachment still waiting, one call each; a denied attachment never comes back.
  - A saved row keeps a hide key, and the control row's hide-finished key takes every saved row off at once (POST /api/messages/hide); both grey while no row is saved. The next scan drops saved rows anyway, and the ledger keeps the verdict either way.
  - A failed save keeps its card: the error text shows and the check key retries. A card caught mid-approve by a service restart settles to the same retryable state at boot.
  - A failed scan posts an error record instead of cards, which the page shows as a "last scan failed" panel; the next good scan clears it.
- The Hermes audit area starts the same script the iris_ops tools start, so a run started in chat shows here too, and one started here shows there.
  - A pass can take an hour, so the run is detached: log rotation restarting this service does not touch it, and the audit's own files carry the state across.
  - One run at a time is the audit's own flock; the run key greys while a run holds it, and the stop key only exists while one does.
  - A category with findings gets a card as soon as it lands; clean categories are counted in the audit status line and named in its tooltip. Starting a run clears the previous pass's cards.
  - Each card's copy key puts that category's block on the clipboard, in the report's own wording.
  - Each finding carries the same two-tap amber x chip the inbox rows deny with; it hides that finding for the rest of the pass — reviewed, nothing to act on. The service filters the record before the page sees it, so the card's count is what is left and a card whose findings are all dismissed goes away.
    - The status line then counts what went, names it per category in its tooltip, and the restore key brings the whole pass's dismissals back.
    - state/hermes-audit-dismissed.json holds them, stamped with the run record's started_at; a list stamped with another run reads as empty, so any starter of the next run clears it and the file never holds more than one pass.
    - Hiding is the page only: the markdown report and the iris_ops tools read the audit's own record and still carry every finding.
- The system area's two keys restart the hermes gateway and the hermes webui.
  - Both processes read the tool-approvals plugin once, at start.
    - A plugin deploy made after that is unenforced until they restart.
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
- Design testing: tapping the [iris] wordmark swaps the app icons for tiles linking to dev1.html..dev5.html next to that page. Each server serves its own dev files, so dropping proposal pages in needs no page or server edit.
