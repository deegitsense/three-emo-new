"""
Minimal health-check HTTP server, built only on the standard library so it
adds zero extra dependencies. Railway (and any uptime checker) can hit this
to confirm the process is alive and to see basic live stats.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _StatusState:
    def __init__(self) -> None:
        self.connected: bool = False
        self.logged_in: bool = False
        self.pairing_code: str | None = None
        self.statuses_viewed: int = 0
        self.statuses_view_failed: int = 0
        self.statuses_liked: int = 0
        self.statuses_like_failed: int = 0
        self.last_view_error: str | None = None
        self.last_like_error: str | None = None
        # Count of a specific, confirmed-benign whatsmeow-internal log line
        # ("Failed to handle retry receipt ... couldn't find message") that
        # is filtered out of the console instead of flooding it — see
        # app/status_bot.py's _BenignWhatsmeowNoiseFilter and README.md's
        # "Reliability" section for how this was confirmed benign.
        self.whatsmeow_noise_suppressed: int = 0
        self._lock = threading.Lock()

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connected": self.connected,
                "logged_in": self.logged_in,
                "pairing_code": self.pairing_code,
                "statuses_viewed": self.statuses_viewed,
                "statuses_view_failed": self.statuses_view_failed,
                "statuses_liked": self.statuses_liked,
                "statuses_like_failed": self.statuses_like_failed,
                "last_view_error": self.last_view_error,
                "last_like_error": self.last_like_error,
                "whatsmeow_noise_suppressed": self.whatsmeow_noise_suppressed,
            }


# Single shared instance imported by the rest of the app.
state = _StatusState()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:  # noqa: A002 - silence default access logs
        return

    def do_GET(self) -> None:
        body = json.dumps(state.snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
