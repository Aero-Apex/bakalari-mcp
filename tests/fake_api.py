"""A fake Bakaláři server (httpx.MockTransport) with realistic payloads."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx

MARKS = {
    "Subjects": [
        {
            "Subject": {"Id": "M", "Name": "Matematika", "Abbrev": "M"},
            "AverageText": "1,67",
            "TemporaryMark": "",
            "Marks": [
                {"MarkDate": "2026-10-01T00:00:00+02:00", "Caption": "Test", "Theme": "Zlomky",
                 "MarkText": "1", "Weight": 3, "TypeNote": "Písemná práce", "IsNew": False, "IsPoints": False},
                {"MarkDate": "2026-10-07T00:00:00+02:00", "Caption": "Ústní", "MarkText": "2-",
                 "Weight": 1, "IsNew": True, "IsPoints": False},
                {"MarkDate": "2026-10-08T00:00:00+02:00", "Caption": "Aktivita", "MarkText": "N",
                 "Weight": 1, "IsNew": False, "IsPoints": False},
            ],
        },
        {
            "Subject": {"Id": "CJ", "Name": "Český jazyk a literatura", "Abbrev": "ČJ"},
            "AverageText": "2",
            "Marks": [
                {"MarkDate": "2026-09-20T00:00:00+02:00", "Caption": "Diktát", "MarkText": "2",
                 "Weight": 2, "IsNew": False, "IsPoints": False},
            ],
        },
    ]
}

TIMETABLE = {
    "Hours": [
        {"Id": 1, "Caption": "1", "BeginTime": "8:00", "EndTime": "8:45"},
        {"Id": 2, "Caption": "2", "BeginTime": "8:55", "EndTime": "9:40"},
    ],
    "Days": [
        {
            "DayOfWeek": 5, "Date": "2026-10-09T00:00:00+02:00", "DayDescription": "", "DayType": "Regular",
            "Atoms": [
                {"HourId": 2, "SubjectId": "CJ", "TeacherId": "T2", "RoomId": "R2", "GroupIds": [], "CycleIds": [],
                 "Change": {"ChangeType": "Canceled", "Description": "Odpadá", "Time": "9.10."}},
                {"HourId": 1, "SubjectId": "M", "TeacherId": "T1", "RoomId": "R1", "GroupIds": ["G1"],
                 "CycleIds": [], "Theme": "Zlomky", "Change": None},
            ],
        },
        {"DayOfWeek": 6, "Date": "2026-10-10T00:00:00+02:00", "DayType": "Weekend", "Atoms": []},
    ],
    "Subjects": [{"Id": "M", "Abbrev": "M", "Name": "Matematika"}, {"Id": "CJ", "Abbrev": "ČJ", "Name": "Český jazyk"}],
    "Teachers": [{"Id": "T1", "Abbrev": "Nov", "Name": "Jan Novák"}, {"Id": "T2", "Abbrev": "Svo", "Name": "Eva Svobodová"}],
    "Rooms": [{"Id": "R1", "Abbrev": "A12", "Name": "Učebna A12"}, {"Id": "R2", "Abbrev": "B3", "Name": "B3"}],
    "Groups": [{"Id": "G1", "Abbrev": "sk1", "Name": "Skupina 1"}],
    "Cycles": [],
}

HOMEWORKS = {
    "Homeworks": [
        {"Subject": {"Name": "Matematika", "Abbrev": "M"}, "DateStart": "2026-10-05T00:00:00+02:00",
         "DateEnd": "2026-10-12T00:00:00+02:00", "Content": "Cvičení 5/3", "Done": False,
         "Teacher": {"Name": "Jan Novák"}, "Attachments": []},
        {"Subject": {"Name": "Český jazyk", "Abbrev": "ČJ"}, "DateEnd": "2026-10-10T00:00:00+02:00",
         "Content": "Sloh", "Done": True},
        {"Subject": {"Name": "Fyzika", "Abbrev": "F"}, "DateEnd": "2026-11-30T00:00:00+01:00",
         "Content": "Referát", "Done": False},
    ]
}

MESSAGES = {
    "Messages": [
        {"Id": "a", "Title": "Třídní schůzky", "Text": "Schůzky budou ve čtvrtek. " * 50,
         "SentDate": "2026-10-08T10:00:00+02:00", "Read": False, "Sender": {"Name": "Novák Jan"},
         "Attachments": [{"Name": "pozvanka.pdf"}]},
        {"Id": "b", "Title": "Výlet", "Text": "Sraz v 8:00", "SentDate": "2026-10-01T09:00:00+02:00",
         "Read": True, "Sender": {"Name": "Svobodová Eva"}},
    ]
}

ABSENCE = {
    "PercentageThreshold": 0.25,
    "Absences": [{"Date": "2026-10-02T00:00:00+02:00", "Unsolved": 4, "Ok": 2, "Missed": 6, "Late": 0}],
    "AbsencesPerSubject": [{"SubjectName": "Matematika", "LessonsCount": 20, "Base": 6, "Late": 1}],
}

USER = {"FullName": "Karel Test", "Class": {"Abbrev": "7.A", "Name": "7.A"}, "UserTypeText": "Žák",
        "SchoolOrganizationName": "ZŠ Test", "EnabledModules": [{"Module": "Marks"}]}


class FakeBakalari:
    """Counts calls and lets tests inject failures."""

    def __init__(self, password: str = "pw"):
        self.password = password
        self.calls: list[tuple[str, str]] = []
        self.logins = 0
        self.refreshes = 0
        self.expires_in = 3600
        self.fail_next: list[int] = []
        self.valid_tokens: set[str] = set()
        self.komens_requires_post = False
        self.events_endpoint = "events/my"
        self.session_dead = False
        self.accept_cookie = True  # False = behaves like a server that only takes Bearer tokens
        self._n = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _new_token(self) -> dict:
        self._n += 1
        tok = f"tok{self._n}"
        self.valid_tokens.add(tok)
        return {"access_token": tok, "refresh_token": f"ref{self._n}", "expires_in": self.expires_in}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/api":
            return httpx.Response(200, json=[{"ApiVersion": "3.52.1", "ApplicationVersion": "2.2", "BaseUrl": "api/3"}])
        if path == "/sessioninfo":
            alive = request.headers.get("Cookie", "").startswith("sid=good") and not self.session_dead
            return httpx.Response(200, json={"success": True, "error": "", "data": {
                "remainingTime": 880.0 if alive else 0.0, "sessionDuration": 900.0 if alive else 0.0}})
        if path == "/sessionextend":
            return httpx.Response(200, headers=[
                ("set-cookie", "BakaAuth=ROTATEDSECRET; Max-Age=3600; Path=/; HttpOnly")])
        if path == "/dashboard":
            if request.headers.get("Cookie") != "sid=good":
                return httpx.Response(302, headers={"Location": "/login?ReturnUrl=%2Fdashboard"})
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                "<html><head><title>SECRETTITLE</title></head><body><script>var appRoot = '/';"
                'var w = [{"Url":"\\u002fTimetable\\u002fWidget?date=x"}];</script>'
                '<a href="/next/prubzna.aspx">Známky</a> <a href="/logout">Odhlásit</a>'
                '<a href="https://evil.example/x">x</a><div id="main" class="bk-widget a"></div></body></html>'))
        if path == "/next/prubzna.aspx":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=(
                '<nav class="bk-sidenav"><ul class="menuitems"><li>SECRETMENU</li></ul></nav>'
                '<script>$.post(appRoot + "Marks/Load", {a: 1}); var u = {url: "/Marks/Detail"};</script>'
                '<table class="marks"><tr><th>Předmět</th><th>Známka</th></tr>'
                '<tr class="row" data-id="7"><td class="subj">SECRETSUBJECT</td><td class="mark">SECRETGRADE</td></tr>'
                '<tr class="row" data-id="8"><td class="subj">Karel Test</td><td class="mark">1</td></tr></table>'))
        if path == "/Timetable/Widget":
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<div class="bk-widget-timetable"><span class="lesson">SECRETLESSON</span></div>')
        if path == "/":
            if request.headers.get("Cookie") == "sid=good":
                return httpx.Response(302, headers={"Location": "/next/prehled.aspx"})
            return httpx.Response(302, headers={"Location": "/login"})
        if path == "/api/login":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") == "refresh_token":
                if not form.get("refresh_token", "").startswith("ref"):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refreshes += 1
                return httpx.Response(200, json=self._new_token())
            if form.get("username") != "kid" or form.get("password") != self.password:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Špatné heslo"})
            self.logins += 1
            return httpx.Response(200, json=self._new_token())

        auth = request.headers.get("Authorization", "")
        cookie_ok = self.accept_cookie and request.headers.get("Cookie") == "sid=good"
        if not cookie_ok and auth.removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(401, headers={"WWW-Authenticate": "Bearer"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))

        ep = path.removeprefix("/api/3/")
        if ep.startswith("komens/messages/"):
            if self.komens_requires_post and request.method == "GET":
                return httpx.Response(405)
            return httpx.Response(200, json=MESSAGES)
        routes = {
            "user": USER, "marks": MARKS, "timetable/actual": TIMETABLE, "timetable/permanent": TIMETABLE,
            "homeworks": HOMEWORKS, "absence/student": ABSENCE,
            "subjects": {"Subjects": [{"SubjectName": "Matematika", "SubjectAbbrev": "M", "TeacherName": "Jan Novák"}]},
            self.events_endpoint: {"Events": [
                {"Title": "Exkurze", "Times": [{"StartTime": "2026-10-20T08:00:00+02:00", "EndTime": "2026-10-20T14:00:00+02:00"}]},
                {"Title": "Starý výlet", "Times": [{"StartTime": "2026-01-02T08:00:00+01:00", "EndTime": "2026-01-02T14:00:00+01:00"}]},
            ]},
        }
        if ep in routes:
            return httpx.Response(200, content=json.dumps(routes[ep]), headers={"content-type": "application/json"})
        return httpx.Response(404)
