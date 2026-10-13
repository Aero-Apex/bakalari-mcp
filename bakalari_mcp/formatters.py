"""Turn raw Bakaláři JSON into compact, readable structures.

Everything here is pure (no I/O) and defensive: the API differs a bit between
school installations, so missing keys are skipped rather than raising.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from .util import (
    WEEKDAYS_CS,
    clip,
    drop_empty,
    fold,
    iso_to_date,
    pick,
    short_dt,
)


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _index(items: Any, key: str = "Id") -> dict[Any, dict]:
    return {i[key]: i for i in _dicts(items) if key in i}


def _first_list(data: Any, *keys: str) -> list[dict]:
    if isinstance(data, list):
        return _dicts(data)
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return _dicts(data[key])
        for value in data.values():  # unknown wrapper key: take the first list
            if isinstance(value, list):
                return _dicts(value)
    return []


def _name(item: dict | None, *keys: str) -> str | None:
    return pick(item or {}, *(keys or ("Name", "Abbrev")))


# ------------------------------------------------------------------- user


def fmt_user(data: dict) -> dict:
    cls = data.get("Class") or {}
    modules = [m.get("Module") for m in _dicts(data.get("EnabledModules"))]
    return drop_empty(
        {
            "name": data.get("FullName"),
            "role": pick(data, "UserTypeText", "UserType"),
            "class": pick(cls, "Name", "Abbrev"),
            "school": data.get("SchoolOrganizationName"),
            "school_type": data.get("SchoolType"),
            "study_year": data.get("StudyYear"),
            "enabled_modules": modules,
        }
    )


# ------------------------------------------------------------------ marks


def parse_mark(text: Any) -> float | None:
    """'1' -> 1.0, '2-' -> 2.5, anything else (N, A, points...) -> None."""
    m = re.fullmatch(r"\s*([1-5])\s*(-)?\s*", str(text or ""))
    if not m:
        return None
    return int(m.group(1)) + (0.5 if m.group(2) else 0.0)


def weighted_average(marks: Iterable[dict]) -> float | None:
    total = weight_sum = 0.0
    for mark in marks:
        value = parse_mark(pick(mark, "MarkText", "CalculatedMarkText"))
        if value is None or mark.get("IsPoints"):
            continue
        try:
            weight = float(pick(mark, "Weight", default=1) or 1)
        except (TypeError, ValueError):
            weight = 1.0
        total += value * weight
        weight_sum += weight
    return round(total / weight_sum, 2) if weight_sum else None


def _mark_row(mark: dict) -> dict:
    points = None
    if mark.get("IsPoints"):
        points = f"{mark.get('PointsText', '?')}/{mark.get('MaxPoints', '?')}"
    return drop_empty(
        {
            "date": (mark.get("MarkDate") or "")[:10],
            "mark": pick(mark, "MarkText", "CalculatedMarkText"),
            "points": points,
            "weight": mark.get("Weight"),
            "caption": mark.get("Caption"),
            "theme": mark.get("Theme"),
            "type": pick(mark, "TypeNote", "Type"),
            "note": mark.get("Note") or None,
            "class_rank": mark.get("ClassRank") or None,
            "new": True if mark.get("IsNew") else None,
        }
    )


def fmt_marks(
    data: dict,
    subject: str | None = None,
    since: date | None = None,
    only_new: bool = False,
    summary_only: bool = False,
) -> dict:
    wanted = fold(subject) if subject else None
    summary: list[dict] = []
    details: list[dict] = []
    for entry in _dicts(data.get("Subjects")):
        info = entry.get("Subject") or {}
        name = pick(info, "Name", "Abbrev", default="?")
        abbrev = info.get("Abbrev")
        if wanted and wanted not in fold(name) and wanted not in fold(abbrev):
            continue
        marks = _dicts(entry.get("Marks"))
        shown = [
            m
            for m in marks
            if (not only_new or m.get("IsNew"))
            and (since is None or (iso_to_date(m.get("MarkDate")) or date.min) >= since)
        ]
        shown.sort(key=lambda m: m.get("MarkDate") or "", reverse=True)
        summary.append(
            drop_empty(
                {
                    "subject": name,
                    "abbrev": abbrev,
                    "school_average": entry.get("AverageText") or None,
                    "computed_average": weighted_average(marks),
                    "temporary_mark": entry.get("TemporaryMark") or None,
                    "marks_total": len(marks),
                    "new_marks": sum(1 for m in marks if m.get("IsNew")),
                }
            )
        )
        if shown or not (only_new or since):
            details.append({"subject": name, "marks": [_mark_row(m) for m in shown]})
    if only_new or since:
        keep = {d["subject"] for d in details if d["marks"]}
        details = [d for d in details if d["subject"] in keep]
    result: dict[str, Any] = {
        "note": "computed_average = weighted average of numeric marks ('2-' counts as 2.5); "
        "it can differ from the school's own average.",
        "summary": summary,
    }
    if not summary_only:
        result["subjects"] = details
    return result


# -------------------------------------------------------------- timetable


def _weekday_name(index: int) -> str:
    return WEEKDAYS_CS[index] if 0 <= index < 7 else "?"


def _hour_sort_key(lesson: dict) -> tuple[int, str]:
    m = re.search(r"\d+", str(lesson.get("hour", "")))
    return (int(m.group()) if m else 99, str(lesson.get("time", "")))


_CANCELLED = {"canceled", "cancelled", "removed"}


def _lesson(atom: dict, ctx: dict[str, dict]) -> dict:
    hour = ctx["hours"].get(atom.get("HourId"), {})
    subject = ctx["subjects"].get(atom.get("SubjectId"), {})
    teacher = ctx["teachers"].get(atom.get("TeacherId"), {})
    room = ctx["rooms"].get(atom.get("RoomId"), {})
    groups = [pick(ctx["groups"].get(g, {}), "Abbrev", "Name") for g in atom.get("GroupIds") or []]
    cycles = [pick(ctx["cycles"].get(c, {}), "Name", "Abbrev") for c in atom.get("CycleIds") or []]
    change = atom.get("Change") if isinstance(atom.get("Change"), dict) else None

    begin, end = hour.get("BeginTime"), hour.get("EndTime")
    change_type = pick(change or {}, "ChangeType", "TypeShort")
    lesson = {
        "hour": hour.get("Caption") or atom.get("HourId"),
        "time": f"{begin}-{end}" if begin and end else None,
        "subject": pick(subject, "Name", "Abbrev") or pick(change or {}, "ChangeSubject"),
        "abbrev": subject.get("Abbrev"),
        "teacher": pick(teacher, "Name", "Abbrev"),
        "room": pick(room, "Abbrev", "Name"),
        "group": ", ".join(g for g in groups if g) or None,
        "cycle": ", ".join(c for c in cycles if c) or None,
        "theme": atom.get("Theme") or None,
    }
    if change:
        lesson["change"] = drop_empty(
            {
                "type": change_type,
                "description": pick(change, "Description", "TypeName"),
                "time": change.get("Time"),
            }
        )
        if fold(change_type) in _CANCELLED:
            lesson["cancelled"] = True
    return drop_empty(lesson)


def fmt_timetable(data: dict, day: date | None = None, permanent: bool = False) -> dict:
    ctx = {
        "hours": _index(data.get("Hours")),
        "subjects": _index(data.get("Subjects")),
        "teachers": _index(data.get("Teachers")),
        "rooms": _index(data.get("Rooms")),
        "groups": _index(data.get("Groups")),
        "cycles": _index(data.get("Cycles")),
    }
    days: list[dict] = []
    for d in _dicts(data.get("Days")):
        d_date = iso_to_date(d.get("Date"))
        if day is not None and d_date != day:
            continue
        if d_date is not None:
            weekday = d_date.weekday()
        else:
            weekday = int(pick(d, "DayOfWeek", default=1)) - 1  # API: 1 = Monday
        lessons = sorted((_lesson(a, ctx) for a in _dicts(d.get("Atoms"))), key=_hour_sort_key)
        day_type = d.get("DayType")
        days.append(
            drop_empty(
                {
                    "date": d_date.isoformat() if d_date else None,
                    "weekday": _weekday_name(weekday),
                    "day_type": day_type if fold(day_type) not in ("", "regular") else None,
                    "description": d.get("DayDescription") or None,
                    "lessons": lessons,
                    "changes": sum(1 for x in lessons if "change" in x) or None,
                }
            )
            | {"lessons": lessons}
        )
    result: dict[str, Any] = {"days": days}
    if permanent:
        cycles = [pick(c, "Name", "Abbrev") for c in _dicts(data.get("Cycles"))]
        if cycles:
            result["cycles"] = cycles
    if day is not None and not days:
        result["note"] = f"No timetable entry for {day.isoformat()} (weekend or holiday?)."
    return result


# -------------------------------------------------------------- homeworks


def fmt_homeworks(
    data: dict,
    include_done: bool = False,
    subject: str | None = None,
    since: date | None = None,
    until: date | None = None,
) -> dict:
    wanted = fold(subject) if subject else None
    rows: list[dict] = []
    total = 0
    for hw in _first_list(data, "Homeworks", "Homework"):
        total += 1
        subj = hw.get("Subject") if isinstance(hw.get("Subject"), dict) else {}
        subj_name = pick(subj, "Name", "Abbrev") or pick(hw, "SubjectName", "SubjectAbbrev")
        if wanted and wanted not in fold(subj_name) and wanted not in fold(subj.get("Abbrev")):
            continue
        done = bool(pick(hw, "Done", "Closed", default=False))
        if done and not include_done:
            continue
        due_raw = pick(hw, "DateEnd", "DateDue", "DateTo")
        due = iso_to_date(due_raw)
        if due and ((since and due < since) or (until and due > until)):
            continue
        teacher = hw.get("Teacher") if isinstance(hw.get("Teacher"), dict) else {}
        text, truncated = clip(pick(hw, "Content", "Text", "Notice"), 1500)
        rows.append(
            drop_empty(
                {
                    "subject": subj_name,
                    "due": short_dt(due_raw),
                    "assigned": short_dt(pick(hw, "DateStart", "DateAssigned", "Date")),
                    "done": done,
                    "teacher": pick(teacher, "Name") or hw.get("TeacherName"),
                    "content": text,
                    "truncated": True if truncated else None,
                    "attachments": [a.get("Name") for a in _dicts(hw.get("Attachments"))],
                }
            )
            | {"done": done}
        )
    rows.sort(key=lambda r: r.get("due") or "9999")
    return {"count": len(rows), "total_in_response": total, "homeworks": rows}


# --------------------------------------------------------------- subjects


def fmt_subjects(data: dict) -> dict:
    rows = []
    for s in _first_list(data, "Subjects"):
        rows.append(
            drop_empty(
                {
                    "subject": pick(s, "SubjectName", "Name"),
                    "abbrev": pick(s, "SubjectAbbrev", "Abbrev"),
                    "teacher": pick(s, "TeacherName"),
                    "teacher_abbrev": pick(s, "TeacherAbbrev"),
                    "email": pick(s, "TeacherSchoolEmail", "TeacherEmail"),
                }
            )
        )
    return {"count": len(rows), "subjects": rows}


# ----------------------------------------------------------------- events


def fmt_events(data: dict, start: date | None = None, end: date | None = None) -> dict:
    rows = []
    for ev in _first_list(data, "Events"):
        times = _dicts(ev.get("Times"))
        first = times[0] if times else {}
        begin = pick(first, "StartTime", "Start") or ev.get("Start")
        finish = pick(first, "EndTime", "End") or ev.get("End")
        begin_date = iso_to_date(begin)
        end_date = iso_to_date(finish) or begin_date
        if begin_date and ((end and begin_date > end) or (start and (end_date or begin_date) < start)):
            continue
        etype = ev.get("EventType") if isinstance(ev.get("EventType"), dict) else {}
        text, _ = clip(ev.get("Description"), 600)
        rows.append(
            drop_empty(
                {
                    "title": pick(ev, "Title", "Name"),
                    "start": short_dt(begin),
                    "end": short_dt(finish),
                    "whole_day": True if first.get("WholeDay") else None,
                    "type": etype.get("Name"),
                    "description": text,
                    "places": [pick(r, "Name", "Abbrev") for r in _dicts(ev.get("Rooms"))],
                    "teachers": [pick(t, "Name", "Abbrev") for t in _dicts(ev.get("Teachers"))],
                }
            )
        )
    rows.sort(key=lambda r: r.get("start") or "")
    return {"count": len(rows), "events": rows}


# ---------------------------------------------------------------- absence


def fmt_absence(data: dict) -> dict:
    def hours(entry: dict) -> dict:
        keys = ("Unsolved", "Ok", "Missed", "Late", "Soon", "School", "DistanceTeaching")
        return {k.lower(): entry[k] for k in keys if entry.get(k)}

    days = []
    totals: dict[str, int] = {}
    for a in _dicts(data.get("Absences")):
        h = hours(a)
        for k, v in h.items():
            totals[k] = totals.get(k, 0) + v
        days.append({"date": (a.get("Date") or "")[:10], **h})
    days.sort(key=lambda r: r["date"], reverse=True)

    subjects = []
    threshold = data.get("PercentageThreshold")
    for s in _dicts(data.get("AbsencesPerSubject")):
        lessons = s.get("LessonsCount") or 0
        base = s.get("Base") or 0
        pct = round(100 * base / lessons, 1) if lessons else None
        subjects.append(
            drop_empty(
                {
                    "subject": s.get("SubjectName"),
                    "lessons": lessons,
                    "absent": base,
                    "absent_percent": pct,
                    "late": s.get("Late") or None,
                    "left_early": s.get("Soon") or None,
                    "school_excused": s.get("School") or None,
                    "over_threshold": bool(pct is not None and threshold and pct >= 100 * threshold) or None,
                }
            )
        )
    return drop_empty(
        {
            "totals_in_hours": totals,
            "unsolved_hours": totals.get("unsolved", 0),
            "threshold_percent": round(100 * threshold, 1) if threshold else None,
            "days": days,
            "per_subject": subjects,
        }
    )


# --------------------------------------------------------------- messages


def _sender(msg: dict) -> str | None:
    sender = msg.get("Sender")
    if isinstance(sender, dict):
        return pick(sender, "Name", "Abbrev")
    return sender if isinstance(sender, str) else None


def _recipients(msg: dict) -> str | None:
    rec = msg.get("Recipients")
    if isinstance(rec, list):
        names = [pick(r, "Name") if isinstance(r, dict) else str(r) for r in rec]
        return ", ".join(n for n in names if n) or None
    return msg.get("RecipientsText") or None


def fmt_messages(
    data: Any,
    limit: int = 15,
    unread_only: bool = False,
    search: str | None = None,
    text_chars: int = 600,
    show_recipients: bool = False,
) -> dict:
    msgs = _first_list(data, "Messages", "Items")
    msgs.sort(key=lambda m: pick(m, "SentDate", "Date", default=""), reverse=True)
    unread_total = sum(1 for m in msgs if m.get("Read") is False)
    needle = fold(search) if search else None
    out: list[dict] = []
    matched = 0
    for m in msgs:
        if unread_only and m.get("Read") is not False:
            continue
        if needle and needle not in fold(f"{m.get('Title', '')} {m.get('Text', '')} {_sender(m) or ''}"):
            continue
        matched += 1
        if len(out) >= limit:
            continue
        text, truncated = clip(m.get("Text"), text_chars)
        out.append(
            drop_empty(
                {
                    "id": m.get("Id"),
                    "title": m.get("Title"),
                    "from": _sender(m),
                    "to": _recipients(m) if show_recipients else None,
                    "sent": short_dt(pick(m, "SentDate", "Date")),
                    "read": m.get("Read"),
                    "pinned": True if m.get("Pinned") else None,
                    "text": text,
                    "text_truncated": True if truncated else None,
                    "attachments": [a.get("Name") for a in _dicts(m.get("Attachments"))],
                }
            )
            | ({"read": m["Read"]} if "Read" in m else {})
        )
    return {
        "total": len(msgs),
        "unread_total": unread_total,
        "matched": matched,
        "shown": len(out),
        "messages": out,
    }
