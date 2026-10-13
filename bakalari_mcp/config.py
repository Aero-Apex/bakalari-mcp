"""Configuration: environment variables, optionally backed by a settings file (see paths.py for where that is)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .util import get_tz

DEFAULT_URL = "https://school.example.cz"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
RECEIVER_TOKEN_MIN = 16
HTTP_TOKEN_MIN = 24
KEEPER_INTERVAL_MIN = 30.0

# Every setting the program reads: (name, default shown in docs, description, is a secret).
SETTINGS: tuple[tuple[str, str, str, bool], ...] = (
    ("BAKALARI_URL", DEFAULT_URL, "School website address, e.g. https://school.example.cz", False),
    ("BAKALARI_COOKIE_FILE", "<data folder>/session.json", "Session cookie file (kept alive by the keeper or the browser extension)", False),
    ("BAKALARI_COOKIE", "", "A cookie copied from the browser; it expires with the website session", True),
    ("BAKALARI_USERNAME", "", "Login without a cookie (REST API)", False),
    ("BAKALARI_PASSWORD", "", "Password for the username login", True),
    ("BAKALARI_TZ", "Europe/Prague", "Time zone used for \"today\"", False),
    ("BAKALARI_TIMEOUT", "30", "HTTP timeout in seconds", False),
    ("BAKALARI_CACHE_TTL", "120", "Seconds to cache responses (0 turns the cache off)", False),
    ("BAKALARI_COOKIE_MAX_AGE", "1200", "Seconds without a renewed cookie before the tools report the session as ended", False),
    ("BAKALARI_KEEPER_INTERVAL", "180", "Seconds between the keeper's renewals (at least 30)", False),
    ("BAKALARI_RECEIVER_HOST", "127.0.0.1", "Address the cookie receiver listens on (browser extension)", False),
    ("BAKALARI_RECEIVER_PORT", "8765", "Port of the cookie receiver", False),
    ("BAKALARI_RECEIVER_TOKEN", "", f"Secret the browser extension sends to the receiver (at least {RECEIVER_TOKEN_MIN} characters)", True),
    ("BAKALARI_HTTP_HOST", "127.0.0.1", "Address the HTTP service listens on", False),
    ("BAKALARI_HTTP_PORT", "8770", "Port of the HTTP service (point the tunnel here)", False),
    ("BAKALARI_HTTP_HOSTS", "", "Public host names of the tunnel, comma-separated", False),
    ("BAKALARI_HTTP_TOKEN", "", f"Secret path of the HTTP service (at least {HTTP_TOKEN_MIN} characters)", True),
    ("BAKALARI_DATA_DIR", "<config folder>", "Folder for the session cookie and logs", False),
    ("BAKALARI_LOG_LEVEL", "WARNING", "Log level: DEBUG, INFO, WARNING or ERROR", False),
)
SECRET_SETTINGS = frozenset(name for name, _, _, secret in SETTINGS if secret)
ENV_ONLY = {
    "BAKALARI_CONFIG": "use this settings file instead of the default one",
    "BAKALARI_ENV_FILE": "older name of BAKALARI_CONFIG",
}


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


def _quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ConfigError("A setting cannot contain a line break.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_settings(path: Path, updates: Mapping[str, str | None]) -> Path:
    """Merge `updates` into the settings file (None removes a setting) and write it atomically, mode 600 where
    the system supports it. Settings not mentioned in `updates` are kept."""
    path = Path(path).expanduser()
    values = parse_env_file(path)
    for key, value in updates.items():
        if value is None or value == "":
            values.pop(key, None)
        else:
            values[key] = str(value)
    known = [name for name, *_ in SETTINGS if name in values]
    rest = sorted(k for k in values if k not in known)
    lines = ["# bakalari-mcp settings. `bakalari-mcp setup` writes this file; you can also edit it by hand."]
    lines += [f"{key}={_quote(values[key])}" for key in known + rest]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # no effect on Windows, where the folder is already private to the user
    except OSError:
        pass
    os.replace(tmp, path)
    return path


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
    if "\t" in first_line and ";" not in first_line:  # row of the devtools cookie table
        cells = [c.strip() for c in first_line.split("\t")]
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


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> dict[str, str]:
    """The settings file merged with BAKALARI_* environment variables (environment wins; empty values are ignored)."""
    from .paths import config_file_in_use

    environ = os.environ if environ is None else environ
    path = Path(env_file) if env_file is not None else config_file_in_use(environ)
    from_env = {k: v for k, v in environ.items() if k.startswith("BAKALARI_") and v != ""}
    return {**parse_env_file(path), **from_env}


@dataclass(frozen=True)
class Config:
    url: str = DEFAULT_URL
    username: str = ""
    password: str = ""
    cookie: str = ""
    cookie_file: Path | None = None  # cookie pushed by the browser extension or kept by the keeper (see receiver.py)
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
        """Real environment variables win over values from the settings file."""
        from .paths import config_file_in_use

        settings = load_settings(environ, env_file)
        cookie = settings.get("BAKALARI_COOKIE", "")
        cookie_file = settings.get("BAKALARI_COOKIE_FILE", "")
        if not cookie and not cookie_file and not (settings.get("BAKALARI_USERNAME") and settings.get("BAKALARI_PASSWORD")):
            where = env_file or config_file_in_use(os.environ if environ is None else environ)
            raise ConfigError(
                "Not configured: set BAKALARI_COOKIE_FILE (cookie kept alive by the keeper or the browser extension), "
                "BAKALARI_COOKIE or BAKALARI_USERNAME + BAKALARI_PASSWORD, as environment variables or in "
                f"{where}. Run `bakalari-mcp setup` to be asked for them."
            )
        return cls(
            url=normalize_url(settings.get("BAKALARI_URL") or DEFAULT_URL),
            username=settings.get("BAKALARI_USERNAME", ""),
            password=settings.get("BAKALARI_PASSWORD", ""),
            cookie=normalize_cookie(cookie) if cookie else "",
            cookie_file=Path(cookie_file).expanduser() if cookie_file else None,
            cookie_max_age=_number(settings, "BAKALARI_COOKIE_MAX_AGE", 1200.0),
            timeout=_number(settings, "BAKALARI_TIMEOUT", 30.0),
            cache_ttl=_number(settings, "BAKALARI_CACHE_TTL", 120.0),
            timezone=settings.get("BAKALARI_TZ") or "Europe/Prague",
        )


def validate(settings: Mapping[str, str], cookie_file_exists: bool | None = None) -> tuple[list[str], list[str]]:
    """Problems that would stop the program (errors) and things that look wrong (warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    def number(name: str, default: float, low: float, high: float | None = None) -> None:
        try:
            value = _number(settings, name, default)
        except ConfigError as exc:
            errors.append(str(exc))
            return
        if value < low or (high is not None and value > high):
            errors.append(f"{name} must be between {low:g} and {high:g}." if high is not None else f"{name} must be at least {low:g}.")

    url = normalize_url(settings.get("BAKALARI_URL") or DEFAULT_URL)
    if not re.match(r"^https?://[^/]+\.[^/]+", url):
        errors.append(f"BAKALARI_URL does not look like a web address: {url!r}.")
    if get_tz(settings.get("BAKALARI_TZ") or "Europe/Prague") is None:
        errors.append(f"BAKALARI_TZ is not a known time zone: {settings.get('BAKALARI_TZ')!r} (try Europe/Prague).")
    number("BAKALARI_TIMEOUT", 30.0, 1.0)
    number("BAKALARI_CACHE_TTL", 120.0, 0.0)
    number("BAKALARI_COOKIE_MAX_AGE", 1200.0, 1.0)
    number("BAKALARI_KEEPER_INTERVAL", 180.0, KEEPER_INTERVAL_MIN)
    number("BAKALARI_RECEIVER_PORT", 8765, 1, 65535)
    number("BAKALARI_HTTP_PORT", 8770, 1, 65535)
    level = (settings.get("BAKALARI_LOG_LEVEL") or "WARNING").upper()
    if level not in LOG_LEVELS:
        errors.append(f"BAKALARI_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}.")

    cookie = settings.get("BAKALARI_COOKIE", "")
    cookie_file = settings.get("BAKALARI_COOKIE_FILE", "")
    has_password = bool(settings.get("BAKALARI_USERNAME") and settings.get("BAKALARI_PASSWORD"))
    if cookie:
        try:
            normalize_cookie(cookie)
        except ConfigError as exc:
            errors.append(str(exc))
    if not cookie and not cookie_file and not has_password:
        errors.append("No login is configured. Run `bakalari-mcp setup`.")
    if (cookie or cookie_file) and has_password:
        warnings.append("A cookie and a username + password are both set; the cookie is used.")
    if cookie_file and not cookie and cookie_file_exists is False:
        warnings.append(f"The cookie file {cookie_file} does not exist yet (run `bakalari-mcp setup` or start the keeper).")
    token = settings.get("BAKALARI_RECEIVER_TOKEN", "")
    if token and len(token) < RECEIVER_TOKEN_MIN:
        errors.append(f"BAKALARI_RECEIVER_TOKEN must be at least {RECEIVER_TOKEN_MIN} characters.")
    http_token = settings.get("BAKALARI_HTTP_TOKEN", "")
    if http_token and len(http_token) < HTTP_TOKEN_MIN:
        errors.append(f"BAKALARI_HTTP_TOKEN must be at least {HTTP_TOKEN_MIN} characters.")
    if settings.get("BAKALARI_HTTP_HOSTS") and not http_token:
        warnings.append("BAKALARI_HTTP_HOSTS is set but BAKALARI_HTTP_TOKEN is not (run `bakalari-mcp http-setup`).")
    return errors, warnings
