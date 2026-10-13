"""Tiny HTTP service that receives the school cookie from the browser extension.

POST /cookie   {"cookie": "a=1; b=2"}   Authorization: Bearer <token>   -> stores it
GET  /health                                                             -> {"ok": true, "has_cookie": ..., "age_seconds": ...}

Only the standard library is used, so it can run next to the MCP server without extra dependencies.
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from . import cookiestore
from .config import ConfigError, normalize_cookie

log = logging.getLogger("bakalari_mcp.receiver")
MAX_BODY = 16 * 1024


def session_minutes_left(school_url: str, cookie: str) -> float | None:
    """Ask the school how long this session still lives (/sessioninfo). Best effort."""
    try:
        r = httpx.get(f"{school_url}/sessioninfo", headers={"Cookie": cookie}, timeout=5)
        seconds = float(r.json()["data"]["remainingTime"])
        return round(seconds / 60, 1)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def make_handler(token: str, store: Path, school_url: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "bakalari-receiver"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")  # the bearer token is the protection
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            sent = self.headers.get("Authorization", "")
            return sent.startswith("Bearer ") and hmac.compare_digest(sent[7:].encode(), token.encode())

        def log_message(self, fmt: str, *args) -> None:  # never log bodies/headers
            log.info("%s %s", self.address_string(), fmt % args)

        def do_OPTIONS(self) -> None:  # permissive preflight; the bearer token is the protection
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_GET(self) -> None:
            if self.path != "/health":
                return self._send(404, {"ok": False, "error": "not found"})
            stored = cookiestore.load(store)
            self._send(200, {"ok": True, "has_cookie": bool(stored),
                             "age_seconds": round(time.time() - stored[1]) if stored else None})

        def do_POST(self) -> None:
            if self.path != "/cookie":
                return self._send(404, {"ok": False, "error": "not found"})
            if not self._authorized():
                return self._send(401, {"ok": False, "error": "bad token"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"ok": False, "error": "bad length"})
            if length <= 0 or length > MAX_BODY:
                return self._send(413, {"ok": False, "error": "body too large or empty"})
            try:
                cookie = normalize_cookie(str(json.loads(self.rfile.read(length))["cookie"]))
            except (ValueError, KeyError, TypeError, ConfigError):
                return self._send(400, {"ok": False, "error": "expected {\"cookie\": \"name=value; ...\"}"})
            cookiestore.save(store, cookie)
            left = session_minutes_left(school_url, cookie) if school_url else None
            log.info("cookie stored (%d cookies, school says %s min left)", cookie.count(";") + 1, left)
            self._send(200, {"ok": True, "cookies": cookie.count(";") + 1, "remaining_minutes": left})

    return Handler


def make_server(host: str, port: int, token: str, store: Path, school_url: str | None) -> ThreadingHTTPServer:
    if len(token) < 16:
        raise ConfigError("BAKALARI_RECEIVER_TOKEN must be at least 16 characters.")
    return ThreadingHTTPServer((host, port), make_handler(token, store, school_url))


def serve(host: str, port: int, token: str, store: Path, school_url: str | None) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(message)s")
    httpd = make_server(host, port, token, store, school_url)
    log.info("receiver listening on %s:%d, cookie file %s", host, httpd.server_address[1], store)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
