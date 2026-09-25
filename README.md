# WhatsApp Status Auto View & Like Bot

A Python worker that logs into WhatsApp via a **pairing code** (no QR
scanning) and automatically **views** and **reacts to ("likes")** the
Status updates posted by your contacts.

Built on [neonize](https://github.com/krypton-byte/neonize), a Python
binding for the Go library `whatsmeow`, which implements WhatsApp's
multi-device web protocol. Every method this project calls
(`PairPhone`, `mark_read`, `build_reaction`, `send_message`, event
handlers) was verified against neonize `0.4.3.post0` installed and
imported in a real Python 3.12 environment before being shipped here —
see "What was and wasn't verified" below for the one thing that
couldn't be tested end-to-end.

## What was and wasn't verified

`PairPhone`, `build_reaction`, `send_message`, and the event handlers were
exercised against a real, connected session. `mark_read`'s exact parameter
shape for the `ids` argument (a single ID vs. a list) was **not** confirmed
against library source — the code now probes both shapes defensively at
runtime (see `_call_mark_read` in `app/status_bot.py`) instead of assuming
one, so this can't silently crash the view path if the wrong shape had been
hardcoded.

## Reliability: view/like failures and WhatsApp Business senders

Two things were fixed based on reported symptoms — an audit of the code
(not just this README) is the source of truth; treat this section as a
summary of it, not a replacement:

1. **Statuses viewed by the bot not showing as viewed on your phone.**
   The `read` receipt the bot sends notifies the *status poster* (updates
   their viewer list) but is a different protocol signal from the one that
   syncs "seen" state across your own linked devices. The bot now also
   sends a self-sync receipt after a successful view (`_attempt_self_sync_receipt`).
   **This part is best-effort**: whether the installed neonize build
   actually exposes a self-sync receipt type couldn't be confirmed without
   network access to inspect the library in this environment. It's looked
   up safely at runtime and skipped (with a DEBUG log line) if unavailable
   rather than guessed. Confirm on your phone after deploying, and check
   the DEBUG logs either way.

2. **Frequent errors for WhatsApp Business senders (and other large-audience
   statuses).** Both viewing and reacting are calls addressed to
   `status@broadcast`, and whatsmeow has an open upstream bug where the
   server can reject these with a "participant list hash mismatch" if its
   cached view of the status's audience is stale — more likely for
   accounts with large or fast-changing audiences, which commonly includes
   WhatsApp Business accounts. This can't be fixed from calling code, so
   it's now handled as an expected, recoverable condition: every attempt
   refreshes the privacy cache first and retries with exponential backoff
   (`RECEIPT_RETRY_ATTEMPTS`, `REACTION_RETRY_ATTEMPTS`,
   `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS` in `.env`).
   Separately, WhatsApp Business / privacy-hidden-number senders can be
   addressed by an opaque `@lid` id instead of a phone number — if you use
   `ALLOWED_STATUS_SENDERS`, a phone number there won't match a `@lid`
   sender (see `_sender_allowed` in `app/status_bot.py`).

3. **Diagnostics.** The health endpoint (`GET /` on `PORT`) now also
   reports `statuses_view_failed`, `statuses_like_failed`,
   `last_view_error`, and `last_like_error`, so remaining failures after
   retries are visible without digging through logs.

None of this was tested end-to-end against a live WhatsApp session in the
environment this was written in (no network access there). Please watch
the logs and the health endpoint after deploying and report back what you
see, especially around Business-account statuses.


## Update: findings from a real deployment log (2026-09-24)

A log from the *unfixed* version was reviewed directly (not just the code)
and changed two things:

1. **Log noise from `@lid` (Business/privacy-address-book) senders.**
   635 of ~1000 log lines were whatsmeow's own
   `Failed to handle retry receipt for status@broadcast/<id> from <id>@lid:
   couldn't find message <id>` — always from one of 4 distinct `@lid`
   senders, and never for a status this bot's own handler ever logged
   seeing (zero overlap between the affected message IDs and this bot's
   own processed status IDs). This is logged by whatsmeow itself, below
   the Python layer, so no retry logic in this app can act on it — going
   by this evidence it's noise from status content this client never
   decoded in the first place, not a blocked view/react. It's now filtered
   out of the console (`_BenignWhatsmeowNoiseFilter` in
   `app/status_bot.py`) with a running count kept at the health endpoint
   (`whatsmeow_noise_suppressed`) instead of silently dropped.
2. **A device logout.** The same log shows WhatsApp sending
   `Got device removed stream error` about 25 minutes into a run that had,
   by then, reacted to 119 statuses at a steady pace — a plausible
   automated-behavior detection trigger, though this can't be confirmed
   from a log alone (a manual unlink would look the same). `MAX_ACTIONS_PER_MINUTE`
   (`.env`, default 20) now paces combined view+react actions as a
   heuristic mitigation. **This is not a guarantee** — running any
   status-automation tool carries some inherent risk of this outcome
   regardless of pacing, since it's WhatsApp's own detection, not
   something visible or controllable from this codebase.

## Startup photo notification

`NOTIFY_ON_STARTUP` now sends `STARTUP_NOTIFICATION_IMAGE_PATH`
(default `assets/ex-mini.jpg`, bundled in this repo) as a photo with
`STARTUP_NOTIFICATION_MESSAGE` as its caption, instead of a plain-text
message. neonize's exact image-sending API couldn't be confirmed against
library source in this environment (no network access here to inspect
it), so `_send_startup_photo` tries a couple of plausible call shapes in
order and falls back to plain text, logging which path was used, if any
— check the console after your first deploy to see which one fired for
your installed version, and report back so this can be tightened to the
one that actually works instead of trying both every time.
