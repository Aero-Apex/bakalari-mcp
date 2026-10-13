"""Komens messages and the noticeboard through the website (cookie mode).

The message list is a DevExpress/ASP.NET *callback* (a form POST that answers with a JavaScript object holding
HTML); a message's full text and the noticeboard are plain JSON endpoints. Everything is converted to the
REST-style shape `formatters.fmt_messages` understands. Nothing here changes state: the website marks a message as
read through a separate call (`SetMessageReadAsync`) that this module never makes.
"""

from __future__ import annotations

import asyncio
import json
import re
from html import unescape

from .client import AuthError, BakalariClient, BakalariError
from .util import fold

LIST_PAGE = "/next/komens.aspx"
LIST_PAGES = {"received": LIST_PAGE, "sent": f"{LIST_PAGE}?l=o"}
LIST_PANEL = "ctl00$cphmain$message_list_content"
DETAIL_URL = "/Komens/GetMessageDataAsync"
LIST_TYPES = {"received": "prijate", "sent": "odeslane"}
NOTICEBOARD_PAGE = "/next/nastenky.aspx"
NOTICEBOARD_URL = "/Noticeboard/GetPostsPageAsync"
LIST_ROWS = 1000  # the callback ignores its page number, so one call has to ask for the whole list
MAX_DETAILS = 40  # full texts fetched per call when searching (one request each)
DETAIL_PARALLEL = 4  # full texts requested at the same time, so the school website is not flooded

_HIDDEN = re.compile(r"<input\b[^>]*>")
_ATTR = re.compile(r'(\w[\w-]*)="([^"]*)"')


def hidden_fields(html: str) -> dict[str, str]:
    """The hidden form fields (view state etc.) an ASP.NET callback has to send back."""
    fields: dict[str, str] = {}
    for tag in _HIDDEN.findall(html):
        attrs = dict(_ATTR.findall(tag))
        if attrs.get("type") == "hidden" and attrs.get("name"):
            fields[attrs["name"]] = unescape(attrs.get("value", ""))
    return fields


_JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_JS_SIMPLE = {"r": "\r", "n": "\n", "t": "\t"}


def _js_unescape(match: re.Match) -> str:
    code = match.group(1)
    if code[0] in "ux" and len(code) > 1:
        return chr(int(code[1:], 16))
    return _JS_SIMPLE.get(code, code)


def callback_result(text: str) -> str:
    """The HTML inside an ASP.NET/DevExpress callback answer ('<n>|<validation><n chars>/*DX*/({...})')."""
    length, sep, rest = text.partition("|")
    body = rest[int(length):] if sep and length.isdigit() else text
    error = re.search(r"'error':\{'message':'(.*?)'\}", body, re.S)
    if error:
        raise BakalariError("The school website could not load the messages: "
                            + unescape(_JS_ESCAPE.sub(_js_unescape, error.group(1))))
    start = body.find("'result':'")
    end = body.rfind("'})")
    if start < 0 or end < start:
        raise BakalariError("The school website answered the message list in an unexpected format.")
    return _JS_ESCAPE.sub(_js_unescape, body[start + len("'result':'"):end])


def _clean(text: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def html_to_text(html: str) -> str:
    """A message body (HTML) -> readable text with line breaks kept."""
    text = re.sub(r"(?i)<br\s*/?>|</(?:div|p|li|h\d|tr)>", "\n", html or "")
    text = unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_ITEM = re.compile(r"<table\b[^>]*\bdata-idmsg=.*?</table>", re.S)


def parse_message_list(html: str) -> list[dict]:
    """The rows of the message list -> dictionaries (no full text yet, only the preview)."""
    rows = []
    for block in _ITEM.findall(html):
        head = block[: block.index(">") + 1]
        attrs = {k.lower(): v for k, v in _ATTR.findall(head)}
        sender = re.search(r'message_list_header">\s*<div>(.*?)</div>', block, re.S)
        when = re.search(r'<span class="nowrap">(.*?)</span>', block, re.S)
        title = re.search(r'message_list_title">(.*?)</div>', block, re.S)
        preview = re.search(r'message_list_previewtext_detail">(.*?)</div>', block, re.S)
        clip_icon = re.search(r'<i[^>]*ico20-zpravy-sponka[^>]*>', block)
        sort_date = attrs.get("data-sortdate", "")
        rows.append({
            "Id": attrs.get("data-idmsg"),
            "Kind": attrs.get("data-msgtype"),
            "SentDate": sort_date.split(":", 1)[-1] if ":" in sort_date else sort_date,
            "SentText": _clean(when.group(1)) if when else "",
            "Read": attrs.get("data-read", "").lower() == "true",
            "Sender": _clean(sender.group(1)) if sender else "",
            "Title": _clean(title.group(1)) if title else "",
            "Preview": _clean(preview.group(1)) if preview else "",
            "HasAttachments": bool(clip_icon and "hidden" not in clip_icon.group(0)),
        })
    return rows


async def _list(c: BakalariClient, kind: str, unread_only: bool) -> list[dict]:
    page = LIST_PAGES[kind]
    fields = hidden_fields(await c.get_html(page, ttl=0))
    arg = {
        "showRepressed": False, "pageNumber": 0, "pageSize": LIST_ROWS, "reloadMessages": True,
        "sortOrder": 0, "onlyRating": True, "onlyExcuses": True, "onlySystemMessages": True,
        "onlyGeneralMessages": True, "onlyUnread": bool(unread_only),
    }
    form = {
        **fields, "__CALLBACKID": LIST_PANEL, "__CALLBACKPARAM": "c0:" + json.dumps(arg, separators=(",", ":")),
        "__EVENTTARGET": "", "__EVENTARGUMENT": "",
    }
    answer = await c.web_post_form(page.split("?")[0], form)
    return parse_message_list(callback_result(answer))


def _light(row: dict, kind: str) -> dict:
    """A message known only from the list row (preview text, no extra request)."""
    return {
        "Id": row["Id"], "Title": row["Title"], "Sender": row["Sender"],
        "RecipientsText": row["Sender"] if kind == "sent" else None,
        "SentDate": row["SentDate"], "Read": row["Read"], "Text": row["Preview"],
        "Attachments": [{"Name": "(attachment)"}] if row["HasAttachments"] else [],
    }


async def _detail(c: BakalariClient, row: dict, kind: str) -> dict:
    try:
        data = await c.web_json(
            "POST", DETAIL_URL, json_body={"idmsg": str(row["Id"]), "komensMessageListType": LIST_TYPES[kind]}
        )
    except AuthError:
        raise  # a session that ended is not a missing text: report it instead of showing previews
    except BakalariError:
        return _light(row, kind)  # fall back to the preview rather than failing the whole list
    data = data if isinstance(data, dict) else {}
    files = [f for f in data.get("Files") or [] if isinstance(f, dict)]
    light = _light(row, kind)
    return {
        **light,
        "Title": data.get("Title") or data.get("CaptionText") or row["Title"],
        "Sender": data.get("Jmeno") or row["Sender"],
        "Text": html_to_text(data.get("MessageText") or "") or row["Preview"],
        "Attachments": [{"Name": f.get("name") or f.get("Name")} for f in files] or light["Attachments"],
    }


async def messages(c: BakalariClient, kind: str, limit: int, unread_only: bool, search: str | None) -> dict:
    """Received or sent messages in the shape of the REST API (`{'Messages': [...]}`).

    Full texts cost one request each, so only the messages that can be shown are fetched in full (when
    searching: a bounded number); the rest are counted from their list rows.
    """
    rows = await _list(c, kind, unread_only)
    if unread_only:
        rows = [r for r in rows if not r["Read"]]
    needle = fold(search) if search else None
    fetch = MAX_DETAILS if needle else limit
    gate = asyncio.Semaphore(DETAIL_PARALLEL)

    async def full_text(row: dict) -> dict:
        async with gate:
            return await _detail(c, row, kind)

    full = await asyncio.gather(*(full_text(r) for r in rows[:fetch]))
    out = list(full) + [_light(r, kind) for r in rows[fetch:]]
    if needle:
        out = [m for m in out if needle in fold(f"{m['Title']} {m['Text']} {m['Sender']}")]
    return {"Messages": out}


def _board_tabs(page: str) -> list[dict]:
    match = re.search(r"'cpTabs':'(\[.*?\])'", page, re.S)
    try:
        return json.loads(unescape(match.group(1))) if match else []
    except ValueError:
        return []


async def noticeboard(c: BakalariClient, limit: int) -> dict:
    """Noticeboard posts of all boards ('Vše' tab), in the REST shape."""
    tabs = _board_tabs(await c.get_html(NOTICEBOARD_PAGE, ttl=0))
    board = next((t for t in tabs if t.get("id") == "_all"), tabs[0] if tabs else None)
    if board is None:
        raise BakalariError("The noticeboard page has no boards (unexpected page layout).")
    posts: list[dict] = []
    for page in range(1, 6):
        body = {"board": board, "page": page, "pageSize": 21, "onlyValidForMe": True}
        chunk = await c.web_json("POST", NOTICEBOARD_URL, json_body=body, ttl=0) or []
        posts += [p for p in chunk if isinstance(p, dict)]
        if len(chunk) < 21 or len(posts) >= limit:
            break
    out = []
    for p in posts:
        sent = p.get("DateSendText") or ""
        m = re.match(r"\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", sent)
        iso = (f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
               + (f"T{int(m.group(4)):02d}:{m.group(5)}:00" if m.group(4) else "T00:00:00")) if m else sent
        files = [f for f in p.get("Files") or [] if isinstance(f, dict)]
        out.append({
            "Id": p.get("Id"),
            "Title": p.get("CaptionText"),
            "Sender": p.get("SenderFullName"),
            "SentDate": iso,
            "Read": bool(p.get("CetlJsem")),
            "Text": html_to_text(p.get("MessageText") or ""),
            "Attachments": [{"Name": f.get("name") or f.get("Name")} for f in files],
        })
    return {"Messages": out}
