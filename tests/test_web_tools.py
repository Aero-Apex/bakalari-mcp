"""Every data tool in cookie mode, against a fake school website with the real response shapes (invented data)."""

import asyncio
import json
from datetime import date

import httpx
import pytest

from bakalari_mcp import server
from bakalari_mcp import cookiestore
from bakalari_mcp import webdata as wd
from bakalari_mcp.client import BakalariClient
from bakalari_mcp.config import Config

TODAY = date(2026, 10, 9)  # a Friday


def atom(hour, subject, abbrev, teacher, room, **extra):
    base = {
        # like the real site, HourIndex is NOT the printed hour number (8:00 is hour 1 but HourIndex 3)
        "Type": "atom", "HourIndex": hour + 2, "Time": "x", "SubjectText": subject, "SubjectAbbrev": abbrev,
        "Teacher": teacher[:3].upper(), "TeacherFullname": teacher, "Room": room, "GroupsNames": "7.X",
        "Theme": "", "ChangeInfo": "", "BadgeChar": "", "HasChanged": False, "Notice": "", "HomeworksCount": 0,
        "StudentAbsence": "NoAbsent", "AtomAbsenceText": "", "CycleText": "",
    }
    return {**base, **extra}


def slot(begin, end, *atoms):
    return {"Type": "atoms", "Begin": begin, "End": end, "Atoms": list(atoms)}


HOURS = [{"Caption": str(i), "BeginTime": t, "EndTime": e} for i, (t, e) in enumerate(
    [("7:00", ""), ("8:00", "8:45"), ("8:55", "9:40"), ("10:00", "10:45"), ("10:55", "11:40")])]


def week(first_label_day, extra_monday=None):
    """Five days; Monday has a substitution, a cancelled lesson and unsorted slots."""
    monday = [
        slot("10:00:00", "10:45:00", atom(3, "Matematika", "M", "Jana Nová", "19")),
        slot("08:00:00", "08:45:00", atom(1, "Fyzika", "F", "Petr Starý", "26", HasChanged=True, BadgeChar="S",
                                          ChangeInfo="Suplování: Starý Petr (NOV)", Theme="Pohyb")),
        slot("08:55:00", "09:40:00", {"Type": "removed", "HourIndex": 0, "ChangeInfo": "Vyjmuto z rozvrhu (TV, Eva Malá)",
                                    "SubjectText": None, "SubjectAbbrev": None}),
    ]
    days = []
    for i, label in enumerate(["po", "út", "st", "čt", "pá"]):
        d = {"DayAbbrev": label, "Date": f"{first_label_day + i}.10.", "DayOff": False, "DayOffName": "", "Hours": []}
        if i == 0:
            d["Hours"] = monday
        elif i == 4:
            d["DayOff"], d["DayOffName"] = True, "Ředitelské volno"
        else:
            d["Hours"] = [slot("08:00:00", "08:45:00", atom(1, "Dějepis", "D", "Karel Dlouhý", "5"))]
        days.append(d)
    return {"TimetableDataAvailable": True, "Hours": HOURS, "Days": days}


PERMANENT = {"Hours": HOURS, "Days": [{"DayAbbrev": "po", "Date": "", "DayOff": False, "DayOffName": "", "Hours": [
    slot("08:00:00", "08:45:00", atom(1, "Matematika", "M", "Jana Nová", "19")),
    slot("08:55:00", "09:40:00", atom(2, "Fyzika", "F", "Petr Starý", "26"))]}]}

HOMEWORK = {
    3: [{"DeadlineDate": "2026-10-12T00:00:00+02:00", "SubjectAbbrev": "F", "Text": "Referát o pohybu",
         "AssignmentDate": "2026-10-09T00:00:00+02:00", "Finished": False, "Closed": False, "Attachments": []}],
    4: [{"DeadlineDate": "2026-10-05T00:00:00+02:00", "SubjectAbbrev": "M", "Text": "Příklady 1-5",
         "AssignmentDate": "2026-10-01T00:00:00+02:00", "Finished": False, "Closed": False, "Attachments": []}],
    2: [{"DeadlineDate": "2026-09-20T00:00:00+02:00", "SubjectAbbrev": "D", "Text": "Starý úkol",
         "AssignmentDate": "2026-09-15T00:00:00+02:00", "Finished": True, "Closed": True, "Attachments": []}],
}

ABSENCE_OVERVIEW = [
    {"Month": "2026-10-01T00:00:00", "Day": 2, "Type": 2, "Count": 6},
    {"Month": "2026-10-01T00:00:00", "Day": 6, "Type": 3, "Count": 2},
    {"Month": "2026-10-01T00:00:00", "Day": 7, "Type": 1, "Count": 1},
    {"Month": "2026-09-01T00:00:00", "Day": 1, "Type": 4, "Count": 0},
]
ABSENCE_SUBJECTS = [
    {"Name": "Matematika ", "LessonCount": 20, "Count": 5, "Percent": 0.25},
    {"Name": "Fyzika ", "LessonCount": 20, "Count": 1, "Percent": 0.05},
]

SUBJECTS = [{"Id": None, "SubjectName": "Matematika", "TeacherName": "Mgr. Jana Nová", "Email": "nova@skola.test",
             "Web": "", "HomePhone": "111111111", "MobilePhone": "777000111", "SchoolPhone": ""}]


def events_html(*items):
    lines = "".join(f'<div class="event-line"><div class="event-info"><span class="event-title">{t}</span>'
                    f'<span class="event-date">{d}</span></div></div>' for t, d in items)
    return f'<div class="bk-widget-events">{lines}</div>'


def site(request: httpx.Request) -> httpx.Response:
    path, method = request.url.path, request.method
    if path == "/login":
        return httpx.Response(200, text="<html>login form</html>")
    if request.headers.get("Cookie") != "sid=good":
        return httpx.Response(302, headers={"Location": "/login?ReturnUrl=" + path})
    body = json.loads(request.content) if request.content else {}
    ok = lambda data: httpx.Response(200, json={"success": True, "error": "", "data": data})  # noqa: E731
    if path == "/Timetable/GetContentData":
        if body["term"] == "perm":
            return ok(PERMANENT)
        monday = {"2026-10-05": 5, "2026-10-12": 12}[body["date"][:10]]
        return ok(week(monday))
    if path == "/HomeWorks/Student/LoadHomeworks":
        return ok(HOMEWORK[body["homeworkState"]])
    if path == "/Absence/Continuous/GetAbsenceOverview":
        return ok(ABSENCE_OVERVIEW)
    if path == "/Absence/Continuous/GetAbsenceBySubjects":
        return ok(ABSENCE_SUBJECTS)
    if path == "/Teaching/Subjects" and method == "POST":
        return ok(SUBJECTS)
    if path == "/Dashboard/CalendarWidget":
        if "2026-11" in str(request.url.params.get("date", "")):
            return httpx.Response(200, text=events_html(("Listopadová akce", "17.11.2026 (úterý)")))
        return httpx.Response(200, text=events_html(
            ("Volby - zkrácené vyučování", "9.10.2026 (pátek)"), ("Výlet", "13.10.2026 - 14.10.2026"),
            ("Dávná akce", "1.10.2026 (čtvrtek)")))
    if path == "/Dashboard":
        return httpx.Response(200, text='<html><span class="lusername" id="x">7.X, Novák Tomáš</span></html>')
    return httpx.Response(404)


@pytest.fixture
def web(tmp_path, monkeypatch):
    store = tmp_path / "session.json"
    cookiestore.save(store, "sid=good")
    config = Config(url="https://school.test", cookie_file=store, cache_ttl=0)
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_client", BakalariClient(config, transport=httpx.MockTransport(site)))
    monkeypatch.setattr(server, "today", lambda tz_name="x": TODAY)
    yield store
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_client", None)


def call(name, args=None):
    result = asyncio.run(server.mcp.call_tool(name, args or {}))
    return result


def data(name, args=None):
    """The tool's structured result as a dict."""
    result = call(name, args)
    return result.structured_content if getattr(result, "structured_content", None) else json.loads(result.content[0].text)


# ------------------------------------------------------------------ timetable


def test_timetable_day_shows_changes_cancellations_and_sorts_by_hour(web):
    out = data("get_timetable_day", {"date": "2026-10-05"})
    day = out["days"][0]
    assert day["date"] == "2026-10-05" and day["weekday"] == "Pondělí" and day["changes"] == 2
    hours = [x["hour"] for x in day["lessons"]]
    assert hours == ["1", "2", "3"]  # sorted, and the cancelled lesson got its hour from the time slot
    first, cancelled, third = day["lessons"]
    assert first["subject"] == "Fyzika" and first["teacher"] == "Petr Starý" and first["room"] == "26"
    assert first["change"] == {"type": "substitution", "description": "Suplování: Starý Petr (NOV)"}
    assert first["time"] == "8:00-8:45" and first["theme"] == "Pohyb"
    assert cancelled["cancelled"] is True and cancelled["abbrev"] == "TV" and cancelled["teacher"] == "Eva Malá"
    assert third["subject"] == "Matematika" and "change" not in third


def test_timetable_week_has_five_days_and_marks_a_day_off(web):
    out = data("get_timetable_actual", {"date": "2026-10-14"})  # a Wednesday: the week of the 12th
    assert [d["date"] for d in out["days"]] == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    friday = out["days"][4]
    assert friday["day_type"] == "day off" and friday["description"] == "Ředitelské volno" and friday["lessons"] == []


def test_timetable_day_outside_the_week_gives_a_note_and_permanent_works(web):
    assert "note" in data("get_timetable_day", {"date": "2026-10-11"})  # a Sunday
    perm = data("get_timetable_permanent")
    assert perm["days"][0]["weekday"] == "Pondělí" and "date" not in perm["days"][0]
    assert [x["subject"] for x in perm["days"][0]["lessons"]] == ["Matematika", "Fyzika"]


def test_label_dates_cross_new_year():
    from bakalari_mcp.webdata import _label_date

    assert _label_date("2.1.", date(2026, 12, 30)) == date(2027, 1, 2)
    assert _label_date("30.12.", date(2027, 1, 2)) == date(2026, 12, 30)


# ------------------------------------------------------------------- homework


def test_homework_hides_closed_by_default_sorts_and_names_subjects(web):
    out = data("get_homeworks")
    assert [(h["subject"], h["due"]) for h in out["homeworks"]] == [("Matematika", "2026-10-05"), ("Fyzika", "2026-10-12")]
    assert "Referát o pohybu" in str(out) and out["count"] == 2
    everything = data("get_homeworks", {"include_done": True})
    # subjects missing from the permanent timetable keep their abbreviation instead of failing
    assert everything["count"] == 3 and any(h["subject"] == "D" for h in everything["homeworks"])
    assert data("get_homeworks", {"due_until": "2026-10-10"})["count"] == 1


# -------------------------------------------------------------------- absence


def test_absence_summarises_hours_and_flags_subjects_over_the_threshold(web):
    out = data("get_absence")
    assert out["totals_in_hours"] == {"excused": 6, "unexcused": 2, "absence_unresolved": 1}
    assert out["unsolved_hours"] == 3  # unresolved + unexcused
    assert out["days"][0]["date"] == "2026-10-07" and {"date": "2026-10-02", "excused": 6} in out["days"]
    math = out["per_subject"][0]
    assert math == {"subject": "Matematika", "lessons": 20, "absent": 5, "absent_percent": 25.0, "over_threshold": True}
    assert out["per_subject"][1]["absent_percent"] == 5.0 and "over_threshold" not in out["per_subject"][1]


# ----------------------------------------------------------- subjects / events / user


def test_subjects_list_teachers_and_emails_but_never_phone_numbers(web):
    text = str(call("get_subjects"))
    assert "Matematika" in text and "nova@skola.test" in text
    assert "777000111" not in text and "111111111" not in text


def test_events_cover_future_months_and_skip_past_ones(web):
    out = data("get_events", {"days_ahead": 60})
    titles = [e["title"] for e in out["events"]]
    assert titles == ["Volby - zkrácené vyučování", "Výlet", "Listopadová akce"]  # past event dropped, next month added
    trip = next(e for e in out["events"] if e["title"] == "Výlet")
    assert trip["start"] == "2026-10-13" and trip["end"] == "2026-10-14"
    assert [e["title"] for e in data("get_events", {"days_ahead": 3})["events"]] == ["Volby - zkrácené vyučování"]


def test_user_comes_from_the_page_header(web):
    assert data("get_user") == {"name": "Novák Tomáš", "class": "7.X"}


# ------------------------------------------------------------ overview + errors


def test_overview_works_in_cookie_mode_and_isolates_a_failing_part(web, monkeypatch):
    marks_page = (__import__("pathlib").Path(__file__).parent / "pages" / "marks_sample.html").read_text(encoding="utf-8")
    orig = site

    def with_marks(request):
        if request.url.path == "/next/prubzna.aspx" and request.headers.get("Cookie") == "sid=good":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=marks_page)
        return orig(request)

    config = server._config
    monkeypatch.setattr(server, "_client", BakalariClient(config, transport=httpx.MockTransport(with_marks)))
    out = data("get_overview", {"date": "2026-10-05"})
    assert out["timetable"]["days"][0]["lessons"][0]["subject"] == "Fyzika"
    assert out["next_school_day"]["days"][0]["date"] == "2026-10-06"
    assert out["homework"]["count"] == 2 and out["absence"]["unsolved_hours"] == 3
    assert out["new_marks"]["summary"][0]["subject"] == "Matematika"
    assert "error" in out["unread_messages"] and "unread_total" not in out["unread_messages"]  # isolated, not fatal


def test_an_ended_session_is_explained_not_crashed(web):
    cookiestore.save(web, "sid=expired")
    for tool in ("get_timetable_day", "get_homeworks", "get_absence", "get_subjects", "get_events", "get_user"):
        with pytest.raises(Exception) as err:
            call(tool)
        assert "logged the session out" in str(err.value), tool


def test_parsers_survive_empty_input():
    assert wd.fmt_timetable_web({}, TODAY) == {"days": []}
    assert wd.homeworks_to_api_shape([]) == {"Homeworks": []}
    assert wd.parse_events_widget("") == [] and wd.parse_user("") == {}
    assert wd.fmt_absence_web([], [], TODAY, TODAY)["unsolved_hours"] == 0


def test_consecutive_same_title_events_merge_into_one_range():
    ev = lambda t, s, e=None: {"title": t, "start": s, "end": e}  # noqa: E731
    out = wd.merge_event_runs([
        ev("Prázdniny", "2026-12-24"), ev("Prázdniny", "2026-12-25"), ev("Štědrý den", "2026-12-24"),
        ev("Prázdniny", "2026-12-26", "2026-12-28"), ev("Prázdniny", "2027-01-05"), ev("Výlet", "2026-10-13", "2026-10-14"),
    ])
    assert [(e["title"], e["start"], e.get("end")) for e in out] == [
        ("Výlet", "2026-10-13", "2026-10-14"),
        ("Prázdniny", "2026-12-24", "2026-12-28"),
        ("Štědrý den", "2026-12-24", None),
        ("Prázdniny", "2027-01-05", None),
    ]


def test_marks_get_subject_abbreviations_so_filtering_by_abbreviation_works(web, monkeypatch):
    marks_page = (__import__("pathlib").Path(__file__).parent / "pages" / "marks_sample.html").read_text(encoding="utf-8")
    orig = site

    def with_marks(request):
        if request.url.path == "/next/prubzna.aspx":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=marks_page)
        return orig(request)

    monkeypatch.setattr(server, "_client", BakalariClient(server._config, transport=httpx.MockTransport(with_marks)))
    out = data("get_marks", {"subject": "M"})
    assert out["summary"][0]["subject"] == "Matematika" and out["summary"][0]["abbrev"] == "M"
    assert data("get_marks", {"subject": "F"})["summary"] == []  # no such subject on the marks page


def test_raw_homeworks_and_absence_return_the_schools_own_rows(web):
    homework = data("get_homeworks", {"raw": True})
    assert len(homework["Homeworks"]) == 2 and "DeadlineDate" in homework["Homeworks"][0]
    absence = data("get_absence", {"raw": True})
    assert absence["overview"][0]["Count"] == 6 and absence["bySubject"][0]["Name"] == "Matematika "


def test_raw_events_keep_each_calendar_row_and_subjects_never_show_phone_numbers(web, monkeypatch):
    orig = site

    def two_days(request):
        if request.url.path == "/Dashboard/CalendarWidget":
            return httpx.Response(200, text=events_html(("Prázdniny", "22.10.2026 (čtvrtek)"),
                                                        ("Prázdniny", "23.10.2026 (pátek)")))
        return orig(request)

    monkeypatch.setattr(server, "_client", BakalariClient(server._config, transport=httpx.MockTransport(two_days)))
    assert [e["start"] for e in data("get_events", {"days_ahead": 60})["events"]] == ["2026-10-22"]  # merged
    raw = data("get_events", {"days_ahead": 60, "raw": True})
    assert [e["start"] for e in raw["events"]] == ["2026-10-22", "2026-10-23"]
    assert "777000111" not in str(call("get_subjects", {"raw": True}))


def test_the_permanent_timetable_is_requested_for_today_in_the_configured_zone(web, monkeypatch):
    from bakalari_mcp import loaders

    sent = []
    orig = site

    def record(request):
        if request.url.path == "/Timetable/GetContentData" and b'"perm"' in request.content:
            sent.append(json.loads(request.content)["date"])
        return orig(request)

    monkeypatch.setattr(server, "_client", BakalariClient(server._config, transport=httpx.MockTransport(record)))
    monkeypatch.setattr(loaders, "today", lambda tz_name="x": date(2026, 10, 9))  # the day in BAKALARI_TZ
    data("get_timetable_permanent")
    assert sent == ["2026-10-09T00:00:00.000Z"]
