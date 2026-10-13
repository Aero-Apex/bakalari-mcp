import asyncio
from datetime import date

import httpx
import pytest

from bakalari_mcp import formatters as fmt
from bakalari_mcp.client import AuthError, BakalariClient, BakalariError
from bakalari_mcp.config import Config, ConfigError, normalize_cookie, normalize_url, parse_env_file
from bakalari_mcp.util import fold, monday_of, next_weekday, parse_date

from fake_api import ABSENCE, HOMEWORKS, MARKS, MESSAGES, TIMETABLE, FakeBakalari


def run(coro):
    return asyncio.run(coro)


def make(fake: FakeBakalari, **kw) -> BakalariClient:
    cfg = Config(url="https://school.test", username="kid", password="pw", cache_ttl=kw.pop("cache_ttl", 0), **kw)
    return BakalariClient(cfg, transport=fake.transport())


# ---------------------------------------------------------------- util/config


def test_parse_date_variants():
    assert parse_date("2026-10-09") == date(2026, 10, 9)
    assert parse_date("9.10.2026") == date(2026, 10, 9)
    assert parse_date("9. 10. 2026") == date(2026, 10, 9)
    assert parse_date("zítra") > parse_date("dnes")
    with pytest.raises(ValueError):
        parse_date("next tuesday")
    with pytest.raises(ValueError):
        parse_date("31.2.2026")


def test_week_helpers():
    assert monday_of(date(2026, 10, 9)) == date(2026, 10, 5)
    assert next_weekday(date(2026, 10, 9)) == date(2026, 10, 12)  # Friday -> Monday


def test_fold():
    assert fold("Český Jazyk") == "cesky jazyk"


def test_normalize_url():
    assert normalize_url("school.cz/") == "https://school.cz"
    assert normalize_url("https://school.cz/bakaweb/login") == "https://school.cz/bakaweb"
    assert normalize_url("http://x.cz/api/3/user") == "http://x.cz"


def test_env_file_roundtrip_with_special_chars(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        '# c\nBAKALARI_URL="https://a.cz"\nBAKALARI_USERNAME=kid\n'
        'BAKALARI_PASSWORD="p\\"a\\\\ss $x `y`"\nexport BAKALARI_TZ=\'Europe/Prague\'\n',
        encoding="utf-8",
    )
    values = parse_env_file(f)
    assert values["BAKALARI_PASSWORD"] == 'p"a\\ss $x `y`'
    cfg = Config.load(environ={}, env_file=f)
    assert cfg.password == 'p"a\\ss $x `y`' and cfg.url == "https://a.cz"


def test_environment_overrides_file_and_missing_is_reported(tmp_path):
    f = tmp_path / ".env"
    f.write_text("BAKALARI_URL=https://file.cz\nBAKALARI_USERNAME=u\nBAKALARI_PASSWORD=p\n")
    assert Config.load(environ={"BAKALARI_URL": "https://env.cz"}, env_file=f).url == "https://env.cz"
    with pytest.raises(ConfigError, match="BAKALARI_PASSWORD"):
        Config.load(environ={}, env_file=tmp_path / "nope")


def test_config_repr_hides_password():
    assert "secret" not in repr(Config(url="u", username="n", password="secret"))


# --------------------------------------------------------------------- client


def test_login_once_for_many_calls():
    fake = FakeBakalari()
    c = make(fake)

    async def go():
        for ep in ("user", "marks", "subjects"):
            await c.get(ep)

    run(go())
    assert fake.logins == 1


def test_expired_token_is_refreshed_not_relogged():
    fake = FakeBakalari()
    fake.expires_in = 0  # min lifetime is clamped to 30s, so force expiry manually
    c = make(fake)

    async def go():
        await c.get("user")
        c._expires_at = 0
        await c.get("user")

    run(go())
    assert fake.logins == 1 and fake.refreshes == 1


def test_revoked_token_triggers_one_relogin():
    fake = FakeBakalari()
    c = make(fake)

    async def go():
        await c.get("user")
        fake.valid_tokens.clear()  # server forgets our token
        c._refresh = None
        return await c.get("user")

    assert run(go())["FullName"] == "Karel Test"
    assert fake.logins == 2


def test_bad_password_gives_clear_error_without_leaking_it():
    fake = FakeBakalari(password="right")
    c = make(fake)
    with pytest.raises(AuthError) as err:
        run(c.get("user"))
    assert "Špatné heslo" in str(err.value) and "pw" not in str(err.value).replace("password", "")


def test_retries_transient_errors(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    fake = FakeBakalari()
    c = make(fake)

    async def go():
        await c.get("user")  # login first so fail_next hits the data call
        fake.fail_next = [503, 502]
        return await c.get("marks")

    assert "Subjects" in run(go())


def test_gives_up_after_retries(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    fake = FakeBakalari()
    c = make(fake)

    async def go():
        await c.get("user")
        fake.fail_next = [500, 500, 500]
        await c.get("marks")

    with pytest.raises(BakalariError, match="HTTP 500"):
        run(go())


def test_cache_avoids_second_request():
    fake = FakeBakalari()
    c = make(fake, cache_ttl=60)

    async def go():
        await c.get("marks")
        await c.get("marks")

    run(go())
    assert sum(1 for _, p in fake.calls if p.endswith("/marks")) == 1


def test_komens_falls_back_to_post_and_remembers():
    fake = FakeBakalari()
    fake.komens_requires_post = True
    c = make(fake)

    async def go():
        await c.get_or_post("komens/messages/received")
        await c.get_or_post("komens/messages/received")

    run(go())
    methods = [m for m, p in fake.calls if p.endswith("received")]
    assert methods == ["GET", "POST", "POST"]


def test_event_endpoint_fallback():
    fake = FakeBakalari()
    fake.events_endpoint = "events"
    c = make(fake)
    assert "Events" in run(c.get_any(["events/my", "events"]))


# ----------------------------------------------------------------- formatters


def test_marks_averages_and_filters():
    out = fmt.fmt_marks(MARKS)
    math = next(s for s in out["summary"] if s["subject"] == "Matematika")
    # (1*3 + 2.5*1) / 4 = 1.375 ; 'N' is ignored
    assert math["computed_average"] == 1.38 or math["computed_average"] == 1.37
    assert math["school_average"] == "1,67" and math["new_marks"] == 1
    assert out["subjects"][0]["marks"][0]["date"] == "2026-10-08"  # newest first

    only_new = fmt.fmt_marks(MARKS, only_new=True)
    assert [s["subject"] for s in only_new["subjects"]] == ["Matematika"]
    assert len(only_new["subjects"][0]["marks"]) == 1

    assert [s["subject"] for s in fmt.fmt_marks(MARKS, subject="cesky")["summary"]] == ["Český jazyk a literatura"]
    assert "subjects" not in fmt.fmt_marks(MARKS, summary_only=True)
    recent = fmt.fmt_marks(MARKS, since=date(2026, 10, 5))
    assert [s["subject"] for s in recent["subjects"]] == ["Matematika"]


def test_timetable_day_is_resolved_sorted_and_flags_cancellations():
    out = fmt.fmt_timetable(TIMETABLE, day=date(2026, 10, 9))
    day = out["days"][0]
    assert day["weekday"] == "Pátek"
    first, second = day["lessons"]
    assert first["subject"] == "Matematika" and first["time"] == "8:00-8:45" and first["room"] == "A12"
    assert first["teacher"] == "Jan Novák" and first["group"] == "sk1"
    assert second["cancelled"] is True and second["change"]["description"] == "Odpadá"
    assert day["changes"] == 1


def test_timetable_missing_day_has_note():
    assert "note" in fmt.fmt_timetable(TIMETABLE, day=date(2026, 10, 14))


def test_homework_hides_done_and_sorts_by_due():
    out = fmt.fmt_homeworks(HOMEWORKS)
    assert [h["subject"] for h in out["homeworks"]] == ["Matematika", "Fyzika"]
    assert len(fmt.fmt_homeworks(HOMEWORKS, include_done=True)["homeworks"]) == 3
    soon = fmt.fmt_homeworks(HOMEWORKS, until=date(2026, 10, 16))
    assert [h["subject"] for h in soon["homeworks"]] == ["Matematika"]


def test_messages_unread_search_and_truncation():
    out = fmt.fmt_messages(MESSAGES, text_chars=100)
    assert out["unread_total"] == 1 and out["messages"][0]["id"] == "a"
    assert out["messages"][0]["text_truncated"] is True and len(out["messages"][0]["text"]) <= 100
    assert fmt.fmt_messages(MESSAGES, unread_only=True)["shown"] == 1
    assert fmt.fmt_messages(MESSAGES, search="vylet")["messages"][0]["id"] == "b"
    assert fmt.fmt_messages(MESSAGES, limit=1)["matched"] == 2
    assert fmt.fmt_messages(MESSAGES)["messages"][1]["read"] is True


def test_absence_summary():
    out = fmt.fmt_absence(ABSENCE)
    assert out["unsolved_hours"] == 4 and out["per_subject"][0]["absent_percent"] == 30.0
    assert out["per_subject"][0]["over_threshold"] is True


def test_formatters_survive_empty_and_odd_payloads():
    for f in (fmt.fmt_marks, fmt.fmt_timetable, fmt.fmt_homeworks, fmt.fmt_subjects, fmt.fmt_events,
              fmt.fmt_absence, fmt.fmt_messages, fmt.fmt_user):
        f({})
    assert fmt.fmt_messages([{"Title": "x"}])["total"] == 1


# --------------------------------------------------------------- cookie mode


def make_cookie(fake: FakeBakalari, cookie: str = "sid=good") -> BakalariClient:
    return BakalariClient(Config(url="https://school.test", cookie=cookie, cache_ttl=0), transport=fake.transport())


def test_normalize_cookie_accepts_browser_formats():
    assert normalize_cookie("Cookie: a=1; b=2") == "a=1; b=2"
    assert normalize_cookie("  a=1;\n b=2 ;") == "a=1; b=2"
    assert normalize_cookie("-H 'cookie: a=1'") == "a=1"
    for bad in ("", "   ", "a=ž"):
        with pytest.raises(ConfigError):
            normalize_cookie(bad)


def test_normalize_cookie_accepts_bare_value_and_devtools_table_row():
    assert normalize_cookie("abcDEF123") == "BakaAuth=abcDEF123"
    assert normalize_cookie("Zm9vYmFy==") == "BakaAuth=Zm9vYmFy=="  # base64 padding is not a cookie name
    assert normalize_cookie("BakaAuth	Xyz123	/	school.example.cz	Session") == "BakaAuth=Xyz123"
    assert normalize_cookie("BakaAuth=Xyz; ASP.NET_SessionId=q") == "BakaAuth=Xyz; ASP.NET_SessionId=q"


def test_cookie_config_needs_no_password_and_defaults_to_school_url(tmp_path):
    cfg = Config.load(environ={"BAKALARI_COOKIE": "Cookie: sid=1"}, env_file=tmp_path / "x")
    assert cfg.uses_cookie and cfg.cookie == "sid=1" and cfg.url == "https://school.example.cz"
    assert "sid=1" not in repr(cfg)
    with pytest.raises(ConfigError, match="BAKALARI_COOKIE"):
        Config.load(environ={}, env_file=tmp_path / "x")


def test_cookie_mode_never_logs_in_and_sends_cookie():
    fake = FakeBakalari()
    c = make_cookie(fake)
    assert run(c.get("user"))["FullName"] == "Karel Test"
    assert fake.logins == 0 and ("POST", "/api/login") not in fake.calls


def test_rejected_cookie_gives_actionable_error():
    fake = FakeBakalari()
    with pytest.raises(AuthError, match="cookie"):
        run(make_cookie(fake, "sid=expired").get("user"))


def test_diagnose_distinguishes_valid_web_session_from_api_refusal():
    fake = FakeBakalari()
    fake.accept_cookie = False
    lines = run(make_cookie(fake).diagnose())
    text = "\n".join(lines)
    assert "3.52.1" in text and "Bearer required" in text and "IS a valid web session" in text
    assert "only accepts Bearer tokens" in text
    assert "expired/invalid" in "\n".join(run(make_cookie(fake, "sid=old").diagnose()))


def test_probe_reports_structure_but_no_cell_text():
    fake = FakeBakalari()
    summary, report = run(make_cookie(fake).probe_site())
    text = "\n".join(summary + report)
    # discovered: menu link, widget url from inline JSON, table structure with header wording
    assert "/next/prubzna.aspx" in text and "/Timetable/Widget?date=x" in text
    assert "table.marks" in text and 'th "Předmět"' in text and "td.mark" in text and "x2" in text
    assert "[data-id]" in text
    # menu chrome is collapsed instead of eating the budget; data-loading calls in scripts are found
    assert "nav.bk-sidenav" in text and "chrome skipped" in text and "menuitems" not in text
    assert "Marks/Load" in text and "/Marks/Detail" in text
    # never leaked: cell texts, page title, cookie; unsafe/foreign links never followed
    for secret in ("SECRETMENU", "SECRETGRADE", "SECRETSUBJECT", "SECRETLESSON", "SECRETTITLE", "Karel Test", "sid=good"):
        assert secret not in text
    assert "logout" not in text and "evil.example" not in text
    assert not any(p == "/logout" for _, p in fake.calls)
    summary, _ = run(make_cookie(fake, "sid=old").probe_site())
    assert "not valid" in "\n".join(summary)


def test_bakaauth_cookie_is_also_offered_as_bearer_token():
    seen = []

    def handler(request):
        seen.append((request.headers.get("Cookie"), request.headers.get("Authorization")))
        return httpx.Response(200, json={"ok": True})

    cfg = Config(url="https://school.test", cookie="ASP.NET_SessionId=s; BakaAuth=tok%2B1", cache_ttl=0)
    c = BakalariClient(cfg, transport=httpx.MockTransport(handler))
    run(c.get("user"))
    assert seen == [("ASP.NET_SessionId=s; BakaAuth=tok%2B1", "Bearer tok+1")]


def test_probe_helpers_report_json_keys_but_never_values():
    from bakalari_mcp import probe

    script = ('var m = JSON.parse(\'{"Items":[{"Subject":"SECRETSUBJ","Done":false,"Due":"2026-10-12"}],"Total":3,'
              '"Padding":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}\');'
              'var cfg = {"url": "/HomeWorks/Load", "columns": [{"caption":"SECRETCAPTION","dataField":"Subject"}], "pad": "yyyyyyyyyyyyyyyyyyyyyyyyyyyy"};')
    structures = probe.inline_json_structures([script])
    text = "\n".join("\n".join(s) for s in structures)
    assert "Items: list" in text and "Subject: str" in text and "Done: bool" in text
    assert "SECRETSUBJ" not in text and "2026-10-12" not in text
    opts = probe.options_structures('<div data-options=\'{"dataSource":{"url":"/Absence/GetData"},"x":1}\'></div>')
    assert "dataSource" in opts[0] and "/Absence/GetData" in opts[0]
    assert "/HomeWorks/Load" in probe.routes_in(script, "homeworks")
    assert probe.page_keyword("/HomeWorks/Student") == "homeworks" and probe.page_keyword("/next/komens.aspx?l=o") == "komens"
    assert probe.routes_in('x="Komens/GetList"; y="Other/Thing"; z="/next/img/a.png"', "komens") == {"Komens/GetList"}
    dates = probe.timetable_dates("2026-10-09T00:00:00")
    assert len(dates) == 8 and dates[0].startswith("2026-10-05") and dates[0].endswith("T00:00:00.000Z")


def test_keepalive_test_logs_lifetimes_and_cookie_names_but_never_values():
    from bakalari_mcp.probe import keepalive_test

    fake = FakeBakalari()
    lines: list[str] = []
    ok = run(keepalive_test("https://school.test", "sid=good", 0, 0, lines.append, transport=fake.transport()))
    text = "\n".join(lines)
    assert ok is True and "finished" in text
    assert "880.0" in text and "900.0" in text and "BakaAuth(Max-Age=3600)" in text
    assert "BakaAuth" in text and "cookie values changed" in text
    assert "ROTATEDSECRET" not in text and "sid=good" not in text

    fake.session_dead = True
    lines.clear()
    assert run(keepalive_test("https://school.test", "sid=good", 0, 5, lines.append, transport=fake.transport())) is False
    assert "SESSION DIED" in "\n".join(lines)


# ------------------------------------------------- website marks page (cookie mode)


def _marks_sample() -> str:
    from pathlib import Path

    return (Path(__file__).parent / "pages" / "marks_sample.html").read_text(encoding="utf-8")


def test_marks_page_parser_reads_subjects_marks_and_the_schools_average():
    from bakalari_mcp.web import parse_marks_page

    data = parse_marks_page(_marks_sample())
    names = [s["Subject"]["Name"] for s in data["Subjects"]]
    assert names == ["Matematika", "Český jazyk & literatura", "Tělesná výchova"]
    math = data["Subjects"][0]
    assert math["AverageText"] == "1,75" and len(math["Marks"]) == 2
    first = math["Marks"][0]
    assert first["MarkDate"] == "2026-10-06T00:00:00" and first["MarkText"] == "1" and first["Weight"] == 3
    assert first["Caption"] == "Test zlomky" and first["IsNew"] is True and first["ClassRank"].startswith("3.")
    assert data["Subjects"][2]["Marks"] == [] and data["Subjects"][2]["AverageText"] == ""
    assert parse_marks_page("<html>nothing here</html>") == {"Subjects": []}


def test_marks_from_the_page_flow_through_the_normal_formatter():
    from bakalari_mcp.web import parse_marks_page

    out = fmt.fmt_marks(parse_marks_page(_marks_sample()))
    math = next(s for s in out["summary"] if s["subject"] == "Matematika")
    assert math["school_average"] == "1,75"  # the invented school figure is shown as given
    assert math["computed_average"] in (1.37, 1.38) and math["new_marks"] == 1  # (1*3 + 2.5*1) / 4
    czech = out["subjects"][1]["marks"]
    assert [m["mark"] for m in czech] == ["4", "N"] or [m["mark"] for m in czech] == ["N", "4"]
    note_row = next(m for m in out["subjects"][0]["marks"] if m.get("note"))
    assert note_row["note"] == "pochvala" and "class_rank" in note_row


def test_mark_notes_lose_stray_html_tags():
    import json
    from html import escape

    from bakalari_mcp.web import parse_marks_page

    blob = escape(json.dumps({"caption": "REF", "MarkText": "5", "vaha": 3, "strdatum": "5.10.2026",
                              "poznamkakzobrazeni": "Referát na téma Strunatci <br>"}), quote=True)
    page = (f'<div id="1" class="predmet-radek"><h3>Přírodopis</h3><div class="znamky">'
            f"<div class=\"znamka-v\" data-clasif='{blob}'></div></div></div>")
    assert parse_marks_page(page)["Subjects"][0]["Marks"][0]["Note"] == "Referát na téma Strunatci"
