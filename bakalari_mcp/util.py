"""Small helpers shared by the client and the formatters."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAYS_CS = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]

_TODAY = {"today", "dnes", "dneska", "now"}
_TOMORROW = {"tomorrow", "zitra"}
_YESTERDAY = {"yesterday", "vcera"}


def fold(text: Any) -> str:
    """Lower-case and strip diacritics so 'Čeština' matches 'cestina'."""
    if text is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def pick(data: Any, *keys: str, default: Any = None) -> Any:
    """First non-None value among `keys` of a dict (the API is not consistent)."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return default


def drop_empty(data: dict) -> dict:
    """Remove None / empty-string / empty-container values (keeps 0 and False)."""
    return {k: v for k, v in data.items() if v is not None and v != "" and v != [] and v != {}}


def clip(text: Any, limit: int) -> tuple[str, bool]:
    """Truncate to `limit` characters. Returns (text, was_truncated)."""
    text = "" if text is None else str(text).strip()
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[: limit - 1].rstrip() + "…", True


def get_tz(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except Exception:  # unknown zone or no tzdata installed
        return None


def today(tz_name: str = "Europe/Prague") -> date:
    return datetime.now(get_tz(tz_name)).date()


def parse_date(value: str | None, tz_name: str = "Europe/Prague") -> date:
    """Accepts YYYY-MM-DD, D.M.YYYY, D.M., today/tomorrow/yesterday (also Czech)."""
    now = today(tz_name)
    if value is None or not str(value).strip():
        return now
    text = fold(str(value).strip())
    if text in _TODAY:
        return now
    if text in _TOMORROW:
        return now + timedelta(days=1)
    if text in _YESTERDAY:
        return now - timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})?", text)
        if not m:
            raise ValueError(
                f"Cannot understand date {value!r}. Use YYYY-MM-DD, D.M.YYYY, "
                "'today', 'tomorrow' or 'yesterday'."
            )
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else now.year
    try:
        return date(y, mo, d)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a valid calendar date.") from exc


def parse_date_opt(value: str | None, tz_name: str = "Europe/Prague") -> date | None:
    return parse_date(value, tz_name) if value and str(value).strip() else None


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_weekday(day: date) -> date:
    """The next Mon-Fri strictly after `day`."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def iso_to_date(value: Any) -> date | None:
    """'2025-01-08T00:00:00+01:00' -> date(2025, 1, 8). None when unparsable."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def short_dt(value: Any) -> str | None:
    """'2025-01-08T09:55:00+01:00' -> '2025-01-08 09:55' (date only if no time part)."""
    if not value:
        return None
    text = str(value)
    if len(text) < 16 or text[10] not in "T ":
        return text[:10]
    if text[11:16] == "00:00":  # a bare date (deadlines, birthdays...) is stored as midnight
        return text[:10]
    return f"{text[:10]} {text[11:16]}"
