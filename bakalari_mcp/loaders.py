"""Cookie-mode data loading: fetches from the website's own endpoints and returns data in the shapes the
formatters expect. (With username + password the REST API is used instead; see server.py.)"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import webdata as wd
from .client import BakalariClient
from .util import fold, get_tz, monday_of, today
from .web import MARKS_PAGE, parse_marks_page


def _iso_midnight_utc(day: date) -> str:
    """The format the website's own scripts send for a date (a JavaScript Date serialised as JSON)."""
    return f"{day.isoformat()}T00:00:00.000Z"


async def marks(c: BakalariClient) -> dict:
    data = parse_marks_page(await c.get_html(MARKS_PAGE))
    # The marks page has full names only; add abbreviations so `subject="AJ"` works like with the REST API.
    by_name = {fold(name): abbrev for abbrev, name in (await subject_names(c)).items()}
    for entry in data["Subjects"]:
        entry["Subject"]["Abbrev"] = by_name.get(fold(entry["Subject"]["Name"]), "")
    return data


async def timetable_week(c: BakalariClient, day: date) -> dict:
    """The actual timetable of the week containing `day` (the site always returns Monday-Friday)."""
    body = {"term": "cal", "date": _iso_midnight_utc(monday_of(day)), "selectedClassId": ""}
    return await c.web_json("POST", wd.TIMETABLE_URL, json_body=body)


async def timetable_permanent(c: BakalariClient) -> dict:
    body = {"term": "perm", "date": _iso_midnight_utc(today(c.config.timezone)), "selectedClassId": ""}
    return await c.web_json("POST", wd.TIMETABLE_URL, json_body=body)


async def subject_names(c: BakalariClient) -> dict[str, str]:
    """Abbreviation -> full subject name (homework only carries the abbreviation); {} if unavailable."""
    try:
        data = await timetable_permanent(c)
    except Exception:  # noqa: BLE001 - names are a nicety, never a reason to fail
        return {}
    names: dict[str, str] = {}
    for d in data.get("Days") or []:
        for slot in d.get("Hours") or []:
            for atom in slot.get("Atoms") or []:
                if atom.get("SubjectAbbrev") and atom.get("SubjectText"):
                    names[atom["SubjectAbbrev"].strip()] = atom["SubjectText"].strip()
    return names


async def homeworks(c: BakalariClient, include_done: bool, raw: bool = False) -> dict:
    states = [wd.HOMEWORK_STATES["actual"], wd.HOMEWORK_STATES["after_deadline"]]
    if include_done:
        states.append(wd.HOMEWORK_STATES["closed"])
    rows: list[dict] = []
    for state in states:
        body = {"homeworkState": state, "onlyUnfinishedHomeworks": False}
        rows += await c.web_json("POST", wd.HOMEWORK_URL, json_body=body) or []
    if raw:
        return {"Homeworks": rows}  # the website's own rows
    return wd.homeworks_to_api_shape(rows, await subject_names(c))


async def subjects(c: BakalariClient) -> dict:
    return wd.subjects_to_api_shape(await c.web_json("POST", wd.SUBJECTS_URL, json_body={}) or [])


async def absence(c: BakalariClient, today: date, tz_name: str, raw: bool = False) -> dict:
    tz = get_tz(tz_name) or ZoneInfo("UTC")
    start = wd.school_year_start(today, tz_name)
    params = {"dateFrom": start.isoformat(), "dateTo": datetime(today.year, today.month, today.day, tzinfo=tz).isoformat()}
    overview = await c.web_json("GET", wd.ABSENCE_OVERVIEW_URL, params=params) or []
    by_subject = await c.web_json(
        "GET", wd.ABSENCE_SUBJECTS_URL,
        params={**params, "lateChecked": "false", "schoolChecked": "false", "soonChecked": "false"},
    ) or []
    if raw:
        return {"overview": overview, "bySubject": by_subject}  # the website's own rows
    return wd.fmt_absence_web(overview, by_subject, start.date(), today)


async def events(c: BakalariClient, start: date, end: date, raw: bool = False) -> dict:
    """Events from the dashboard calendar widget, one month at a time."""
    found: dict[tuple[str, str], dict] = {}
    month = date(start.year, start.month, 1)
    for _ in range(13):
        if month > end:
            break
        is_first_month = month == date(start.year, start.month, 1)
        path = wd.EVENTS_WIDGET if is_first_month else f"{wd.EVENTS_WIDGET}?date={_iso_midnight_utc(month)}"
        for ev in wd.parse_events_widget(await c.get_html(path)):
            found[(ev["title"], ev["start"])] = ev
        month = (month + timedelta(days=32)).replace(day=1)
    chosen = [e for e in found.values()
              if date.fromisoformat(e.get("end") or e["start"]) >= start and date.fromisoformat(e["start"]) <= end]
    if not raw:  # raw keeps every calendar row as the widget lists it, a multi-day event can be several rows
        chosen = wd.merge_event_runs(chosen)
    chosen.sort(key=lambda e: (e["start"], e["title"]))
    return {"count": len(chosen), "events": chosen}


async def user(c: BakalariClient) -> dict:
    return wd.parse_user(await c.get_html(wd.DASHBOARD))
