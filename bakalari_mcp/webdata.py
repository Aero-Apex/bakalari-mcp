"""Readers for the JSON/HTML endpoints the Bakaláři *website* itself uses (cookie mode).

Shapes were taken from the live site. Parsers are defensive: a missing key is skipped, never fatal.
Output is either the REST-style dictionary the existing formatters understand, or the same final
shape those formatters produce, so the MCP tools look identical for both ways of logging in.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

from .util import WEEKDAYS_CS, drop_empty, get_tz, monday_of

TIMETABLE_URL = "/Timetable/GetContentData"
HOMEWORK_URL = "/HomeWorks/Student/LoadHomeworks"
HOMEWORK_STATES = {"actual": 3, "after_deadline": 4, "closed": 2}
ABSENCE_OVERVIEW_URL = "/Absence/Continuous/GetAbsenceOverview"
ABSENCE_SUBJECTS_URL = "/Absence/Continuous/GetAbsenceBySubjects"
SUBJECTS_URL = "/Teaching/Subjects"
EVENTS_WIDGET = "/Dashboard/CalendarWidget"
DASHBOARD = "/Dashboard"

ABSENCE_TYPES = {
    1: "absence_unresolved",
    2: "excused",
    3: "unexcused",
    4: "late",
    5: "not_counted",
    6: "left_early",
    7: "distance_teaching",
}
ABSENCE_THRESHOLD_PERCENT = 20  # the website highlights subjects above this share of missed lessons


def _clean(text: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _hm(text: str | None) -> str:
    """'08:00:00' / '8:00' -> '8:00'."""
    m = re.match(r"\s*0?(\d{1,2}):(\d{2})", text or "")
    return f"{int(m.group(1))}:{m.group(2)}" if m else ""


def _label_date(label: str, ref: date) -> date | None:
    """'12.10.' -> a date, choosing the year nearest to `ref` (weeks can straddle New Year)."""
    m = re.match(r"\s*(\d{1,2})\.\s*(\d{1,2})\.", label or "")
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - ref).days) < abs((best - ref).days):
            best = cand
    return best


_CHANGE_KIND = {"S": "substitution", "N": "added", "M": "changed", "O": "removed"}


def fmt_timetable_web(data: dict, ref: date, day: date | None = None, permanent: bool = False) -> dict:
    """The website's timetable JSON -> the same output shape as `formatters.fmt_timetable`."""
    hours = [h for h in data.get("Hours") or [] if isinstance(h, dict)]
    number_by_begin = {_hm(h.get("BeginTime")): h.get("Caption") for h in hours}
    monday = monday_of(ref)
    days_out: list[dict] = []
    for index, d in enumerate(data.get("Days") or []):
        if not isinstance(d, dict):
            continue
        d_date = None if permanent else (_label_date(d.get("Date") or "", ref) or monday + timedelta(days=index))
        if day is not None and d_date != day:
            continue
        abbrev = d.get("DayAbbrev") or ""
        if d_date:
            weekday = WEEKDAYS_CS[d_date.weekday()]
        else:
            weekday = WEEKDAYS_CS[index] if index < 5 else abbrev
        lessons: list[dict] = []
        for slot in d.get("Hours") or []:
            for atom in slot.get("Atoms") or []:
                removed = atom.get("Type") == "removed"
                begin, end = _hm(slot.get("Begin") or atom.get("Begin")), _hm(slot.get("End") or atom.get("End"))
                # The lesson's printed number comes from its start time; the atom's own HourIndex is an
                # internal position (it is offset from the number shown on the website).
                number = number_by_begin.get(begin)
                if number is None and not removed:
                    number = atom.get("HourIndex")
                info = (atom.get("ChangeInfo") or "").strip()
                lesson: dict[str, Any] = {
                    "hour": str(number) if number is not None else None,
                    "time": f"{begin}-{end}" if begin and end else (atom.get("Time") or "").replace(" ", ""),
                }
                if removed:
                    m = re.search(r"\(([^,)]+),\s*([^)]+)\)", info)
                    lesson.update({
                        "abbrev": m.group(1) if m else None,
                        "teacher": m.group(2) if m else None,
                        "cancelled": True,
                        "change": {"type": "removed", "description": info},
                    })
                else:
                    absent = atom.get("StudentAbsence") not in (None, "NoAbsent")
                    lesson.update({
                        "subject": atom.get("SubjectText") or atom.get("SubjectName"),
                        "abbrev": atom.get("SubjectAbbrev"),
                        "teacher": atom.get("TeacherFullname") or atom.get("Teacher"),
                        "room": atom.get("Room"),
                        "group": atom.get("GroupsNames"),
                        "cycle": atom.get("CycleText") or None,
                        "theme": atom.get("Theme") or None,
                        "notice": atom.get("Notice") or None,
                        "homework": True if atom.get("HomeworksCount") else None,
                        "absent": (atom.get("AtomAbsenceText") or "absent") if absent else None,
                    })
                    if atom.get("HasChanged") or info:
                        kind = _CHANGE_KIND.get(atom.get("BadgeChar") or "", "changed")
                        lesson["change"] = drop_empty({"type": kind, "description": info})
                lessons.append(drop_empty(lesson))
        lessons.sort(key=lambda x: (int(x["hour"]) if str(x.get("hour", "")).isdigit() else 99, x.get("time", "")))
        entry: dict[str, Any] = {
            "date": d_date.isoformat() if d_date else None,
            "weekday": weekday,
            "day_type": "day off" if d.get("DayOff") else None,
            "description": d.get("DayOffName") or None,
            "changes": sum(1 for x in lessons if "change" in x) or None,
        }
        days_out.append({**drop_empty(entry), "lessons": lessons})
    result: dict[str, Any] = {"days": days_out}
    if day is not None and not days_out:
        result["note"] = f"No timetable entry for {day.isoformat()} (weekend or holiday?)."
    return result


def keep_only_day(data: dict, ref: date, day: date) -> dict:
    """The raw week response reduced to one day (the site only serves whole weeks)."""
    monday = monday_of(ref)
    days = [
        d for i, d in enumerate(data.get("Days") or [])
        if isinstance(d, dict) and (_label_date(d.get("Date") or "", ref) or monday + timedelta(days=i)) == day
    ]
    return {**{k: v for k, v in data.items() if k != "Raster"}, "Days": days}  # Raster = drawing grid only


def homeworks_to_api_shape(rows: list[dict], subject_names: dict[str, str] | None = None) -> dict:
    """LoadHomeworks rows -> the REST-style dict that `formatters.fmt_homeworks` understands."""
    names = subject_names or {}
    out = []
    for r in rows or []:
        abbrev = (r.get("SubjectAbbrev") or "").strip()
        attachments = [a for a in r.get("Attachments") or [] if isinstance(a, dict)]
        out.append({
            "Subject": {"Abbrev": abbrev, "Name": names.get(abbrev, abbrev)},
            "DateStart": r.get("AssignmentDate"),
            "DateEnd": r.get("DeadlineDate"),
            "Content": r.get("Text"),
            "Done": bool(r.get("Finished") or r.get("Closed")),
            "Attachments": [{"Name": a.get("Name") or a.get("FileName")} for a in attachments],
        })
    return {"Homeworks": out}


def subjects_to_api_shape(rows: list[dict]) -> dict:
    """Only names and school e-mail addresses: private phone numbers are deliberately left out."""
    return {
        "Subjects": [
            {
                "SubjectName": (r.get("SubjectName") or "").strip(),
                "TeacherName": (r.get("TeacherName") or "").strip(),
                "TeacherEmail": (r.get("Email") or "").strip(),
            }
            for r in rows or []
        ]
    }


def school_year_start(today: date, tz_name: str = "Europe/Prague") -> datetime:
    year = today.year if today.month >= 9 else today.year - 1
    return datetime(year, 9, 1, tzinfo=get_tz(tz_name) or ZoneInfo("UTC"))


def fmt_absence_web(overview: list[dict], by_subject: list[dict], start: date, end: date) -> dict:
    totals: dict[str, int] = {}
    per_day: dict[str, dict[str, int]] = {}
    for r in overview or []:
        count = int(r.get("Count") or 0)
        if not count:
            continue
        kind = ABSENCE_TYPES.get(int(r.get("Type") or 0), f"type_{r.get('Type')}")
        month = (r.get("Month") or "")[:7]
        day = f"{month}-{int(r.get('Day') or 0):02d}"
        per_day.setdefault(day, {})
        per_day[day][kind] = per_day[day].get(kind, 0) + count
        totals[kind] = totals.get(kind, 0) + count
    subjects = []
    for s in by_subject or []:
        lessons, absent = int(s.get("LessonCount") or 0), int(s.get("Count") or 0)
        pct = round(100 * float(s.get("Percent") or 0), 1)
        subjects.append(drop_empty({
            "subject": (s.get("Name") or "").strip(),
            "lessons": lessons,
            "absent": absent,
            "absent_percent": pct,
            "over_threshold": True if pct >= ABSENCE_THRESHOLD_PERCENT else None,
        }) | {"absent_percent": pct})
    subjects.sort(key=lambda x: -x["absent_percent"])
    result = drop_empty({
        "period": f"{start.isoformat()} - {end.isoformat()}",
        "totals_in_hours": totals,
        "threshold_percent": ABSENCE_THRESHOLD_PERCENT,
        "days": [{"date": d, **v} for d, v in sorted(per_day.items(), reverse=True)],
        "per_subject": subjects,
    })
    result["unsolved_hours"] = totals.get("absence_unresolved", 0) + totals.get("unexcused", 0)
    return result


_EVENT = re.compile(r'event-title">(.*?)</span>\s*<span class="event-date">(.*?)</span>', re.S)
_DMY = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")


def parse_events_widget(html: str) -> list[dict]:
    """The calendar widget -> [{'title', 'start', 'end'}] (ISO dates)."""
    events = []
    for title, when in _EVENT.findall(html):
        days = []
        for d, m, y in _DMY.findall(unescape(when)):
            try:
                days.append(date(int(y), int(m), int(d)))
            except ValueError:
                continue
        if not days:
            continue
        events.append({
            "title": _clean(title),
            "start": days[0].isoformat(),
            "end": days[-1].isoformat() if days[-1] != days[0] else None,
        })
    return events


def merge_event_runs(events: list[dict]) -> list[dict]:
    """Join same-titled events on consecutive days into one range (62 'Hlavní prázdniny' rows -> 1)."""
    merged: list[dict] = []
    open_run: dict[str, dict] = {}
    for ev in sorted(events, key=lambda e: (e["start"], e["title"])):
        last = open_run.get(ev["title"])
        ev_end = date.fromisoformat(ev.get("end") or ev["start"])
        if last is not None:
            last_end = date.fromisoformat(last.get("end") or last["start"])
            if date.fromisoformat(ev["start"]) <= last_end + timedelta(days=1):
                if ev_end > last_end:
                    last["end"] = ev_end.isoformat()
                continue
        run = dict(ev)
        open_run[ev["title"]] = run
        merged.append(run)
    return merged


def parse_user(html: str) -> dict:
    """The logged-in user from the page header: '7.X, Surname Name' -> class and name."""
    m = re.search(r'<span class="lusername"[^>]*>(.*?)</span>', html, re.S)
    text = _clean(m.group(1)) if m else ""
    cls, sep, name = text.partition(",")
    return drop_empty({"name": (name if sep else text).strip(), "class": cls.strip() if sep else None})
