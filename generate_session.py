"""
Standalone WhatsApp session generator — QR-code login, run from a terminal.

This is an ALTERNATIVE to the phone-number pairing-code flow the main app
(main.py / app/status_bot.py) uses by default. Run this script once,
scan the QR code it prints with your phone, and it saves a paired
session file to disk. It does nothing else — no status watching, no
viewing, no liking, no message sending. Its only job is to open a
WhatsApp Web-style QR login and persist the resulting session.

neonize/whatsmeow don't care which method produced a session file — a
QR-paired session and a pairing-code-paired session are interchangeable.
So you only ever need to run ONE of the two flows, and afterward:

    python main.py

will find the same session file and skip pairing entirely.

Usage:
    python generate_session.py
    python generate_session.py --session-path /data/session.db3

By default this reads SESSION_DB_PATH the same way app/config.py does
(env var, then .env file, then the project's data/session.db3), so a
session generated here is picked up by the main app with zero extra
configuration.

Like the rest of this app, this script writes ONLY the session/login
data to disk (via neonize's own sqlite store at --session-path). It does
not create or write to any activity/log file.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))


def _running_in_managed_container() -> bool:
    return any(key.startswith("RAILWAY_") for key in os.environ)


def _ensure_dependencies() -> None:
    """Same self-install behavior as main.py, so this script works
    standalone even before `pip install -r requirements.txt` has been run."""
    if _running_in_managed_container():
        return

    missing = False
    for module_name in ("neonize", "dotenv", "segno"):
        try:
            __import__(module_name)
        except ImportError:
            missing = True
            break

    if not missing:
        return

    print("First run detected — installing required Python packages...")
    requirements_path = os.path.join(_HERE, "requirements.txt")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", requirements_path])
    except subprocess.CalledProcessError as exc:
        print(f"Failed to install dependencies automatically: {exc}")
        print(f"Try running manually: pip install -r {requirements_path}")
        sys.exit(1)

    print("Dependencies installed. Restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


_ensure_dependencies()

# Everything below depends on packages _ensure_dependencies() just
# guaranteed are installed, so these imports are safe here.
from neonize.client import NewClient  # noqa: E402
from neonize.events import ConnectedEv, LoggedOutEv, PairStatusEv, event  # noqa: E402

from app.config import Config  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a paired WhatsApp session file via QR-code login."
    )
    parser.add_argument(
        "--session-path",
        default=Config.SESSION_DB_PATH,
        help=(
            "Where to save the session file. Defaults to whatever "
            "SESSION_DB_PATH resolves to (same default main.py uses: "
            f"currently {Config.SESSION_DB_PATH})."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for the QR code to be scanned before giving up (default: 180).",
    )
    return parser.parse_args()


def _run_connect_loop(client: NewClient, error_holder: dict) -> None:
    """Runs the actual (session-long-blocking, see README) client.connect()
    call on its own thread — it only returns once the connection ends, so
    the main thread below has to wait on a separate signal instead of on
    this call returning."""
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001 - surfaced to the main thread below
        error_holder["exc"] = exc
    finally:
        event.set()


def main() -> None:
    args = _parse_args()
    session_path = os.path.abspath(args.session_path)
    os.makedirs(os.path.dirname(session_path) or ".", exist_ok=True)

    if os.path.exists(session_path):
        print(f"A session file already exists at: {session_path}")
        answer = input("Overwrite it with a fresh QR login? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Left the existing session file untouched. Nothing to do.")
            return
        # Remove the sqlite file itself plus any WAL/SHM companion files
        # sqlite may have left alongside it, so the new client starts from
        # a genuinely clean slate instead of reusing stale journal data.
        for suffix in ("", "-wal", "-shm"):
            candidate = session_path + suffix
            if os.path.exists(candidate):
                os.remove(candidate)

    client = NewClient(session_path)

    if client.is_logged_in:
        print(f"This session file is already paired and logged in: {session_path}")
        print("Nothing to do — you can run `python main.py` directly.")
        return

    done = threading.Event()
    outcome: dict = {"paired": False}
    error_holder: dict = {}

    @client.event(ConnectedEv)
    def on_connected(_client, _ev) -> None:
        outcome["paired"] = True
        done.set()

    @client.event(PairStatusEv)
    def on_pair_status(_client, ev) -> None:
        print(f"\nPaired successfully as {ev.ID.User}.")

    @client.event(LoggedOutEv)
    def on_logged_out(_client, _ev) -> None:
        print("\nThis device was logged out immediately after pairing — try again.")
        outcome["paired"] = False
        done.set()

    print("=" * 64)
    print("Scan this QR code with WhatsApp:")
    print("  WhatsApp app > Settings > Linked Devices > Link a Device")
    print("(it refreshes automatically every so often until scanned)")
    print("=" * 64)

    event.clear()
    threading.Thread(target=_run_connect_loop, args=(client, error_holder), daemon=True).start()

    finished = done.wait(timeout=args.timeout)

    try:
        client.disconnect()
    except Exception:
        pass
    event.set()  # release the connect-loop thread if it's still waiting

    if "exc" in error_holder:
        print(f"\nConnection error: {error_holder['exc']}")
        sys.exit(1)

    if not finished:
        print(f"\nTimed out after {args.timeout:.0f}s waiting for a scan. Run this script again to retry.")
        sys.exit(1)

    if outcome["paired"]:
        size = os.path.getsize(session_path) if os.path.exists(session_path) else 0
        print("=" * 64)
        print(f"Session saved to: {session_path} ({size} bytes)")
        print("Only login/session data is stored here — no activity is ever logged to it.")
        print("You can now run the bot with: python main.py")
        if session_path != os.path.abspath(Config.SESSION_DB_PATH):
            print(f"(set SESSION_DB_PATH={session_path} first, since that isn't the current default)")
        print("=" * 64)
    else:
        print("\nPairing didn't complete — try again.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
