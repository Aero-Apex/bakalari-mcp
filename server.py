"""Bakaláři MCP server (stdio).

Run:      uv run server.py
Self-test: uv run server.py --check     (logs in and prints who you are)
Discovery: uv run server.py --probe     (cookie mode: which addresses the website itself uses)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Callable
from urllib.parse import unquote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from bakalari_mcp import formatters as fmt
from bakalari_mcp import komens, loaders
from bakalari_mcp import webdata as wd
from bakalari_mcp.client import BakalariClient, BakalariError
from bakalari_mcp.config import Config, ConfigError
from bakalari_mcp.util import monday_of, next_weekday, parse_date, parse_date_opt, today

__version__ = "2.0.0"

logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bakalari_mcp")

INSTRUCTIONS = """\
Read-only access to the Czech school system Bakaláři (timetable, marks, homework, messages, absence).
- Start with get_overview for "what's new / what do I have today" style questions.
- Dates accept YYYY-MM-DD, D.M.YYYY, today, tomorrow, yesterday (Czech 'dnes', 'zítra', 'včera' also work).
- subject filters are accent- and case-insensitive substrings ("cestina" matches "Český jazyk a literatura"
  only if the name contains it, so try the abbreviation or a distinctive part).
- Every data tool has raw=true for the school's own data when you need a field that is not shown.
- Marks: use summary_only=true for a quick per-subject picture; school_average is Bakaláři's own number.
"""

mcp = MCPServer("bakalari-mcp", instructions=INSTRUCTIONS, version=__version__)

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False, open_world_hint=True)

_config: Config | None = None
_client: BakalariClient | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def client() -> BakalariClient:
    global _client
    if _client is None:
        _client = BakalariClient(get_config())
    return _client


def tz() -> str:
    return get_config().timezone


def safe(fn: Callable) -> Callable:
    """Turn expected failures into tool errors the model can read and explain."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (BakalariError, ConfigError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def render(data: Any, formatter: Callable[..., dict], raw: bool, **kwargs: Any) -> dict:
    """Format `data`; on request (or if formatting breaks) hand back the raw response."""
    if raw:
        return data if isinstance(data, dict) else {"data": data}
    try:
        return formatter(data, **kwargs)
    except Exception:  # noqa: BLE001 - a quirky school install must not kill the tool
        log.exception("formatter %s failed", getattr(formatter, "__name__", formatter))
        return {"note": "Could not simplify this response, here is the raw data.", "raw": data}


Raw = Annotated[bool, Field(description="Return the school's own data instead of the summary (for fields that are not shown).")]
DateArg = Annotated[
    str | None, Field(description="YYYY-MM-DD, D.M.YYYY, today, tomorrow or yesterday. Default: today.")
]
SubjectArg = Annotated[str | None, Field(description="Only this subject (name or abbreviation, accents ignored).")]


# ------------------------------------------------------------------ tools
# With a cookie login the data comes from the website's own pages (bakalari_mcp.loaders); with username +
# password it comes from the REST API. Both are shown through the same formatters.


def cookie_mode() -> bool:
    return get_config().uses_cookie


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_user(raw: Raw = False) -> dict:
    """Who is logged in: name, class, school and which Bakaláři modules are enabled."""
    if cookie_mode():
        return await loaders.user(client())
    return render(await client().get("user"), fmt.fmt_user, raw)


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_marks(
    subject: SubjectArg = None,
    since: Annotated[str | None, Field(description="Only marks given on/after this date.")] = None,
    only_new: Annotated[bool, Field(description="Only marks you have not seen yet.")] = False,
    summary_only: Annotated[bool, Field(description="Only the per-subject averages, no individual marks.")] = False,
    raw: Raw = False,
) -> dict:
    """Marks with per-subject averages (the school's and a weighted one computed here), newest first."""
    c = client()
    data = await loaders.marks(c) if cookie_mode() else await c.get("marks")
    return render(
        data,
        fmt.fmt_marks,
        raw,
        subject=subject,
        since=parse_date_opt(since, tz()),
        only_new=only_new,
        summary_only=summary_only,
    )


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_timetable_actual(date: DateArg = None, raw: Raw = False) -> dict:
    """The actual timetable (with substitutions/cancellations) of the whole week containing `date`."""
    day = parse_date(date, tz())
    c = client()
    if cookie_mode():
        return render(await loaders.timetable_week(c, day), wd.fmt_timetable_web, raw, ref=day)
    data = await c.get("timetable/actual", {"date": monday_of(day).isoformat()})
    return render(data, fmt.fmt_timetable, raw)


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_timetable_day(date: DateArg = None, raw: Raw = False) -> dict:
    """The actual timetable of a single day (default today), including changes like cancelled lessons."""
    day = parse_date(date, tz())
    c = client()
    if cookie_mode():
        week = await loaders.timetable_week(c, day)
        if raw:
            week = wd.keep_only_day(week, day, day)
        return render(week, wd.fmt_timetable_web, raw, ref=day, day=day)
    data = await c.get("timetable/actual", {"date": monday_of(day).isoformat()})
    return render(data, fmt.fmt_timetable, raw, day=day)


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_timetable_permanent(raw: Raw = False) -> dict:
    """The permanent (regular) timetable without substitutions."""
    c = client()
    if cookie_mode():
        return render(await loaders.timetable_permanent(c), wd.fmt_timetable_web, raw, ref=today(tz()), permanent=True)
    return render(await c.get("timetable/permanent"), fmt.fmt_timetable, raw, permanent=True)


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_homeworks(
    include_done: Annotated[bool, Field(description="Also list homework already marked as done.")] = False,
    subject: SubjectArg = None,
    due_from: Annotated[str | None, Field(description="Only homework due on/after this date.")] = None,
    due_until: Annotated[str | None, Field(description="Only homework due on/before this date.")] = None,
    raw: Raw = False,
) -> dict:
    """Homework sorted by deadline. Done items are hidden unless include_done is set."""
    c = client()
    data = await loaders.homeworks(c, include_done, raw) if cookie_mode() else await c.get("homeworks")
    return render(
        data,
        fmt.fmt_homeworks,
        raw,
        include_done=include_done,
        subject=subject,
        since=parse_date_opt(due_from, tz()),
        until=parse_date_opt(due_until, tz()),
    )


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_subjects(raw: Raw = False) -> dict:
    """Enrolled subjects with their teachers."""
    c = client()
    data = await loaders.subjects(c) if cookie_mode() else await c.get("subjects")
    return render(data, fmt.fmt_subjects, raw)


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_events(
    days_ahead: Annotated[int, Field(ge=1, le=366, description="How far ahead to look.")] = 60,
    raw: Raw = False,
) -> dict:
    """School events and calendar entries (trips, exams, holidays) from today on."""
    start = today(tz())
    end = start + timedelta(days=days_ahead)
    if cookie_mode():
        return await loaders.events(client(), start, end, raw)
    data = await client().get_any(["events/my", "events"])
    return render(data, fmt.fmt_events, raw, start=start, end=end)


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_absence(raw: Raw = False) -> dict:
    """Absence overview: unsolved hours, per-day records and per-subject absence percentage."""
    c = client()
    if cookie_mode():
        return await loaders.absence(c, today(tz()), tz(), raw)
    return render(await c.get("absence/student"), fmt.fmt_absence, raw)


Limit = Annotated[int, Field(ge=1, le=100, description="Maximum number of messages.")]
SearchArg = Annotated[str | None, Field(description="Only messages whose title, text or sender contains this.")]
TextChars = Annotated[int, Field(ge=0, le=20000, description="Max characters of each message text (0 = unlimited).")]


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_messages_received(
    limit: Limit = 15,
    unread_only: Annotated[bool, Field(description="Only unread messages.")] = False,
    search: SearchArg = None,
    text_chars: TextChars = 600,
    raw: Raw = False,
) -> dict:
    """Received Komens messages, newest first, with unread count."""
    if cookie_mode():
        data = await komens.messages(client(), "received", limit, unread_only, search)
    else:
        data = await client().get_or_post("komens/messages/received")
    return render(
        data, fmt.fmt_messages, raw, limit=limit, unread_only=unread_only, search=search, text_chars=text_chars
    )


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_messages_sent(
    limit: Limit = 15, search: SearchArg = None, text_chars: TextChars = 600, raw: Raw = False
) -> dict:
    """Sent Komens messages, newest first, with recipients."""
    if cookie_mode():
        data = await komens.messages(client(), "sent", limit, False, search)
    else:
        data = await client().get_or_post("komens/messages/sent")
    return render(
        data, fmt.fmt_messages, raw, limit=limit, search=search, text_chars=text_chars, show_recipients=True
    )


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_noticeboard(
    limit: Limit = 15, search: SearchArg = None, text_chars: TextChars = 600, raw: Raw = False
) -> dict:
    """Noticeboard (nástěnka) messages, newest first."""
    if cookie_mode():
        data = await komens.noticeboard(client(), limit)
    else:
        data = await client().get_or_post("komens/messages/noticeboard")
    return render(data, fmt.fmt_messages, raw, limit=limit, search=search, text_chars=text_chars)


@mcp.tool(annotations=READ_ONLY)
@safe
async def get_overview(date: DateArg = None) -> dict:
    """Daily briefing in one call: that day's timetable, the next school day, new marks, open
    homework due within a week (or overdue), unread messages and unsolved absence."""
    c = client()
    day = parse_date(date, tz())
    nxt = next_weekday(day)
    web = cookie_mode()

    async def timetable(d):
        if web:
            return wd.fmt_timetable_web(await loaders.timetable_week(c, d), ref=d, day=d)
        return fmt.fmt_timetable(await c.get("timetable/actual", {"date": monday_of(d).isoformat()}), day=d)

    async def marks():
        data = fmt.fmt_marks(await loaders.marks(c) if web else await c.get("marks"), only_new=True)
        return {"summary": [s for s in data["summary"] if s.get("new_marks")], "new_marks": data.get("subjects", [])}

    async def homework():
        data = await loaders.homeworks(c, False) if web else await c.get("homeworks")
        return fmt.fmt_homeworks(data, until=day + timedelta(days=7))

    async def messages():
        if web:
            data = await komens.messages(c, "received", 5, True, None)
        else:
            data = await c.get_or_post("komens/messages/received")
        return fmt.fmt_messages(data, limit=5, unread_only=True, text_chars=200)

    async def absence():
        if web:
            return {"unsolved_hours": (await loaders.absence(c, today(tz()), tz())).get("unsolved_hours", 0)}
        data = fmt.fmt_absence(await c.get("absence/student"))
        return {"unsolved_hours": data.get("unsolved_hours", 0)}

    parts = {
        "timetable": timetable(day),
        "next_school_day": timetable(nxt),
        "new_marks": marks(),
        "homework": homework(),
        "unread_messages": messages(),
        "absence": absence(),
    }
    results = await asyncio.gather(*parts.values(), return_exceptions=True)
    out: dict[str, Any] = {"date": day.isoformat()}
    for name, result in zip(parts, results):
        if isinstance(result, BaseException):
            if isinstance(result, (BakalariError, ValueError)):
                out[name] = {"error": str(result)}
            else:
                log.error("overview part %s failed", name, exc_info=result)
                out[name] = {"error": f"unexpected {type(result).__name__}"}
        else:
            out[name] = result
    if all(isinstance(r, BaseException) for r in results):
        first = next(r for r in results if isinstance(r, BaseException))
        raise ToolError(str(first) if isinstance(first, (BakalariError, ValueError)) else "Overview failed.")
    return out


# ---------------------------------------------------------------- prompts


@mcp.prompt()
def daily_briefing() -> str:
    """A short morning briefing for the student."""
    return (
        "Call get_overview and write a short briefing in Czech: today's lessons with times and anything "
        "cancelled or substituted, what to prepare for the next school day, homework due soon (most urgent "
        "first), new marks and unread messages. Skip sections that are empty."
    )


@mcp.prompt()
def homework_plan(days: int = 7) -> str:
    """Plan homework over the next days."""
    return (
        f"Call get_homeworks (due_until in {days} days) and get_timetable_actual, then propose a realistic "
        "plan: which homework to do on which day, hardest and earliest deadlines first."
    )


@mcp.prompt()
def marks_report(subject: str = "") -> str:
    """Explain how the marks are going."""
    scope = f" for {subject}" if subject else ""
    return (
        f"Call get_marks{scope} and summarise how things are going: averages, recent trend, and what mark "
        "would be needed on the next test to improve the average."
    )


# ------------------------------------------------------------------- main


async def _check_cookie_session() -> int:
    """Cookie mode: report the pipeline (browser extension -> cookie file -> school session)."""
    cfg = get_config()
    try:
        status = await client().session_status()
    except BakalariError as exc:
        print(f"FAILED: {exc}")
        return 1
    source = f"last confirmed alive {status['cookie_age_minutes']} min ago" if status["cookie_age_minutes"] is not None else "fixed cookie"
    if not status["alive"]:
        print(f"FAILED: the school says this session is over (cookie {source}). Log in to {cfg.url} in the browser and paste a fresh cookie (bash deploy.sh).")
        return 1
    print(f"OK: cookie session alive at {cfg.url}, {status['remaining_minutes']} min left (cookie {source}).")
    return 0


async def _check() -> int:
    try:
        if get_config().uses_cookie:
            return await _check_cookie_session()
    except ConfigError as exc:
        print(f"FAILED: {exc}")
        return 1
    try:
        c = client()
        user = fmt.fmt_user(await c.verify())
    except ConfigError as exc:
        print(f"FAILED: {exc}")
        return 1
    except BakalariError as exc:
        print(f"FAILED: {exc}")
        print("\nDiagnosis:")
        for line in await client().diagnose():
            print("  " + line)
        return 1
    mode = "cookie" if get_config().uses_cookie else "password"
    print(f"OK: logged in to {get_config().url} ({mode}) as {user.get('name', '?')} ({user.get('class', '-')})")
    print("Tools:", ", ".join(sorted(t.name for t in await mcp.list_tools())))
    await c.aclose()
    return 0


async def _probe() -> int:
    """Write a structure-only report of the logged-in website (cookie mode; no personal data)."""
    from pathlib import Path

    try:
        if not get_config().uses_cookie:
            print("--probe needs BAKALARI_COOKIE.")
            return 1
        summary, report = await client().probe_site()
    except (BakalariError, ConfigError) as exc:
        print(f"FAILED: {exc}")
        return 1
    for line in summary:
        print(line)
    path = Path(__file__).resolve().parent / "probe-report.txt"
    path.write_text(chr(10).join(report) + chr(10), encoding="utf-8")
    path.chmod(0o600)
    print(f"Structure report ({len(report)} lines, no personal data) saved to: {path}")
    return 0


async def _keepalive(args: list[str]) -> int:
    """Measure how long a cookie session can be kept alive (see probe.keepalive_test)."""
    from bakalari_mcp.probe import keepalive_test

    interval = float(args[0]) if args else 240.0
    minutes = float(args[1]) if len(args) > 1 else 240.0
    try:
        cfg = get_config()
    except ConfigError as exc:
        print(f"FAILED: {exc}")
        return 1
    if not cfg.uses_cookie:
        print("--keepalive-test needs BAKALARI_COOKIE.")
        return 1
    try:
        cookie = client().current_cookie()
    except BakalariError as exc:
        print(f"FAILED: {exc}")
        return 1
    ok = await keepalive_test(cfg.url, cookie, interval, minutes, lambda line: print(line, flush=True))
    return 0 if ok else 2


def _receiver() -> int:
    """Run the service the browser extension pushes its cookie to (keeps running; use systemd).

    It listens on 127.0.0.1 unless BAKALARI_RECEIVER_HOST names another address on purpose.
    """
    import os

    from bakalari_mcp.config import DEFAULT_ENV_FILE, parse_env_file
    from bakalari_mcp.receiver import serve

    try:
        cfg = get_config()
        env = {**parse_env_file(Path(os.environ.get("BAKALARI_ENV_FILE") or DEFAULT_ENV_FILE)), **os.environ}
        token = env.get("BAKALARI_RECEIVER_TOKEN", "")
        if not cfg.cookie_file:
            raise ConfigError("BAKALARI_COOKIE_FILE is not set - run deploy.sh and choose the browser extension.")
        if not token:
            raise ConfigError("BAKALARI_RECEIVER_TOKEN is not set - run deploy.sh and choose the browser extension.")
        serve(env.get("BAKALARI_RECEIVER_HOST", "127.0.0.1"), int(env.get("BAKALARI_RECEIVER_PORT", "8765")),
              token, cfg.cookie_file, cfg.url)
    except (ConfigError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


def _keeper() -> int:
    """Run forever: keep the pasted cookie alive (set up as a service by deploy.sh)."""
    import os

    from bakalari_mcp.config import DEFAULT_ENV_FILE, parse_env_file
    from bakalari_mcp.keeper import run_keeper

    try:
        cfg = get_config()
        env = {**parse_env_file(Path(os.environ.get("BAKALARI_ENV_FILE") or DEFAULT_ENV_FILE)), **os.environ}
        if not cfg.cookie_file:
            raise ConfigError("BAKALARI_COOKIE_FILE is not set - run deploy.sh and choose option 1.")
        interval = float(env.get("BAKALARI_KEEPER_INTERVAL", "180"))
    except (ConfigError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    try:
        asyncio.run(run_keeper(cfg.url, cfg.cookie_file, interval,
                               lambda line: print(time.strftime("%Y-%m-%d %H:%M:%S ") + line, flush=True)))
    except KeyboardInterrupt:
        pass
    return 0


def _store_cookie() -> int:
    """Read a cookie from stdin (so it never appears in a process list) and save it for the keeper."""
    from bakalari_mcp import cookiestore
    from bakalari_mcp.config import normalize_cookie

    try:
        cfg = get_config()
        if not cfg.cookie_file:
            raise ConfigError("BAKALARI_COOKIE_FILE is not set.")
        cookiestore.save(cfg.cookie_file, normalize_cookie(sys.stdin.read()))
    except ConfigError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print("Cookie saved.")
    return 0


class _RequestLog:
    """Logs one line per HTTP request with the secret masked, e.g.
    'POST /<secret>/mcp -> 200 ua=... origin=...' - enough to see what a client asked for."""

    def __init__(self, app, secret: str, write=None):
        self.app, self.secret = app, secret
        self.write = write or (lambda line: print(line, flush=True))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = {"code": "-"}

        async def spy(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, spy)
        finally:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            path = (scope.get("raw_path", b"") or scope["path"].encode()).decode("latin-1")
            if scope.get("query_string"):
                path += "?" + scope["query_string"].decode("latin-1")
            line = (f'{time.strftime("%H:%M:%S")} {scope["method"]} {path} -> {status["code"]} '
                    f'ua={headers.get("user-agent", "-")[:60]} origin={headers.get("origin", "-")} '
                    f'host={headers.get("host", "-")} accept={headers.get("accept", "-")[:40]}')
            self.write(line.replace(self.secret, "<secret>"))


class _PathCleaner:
    """Drops spaces / line breaks (also percent-encoded) from the request path.

    A long address pasted from a wrapped terminal often carries a stray line break, which would otherwise
    split the secret in two. Removing whitespace cannot make a wrong secret right, so nothing is weakened."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw = scope.get("raw_path") or scope["path"].encode()
            cleaned = re.sub(rb"(?i)%20|%09|%0a|%0d|\s+", b"", raw)
            if cleaned != raw:
                scope = dict(scope, raw_path=cleaned, path=unquote(cleaned.decode("latin-1")))
        await self.app(scope, receive, send)


def build_http_app(token: str, public_hosts: list[str], host: str = "127.0.0.1"):
    """The MCP server as an HTTP app. The secret `token` is part of the path, every other path is a 404,
    and only localhost plus the listed public host names are accepted (DNS-rebinding protection)."""
    from mcp.server.transport_security import TransportSecuritySettings

    if len(token) < 24:
        raise ConfigError("BAKALARI_HTTP_TOKEN must be at least 24 characters.")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *public_hosts, *[f"{h}:*" for h in public_hosts]],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", *[f"https://{h}" for h in public_hosts]],
    )
    app = mcp.streamable_http_app(streamable_http_path=f"/{token}/mcp", stateless_http=True, json_response=True,
                                  transport_security=security, host=host)
    from starlette.middleware.cors import CORSMiddleware

    # Lets claude.ai's own page check the connector from the browser; the secret path still guards the data.
    app.add_middleware(_RequestLog, secret=token)
    app.add_middleware(CORSMiddleware, allow_origins=["https://claude.ai", "https://claude.com"],
                       allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"],
                       expose_headers=["Mcp-Session-Id"])
    app.add_middleware(_PathCleaner)  # outermost: runs first
    return app


def _http() -> int:
    """Serve over HTTP for a Cloudflare tunnel (cloudflared connects to 127.0.0.1:<port>)."""
    import os

    import uvicorn

    from bakalari_mcp.config import DEFAULT_ENV_FILE, parse_env_file

    try:
        get_config()
        env = {**parse_env_file(Path(os.environ.get("BAKALARI_ENV_FILE") or DEFAULT_ENV_FILE)), **os.environ}
        hosts = [h.strip() for h in env.get("BAKALARI_HTTP_HOSTS", "").split(",") if h.strip()]
        host = env.get("BAKALARI_HTTP_HOST", "127.0.0.1")
        port = int(env.get("BAKALARI_HTTP_PORT", "8770"))
        app = build_http_app(env.get("BAKALARI_HTTP_TOKEN", ""), hosts, host)
    except (ConfigError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"MCP over HTTP on http://{host}:{port}/<secret>/mcp (allowed public hosts: {', '.join(hosts) or 'none'})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)  # no logs: the path holds the secret
    return 0


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(__version__)
        return 0
    if "--http" in argv:
        return _http()
    if "--keeper" in argv:
        return _keeper()
    if "--store-cookie" in argv:
        return _store_cookie()
    if "--receiver" in argv:
        return _receiver()
    if "--keepalive-test" in argv:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
        return asyncio.run(_keepalive(argv[argv.index("--keepalive-test") + 1:]))
    if "--probe" in argv:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
        return asyncio.run(_probe())
    if "--check" in argv:
        for stream in (sys.stdout, sys.stderr):  # Czech text must not crash legacy consoles
            stream.reconfigure(encoding="utf-8", errors="replace")
        return asyncio.run(_check())
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
