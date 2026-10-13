"""Keeps a pasted school cookie alive on the server.

Every few minutes it calls the school's own /sessionextend (what the website's "still there?" popup does),
stores the cookie the school sends back and notes how long the session still lives. If the session ever ends
it says so in the log and waits for a new cookie (`bakalari-mcp setup`, or `bakalari-mcp store-cookie`), which it picks up by itself.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from . import cookiestore
from .probe import _session_numbers


async def run_keeper(
    url: str,
    store: Path,
    interval: float,
    log: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None = None,
    iterations: int | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    host = urlparse(url).hostname or ""
    last_saved: float | None = None
    state = ""
    count = 0
    log(f"keeper started for {url}, renewing every {interval:.0f}s, cookie file {store}")
    async with httpx.AsyncClient(base_url=url, timeout=30, transport=transport,
                                 headers={"User-Agent": "bakalari-mcp/2.0 keeper"}) as http:
        while iterations is None or count < iterations:
            count += 1
            stored = cookiestore.load(store)
            if stored is None:
                if state != "empty":
                    log("no cookie yet - run `bakalari-mcp setup` and choose the cookie login")
                state = "empty"
                await sleep(interval)
                continue
            cookie, received = stored
            if received != last_saved:  # a person or the installer wrote a new cookie: start from it
                http.cookies = cookiestore.jar_from_header(cookie, host)
                last_saved = received
                log("using the cookie from the cookie file")
            try:
                before = _session_numbers(await http.get("/sessioninfo"))
                if not before.get("remainingTime"):
                    if state != "dead":
                        log("SESSION EXPIRED - the school no longer accepts this cookie. "
                            "Log in to the website again and paste a fresh cookie (bakalari-mcp setup).")
                    state = "dead"
                    await sleep(interval)
                    continue
                extend = await http.get("/sessionextend")
                after = _session_numbers(await http.get("/sessioninfo"))
            except httpx.HTTPError as exc:
                log(f"network problem ({type(exc).__name__}), will retry")
                await sleep(interval)
                continue
            now = time.time()
            cookiestore.save(store, cookiestore.header_from_jar(http.cookies), now=now)
            last_saved = now
            left = after.get("remainingTime")
            log(f"renewed: HTTP {extend.status_code}, session had {before.get('remainingTime', 0) / 60:.1f} min, "
                f"now {(left or 0) / 60:.1f} min of {after.get('sessionDuration', '?')} min")
            state = "alive"
            await sleep(interval)
