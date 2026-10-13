"""Discovery of how the Bakaláři *website* is built (cookie mode), without leaking personal data.

`build_report()` fetches the dashboard, the pages it links to and its widgets, and writes only
their STRUCTURE: tag / id / class names, data-attribute names, table-header and button labels.
Cell texts (grades, names, messages) are never included.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
SKIP = {"script", "style", "svg", "noscript", "head", "template"}
CHROME_TEXT = {"th", "label", "button", "legend", "caption"}  # UI wording, not data
DENY_LINK = re.compile(r"logout|odhla|signout|sign-out|exit|konec|smaz|delete|remove|zrus|cancel|mailto:|javascript:", re.I)
MAX_LINES = 260
MAX_DEPTH = 22
# shared page chrome (menu, top bar, DevExpress popups) would use up the whole budget on every page
CHROME_CLASS = ("bk-menu", "bk-sidenav", "bk-navtop", "dxpc", "navbar", "simplebar")
CHROME_ID = ("sidebar", "simplebarContent", "_menu_nav", "pagePopups", "pcConfirmDialog", "generalWarning")
# pages worth reading: everything the MCP tools need
WANTED = re.compile(
    r"^/(next/(prubzna|vychovna|komens|nastenky)\.aspx|Marks/|Timetable|HomeWorks/|Teaching/Subjects|"
    r"Absence/|core/Events/|Dashboard/\w+Widget|Services/Widget)", re.I)


class Node:
    __slots__ = ("tag", "ident", "classes", "data", "extra", "text", "children")

    def __init__(self, tag: str, attrs: dict[str, str | None] | None = None):
        attrs = attrs or {}
        self.tag = tag
        self.ident = attrs.get("id") or ""
        self.classes = tuple((attrs.get("class") or "").split())
        self.data = tuple(sorted(k for k in attrs if k.startswith("data-")))
        self.extra = tuple(k for k in ("href", "onclick", "name", "type", "role") if k in attrs)
        if attrs.get("type"):
            self.extra = tuple(e for e in self.extra if e != "type") + (f"type={attrs['type']}",)
        self.text = ""
        self.children: list[Node] = []

    def signature(self) -> tuple:
        return (self.tag, self.classes, self.data, self.extra)

    def label(self) -> str:
        s = self.tag + (f"#{self.ident}" if self.ident else "") + "".join("." + c for c in self.classes)
        if self.data:
            s += " [" + ",".join(self.data) + "]"
        if self.extra:
            s += " {" + ",".join(self.extra) + "}"
        if self.text:
            s += f' "{self.text[:30]}"'
        return s


class _Skeleton(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.skip_tag: str | None = None
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in SKIP:
            self.skip_tag, self.skip_depth = tag, 1
            return
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if not self.skip_tag and tag not in SKIP:
            self.stack[-1].children.append(Node(tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        node = self.stack[-1]
        if not self.skip_tag and node.tag in CHROME_TEXT and data.strip():
            node.text = (node.text + " " + " ".join(data.split())).strip()


def html_skeleton(html: str) -> list[str]:
    parser = _Skeleton()
    parser.feed(html)
    lines: list[str] = []

    def walk(node: Node, depth: int) -> None:
        if len(lines) >= MAX_LINES or depth > MAX_DEPTH:
            return
        i = 0
        kids = node.children
        while i < len(kids):
            j = i
            while j + 1 < len(kids) and kids[j + 1].signature() == kids[i].signature():
                j += 1
            count = j - i + 1
            first = kids[i]
            is_chrome = (
                first.tag in ("nav", "footer")
                or (first.tag == "header" and not first.classes)
                or any(c.startswith(CHROME_CLASS) for c in first.classes)
                or first.ident.startswith(CHROME_ID)
            )
            lines.append("  " * depth + first.label() + (f"  x{count}" if count > 1 else "") + ("  [chrome skipped]" if is_chrome else ""))
            if len(lines) >= MAX_LINES:
                return
            if not is_chrome:
                walk(first, depth + 1)
            i = j + 1

    walk(parser.root, 0)
    if len(lines) >= MAX_LINES:
        lines.append("... (truncated)")
    return lines


def json_skeleton(value: Any, depth: int = 0) -> list[str]:
    pad = "  " * depth
    if isinstance(value, dict):
        out: list[str] = []
        for k, v in list(value.items())[:40]:
            kind = type(v).__name__
            out.append(f"{pad}{k}: {kind}")
            if isinstance(v, (dict, list)) and depth < 4:
                out += json_skeleton(v, depth + 1)
        return out
    if isinstance(value, list):
        return [f"{pad}[{len(value)} items]"] + (json_skeleton(value[0], depth + 1) if value and depth < 4 else [])
    return []


def extract_links(html: str, base_url: str) -> list[str]:
    """Same-origin links and widget URLs on the dashboard (paths only)."""
    found: list[str] = []
    for m in re.findall(r"""href=["']([^"'#]+)["']""", html, re.I):
        found.append(m)
    for m in re.findall(r'''["']?Url["']?\s*:\s*["']([^"']+)["']''', html):
        found.append(m.replace("\\u002f", "/").replace("\\/", "/"))
    for m in re.findall(r"""data-(?:url|href)=["']([^"']+)["']""", html, re.I):
        found.append(m)
    out: dict[str, None] = {}
    base_host = urlparse(base_url).netloc
    for raw in found:
        if DENY_LINK.search(raw) or re.search(r"\.(css|js|png|jpg|svg|ico|woff2?|gif)(\?|$)", raw, re.I):
            continue
        absolute = urljoin(base_url, raw)
        parsed = urlparse(absolute)
        if parsed.netloc != base_host:
            continue
        out[parsed.path + (f"?{parsed.query}" if parsed.query else "")] = None
    return list(out)


THIRD_PARTY = re.compile(
    r"thirdparty|jquery|devexpress|/dx[.\-_/]|cookieconsent|localization|moment|knockout|bootstrap|popper|simplebar|"
    r"fullcalendar|chart|polyfill|session-popup|reports-popup", re.I)
ROUTE = re.compile(
    r"""["'`]((?:~?/|\.\./)?[A-Za-z][\w\-]*(?:/[\w\-{}$:.]+){1,5}(?:\.aspx(?:/\w+)?)?(?:\?[\w=&{}$.\-]*)?)["'`]""")
APP_ROOT_CALL = re.compile(r"""appRoot\s*\+\s*["']([^"']+)["']""")
AJAX_CALL = re.compile(r"""(?:\$\.(?:get|post|ajax|getJSON)|fetch)\(\s*["'`]([^"'`]+)["'`]|\burl\s*:\s*["'`]([^"'`]+)["'`]""")
ASPX_METHOD = re.compile(r"""["'`]([\w/.\-]*\.aspx/\w+)["'`]""")


def page_keyword(path: str) -> str:
    """'/HomeWorks/Student' -> 'homeworks', '/next/komens.aspx' -> 'komens'."""
    parts = [p for p in re.split(r"[/?]", path) if p and p.lower() not in ("next", "core")]
    return re.sub(r"\.aspx$", "", parts[0], flags=re.I).lower() if parts else ""


def routes_in(text: str, keyword: str) -> set[str]:
    """URL-like strings in a script: every explicit ajax call plus routes that mention the page keyword."""
    found: set[str] = set()
    for pat in (APP_ROOT_CALL, ASPX_METHOD):
        found.update(m for m in pat.findall(text))
    for m in AJAX_CALL.findall(text):
        found.add(m[0] or m[1])
    if keyword:
        found.update(m for m in ROUTE.findall(text) if keyword in m.lower())
    return {m for m in found if 3 < len(m) < 140 and not DENY_LINK.search(m)
            and not re.search(r"\.(css|js|png|svg|gif|jpg|ico|woff2?|html?)(\?|$)|^https?:|^text/|^application/", m, re.I)}


def structure_of(value: Any, depth: int = 0, max_depth: int = 5) -> list[str]:
    """Keys and value types only - never values."""
    pad = "  " * depth
    if isinstance(value, dict):
        out: list[str] = []
        for k, v in list(value.items())[:40]:
            out.append(f"{pad}{str(k)[:40]}: {type(v).__name__}")
            if isinstance(v, (dict, list)) and depth < max_depth:
                out += structure_of(v, depth + 1, max_depth)
        return out
    if isinstance(value, list):
        head = f"{pad}[{len(value)} items]"
        return [head] + (structure_of(value[0], depth + 1, max_depth) if value and depth < max_depth else [])
    return []


def _json_after(text: str, start: int) -> Any | None:
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except ValueError:
        return None


def inline_json_structures(scripts: list[str]) -> list[list[str]]:
    """Structure of JSON objects embedded in inline scripts (JSON.parse('...') or literals)."""
    blobs: list[Any] = []
    for text in scripts:
        for m in re.finditer(r"""JSON\.parse\(\s*(['"])(.*?)(?<!\\)\1\s*\)""", text, re.S):
            body = m.group(2)
            if len(body) < 80:
                continue
            body = body.replace("\\'", "'") if m.group(1) == "'" else body
            body = re.sub(r'(?<!\\)"', r'\\"', body) if m.group(1) == "'" else body
            try:
                blobs.append(json.loads(json.loads('"' + body + '"')))
            except ValueError:
                continue
        for m in re.finditer(r"[=:(,]\s*([\[{])", text):
            if text[m.start(1): m.start(1) + 8].strip() in ("{}", "[]"):
                continue
            value = _json_after(text, m.start(1))
            if isinstance(value, (dict, list)) and len(json.dumps(value)) > 150:
                blobs.append(value)
    seen: set[str] = set()
    out: list[list[str]] = []
    for blob in blobs:
        skeleton = structure_of(blob)
        key = "\n".join(skeleton)
        if skeleton and key not in seen:
            seen.add(key)
            out.append(skeleton)
    return out[:6]


def options_structures(page_html: str) -> list[str]:
    """Keys of data-options JSON (widget configuration) plus any URL-looking string values."""
    out: list[str] = []
    for dq, sq in re.findall(r"""data-options=(?:"([^"]*)"|'([^']*)')""", page_html)[:40]:
        raw = dq or sq
        try:
            value = json.loads(unescape(raw))
        except ValueError:
            continue
        urls = sorted({s for s in re.findall(r'"([^"]*(?:/|\.aspx)[^"]*)"', json.dumps(value)) if 3 < len(s) < 120})
        keys = ",".join(structure_of(value, 0, 1)[:14]).replace("\n", " ")
        line = f"data-options: {keys}" + (f"  urls={urls[:6]}" if urls else "")
        if line not in out:
            out.append(line)
    return out[:12]


def timetable_dates(today_iso: str) -> list[str]:
    day = datetime.fromisoformat(today_iso[:10])
    monday = day - timedelta(days=day.weekday())
    dates = [monday + timedelta(days=i) for i in range(0, 4)] + [monday + timedelta(days=7 + i) for i in range(0, 4)]
    return [d.strftime("%Y-%m-%dT00:00:00.000Z") for d in dates]


async def build_report(http: httpx.AsyncClient, cookie: str, today_iso: str) -> tuple[list[str], list[str]]:
    """Returns (summary_lines_for_screen, full_report_lines_for_file)."""
    headers = {"Cookie": cookie}
    report: list[str] = [f"# Bakalari website structure report - {datetime.now().isoformat(timespec='seconds')}",
                         "# Contains tag/class names, table headers, JSON key names. No values/cell texts."]
    summary: list[str] = []

    dash = await http.get("/dashboard", headers=headers)
    summary.append(f"/dashboard -> HTTP {dash.status_code}, final {dash.url.path}")
    if "login" in dash.url.path.lower():
        summary.append("Redirected to the login page: the cookie is not valid for the dashboard.")
        return summary, report
    html = dash.text
    app_root = (re.findall(r"""appRoot\s*=\s*["']([^"']*)["']""", html) or ["?"])[0]
    links = extract_links(html, str(dash.url))
    summary.append(f"appRoot={app_root!r}; {len(links)} links/widget URLs found")
    report.append(f"appRoot={app_root!r}")
    report += ["link: " + link for link in links]

    script_cache: dict[str, str] = {}

    async def fetch_script(absolute: str) -> str:
        key = urlparse(absolute).path
        if key not in script_cache:
            try:
                r = await http.get(absolute, headers=headers)
                script_cache[key] = r.text[:3_000_000] if r.status_code == 200 else ""
            except httpx.HTTPError:
                script_cache[key] = ""
        return script_cache[key]

    async def analyse_page(path: str, page_html: str, page_url: str) -> list[str]:
        inline = re.findall(r"<script(?![^>]+src)[^>]*>(.*?)</script>", page_html, re.I | re.S)
        sources = [urljoin(page_url, s) for s in re.findall(r"""<script[^>]+src=["']([^"']+)["']""", page_html, re.I)]
        own = [s for s in sources if urlparse(s).netloc == urlparse(page_url).netloc and not THIRD_PARTY.search(s)]
        keyword = page_keyword(path)
        found: set[str] = set()
        for text in inline + [await fetch_script(s) for s in own[:20]]:
            found |= routes_in(text, keyword)
        lines = ["scripts: " + ", ".join(urlparse(s).path for s in own[:20])]
        lines.append("routes/ajax URLs in scripts: " + (", ".join(sorted(found)[:40]) or "(none)"))
        for skel in inline_json_structures(inline):
            lines.append("embedded JSON structure:")
            lines += ["  " + s for s in skel[:60]]
        lines += options_structures(page_html)
        return lines

    todo = [p for p in links if p != "/dashboard" and WANTED.search(p)]
    summary.append(f"reading {len(todo)} pages that matter...")
    for path in todo[:45]:
        try:
            r = await http.get(path, headers=headers)
        except httpx.HTTPError as exc:
            report.append(f"\n=== {path} -> error {type(exc).__name__}")
            continue
        ctype = r.headers.get("content-type", "")
        report.append(f"\n=== GET {path} -> HTTP {r.status_code}, {ctype}, {len(r.content)} bytes, final {r.url.path}")
        if r.status_code != 200 or len(r.content) > 3_000_000:
            continue
        if "json" in ctype:
            try:
                report += structure_of(r.json())
            except ValueError:
                report.append("(invalid json)")
        elif "html" in ctype:
            if not path.endswith("Widget"):
                report += await analyse_page(path, r.text, str(r.url))
            report += html_skeleton(r.text)

    # the timetable widget for several days (today may be a day off), as the dashboard script requests it
    shown = 0
    for date in timetable_dates(today_iso):
        path = f"/Timetable/Widget?date={date}"
        try:
            r = await http.get(path, headers=headers)
        except httpx.HTTPError:
            continue
        lessons = len(r.content) > 3500 and "timetable-dayOff" not in r.text
        report.append(f"\n=== GET {path} -> HTTP {r.status_code}, {len(r.content)} bytes" + ("  [has lessons]" if lessons else "  [day off/empty]"))
        if r.status_code == 200 and lessons and shown < 2:
            report += html_skeleton(r.text)
            shown += 1
    path = f"/Timetable/SubstitutionWidget?date={timetable_dates(today_iso)[0]}"
    r = await http.get(path, headers=headers)
    report.append(f"\n=== GET {path} -> HTTP {r.status_code}, {len(r.content)} bytes")
    if r.status_code == 200:
        report += html_skeleton(r.text)

    report.append("\n=== dashboard")
    report += html_skeleton(html)
    return summary, report


# --------------------------------------------------------------- session lifetime test


def _set_cookie_summary(response: httpx.Response) -> list[str]:
    """Names and lifetime attributes of Set-Cookie headers - never the values."""
    out: list[str] = []
    for raw in response.headers.get_list("set-cookie"):
        parts = [p.strip() for p in raw.split(";")]
        name = parts[0].partition("=")[0]
        attrs = [p for p in parts[1:] if p.lower().startswith(("expires", "max-age"))]
        out.append(name + (f"({', '.join(attrs)})" if attrs else "(session cookie)"))
    return out


def _session_numbers(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json().get("data") or {}
    except (ValueError, AttributeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, (int, float))}


async def keepalive_test(url: str, cookie: str, interval: float, max_minutes: float, log: Any,
                         transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """Keep a website session alive with the site's own /sessionextend and log how it behaves.

    Returns True if the session survived until max_minutes. Cookie values are never logged.
    """
    import asyncio
    import time

    host = urlparse(url).hostname or ""
    jar = httpx.Cookies()
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            jar.set(name, value, domain=host, path="/")
    started = time.monotonic()
    log(f"keep-alive test against {url}, every {interval:.0f}s for up to {max_minutes:.0f} min")
    async with httpx.AsyncClient(base_url=url, cookies=jar, timeout=30, transport=transport,
                                 headers={"User-Agent": "bakalari-mcp/2.0 keepalive-test"}) as http:
        previous = {c.name: c.value for c in http.cookies.jar}
        while True:
            minutes = (time.monotonic() - started) / 60
            try:
                before = await http.get("/sessioninfo")
                extend = await http.get("/sessionextend")
                after = await http.get("/sessioninfo")
            except httpx.HTTPError as exc:
                log(f"[{minutes:6.1f} min] network error {type(exc).__name__} - will retry")
                await asyncio.sleep(interval)
                continue
            now = {c.name: c.value for c in http.cookies.jar}
            changed = sorted(k for k in now if previous.get(k) != now[k])
            previous = now
            cookies_seen = sorted({s for r in (before, extend, after) for s in _set_cookie_summary(r)})
            b, a = _session_numbers(before), _session_numbers(after)
            log(f"[{minutes:6.1f} min] before: {b or before.status_code} | extend: HTTP {extend.status_code} | "
                f"after: {a or after.status_code} | cookie values changed: {changed or 'none'} | set-cookie: {cookies_seen or 'none'}")
            if not a.get("remainingTime"):
                log(f"SESSION DIED (or was never valid) at {minutes:.1f} minutes")
                return False
            if minutes >= max_minutes:
                log(f"session still alive after {minutes:.1f} minutes - test finished")
                return True
            await asyncio.sleep(interval)
