---
name: actions-inbox
description: Scan the inbox via the ops service and save action sets (calendar changes, shipments to track, documents to file) for the user to approve on the actions page. Used by the actions-inbox-scan cron job.
---

# Actions inbox

Turn new inbox emails into sets of proposed actions — calendar changes, shipments to track and documents to file — the user approves with one tap on the actions page. The LLM proposes, it never executes — approving happens only on the page, by the user.

- An email that needs no calendar change, ships nothing and carries no document worth keeping is ignored — a plain mention, a task for a person, a reminder request, a receipt, a reply the user has to write.

## Flow (the cron job runs this)

1. Call `scan` (no arguments).
2. Group the new emails from what their snippets show.
3. Read the ones you need with `read_email`. Read every email of a group before proposing a set for it: a set's rows never rest on a cut snippet.
4. For each group: ignore it, or build one set when the group needs a calendar change, carries a shipment to track or carries a document worth keeping, then call `save_set` once per group.
5. Final message: exactly `[SILENT]` — the job delivers nothing.

## Grouping

- Invitation prefixes ("Updated invitation:", "Canceled event:", "Synced invitation:") and different senders can all concern one event.
  - Extract the event identity (title + time) and group on it.
- The calendar sync service (calsync@example.org) mirrors invitations; its emails group with the original sender's emails for the same event.
- An email with no relations is its own group.

## Deciding the action

- Collapse all emails of a group into the final state first, then judge how much changed: one piece of information, or more.
  - Invitation emails:
    - Order events by their ICS stamped time; within one uid the latest stamp wins.
    - A CANCEL naming a single occurrence removes only that occurrence of its OWN uid — it says nothing about another uid's series, even at the same title and time.
    - A series REQUEST with an rrule and no exceptions includes every occurrence of that rule, including a date another uid canceled.
    - A sequence gap or a state that does not add up means an email is missing from the scan: state the assumption in the rationale instead of resolving with confidence.
  - Other emails: the latest email (receivedAt) wins on shared fields.
- Small change → minimal diff: update in place (an update_event row).
- Large change, or any recurrence-rule change → replacement, never an update:
  - One delete_event span=future for the old series, anchored on its first occurrence on or after max(today, the new series' start) — start_local must be a real occurrence start, the executor matches it exactly.
  - One create_event for the new series, after that delete.
  - One delete_event per canceled occurrence of the NEW series — create_event cannot encode exceptions.
  - Past occurrences always stay; delete forward only.
- Before proposing any calendar write, check calendar reality with macos_calendar list_events.
  - If reality already matches the final state — including the meeting link in the event's notes — the group needs no set: ignore it.
  - This diff is also the duplicate-work guard.
- A meeting link in the email (Zoom, Meet, Teams, ...) is part of the final state.
  - An existing event that lacks it does not match — propose an update_event row adding the link to the event's notes.
  - Take the full URL from the email's "Links in the HTML version" list, not from the body text — senders show URLs truncated there.
  - If no full URL is available anywhere, write the base link plus meeting ID and passcode; never write a truncated URL.
  - Add a link only to the event the email clearly concerns (same meeting, matching time) — an email never plants its link on an unrelated event.
- A plain email proposing or confirming a meeting time (no ICS attached) is actionable.
  - A settled time (both sides already agreed): propose one create_event for it, assumption stated in the rationale.
  - A time still open for the user to pick (someone proposes a time or asks when): propose a suggested time — see below.
  - Never ignore it without a proposal — the user denies if they do not want the event.
- A time stated in another timezone ("3pm UTC") converts to local time first — event times are always local.
- No cleanup proposals for past events; fine when they vanish with a deleted series.
- A missing fact that changes the action (which day, which room): still propose the likely option, and state the assumption plainly in the rationale.
  - The user denies and teaches; the correction lands here under per-sender notes.

## Shipments

- A shipping notice with a tracking number gets a set: its add_delivery rows, then the archive_email row.
  - A carrier from the number's shape, when the email names none: `1Z` plus 16 characters is UPS, 12 digits is FedEx, 22 digits starting `92` is USPS.
  - Ignore a shipping notice only when even the number's shape gives no carrier candidate.
  - Example row: `{"kind": "add_delivery", "label": "Track the Adafruit shipment in Parcel", "args": {"tracking_number": "1Z999AA10123456784", "carrier": "UPS", "description": "Adafruit Feather Kit"}}`
- An order confirmation without a tracking number is ignored; the shipping notice with the number comes later.
- A carrier's later mail for a shipment already in a set (out for delivery, delivered) is ignored.

## Documents

- Portal mail (Hillcrest's club portal notices) links its PDF instead of attaching it. That link is the file.
- A document with lasting value gets a set: its copy rows, then the archive_email row.
  - Annual audits have lasting value; a receipt for a small purchase has none.
- Club papers go under /Users/demo/Documents/SharedPapers/club: money papers (audits, budgets) → `finances`; minutes → `minutes`; newsletters → `newsletter`.
  - Those folders name files `<folder>-<kind>-<year>.pdf`: `finances-audit-2026.pdf`, `finances-budget-2026-2027.pdf`.
- Example row: `{"kind": "copy", "label": "Save the 2026 audit to the club finances folder", "args": {"email": "<id>", "link": "https://api.club-portal.example/files/…", "folder": "/Users/demo/Documents/SharedPapers/club/finances", "filename": "finances-audit-2026.pdf"}}`
- A document email that also announces an event gets both: the calendar row and the copy row in one set.

## Time suggestions

- When the meeting time is the user's to pick, propose one create_event row with the best time and its `suggestion` line.
- Picking the time:
  - The sender's own proposed time weighs most.
  - Check Work with macos_calendar list_events; never suggest a time that conflicts with an existing event.

## Thread follow-ups (thread_after)

- The scan attaches `thread_after` to new emails and to pending sets that carry a suggestion or open_email row: the messages in the same thread received after that email, oldest first.
  - Each entry: from, subject, receivedAt, body, and `from_you` — true when the user sent it.
  - An empty body on an entry means its fetch failed; the message exists, its content is unknown.
  - `thread_error` instead of `thread_after` means the check failed; treat it as "no follow-ups seen".
- For a time-suggestion case, three outcomes:
  - No `from_you` message in thread_after: add one open_email row before the archive row.
    - Label: one sentence telling the user to reply with their chosen time, naming the sender.
    - args.email is the member email's id string — the page renders an open-in-webmail button; approving only marks the row done.
    - Example row: `{"kind": "open_email", "label": "Reply to Nathan with the time you pick", "args": {"email": "<id>"}}`
  - A `from_you` message names one settled time: propose one create_event for that time (no suggestion, no open_email row), superseding the suggested-time set if one is pending.
  - A `from_you` message exists but settles nothing ("let me check"): keep the suggested-time row, no open_email row.
- The archive row stays last; an open_email row goes right before it.

## Calendars

- Pick the calendar whose description fits the event.
- Meetings with the user, with or without a Meet/Zoom link → Work.
- Club events (Hillcrest: tournaments, court closures, socials) → Family.
- One event goes on one calendar. Never put an event on both Work and Shared: the busy-events job copies every Work event to Shared as a "Work meeting" block.
- "Work meeting" events on Shared belong to that job. Never propose a change to one.

## Sets

- Every set normally ends with an archive_email row covering all member emails.
- Rationale: at most 3 short sentences, plain words, one idea per sentence (plain-English style).
  - State the final state, what the calendar holds now, and any assumption.
  - ICS forensics (uids, stamped times, sequence numbers) stay out — the emails hold them.
  - The server rejects a rationale over 300 characters.
- Row label: one plain sentence naming the exact arguments — the page renders it as-is.
- Selector rows (update_event, delete_event) name the target by calendar + title + start_local + span.
  - The service reads the live event when the set is saved and records its current fields itself (args.snapshot). Never write args.snapshot.
  - A selector that matches no live event rejects the whole save with an error naming the row — fix the selector or drop the row, then resend.
  - When two events share title and start (lookalikes), add args.expected as a tie-breaker: current facts about the intended event (e.g. notes_contains with the old Meet link). It must be true of the event right now, and must match exactly one of the lookalikes, or the save is rejected.
  - args.expected states what is, never the change the row will make. Wrong: notes_contains with the link the row is about to add.
- A span=future row without its `series` object reads "all later occurrences" on the page.
- A row executed before its prerequisite lands fails its pre-check (an exception-occurrence delete before its create matches nothing); repair is manual.
- kind="ignore" for every group that needs no calendar change, ships nothing and carries no document worth keeping: newsletters, notifications, marketing, tasks for a person, mail the user only has to answer, and events the calendar already holds as the email describes. It marks the emails processed, no set.
  - One ignore group per topic, same as action groups — never one ignore across unrelated emails.

## Per-sender notes (accrue as the user teaches)

- Morgan (Fitness Group): updated/canceled invitations collapse to the final series state; large changes replace the series.
- calsync@example.org: the calendar sync service; group with the original sender's emails for the same event.
- Hillcrest Tennis Club: club notices → Family events. "ACTION REQUIRED" items whose day depends on the user's details: propose with the assumption stated. Notices with a linked or attached document (audit, budget, minutes) → a copy row into /Users/demo/Documents/SharedPapers/club.
- An online class (booking@scheduler.example): booking reminders carry the Zoom link; the auto-imported calendar event's notes hold only booking boilerplate, no link. Check the event and propose the update_event adding the link.

## Worked example: the fitness-group set

- Emails: "Updated invitation … (Tue Jul 14 to Mon Aug 3)" + "Updated invitation … (Tue Aug 4 to Mon Aug 31, new Meet link)" + "Canceled event … Tue Aug 11" + the sync service's copy.
- The ICS facts split them into two uids:
  - Old uid: a CANCEL of the Aug 11 occurrence, then the series truncated (rrule until Aug 4) — it now ends Jul 28.
  - New uid: a new weekly series, Tue Aug 4 until Sep 1, no exceptions — Aug 11 included.
- Subjects alone mis-read this thread: the Aug 11 cancel belongs to the old uid, and the truncation makes it moot; the new series keeps Aug 11.
- Final state: old series ends Jul 28; new series weekly Tue 18:30–19:30 Aug 4–25 with the new Meet link, every occurrence standing.
- More than one piece of information changed → replacement:
  1. delete_event span=future on the old Work series (expected notes_contains the old link), anchored per the rule above.
  2. create_event Work, weekly Tue 18:30–19:30, 2026-08-04, repeat_until 2026-08-31, notes with the new Meet link.
  3. archive_email all 4 emails.
