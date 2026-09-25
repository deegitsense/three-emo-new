"""
StatusBot wires up a neonize (whatsmeow) WhatsApp client that:
  - logs in via a phone-number pairing code (no QR scanning required)
  - detects incoming WhatsApp Status updates from your contacts
  - marks them as viewed
  - reacts to them with an emoji ("likes" them), cycling through the
    configured emoji list in order (one step forward per status)

Notes on how status detection works:
WhatsApp statuses arrive over the same protocol channel as normal messages,
addressed to the special JID "status@broadcast", with the actual poster in
the message's sender field. There is no separate "status event" in the
underlying protocol — we simply filter the normal message stream for that
chat JID.

Reliability notes (read this before assuming a failure is a new bug):
Both viewing and reacting to a status are network calls addressed to
"status@broadcast", and whatsmeow has a still-open upstream issue where the
server can reject such calls with a "participant list hash mismatch" if its
cached copy of who's allowed to see that status is stale (whatsmeow tracker
issue #668). Statuses from accounts with large or fast-changing audiences —
which commonly includes WhatsApp Business accounts with many customers or
followers — hit this more than a plain 1:1 contact would. This module can't
fix that upstream bug, but treats it as an expected, recoverable condition:
every view/react attempt refreshes the status-privacy cache first and is
retried with exponential backoff + jitter before being logged as a real
failure (see RECEIPT_RETRY_ATTEMPTS / REACTION_RETRY_ATTEMPTS /
RETRY_BASE_DELAY_SECONDS / RETRY_MAX_DELAY_SECONDS in app/config.py).

A second, unrelated symptom this build addresses: a status marked "viewed"
here (a linked/companion-device session) can still show up as unviewed on
your primary phone, because the "read" receipt sent to the poster and the
receipt that syncs "seen" state across your OWN devices are two different
signals in WhatsApp's protocol. _attempt_self_sync_receipt below sends
both. Whether this neonize build actually exposes the second ("self-sync")
receipt type could not be confirmed against library source in this
environment (no network access here to install/inspect neonize directly) —
it's looked up defensively at runtime and skipped with a debug log if
unavailable, rather than guessed and hardcoded. Watch your phone after
deploying this to confirm the tray actually clears, and check DEBUG logs
for "self-sync receipt" lines either way.

Third: WhatsApp Business / privacy-hidden-number senders can be addressed
as <opaque-id>@lid instead of <phone>@s.whatsapp.net. See _sender_allowed
below for how that interacts with ALLOWED_STATUS_SENDERS.

Findings from a real deployment log (2026-09-24, pre-dating the fixes
above) that shaped the two additions below:
  - 635 of that log's ~1000 lines were the exact same whatsmeow-internal
    line: "Failed to handle retry receipt for status@broadcast/<id> from
    <id>@lid: couldn't find message <id>" — always from one of 4 distinct
    @lid senders, and NEVER for a status this bot's own handler ever saw
    (zero overlap between the message IDs in that error and the message
    IDs in this bot's own "New status from" log lines). This is logged by
    whatsmeow itself, below the Python layer — nothing in this file can
    catch or retry it — and, going by this evidence, it corresponds to
    status content this client never even decoded, not to anything this
    bot tried and failed to view/react to. It's treated as confirmed
    background noise from certain LID/Business-style senders and filtered
    out of the console (see _BenignWhatsmeowNoiseFilter) rather than left
    to flood the logs, with a running count kept at the health endpoint so
    nothing is silently lost.
  - Separately, and more seriously: that same log shows WhatsApp itself
    sending "Got device removed stream error" ~25 minutes into a run that
    had, by that point, reacted to 119 statuses at a steady pace with only
    a short randomized delay and a fixed 3-emoji rotation — a plausible
    trigger for WhatsApp's automated-behavior detection, though this can't
    be confirmed from a log alone (it could equally have been a manual
    unlink). _RateLimiter below caps the pace as a heuristic mitigation.
    It is NOT a guarantee — running any status-automation tool against a
    real account carries some inherent risk of exactly this, regardless of
    pacing.
"""
from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
from collections import deque
from typing import Deque, Set

from neonize.client import NewClient
from neonize.events import (
    ConnectedEv,
    DisconnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    event,
)
from neonize.utils.enum import ReceiptType
from neonize.utils.jid import JIDToNonAD, build_jid

from . import health_server
from .config import Config

logger = logging.getLogger("status_bot")

STATUS_BROADCAST_JID = build_jid("status", server="broadcast")


def _is_status_broadcast(chat) -> bool:
    return chat.User == STATUS_BROADCAST_JID.User and chat.Server == STATUS_BROADCAST_JID.Server


class _SeenIds:
    """Bounded set that remembers recently processed message IDs so a
    reconnect / offline-sync replay doesn't cause double reactions."""

    def __init__(self, max_size: int = 5000) -> None:
        self._order: Deque[str] = deque()
        self._set: Set[str] = set()
        self._max_size = max_size
        self._lock = threading.Lock()

    def add_if_new(self, item: str) -> bool:
        with self._lock:
            if item in self._set:
                return False
            self._order.append(item)
            self._set.add(item)
            if len(self._order) > self._max_size:
                oldest = self._order.popleft()
                self._set.discard(oldest)
            return True


class _BenignWhatsmeowNoiseFilter(logging.Filter):
    """Drops a specific, confirmed-benign whatsmeow-internal log line (see
    the module docstring) instead of letting it flood the console. Counts
    what it drops and mirrors that count to the health endpoint
    (`whatsmeow_noise_suppressed`) so the information isn't lost, just
    decluttered. Only matches this one exact, evidence-backed pattern —
    every other whatsmeow.Client log line passes through untouched."""

    _NOISE = "Failed to handle retry receipt for status@broadcast"

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if self._NOISE in record.getMessage():
            self.count += 1
            health_server.state.update(whatsmeow_noise_suppressed=self.count)
            return False
        return True


class _RateLimiter:
    """Blocks the calling thread until fewer than `max_per_minute` actions
    have happened in the trailing 60 seconds. See the module docstring's
    note on the "device removed" event found in a real log — this is a
    heuristic mitigation for that, not a guarantee. max_per_minute <= 0
    disables the cap entirely."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max_per_minute
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.max_per_minute <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(now)
                    return
                wait = 60 - (now - self._timestamps[0])
            time.sleep(max(wait, 0.05))


def _prompt_for_phone_number() -> str:
    print()
    print("=" * 64)
    print("No existing WhatsApp session found — pairing is required.")
    print("=" * 64)
    while True:
        raw = input(
            "Enter the WhatsApp number to pair "
            "(digits only, country code, no '+' or spaces, e.g. 15551234567): "
        ).strip()
        digits = raw.lstrip("+").replace(" ", "").replace("-", "")
        if digits.isdigit() and len(digits) >= 8:
            return digits
        print("That doesn't look like a valid number — try again.")


class StatusBot:
    def __init__(self, config: type[Config]) -> None:
        self.config = config
        os.makedirs(os.path.dirname(config.SESSION_DB_PATH) or ".", exist_ok=True)

        # Log exactly what's being used, before anything else — this is the
        # single most common source of "it still asks me to pair" reports:
        # SESSION_DB_PATH pointing somewhere different from wherever a
        # session file was actually placed (e.g. a Railway Volume mount).
        exists = os.path.exists(config.SESSION_DB_PATH)
        size = os.path.getsize(config.SESSION_DB_PATH) if exists else 0
        logger.info("Session file path: %s", config.SESSION_DB_PATH)
        logger.info(
            "Session file found: %s%s",
            exists,
            f" ({size} bytes)" if exists else " — a fresh pairing will be required",
        )

        self.client = NewClient(config.SESSION_DB_PATH)
        logger.info("is_logged_in (from existing session, if any): %s", self.client.is_logged_in)

        self._seen = _SeenIds()
        self._viewed_count = 0
        self._view_failed_count = 0
        self._liked_count = 0
        self._like_failed_count = 0
        self._counters_lock = threading.Lock()
        self._startup_notified = False

        # Cache for _call_mark_read's signature probing — see its docstring.
        self._mark_read_expects_list: bool | None = None

        # See module docstring: filters out one confirmed-benign whatsmeow
        # log line instead of letting it flood the console, and paces
        # outbound view/react actions as a heuristic anti-detection measure.
        self._noise_filter = _BenignWhatsmeowNoiseFilter()
        logging.getLogger("whatsmeow.Client").addFilter(self._noise_filter)
        self._rate_limiter = _RateLimiter(config.MAX_ACTIONS_PER_MINUTE)

        # Round-robin position into config.REACTION_EMOJIS: each new status
        # that gets liked takes the NEXT emoji in the list, wrapping back to
        # the start once the end is reached. Guarded by its own lock since
        # each status is liked on its own background thread.
        self._emoji_index = 0
        self._emoji_lock = threading.Lock()

        self._register_handlers()

    def _next_reaction_emoji(self) -> str:
        emojis = self.config.REACTION_EMOJIS
        with self._emoji_lock:
            emoji = emojis[self._emoji_index % len(emojis)]
            self._emoji_index += 1
        return emoji

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _register_handlers(self) -> None:
        client = self.client

        @client.qr
        def _suppress_qr_terminal_spam(_client, _data: bytes) -> None:
            # neonize registers a default handler that prints an ASCII QR
            # code straight to the terminal (via segno) any time whatsmeow's
            # normal QR login channel fires — which it does in the
            # background even when we're using PairPhone/pairing-code login
            # instead. Left at its default, this repeatedly dumps garbled,
            # line-wrapped QR blocks into any non-interactive log viewer
            # (like Railway's), burying the actual pairing code. We only
            # want the pairing-code flow, so this override is a deliberate
            # no-op.
            logger.debug("Ignoring a QR event (this app uses pairing-code login only)")

        @client.event(ConnectedEv)
        def on_connected(_client, _ev) -> None:
            logger.info("Connected to WhatsApp")
            health_server.state.update(connected=True)
            if self.config.NOTIFY_ON_STARTUP and not self._startup_notified:
                self._startup_notified = True
                threading.Thread(target=self._send_startup_notification, daemon=True).start()

        @client.event(DisconnectedEv)
        def on_disconnected(_client, _ev) -> None:
            logger.warning("Disconnected from WhatsApp")
            health_server.state.update(connected=False)

        @client.event(LoggedOutEv)
        def on_logged_out(_client, _ev) -> None:
            logger.error(
                "This device was logged out by WhatsApp (unlinked from the "
                "phone). Delete %s and redeploy to pair again.",
                self.config.SESSION_DB_PATH,
            )
            health_server.state.update(connected=False, logged_in=False)

        @client.event(PairStatusEv)
        def on_pair_status(_client, ev) -> None:
            logger.info("Paired successfully as %s", ev.ID.User)
            logger.info("✅ Bot connected successfully — now watching for statuses.")
            health_server.state.update(logged_in=True, pairing_code=None)

        @client.event(MessageEv)
        def on_message(client, ev) -> None:
            try:
                self._handle_message(client, ev)
            except Exception:
                logger.exception("Error while handling an incoming event")

    def _handle_message(self, client, ev) -> None:
        source = ev.Info.MessageSource

        if not _is_status_broadcast(source.Chat):
            return  # not a status update, ignore
        if source.IsFromMe:
            return  # don't react to your own posted statuses

        if not self._sender_allowed(source.Sender):
            return

        message_id = ev.Info.ID
        if not self._seen.add_if_new(message_id):
            return  # already processed (e.g. replayed on reconnect)

        logger.info(
            "New status from %s@%s (id=%s)",
            source.Sender.User, getattr(source.Sender, "Server", "?"), message_id,
        )

        # Do the actual work off the event-dispatch thread so a slow network
        # call (or the intentional human-like delay before reacting) never
        # blocks processing of the next incoming event.
        threading.Thread(
            target=self._process_status,
            args=(client, source, message_id),
            daemon=True,
        ).start()

    def _sender_allowed(self, sender) -> bool:
        """True if this sender's statuses should be viewed/liked at all.

        Plain consumer WhatsApp senders are addressed as
        <phone>@s.whatsapp.net, so `.User` is a phone number and a simple
        membership check against ALLOWED_STATUS_SENDERS works. WhatsApp
        Business accounts and privacy/address-book-hidden numbers can
        instead be addressed as <opaque-id>@lid — `.User` there is NOT a
        phone number, so a phone-number allowlist can never match it there.
        When that happens we also check the full JID string (in case
        someone deliberately added a lid identifier to the list), and log a
        hint rather than silently dropping the status with no explanation.
        """
        allowed = self.config.ALLOWED_STATUS_SENDERS
        if not allowed:
            return True
        if sender.User in allowed or str(sender) in allowed:
            return True
        if "lid" in (getattr(sender, "Server", "") or "").lower():
            logger.debug(
                "Status from a LID-addressed sender (%s@%s) didn't match "
                "ALLOWED_STATUS_SENDERS by phone number — add the exact id "
                "(%s) to the list instead if you want to allow it.",
                sender.User, sender.Server, str(sender),
            )
        return False

    # ------------------------------------------------------------------
    # Status actions
    # ------------------------------------------------------------------
    def _process_status(self, client, source, message_id: str) -> None:
        # View and react are independent, separately-retried network
        # operations — run them on their own threads so one's retries /
        # backoff never delay the other.
        workers = []
        if self.config.VIEW_STATUSES:
            workers.append(threading.Thread(
                target=self._safe_view_status, args=(client, source, message_id), daemon=True,
            ))
        if self.config.LIKE_STATUSES:
            workers.append(threading.Thread(
                target=self._safe_like_status, args=(client, source, message_id), daemon=True,
            ))
        for w in workers:
            w.start()

    def _safe_view_status(self, client, source, message_id: str) -> None:
        try:
            self._view_status(client, source, message_id)
        except Exception:
            logger.exception("Unexpected error viewing status %s", message_id)

    def _safe_like_status(self, client, source, message_id: str) -> None:
        try:
            self._like_status(client, source, message_id)
        except Exception:
            logger.exception("Unexpected error reacting to status %s", message_id)

    def _retry_with_backoff(
        self, description: str, attempts: int, base_delay: float, max_delay: float, action
    ) -> bool:
        """Runs action() (a zero-arg callable that raises on failure) up to
        `attempts` times, with exponential backoff + jitter between tries.
        Returns True on the first successful call, False if every attempt
        failed (each attempt's failure is logged as it happens)."""
        for attempt in range(1, attempts + 1):
            try:
                action()
                if attempt > 1:
                    logger.info("%s succeeded on attempt %d/%d", description, attempt, attempts)
                return True
            except Exception as exc:
                if attempt < attempts:
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.25)  # jitter
                    logger.warning(
                        "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                        description, attempt, attempts, exc, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "%s failed after %d attempt(s), giving up: %s",
                        description, attempts, exc,
                    )
        return False

    def _refresh_status_privacy(self, client) -> None:
        """Best-effort nudge to get the client's cached status-audience
        list back in sync with the server before a view/react attempt —
        see the module docstring re: whatsmeow issue #668. Not guaranteed
        to eliminate the error on its own, which is why every caller also
        goes through _retry_with_backoff."""
        try:
            client.get_status_privacy()
        except Exception:
            logger.debug("get_status_privacy() refresh failed, continuing anyway", exc_info=True)

    def _call_mark_read(self, client, source, message_id: str, receipt=ReceiptType.READ) -> None:
        """neonize's mark_read `ids` parameter shape (a single ID vs. a
        list) isn't something this environment could verify against
        library source (no network access here to install/inspect neonize
        directly). whatsmeow's underlying Go MarkRead takes a list, so
        that's tried first; a single-string fallback covers bindings that
        flatten it. Whichever shape works is cached so later calls don't
        pay the double-attempt cost."""
        if self._mark_read_expects_list is not False:
            try:
                client.mark_read([message_id], chat=source.Chat, sender=source.Sender, receipt=receipt)
                self._mark_read_expects_list = True
                return
            except TypeError:
                if self._mark_read_expects_list is True:
                    raise  # already confirmed list-shape elsewhere — a real error, don't mask it
                self._mark_read_expects_list = False
        client.mark_read(message_id, chat=source.Chat, sender=source.Sender, receipt=receipt)

    def _view_status(self, client, source, message_id: str) -> None:
        self._refresh_status_privacy(client)

        def _attempt() -> None:
            self._refresh_status_privacy(client)  # re-sync before each retry too
            self._rate_limiter.acquire()
            self._call_mark_read(client, source, message_id)

        ok = self._retry_with_backoff(
            f"Mark status {message_id} as viewed",
            self.config.RECEIPT_RETRY_ATTEMPTS,
            self.config.RETRY_BASE_DELAY_SECONDS,
            self.config.RETRY_MAX_DELAY_SECONDS,
            _attempt,
        )
        with self._counters_lock:
            if ok:
                self._viewed_count += 1
                health_server.state.update(statuses_viewed=self._viewed_count)
            else:
                self._view_failed_count += 1
                health_server.state.update(
                    statuses_view_failed=self._view_failed_count,
                    last_view_error=f"status {message_id} from {source.Sender.User}",
                )
        if ok:
            logger.debug("Marked status %s as viewed", message_id)
            self._attempt_self_sync_receipt(client, source, message_id)

    def _attempt_self_sync_receipt(self, client, source, message_id: str) -> None:
        """The `read` receipt above tells the STATUS POSTER you viewed it
        (updates their viewer list). Whether it also clears the status
        from the tray on your OTHER linked devices (e.g. your phone) is a
        separate sync signal in WhatsApp's protocol. This looks up a
        self-sync receipt type by name defensively — never hardcoding an
        assumption this build couldn't verify — and skips cleanly if this
        neonize build doesn't expose one, rather than crashing."""
        self_receipt = (
            getattr(ReceiptType, "READ_SELF", None)
            or getattr(ReceiptType, "ReadSelf", None)
        )
        if self_receipt is None:
            logger.debug(
                "No self-sync receipt type exposed by this neonize build — "
                "skipped the extra own-device sync attempt for status %s.",
                message_id,
            )
            return
        try:
            self._call_mark_read(client, source, message_id, receipt=self_receipt)
            logger.debug("Sent self-sync receipt for status %s", message_id)
        except Exception:
            logger.debug(
                "Self-sync receipt for status %s failed (non-fatal)", message_id, exc_info=True
            )

    def _like_status(self, client, source, message_id: str) -> None:
        delay = random.uniform(
            self.config.MIN_REACT_DELAY_SECONDS, self.config.MAX_REACT_DELAY_SECONDS
        )
        time.sleep(delay)

        # Picked once per status (not per retry attempt) so a retried
        # status still lands on a single emoji instead of burning through
        # several steps of the round-robin.
        reaction_emoji = self._next_reaction_emoji()

        def _attempt() -> None:
            self._refresh_status_privacy(client)
            self._rate_limiter.acquire()
            # Verified against whatsmeow's own godoc example and against
            # the actual source of a real, actively maintained
            # whatsmeow-based project (go-whatsapp-web-multidevice): `to`
            # must be the SAME JID as build_reaction's `chat` argument —
            # never the poster's JID directly. For a status this means
            # `to = status@broadcast`.
            reaction_message = client.build_reaction(
                source.Chat, source.Sender, message_id, reaction=reaction_emoji
            )
            client.send_message(source.Chat, reaction_message)

        ok = self._retry_with_backoff(
            f"React to status {message_id} with {reaction_emoji}",
            self.config.REACTION_RETRY_ATTEMPTS,
            self.config.RETRY_BASE_DELAY_SECONDS,
            self.config.RETRY_MAX_DELAY_SECONDS,
            _attempt,
        )
        with self._counters_lock:
            if ok:
                self._liked_count += 1
                health_server.state.update(statuses_liked=self._liked_count)
            else:
                self._like_failed_count += 1
                health_server.state.update(
                    statuses_like_failed=self._like_failed_count,
                    last_like_error=f"status {message_id} from {source.Sender.User}",
                )
        if ok:
            logger.info("Reacted to status %s with %s", message_id, reaction_emoji)

    def _send_startup_notification(self) -> None:
        # Small buffer so the freshly authenticated session has settled
        # before we try to send through it.
        time.sleep(2)
        try:
            me = self.client.get_me()
            own_jid = JIDToNonAD(me.JID)
        except Exception:
            logger.exception("Failed to resolve own JID for startup notification")
            return

        caption = self.config.STARTUP_NOTIFICATION_MESSAGE
        image_path = self.config.STARTUP_NOTIFICATION_IMAGE_PATH
        if image_path and self._send_startup_photo(own_jid, image_path, caption):
            logger.info("Sent startup confirmation photo to your own WhatsApp inbox")
            return
        if image_path:
            logger.warning(
                "Couldn't send the startup photo — falling back to a "
                "plain-text startup notification instead."
            )

        try:
            self.client.send_message(own_jid, caption)
            logger.info("Sent startup confirmation message to your own WhatsApp inbox")
        except Exception:
            logger.exception("Failed to send startup notification to your own inbox")

    def _send_startup_photo(self, own_jid, image_path: str, caption: str) -> bool:
        """Sends `image_path` with `caption` as the startup notification.

        neonize's exact API for image messages could not be confirmed
        against library source in this environment (no network access here
        to install/inspect neonize directly). This tries, in order: a
        higher-level send_image() convenience method some neonize builds
        expose, then the build_X()+send_message() pattern this codebase has
        already confirmed working for reactions (client.build_reaction() +
        client.send_message()). Returns False (never raises) if neither
        shape works, so the caller can fall back to plain text instead of
        the whole notification silently disappearing. Check the WARNING/
        DEBUG logs after your first deploy to see which path was used, or
        whether both failed — that tells us exactly what to adjust here.
        """
        if not os.path.exists(image_path):
            logger.warning(
                "Startup notification image not found at %s — skipping the "
                "photo and using text only", image_path,
            )
            return False
        try:
            with open(image_path, "rb") as fh:
                image_bytes = fh.read()
        except Exception:
            logger.exception("Couldn't read startup notification image at %s", image_path)
            return False

        client = self.client

        send_image = getattr(client, "send_image", None)
        if callable(send_image):
            try:
                send_image(own_jid, image_bytes, caption=caption)
                return True
            except Exception:
                logger.debug(
                    "client.send_image(...) didn't work, trying "
                    "build_image_message(...) instead", exc_info=True,
                )

        build_image_message = getattr(client, "build_image_message", None)
        if callable(build_image_message):
            try:
                image_message = build_image_message(image_bytes, caption=caption)
                client.send_message(own_jid, image_message)
                return True
            except Exception:
                logger.debug("client.build_image_message(...) didn't work either", exc_info=True)

        logger.warning(
            "Neither send_image() nor build_image_message() worked on this "
            "neonize build (or neither method exists on it) — couldn't "
            "confirm the right call without network access to inspect the "
            "library in this environment."
        )
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _run_connect_loop(self) -> None:
        """Runs the actual client.connect() call.

        Verified directly against the installed neonize 0.4.3.post0
        source: connect() (connect_with_proxy()) is a genuinely blocking
        call for the *entire* session — it only returns once the shared
        `event` singleton is set (shutdown()/a scheduled restart) or the
        underlying Go client ends on its own (e.g. a fatal disconnect).
        It has to run on its own thread, otherwise nothing below it in
        run() — requesting a pairing code, then waiting for a shutdown
        signal — would ever execute during normal operation.
        """
        try:
            self.client.connect()
        except Exception:
            logger.exception("WhatsApp connection ended with an error")
        finally:
            # If the Go side stopped on its own (not via shutdown()), make
            # sure event.wait() below can't block forever waiting for a
            # signal that was never coming.
            event.set()

    def run(self, restart_after_seconds: float | None = None) -> None:
        # event is a module-level singleton reused by neonize; clear it up
        # front so a stale "set" state can't linger from a previous call.
        event.clear()

        already_paired = self.client.is_logged_in

        if not already_paired and not self.config.PHONE_NUMBER:
            if sys.stdin.isatty():
                self.config.PHONE_NUMBER = _prompt_for_phone_number()
            else:
                raise SystemExit(
                    "No existing session and PHONE_NUMBER is not set. In a "
                    "non-interactive environment (e.g. Railway), set the "
                    "PHONE_NUMBER environment variable before starting the app."
                )

        logger.info("Connecting to WhatsApp servers...")
        threading.Thread(target=self._run_connect_loop, daemon=True).start()

        if not already_paired:
            time.sleep(2)
            try:
                code = self.client.PairPhone(self.config.PHONE_NUMBER, True)
            except Exception:
                logger.exception("Failed to request a pairing code")
                raise
            health_server.state.update(pairing_code=code)
            logger.info("=" * 64)
            logger.info("WHATSAPP PAIRING CODE: %s", code)
            logger.info(
                "On your phone: WhatsApp > Settings > Linked Devices > "
                "Link a Device > 'Link with phone number instead' > enter this code"
            )
            logger.info("=" * 64)
        else:
            logger.info("Existing session found — resuming without a new pairing code")
            logger.info("✅ Bot connected successfully — now watching for statuses.")

        if restart_after_seconds:
            finished_cleanly = event.wait(timeout=restart_after_seconds)
            if finished_cleanly:
                return  # a real shutdown() happened — nothing more to do
            logger.info(
                "Scheduled restart interval (%.1fh) reached — restarting the "
                "process for a clean session. Pairing will be skipped since "
                "the session is already saved.",
                restart_after_seconds / 3600,
            )
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("Error while disconnecting for scheduled restart")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            event.wait()  # blocks until shutdown() calls event.set()

    def shutdown(self) -> None:
        logger.info("Shutting down WhatsApp client...")
        try:
            self.client.disconnect()
        except Exception:
            logger.exception("Error while disconnecting")
        event.set()
