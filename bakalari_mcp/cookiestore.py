"""The browser's current school cookie, as pushed by the extension (one small JSON file)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def save(path: Path, cookie: str, now: float | None = None) -> None:
    """Atomically replace the file; it holds a login, so only the owner may read it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"cookie": cookie, "received_at": time.time() if now is None else now}, fh)
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # not supported on every platform
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path: Path) -> tuple[str, float] | None:
    """(cookie, received_at) or None when nothing valid has been stored yet."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cookie, received = str(data["cookie"]), float(data["received_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (cookie, received) if cookie else None


def jar_from_header(header: str, host: str):
    """A cookie jar seeded from 'a=1; b=2' (scoped to `host`, so server updates replace, not duplicate)."""
    import httpx

    jar = httpx.Cookies()
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            jar.set(name, value, domain=host, path="/")
    return jar


def header_from_jar(jar) -> str:
    """'a=1; b=2' from a jar, one entry per cookie name (the newest wins)."""
    values: dict[str, str] = {}
    for cookie in jar.jar:
        values[cookie.name] = cookie.value or ""
    return "; ".join(f"{k}={v}" for k, v in values.items())
