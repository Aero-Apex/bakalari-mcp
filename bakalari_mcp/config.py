"""Configuration from environment variables, optionally backed by a .env file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_URL = "https://school.example.cz"
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class ConfigError(Exception):
    """Missing or invalid configuration."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal dotenv parser: KEY=value, quotes optional, '#' comments.

    Inside double quotes `\\\\` and `\\"` are unescaped; single quotes are literal.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, raw = line.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = re.sub(r'\\(["\\])', r"\1", raw[1:-1])
        elif len(raw) >= 2 and raw[0] == raw[-1] == "'":
            raw = raw[1:-1]
        values[key] = raw
    return values


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    # People often paste the login page, e.g. https://school.cz/bakaweb/login
    url = re.sub(r"/(api(/.*)?|login|next(/.*)?)$", "", url, flags=re.IGNORECASE)
    return url.rstrip("/")


AUTH_COOKIE_NAME = "BakaAuth"  # the login cookie of Bakaláři; used when only its value is pasted


def normalize_cookie(raw: str) -> str:
    """Turn whatever was copied from the browser into a `Cookie` header value.

    Accepted: the whole header ('Cookie: a=1; b=2'), 'a=1; b=2', a row copied from the
    Application -> Cookies table ('BakaAuth<TAB>value<TAB>...') or just the BakaAuth value.
    """
    text = raw.strip().strip("'\"")
    text = re.sub(r"^\s*(-H\s+['\"]?)?cookie\s*:\s*", "", text, flags=re.IGNORECASE)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if "	" in first_line and ";" not in first_line:  # row of the devtools cookie table
        cells = [c.strip() for c in first_line.split("	")]
        if len(cells) >= 2 and cells[0] and cells[1]:
            text = f"{cells[0]}={cells[1]}"
    text = " ".join(text.split()).strip("'\"; ")
    if not text:
        raise ConfigError("The cookie is empty.")
    name, eq, rest = text.partition("=")
    if not eq or not rest.strip("=") or len(name) > 40 or " " in name or ";" in name:
        text = f"{AUTH_COOKIE_NAME}={text}"  # a bare value: assume it is the login cookie
    bad = [i for i, c in enumerate(text) if ord(c) > 126 or ord(c) < 32]
    if bad:
        raise ConfigError(
            f"The cookie contains {len(bad)} unsupported character(s) (first at position {bad[0]}, total length "
            f"{len(text)}). Copy it again from the Network tab -> Request Headers -> Cookie."
        )
    return text


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc


@dataclass(frozen=True)
class Config:
    url: str = DEFAULT_URL
    username: str = ""
    password: str = ""
    cookie: str = ""
    cookie_file: Path | None = None  # cookie pushed by the browser extension (see receiver.py)
    cookie_max_age: float = 1200.0  # seconds; older than this the browser is considered gone
    timeout: float = 30.0
    cache_ttl: float = 120.0
    timezone: str = "Europe/Prague"

    @property
    def uses_cookie(self) -> bool:
        return bool(self.cookie or self.cookie_file)

    def __repr__(self) -> str:  # never leak secrets into logs
        who = "cookie=***" if self.cookie else "browser-cookie-file" if self.cookie_file else f"username={self.username!r}, password=***"
        return f"Config(url={self.url!r}, {who})"

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> "Config":
        """Real environment variables win over values from the .env file."""
        environ = os.environ if environ is None else environ
        if env_file is None:
            override = environ.get("BAKALARI_ENV_FILE")
            env_file = Path(override) if override else DEFAULT_ENV_FILE
        merged = {**parse_env_file(env_file), **{k: v for k, v in environ.items() if v != ""}}

        cookie = merged.get("BAKALARI_COOKIE", "")
        cookie_file = merged.get("BAKALARI_COOKIE_FILE", "")
        if not cookie and not cookie_file and not (merged.get("BAKALARI_USERNAME") and merged.get("BAKALARI_PASSWORD")):
            raise ConfigError(
                "Not configured: set BAKALARI_COOKIE_FILE (browser extension), BAKALARI_COOKIE or "
                "BAKALARI_USERNAME + BAKALARI_PASSWORD, "
                f"as environment variables or in {env_file} (run deploy.sh to be asked for them)."
            )
        return cls(
            url=normalize_url(merged.get("BAKALARI_URL") or DEFAULT_URL),
            username=merged.get("BAKALARI_USERNAME", ""),
            password=merged.get("BAKALARI_PASSWORD", ""),
            cookie=normalize_cookie(cookie) if cookie else "",
            cookie_file=Path(cookie_file) if cookie_file else None,
            cookie_max_age=_number(merged, "BAKALARI_COOKIE_MAX_AGE", 1200.0),
            timeout=_number(merged, "BAKALARI_TIMEOUT", 30.0),
            cache_ttl=_number(merged, "BAKALARI_CACHE_TTL", 120.0),
            timezone=merged.get("BAKALARI_TZ") or "Europe/Prague",
        )
