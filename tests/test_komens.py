"""Messages and noticeboard in cookie mode, against a fake school website in the real markup (invented data)."""

import asyncio
import json

import httpx
import pytest

from bakalari_mcp import server
from bakalari_mcp import cookiestore, komens
from bakalari_mcp.client import BakalariClient
from bakalari_mcp.config import Config
from test_web_tools import TODAY, site as base_site


def list_row(idmsg, read, sender, when, title, preview, clip=False):
    hidden = "" if clip else "visibility:hidden;"
    return (
        f'<li data-testid="komens-messages-item"><table data-idmsg="{idmsg}" data-sortdate="0:{when}" '
        f'data-msgtype="OBECNA" data-read="{str(read).lower()}" data-confirmed="true" data-canConfirm="False" '
        f'class="needsclick link message_list_table_item" onclick="collapseAction()"><tr><td class="x"></td><td>'
        f"<i class=\"ico20-zpravy-sponka bk-font-20\" style='{hidden}'></i></td><td>"
        f'<div class="message_list_header_wrapper"><div class="message_list_header"> <div>{sender}</div> '
        f"<div class=\"message_list_recipients_count\" style='display:none'>+ 0</div></div>"
        f'<span class="nowrap">1.1.2026 10:00</span></div></td></tr><tr><td class="message_list_preview" colspan="2">'
        f'<div class="message_list_previewtext_title"><div class="message_list_title">{title}</div></div>'
        f'<div class="message_list_previewtext_detail"> {preview} </div></td></tr></table></li>'
    )


ROWS = [
    list_row(201, True, "Mgr. Jana Nov&#225;", "2026-10-07T10:43:58", "Logick&#225; olympi&#225;da", "Vážení žáci&hellip;", True),
    list_row(202, False, "Škola", "2026-10-08T08:00:00", "Nepřečtená zpráva", "Ahoj"),
    list_row(203, False, "Ředitelství", "2026-09-01T08:00:00", "Starší novinka", "Začátek roku"),
]
DETAILS = {
    "201": {"Title": "Logická olympiáda", "Jmeno": "Mgr. Jana Nová", "Files": [{"id": "9", "name": "pokyny.pdf"}],
            "MessageText": "<div>Vážení žáci,</div><div><br></div><div>sejdeme se&nbsp;v úterý.</div>"},
    "202": {"Title": "Nepřečtená zpráva", "Jmeno": "Škola", "Files": [], "MessageText": "Ahoj <b>všichni</b>"},
    "203": {"Title": "Starší novinka", "Jmeno": "Ředitelství", "Files": [], "MessageText": "Začátek roku"},
}
CALLS: list[str] = []
DETAIL_LOGGED_OUT = False  # True: the school ends the session when a message text is requested


def dx_answer(rows, error=None):
    if error:
        return "5|abcde/*DX*/({'error':{'message':'" + error + "'},'result':''})"
    html = '<ul data-testid="komens-messages-wrapper">\r\n' + "".join(rows) + "</ul>"
    escaped = html.replace("\\", "\\\\").replace("'", "\\'").replace("\r", "\\r").replace("\n", "\\n")
    return "5|abcde/*DX*/({'id':0,'result':'" + escaped + "'})"


def site(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.headers.get("Cookie") != "sid=good" or not path.startswith(("/next/komens", "/Komens/", "/next/nastenky", "/Noticeboard/")):
        return base_site(request)
    CALLS.append(f"{request.method} {path}")
    if path == "/next/komens.aspx" and request.method == "GET":
        kind = "odeslane" if request.url.params.get("l") == "o" else "prijate"
        return httpx.Response(200, text=(
            '<form id="formbaka"><input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="vs" />'
            f'<input type="hidden" name="ctl00$cphmain$hfListType" value="{kind}" /></form>'))
    if path == "/next/komens.aspx":
        form = {k: v[0] for k, v in __import__("urllib.parse").parse.parse_qs(request.content.decode()).items()}
        assert form["__CALLBACKID"] == "ctl00$cphmain$message_list_content"
        assert form["__VIEWSTATE"] == "vs" and form["__CALLBACKPARAM"].startswith("c0:")
        arg = json.loads(form["__CALLBACKPARAM"][3:])
        rows = [r for r in ROWS if 'data-read="false"' in r] if arg["onlyUnread"] else ROWS
        rows = rows[: arg["pageSize"]]  # like the school: the page size is honoured, the page number is not
        return httpx.Response(200, text=dx_answer(rows))
    if path == "/Komens/GetMessageDataAsync":
        if DETAIL_LOGGED_OUT:
            return httpx.Response(302, headers={"Location": "/login?ReturnUrl=%2Fnext%2Fkomens.aspx"})
        body = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": DETAILS[body["idmsg"]]})
    if path == "/next/nastenky.aspx":
        tabs = '[{"id":"_all","kod":"@nast","typ":0,"nazev":"Vše"},{"id":"1L","typ":3,"nazev":"7.X"}]'
        return httpx.Response(200, text=f"<script>ASPx.createControl(x,'a','b',{{'cpTabs':'{tabs}','emptyHeight':true}});</script>")
    if path == "/Noticeboard/GetPostsPageAsync":
        body = json.loads(request.content)
        assert body["board"]["id"] == "_all" and body["page"] >= 1
        post = {"Id": "7", "CaptionText": "Třídní schůzky", "SenderFullName": "Mgr. Nová", "DateSendText": "5.10.2026",
                "CetlJsem": False, "MessageText": "<div>Přijďte&nbsp;včas</div>", "Files": []}
        return httpx.Response(200, json={"success": True, "data": [post] if body["page"] == 1 else []})
    return httpx.Response(404)


@pytest.fixture
def web(tmp_path, monkeypatch):
    store = tmp_path / "session.json"
    cookiestore.save(store, "sid=good")
    config = Config(url="https://school.test", cookie_file=store, cache_ttl=0)
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_client", BakalariClient(config, transport=httpx.MockTransport(site)))
    monkeypatch.setattr(server, "today", lambda tz_name="x": TODAY)
    CALLS.clear()
    yield
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_client", None)


def data(name, args=None):
    result = asyncio.run(server.mcp.call_tool(name, args or {}))
    return result.structured_content if getattr(result, "structured_content", None) else json.loads(result.content[0].text)


def test_received_messages_have_full_text_unread_flags_and_never_mark_anything_read(web):
    out = data("get_messages_received", {"limit": 2})
    assert out["total"] == 3 and out["unread_total"] == 2 and out["shown"] == 2
    first = out["messages"][0]
    assert first["title"] == "Nepřečtená zpráva" and first["read"] is False and first["text"] == "Ahoj všichni"
    olympiad = data("get_messages_received", {"search": "olympiáda"})["messages"][0]
    assert olympiad["from"] == "Mgr. Jana Nová" and olympiad["attachments"] == ["pokyny.pdf"]
    assert olympiad["text"] == "Vážení žáci,\n\nsejdeme se v úterý."
    assert olympiad["sent"] == "2026-10-07 10:43"
    assert not any("SetMessage" in c for c in CALLS)  # reading must not mark anything as read


def test_unread_only_and_search_filters(web):
    unread = data("get_messages_received", {"unread_only": True})
    assert sorted(m["id"] for m in unread["messages"]) == ["202", "203"] and unread["total"] == 2
    assert data("get_messages_received", {"search": "ředitelství"})["matched"] == 1


def test_limit_only_fetches_the_texts_it_shows(web):
    data("get_messages_received", {"limit": 1})
    assert sum(1 for c in CALLS if c.endswith("GetMessageDataAsync")) == 1


def test_sent_messages_use_the_sent_list_and_the_noticeboard_reads_all_boards(web):
    assert data("get_messages_sent")["total"] == 3
    post = data("get_noticeboard")["messages"][0]
    assert post["title"] == "Třídní schůzky" and post["sent"] == "2026-10-05"
    assert post["text"] == "Přijďte včas" and post["read"] is False


def test_the_overview_lists_unread_messages(web):
    assert data("get_overview", {"date": "2026-10-05"})["unread_messages"]["unread_total"] == 2


def test_a_callback_error_is_reported_and_html_is_turned_into_text():
    with pytest.raises(Exception, match="could not load the messages"):
        komens.callback_result(dx_answer([], error="Zpětn&#233;ho vol&#225;n&#237; se nezdařilo"))
    assert komens.html_to_text("<div>a</div><div><br></div><div>b&amp;c</div>") == "a\n\nb&c"


def test_raw_day_timetable_contains_only_that_day(web):
    raw = data("get_timetable_day", {"date": "2026-10-06", "raw": True})
    assert len(raw["Days"]) == 1 and raw["Days"][0]["Date"].startswith("6.10")


def test_the_whole_list_is_read_in_one_call_so_old_messages_are_counted_and_searchable(web, monkeypatch):
    old = [list_row(900 + i, True, "Archiv", f"2025-01-{i + 1:02d}T08:00:00", f"Stará zpráva {i:02d}", "Starý text")
           for i in range(30)]
    monkeypatch.setattr("test_komens.ROWS", ROWS + old)
    for i in range(30):
        monkeypatch.setitem(DETAILS, str(900 + i), {"Title": f"Stará zpráva {i:02d}", "Jmeno": "Archiv", "Files": [],
                                                    "MessageText": f"Text {i:02d}"})
    out = data("get_messages_received", {"limit": 1})
    assert out["total"] == 33 and out["unread_total"] == 2 and out["shown"] == 1
    found = data("get_messages_received", {"search": "zpráva 07"})
    assert found["matched"] == 1 and found["messages"][0]["text"] == "Text 07"


def test_a_session_that_ends_while_texts_are_read_is_reported_not_replaced_by_previews(web, monkeypatch):
    monkeypatch.setattr("test_komens.DETAIL_LOGGED_OUT", True)
    with pytest.raises(Exception, match="logged the session out"):
        data("get_messages_received", {"limit": 2})
