"""
Entrypoint. Run with:  python main.py

On a local machine, this installs its own dependencies from
requirements.txt on first run (so a separate `pip install` step isn't
something you have to remember) and then restarts itself once they're
available.

On Railway (or any platform that already installs requirements.txt
during its build step — Docker, Nixpacks, etc.), this self-install is
skipped entirely. Attempting a runtime `pip install` on such platforms
can fail outright (e.g. no writable/importable pip in the runtime
image) even though it isn't needed, since the dependencies are already
present from the build.
"""
from __future__ import annotations

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _running_in_managed_container() -> bool:
    """True when deployed on a platform that already installs
    requirements.txt during its own build step, so a runtime pip install
    would be redundant and potentially unsafe to attempt."""
    if any(key.startswith("RAILWAY_") for key in os.environ):
        return True
    return False


def _ensure_dependencies() -> None:
    """Install requirements.txt automatically if anything is missing, then
    re-exec this same script so the newly installed packages are importable.
    No-op on managed platforms like Railway — see module docstring."""
    if _running_in_managed_container():
        return

    missing = False
    for module_name in ("neonize", "dotenv"):
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

# Everything below this line depends on packages that _ensure_dependencies()
# just guaranteed are installed, so these imports are safe here.
import logging  # noqa: E402
import signal  # noqa: E402

from app.config import Config  # noqa: E402
from app.health_server import start_health_server  # noqa: E402
from app.status_bot import StatusBot  # noqa: E402


def _configure_logging() -> None:
    # Console-only, for the current run. Activity (statuses viewed/liked,
    # sender numbers, message ids, etc.) is intentionally never written to
    # disk — the only thing this app persists between runs is the paired
    # WhatsApp session file itself (SESSION_DB_PATH), which holds login
    # credentials/keys, not an activity history.
    level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    # force=True: importing neonize attaches its own default StreamHandler
    # to the root logger as a side effect, which would otherwise make
    # basicConfig() a silent no-op here.
    logging.basicConfig(level=level, format=fmt, handlers=[logging.StreamHandler()], force=True)


def main() -> None:
    Config.validate()
    _configure_logging()
    logger = logging.getLogger("main")

    start_health_server(Config.PORT)
    logger.info("Health check server listening on port %s", Config.PORT)

    bot = StatusBot(Config)

    def _handle_signal(signum, _frame) -> None:
        logger.info("Received signal %s, shutting down", signum)
        bot.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    restart_after_seconds = Config.AUTO_RESTART_HOURS * 3600 if Config.AUTO_RESTART_HOURS > 0 else None
    bot.run(restart_after_seconds=restart_after_seconds)


if __name__ == "__main__":
    main()
