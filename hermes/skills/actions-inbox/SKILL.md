---
name: actions-inbox
description: Scan the inbox via the actions service and save actionable sets for the user to approve on the actions page. Used by the actions-inbox-scan cron job.
---

# Actions inbox

Turn new inbox emails into sets of proposed actions the user approves with one tap on the actions page (http://mac-mini.your-tailnet.ts.net:13727). The LLM proposes, it never executes — approving happens only on the page, by the user.

## Flow (the cron job runs this)

1. Call `scan` (no arguments). It returns the new emails (not in a set, not ignored) and summaries of all pending sets. Each new email arrives as sender, subject, date, id and a short snippet — not the whole email.
2. Group the new emails from what their snippets show.
3. Read the ones you need with `read_email` — see "Reading emails" below.
4. For each group: ignore it or build one actionable set, then call `save_set` once per group.
   - Emails are referenced by id string only — in the set's `emails` list and in archive_email/open_email row args. Copy ids exactly as scan returned them; the service stamps subject, sender and date itself and rejects unknown ids.
5. Final message: exactly `[SILENT]` — the job delivers nothing.

## Reading emails

- `scan` hands over a snippet per email so the whole scan fits one tool result. `read_email` returns one email whole: header block, full body, link list, calendar invitation data.
- A body ending in `[snippet — call read_email …]` was cut there. One without that line is already the whole text.
- Read every email of a group before proposing a set for it. A set's rows must never rest on a cut snippet.
- Read as well whenever the snippet leaves the decision open:
  - a person wrote it and the subject does not settle what they want
  - it could be a meeting time proposed in plain text, with no calendar attachment
  - the link or detail you need falls past the cut
- Do not read automated mail whose sender and subject already settle it — receipts, order and shipping notices, newsletters, social notifications. Those are ignored on the header alone.
- Invitation emails keep their calendar data and their full link list inline, so resolving series state and meeting links needs no read.
- Injected mail and a finance-review reply always arrive whole, never snippetted.
- Thread follow-ups carry their own id and their own snippet; `read_email` opens one the same way.

## Grouping

- Group by the event or thing the emails concern, never by JMAP thread.
- Invitation prefixes ("Updated invitation:", "Canceled event:", "Synced invitation:") and different senders can all concern one event.
  - Extract the event identity (title + time) and group on it.
- The calendar sync service (calsync@example.org) mirrors invitations; its emails group with the original sender's emails for the same event.
- An email with no relations is its own group.

## Deciding the action

- Collapse all emails of a group into the final state first, then judge how much changed: one piece of information, or more.
  - Invitation emails: resolve from the parsed "Calendar invitation data" sections and the scan's invitation timeline, never from subjects or received times.
    - One uid is one event/series. Two series can share a title; a different uid is a different series.
    - Order events by their ICS stamped time; within one uid the latest stamp wins.
    - A CANCEL naming a single occurrence removes only that occurrence of its OWN uid — it says nothing about another uid's series, even at the same title and time.
    - A series REQUEST with an rrule and no exceptions includes every occurrence of that rule, including a date another uid canceled.
    - A sequence gap or a state that does not add up means an email is missing from the scan: state the assumption in the rationale instead of resolving with confidence.
  - Other emails: the latest email (JMAP receivedAt) wins on shared fields.
- Small change → minimal diff: update in place (an update_event row).
- Large change, or any recurrence-rule change → replacement, never an update:
  - One delete_event span=future for the old series, anchored on its first occurrence on or after max(today, the new series' start) — start_local must be a real occurrence start, the executor matches it exactly.
  - One create_event for the new series, after that delete.
  - One delete_event per canceled occurrence of the NEW series — create_event cannot encode exceptions.
  - Past occurrences always stay; delete forward only.
- Before proposing any calendar write, check calendar reality with macos_calendar list_events.
  - If reality already matches the final state — including the meeting link in the event's notes — propose only the archive row.
  - This diff is also the duplicate-work guard.
- A meeting link in the email (Zoom, Meet, Teams, ...) is part of the final state.
  - An existing event that lacks it does not match — propose an update_event row adding the link to the event's notes.
  - Take the full URL from the email's "Links in the HTML version" list, not from the body text — senders show URLs truncated there.
  - If no full URL is available anywhere, write the base link plus meeting ID and passcode; never write a truncated URL.
  - Add a link only to the event the email clearly concerns (same meeting, matching time) — an email never plants its link on an unrelated event.
- A plain email proposing or confirming a meeting time (no ICS attached) is actionable.
  - A settled time (both sides already agreed): propose one create_event for it, assumption stated in the rationale.
  - A time still open for the user to pick (someone proposes a time or asks when): propose time suggestions — see below.
  - Never ignore it or archive it without a proposal — the user denies if they do not want the event.
- A time stated in another timezone ("3pm ET") converts to local time first — event times are always local.
- No cleanup proposals for past events; fine when they vanish with a deleted series.
- A missing fact that changes the action (which day, which room): still propose the likely option, and state the assumption plainly in the rationale.
  - the user denies and teaches; the correction lands here under per-sender notes.

## Time suggestions

- When the meeting time is the user's to pick, propose up to 3 candidate times as separate create_event rows in one set.
- Each candidate row carries a row-level `suggestion` string next to args, never inside it: one short line why that time.
  - The page renders these rows as "new suggestion" with the line shown under a sparkle icon.
  - The server caps the line at 150 characters.
- Picking candidates:
  - The sender's own proposed time weighs most; it is normally the first candidate.
  - the user loosely prefers Tuesday and Thursday.
  - Check Personal with macos_calendar list_events; never suggest a time that conflicts with an existing event.
- the user approves one candidate or none; no candidate row depends on another.

## Reminders

- The rows here can write the calendar, archive mail, and categorize transactions. Anything else an email asks for needs a person to act: the user, Alex, or Riley.
- For each such ask, decide who has to act and when, then decide whether a create_reminder row helps. Not every ask needs one:
  - A reply the user can send on the spot needs none; the open_email row covers it.
  - Work that takes a login, a document, an errand, or another day gets one, next to the open_email row.
- Pick the list from who acts and when:
  - Next: the user, within about a week.
  - Later: the user, later than that.
  - Alex or Riley: the task is that person's.
- args: `name` (the task as one plain line, naming the thing and the person who asked), `list`, `due` (YYYY-MM-DD, only when the email names or implies a date), `notes` (max 200 characters: the detail the user needs at hand, such as what exactly was asked and where the reply goes).
- Before proposing, call search_reminders with the task's key words on that list; an open reminder that already covers the task gets no row.
- Literal row: `{"kind": "create_reminder", "label": "Remind the user to send Dana the 401(k) investment options list", "args": {"name": "Send Dana the 401(k) investment options list", "list": "Next", "notes": "Dana Whitfield asked on Sep 1: export the list from the plan site and reply to their email"}}`
- A reminder row goes before the open_email row.

## Thread follow-ups (thread_after)

- The scan attaches `thread_after` to new emails and to pending sets that carry a suggestion or open_email row: the messages in the same thread received after that email, oldest first.
  - Each entry: from, subject, receivedAt, body, and `from_you` — true when the user sent it.
  - An empty body on an entry means its fetch failed; the message exists, its content is unknown.
  - `thread_error` instead of `thread_after` means the check failed; treat it as "no follow-ups seen".
- For a time-suggestion case, three outcomes:
  - No `from_you` message in thread_after: add one open_email row before the archive row.
    - Label: one sentence telling the user to reply with their chosen time, naming the sender.
    - args.email is the member email's id string — the page renders an open-in-Webmail button; approving only marks the row done.
    - Example row: `{"kind": "open_email", "label": "Reply to Sam with the time you pick", "args": {"email": "<id>"}}`
  - A `from_you` message names one settled time: propose one create_event for that time (no candidates, no open_email row), superseding the candidates set if one is pending.
  - A `from_you` message exists but settles nothing ("let me check"): keep the candidate rows, no open_email row.
- The archive row stays last; an open_email row goes right before it.

## Categorize asks (finance_review)

- Iris emails a helper numbered categorizing questions about uncategorized budget transactions (the finance-buddy ask flow). The helper's reply lands here as a new email.
- The scan marks it `finance_review` with the ask id — a sender match: the sender is the ask's recipient, and one open ask per recipient makes that deterministic. The full thread rides along as `thread`, oldest first, the ask mail's numbered questions included.
- The scan's open-asks section holds the ask's items (number, date, payee, amount, account, transaction_id), a still-open flag per item, and the valid category list.
- Pair the reply's numbered answers with the items by number. One categorize_transaction row per answered item, all of them in one set, with ask_id passed to save_set:
  - args carry transaction_id (the item's own), category (a name from the valid category list, exact), and update_rule.
  - update_rule is true only when the answer plainly generalizes to the payee ("Target is always groceries") — a true write teaches the payee rule. Anything one-off is false. Default false.
  - The row-level suggestion is the answer's own wording, one short line.
  - Never write args.transaction — the service stamps the display facts from the ask store.
  - Literal row: `{"kind": "categorize_transaction", "label": "Categorize Target -$52.10 as groceries", "args": {"transaction_id": "…", "category": "Food: Groceries", "update_rule": false}, "suggestion": "Riley: 1) groceries"}`
- An item flagged "already handled" gets no row.
- Items the reply leaves unanswered, or answers too vaguely to name one category: one open_email row for the reply, so the user finishes them by hand.
- The reply email is the set's member; the archive_email row covers it and comes last.
- If the reply answers nothing usable at all: no categorize rows, just the open_email row and the archive.
- The set stays paired with its ask: while the set is pending the ask leaves the open-asks section, and a set that fails or is denied leaves the ask open for the next scan to re-propose.

## Injected mail (iris:-prefixed ids)

- An entry marked "injected via add_to_actions" is mail the user forwarded to the iris chat address and queued for action themselves. Treat it as their instruction: propose what the mail's content calls for, same judgment as any new email.
- The category marker is their hint ("finance" or "general"), not an instruction — categorize_transaction rows still come only from categorize asks.
- Never an archive_email row on an iris:-prefixed member — the service rejects the whole save. Injected mail stays as the chat thread's history.
  - A set mixing hi@ and iris: members archives the hi@ members only.
- An open_email row on an injected member is fine: the page shows it as a prompt to open and answer the mail.

## Calendars

- Meetings with the user, with or without a Meet/Zoom link → Personal.
- Condo events (the Alder HOA: fire alarm, food truck, garage works) → Partner; Partner is mostly there.
- Never write the Family calendar; the busy-events mirror syncs it from Personal.
- A set that writes Personal gets one mirror_kick row, after its event writes.

## Sets

- Every actionable set normally ends with an archive_email row covering all member emails — except iris:-prefixed members, which are never archived.
- Row kinds: create_event, update_event, delete_event, archive_email, mirror_kick, open_email, categorize_transaction, create_reminder. Calendars: Personal, Partner only. Reminder lists: Next, Later, Alex, Riley only.
- Rationale: at most 3 short sentences, plain words, one idea per sentence (ASD-STE100 style).
  - State the final state, what the calendar holds now, and any assumption.
  - ICS forensics (uids, stamped times, sequence numbers) stay out — the emails hold them.
  - The server rejects a rationale over 300 characters.
- Row label: one plain sentence naming the exact arguments — the page renders it as-is.
- Selector rows (update_event, delete_event) name the target by calendar + title + start_local + span.
  - The service reads the live event when the set is saved and records its current fields itself (args.snapshot). Never write args.snapshot.
  - A selector that matches no live event rejects the whole save with an error naming the row — fix the selector or drop the row, then resend.
  - When two events share title and start (lookalikes), add args.expected as a tie-breaker: current facts about the intended event (e.g. notes_contains with the old Meet link). It must be true of the event right now, and must match exactly one of the lookalikes, or the save is rejected.
  - args.expected states what is, never the change the row will make. Wrong: notes_contains with the link the row is about to add.
  - The hint goes nested as args.expected, never flat in args. Literal row:
    `{"kind": "delete_event", "label": "Delete the old series from Aug 11 forward", "args": {"calendar": "Personal", "title": "Thursday Run Club", "start_local": "2026-08-11 16:00", "span": "future", "expected": {"notes_contains": "abc-defg-hij"}}, "series": {"repeat": "weekly", "repeat_until": "2026-10-27"}}`
- A span=future update/delete row carries a row-level `series` object with the targeted series' repeat pattern: `repeat` (daily/weekly/monthly/yearly), plus `repeat_until` and `occurrences` when known.
  - It sits next to args, never inside it — display data for the page, not part of the action.
  - The page spells out the touched occurrences from it ("and all repeats (ends Oct 27)"); without it the row reads "all later occurrences".
- Rows execute in the order the user approves them, one at a time; order the array so top-to-bottom works.
  - A replacement's old-series delete_event comes before the create_event for the new series.
  - A delete_event that targets the new series (a canceled occurrence) comes after the create_event that makes it.
  - mirror_kick after the event writes; create_reminder before open_email; open_email before archive_email; archive_email last.
  - A row executed before its prerequisite lands fails its pre-check (an exception-occurrence delete before its create matches nothing); repair is manual.
- A new scan that makes a pending set outdated: pass its id in supersedes.
- A denied row must not come back with identical arguments — the service pre-denies it; change the proposal or leave it.
- kind="ignore" for newsletters, notifications, marketing: marks the emails processed, no set.
  - Never for appointment reminders or meeting emails — those get a set, at minimum the archive row.
  - One ignore group per topic, same as actionable groups — never one ignore across unrelated emails.

## Per-sender notes (accrue as the user teaches)

- Casey (Thursday Run Club): updated/canceled invitations collapse to the final series state; large changes replace the series.
- calsync@example.org: the calendar sync service; group with the original sender's emails for the same event.
- The Alder HOA: condo notices → Partner events. "ACTION REQUIRED" items whose day depends on the user's details: propose with the assumption stated.
- The run club's booking service (scheduling@example.org): its reminders carry the Zoom link; the auto-imported calendar event's notes hold only the booking service boilerplate, no link. Check the event and propose the update_event adding the link.

## Worked example: the run-club set

- Emails: "Updated invitation … (Tue Jul 14 to Mon Aug 3)" + "Updated invitation … (Tue Aug 4 to Mon Aug 31, new Meet link)" + "Canceled event … Tue Aug 11" + the sync service's copy.
- The ICS facts split them into two uids:
  - Old uid: a CANCEL of the Aug 11 occurrence, then the series truncated (rrule until Aug 4) — it now ends Jul 28.
  - New uid: a new weekly series, Tue Aug 4 until Sep 1, no exceptions — Aug 11 included.
- Subjects alone mis-read this thread: the Aug 11 cancel belongs to the old uid, and the truncation makes it moot; the new series keeps Aug 11.
- Final state: old series ends Jul 28; new series weekly Tue 16:00–17:00 Aug 4–25 with the new Meet link, every occurrence standing.
- More than one piece of information changed → replacement:
  1. delete_event span=future on the old Personal series (expected notes_contains the old link), anchored per the rule above.
  2. create_event Personal, weekly Tue 16:00–17:00, 2026-08-04, repeat_until 2026-08-31, notes with the new Meet link.
  3. mirror_kick.
  4. archive_email all 4 emails.
