"""
Central configuration for the WhatsApp status bot.
Everything is controlled via environment variables so the same code
runs unchanged locally and on Railway.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# Automatically load variables from a .env file in the working directory,
# if one exists. This makes `.env` work the same way on Windows
# (PowerShell/cmd), macOS, and Linux, without needing shell-specific
# export syntax. Real environment variables (e.g. ones Railway injects)
# always take priority and are never overwritten by the .env file.
load_dotenv()


def _bool(env_name: str, default: bool) -> bool:
    val = os.getenv(env_name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(env_name: str, default: float) -> float:
    val = os.getenv(env_name)
    if val is None or not val.strip():
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _int(env_name: str, default: int) -> int:
    val = os.getenv(env_name)
    if val is None or not val.strip():
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_session_path() -> str:
    # Resolve relative to the project root (parent of this app/ folder),
    # not the current working directory — so the session file is found
    # in the same place no matter where `python main.py` is launched from.
    return os.path.join(_project_root(), "data", "session.db3")


def _default_startup_image_path() -> str:
    return os.path.join(_project_root(), "assets", "ex-mini.jpg")


class Config:
    # Phone number to pair, digits only with country code, no "+" and no spaces.
    # Example: 15551234567 for a US number.
    PHONE_NUMBER: str = os.getenv("PHONE_NUMBER", "").strip().lstrip("+")

    # Where the WhatsApp session (sqlite) is stored. Defaults to a stable
    # folder next to this project, so restarting the app locally reuses the
    # same session instead of asking you to re-pair every time. On Railway,
    # set this to a path inside an attached Volume (e.g. /data/session.db3)
    # so it survives redeploys too.
    SESSION_DB_PATH: str = os.getenv("SESSION_DB_PATH", _default_session_path())

    # Feature toggles.
    VIEW_STATUSES: bool = _bool("VIEW_STATUSES", True)
    LIKE_STATUSES: bool = _bool("LIKE_STATUSES", True)

    # Emojis used to react to ("like") statuses. Each new status gets the
    # NEXT emoji in this list (in order), wrapping back to the start once
    # the end is reached — so a run of statuses cycles 💜, then 🤍, then 🌸,
    # then back to 💜, and so on. Defaults to the sequence from the bottom
    # of README.md. Override with a comma-separated list, e.g.
    # REACTION_EMOJIS=❤️,😂,🔥
    REACTION_EMOJIS: list[str] = [
        e.strip()
        for e in os.getenv("REACTION_EMOJIS", "💜,🤍,🌸").split(",")
        if e.strip()
    ]

    # Randomized delay (seconds) before reacting, to avoid firing reactions
    # instantly/mechanically for every single status.
    MIN_REACT_DELAY_SECONDS: float = _float("MIN_REACT_DELAY_SECONDS", 1.0)
    MAX_REACT_DELAY_SECONDS: float = _float("MAX_REACT_DELAY_SECONDS", 2.0)

    # View/react calls to status@broadcast can be rejected transiently by
    # WhatsApp's servers (whatsmeow issue #668 — "participant list hash
    # mismatch"), more often for statuses with a large/fast-changing
    # audience (common for WhatsApp Business accounts). Each attempt is
    # retried this many times with exponential backoff before being logged
    # as a real failure. See app/status_bot.py's module docstring.
    RECEIPT_RETRY_ATTEMPTS: int = _int("RECEIPT_RETRY_ATTEMPTS", 3)
    REACTION_RETRY_ATTEMPTS: int = _int("REACTION_RETRY_ATTEMPTS", 3)
    RETRY_BASE_DELAY_SECONDS: float = _float("RETRY_BASE_DELAY_SECONDS", 1.5)
    RETRY_MAX_DELAY_SECONDS: float = _float("RETRY_MAX_DELAY_SECONDS", 12.0)

    # Optional whitelist: comma-separated phone numbers (digits only, no "+").
    # If set, only statuses from these numbers are viewed/liked.
    # If empty, every status the account can see is processed.
    ALLOWED_STATUS_SENDERS: list[str] = [
        s.strip().lstrip("+")
        for s in os.getenv("ALLOWED_STATUS_SENDERS", "").split(",")
        if s.strip()
    ]

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Activity (statuses viewed/liked, senders, message ids, etc.) is only
    # ever printed to the console for the current run — it is never written
    # to disk. The ONLY thing this app persists between runs is the paired
    # WhatsApp session itself, at SESSION_DB_PATH. That file holds login
    # credentials/keys for the linked device, not a history of activity.

    # Periodically restart the whole process for a clean session (fresh
    # memory, fresh connection) — skips pairing since the session file is
    # already saved. Defaults to once every 24 hours. 0 disables this
    # entirely.
    AUTO_RESTART_HOURS: float = _float("AUTO_RESTART_HOURS", 24.0)

    # When the bot successfully connects, send a confirmation message to
    # the connected account's own WhatsApp inbox ("Message Yourself"). If
    # STARTUP_NOTIFICATION_IMAGE_PATH points at a real file, it's sent as a
    # photo with STARTUP_NOTIFICATION_MESSAGE as the caption; set it to an
    # empty string to send plain text only, same as before.
    NOTIFY_ON_STARTUP: bool = _bool("NOTIFY_ON_STARTUP", True)
    STARTUP_NOTIFICATION_MESSAGE: str = os.getenv(
        "STARTUP_NOTIFICATION_MESSAGE", "> BOT CONNECTED 🟢"
    )
    STARTUP_NOTIFICATION_IMAGE_PATH: str = os.getenv(
        "STARTUP_NOTIFICATION_IMAGE_PATH", _default_startup_image_path()
    )

    # Caps combined view+react network actions to this many per rolling
    # 60s window (0 disables the cap). Added after a real deployment log
    # showed WhatsApp force-unlinking this device ("device removed") after
    # a sustained run of automated status reactions — see the "Reliability"
    # section of README.md. This is a heuristic mitigation, not a
    # guarantee: it can't be verified against WhatsApp's actual detection
    # logic, and using any automation here carries some inherent risk
    # under WhatsApp's terms regardless of pacing.
    MAX_ACTIONS_PER_MINUTE: int = _int("MAX_ACTIONS_PER_MINUTE", 20)

    # Railway injects PORT automatically for web-type services.
    PORT: int = int(os.getenv("PORT", "8080"))

    @classmethod
    def validate(cls) -> None:
        if cls.MIN_REACT_DELAY_SECONDS < 0 or cls.MAX_REACT_DELAY_SECONDS < cls.MIN_REACT_DELAY_SECONDS:
            raise SystemExit(
                "MIN_REACT_DELAY_SECONDS / MAX_REACT_DELAY_SECONDS are invalid "
                "(min must be >= 0 and <= max)"
            )
        if cls.AUTO_RESTART_HOURS < 0:
            raise SystemExit("AUTO_RESTART_HOURS must be >= 0")
        if cls.LIKE_STATUSES and not cls.REACTION_EMOJIS:
            raise SystemExit(
                "REACTION_EMOJIS is empty — set at least one emoji to react with "
                "(e.g. REACTION_EMOJIS=💜,🤍,🌸)"
            )
        if cls.RECEIPT_RETRY_ATTEMPTS < 1 or cls.REACTION_RETRY_ATTEMPTS < 1:
            raise SystemExit("RECEIPT_RETRY_ATTEMPTS / REACTION_RETRY_ATTEMPTS must be >= 1")
        if cls.RETRY_BASE_DELAY_SECONDS < 0 or cls.RETRY_MAX_DELAY_SECONDS < cls.RETRY_BASE_DELAY_SECONDS:
            raise SystemExit(
                "RETRY_BASE_DELAY_SECONDS / RETRY_MAX_DELAY_SECONDS are invalid "
                "(base must be >= 0 and <= max)"
            )
        if cls.MAX_ACTIONS_PER_MINUTE < 0:
            raise SystemExit("MAX_ACTIONS_PER_MINUTE must be >= 0 (0 disables the cap)")
