"""End-to-end through the MCP layer against the fake API."""

import asyncio

import pytest

from bakalari_mcp import server
from bakalari_mcp.client import BakalariClient
from bakalari_mcp.config import Config

from fake_api import FakeBakalari


@pytest.fixture
def fake(monkeypatch):
    fake = FakeBakalari()
    cfg = Config(url="https://school.test", username="kid", password="pw", cache_ttl=0)
    monkeypatch.setattr(server, "_config", cfg)
    monkeypatch.setattr(server, "_client", BakalariClient(cfg, transport=fake.transport()))
    return fake


def call(name, args=None):
    return asyncio.run(server.mcp.call_tool(name, args or {}))


def test_all_tools_registered_with_read_only_hint():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"get_overview", "get_marks", "get_timetable_day", "get_homeworks", "get_messages_received",
            "get_user", "get_subjects", "get_events", "get_absence", "get_noticeboard",
            "get_messages_sent", "get_timetable_actual", "get_timetable_permanent"} <= names
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


def test_prompts_registered():
    names = {p.name for p in asyncio.run(server.mcp.list_prompts())}
    assert {"daily_briefing", "homework_plan", "marks_report"} <= names


def test_marks_tool_with_arguments(fake):
    result = call("get_marks", {"subject": "mat", "summary_only": True})
    assert "Matematika" in str(result)


def test_timetable_day_tool(fake):
    assert "Odpadá" in str(call("get_timetable_day", {"date": "2026-10-09"}))


def test_raw_flag(fake):
    assert "FullName" in str(call("get_user", {"raw": True}))


def test_events_filtered_to_future(fake):
    text = str(call("get_events", {"days_ahead": 366}))
    assert "Starý výlet" not in text


def test_overview_collects_everything(fake):
    text = str(call("get_overview", {"date": "2026-10-09"}))
    for needle in ("Matematika", "Třídní schůzky", "unsolved_hours", "next_school_day"):
        assert needle in text


def test_overview_reports_partial_failure(fake, monkeypatch):
    orig = fake.handle

    def handle(request):
        if request.url.path.endswith("/absence/student") and request.url.path != "/api/login":
            import httpx
            return httpx.Response(403)
        return orig(request)

    monkeypatch.setattr(fake, "handle", handle)
    monkeypatch.setattr(server, "_client", BakalariClient(server._config, transport=fake.transport()))
    text = str(call("get_overview", {"date": "2026-10-09"}))
    assert "no permission" in text and "Třídní schůzky" in text


def test_errors_become_tool_errors_not_crashes(monkeypatch):
    fake = FakeBakalari(password="other")
    cfg = Config(url="https://school.test", username="kid", password="pw", cache_ttl=0)
    monkeypatch.setattr(server, "_config", cfg)
    monkeypatch.setattr(server, "_client", BakalariClient(cfg, transport=fake.transport()))
    with pytest.raises(Exception) as err:
        call("get_user")
    assert "rejected the login" in str(err.value)


def test_bad_date_is_a_readable_error(fake):
    with pytest.raises(Exception) as err:
        call("get_timetable_day", {"date": "someday"})
    assert "Cannot understand date" in str(err.value)
