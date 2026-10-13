"""Browser-extension cookie flow: store, receiver, and the client reading the pushed cookie."""

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from bakalari_mcp import cookiestore
from bakalari_mcp.client import AuthError, BakalariClient
from bakalari_mcp.config import Config, ConfigError
from bakalari_mcp.receiver import make_server

from fake_api import FakeBakalari

TOKEN = "t" * 24


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def receiver(tmp_path):
    store = tmp_path / "session.json"
    httpd = make_server("127.0.0.1", 0, TOKEN, store, school_url=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", store
    httpd.shutdown()
    httpd.server_close()


def post(base, body, token=TOKEN, path="/cookie"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST", headers={
        "Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------- store


def test_store_roundtrip_is_atomic_and_private(tmp_path):
    path = tmp_path / "sub" / "session.json"
    assert cookiestore.load(path) is None
    cookiestore.save(path, "a=1; b=2", now=1000.0)
    assert cookiestore.load(path) == ("a=1; b=2", 1000.0)
    assert [p.name for p in path.parent.iterdir()] == ["session.json"]  # no temp files left behind
    path.write_text("not json")
    assert cookiestore.load(path) is None


# ------------------------------------------------------------- receiver


def test_receiver_stores_cookie_with_valid_token(receiver):
    base, store = receiver
    status, body = post(base, {"cookie": "Cookie: BakaAuth=abc; ASP.NET_SessionId=x"})
    assert status == 200 and body["ok"] and body["cookies"] == 2
    assert cookiestore.load(store)[0] == "BakaAuth=abc; ASP.NET_SessionId=x"


def test_receiver_rejects_bad_token_and_garbage(receiver):
    base, store = receiver
    assert post(base, {"cookie": "a=1"}, token="wrong" * 6)[0] == 401
    assert post(base, {"cookie": "a=1"}, token=None)[0] == 401
    assert post(base, {"nope": 1})[0] == 400
    assert post(base, b"not json")[0] == 400
    assert post(base, {"cookie": "a=ž"})[0] == 400
    assert post(base, b"x" * 20000)[0] == 413
    assert post(base, {"cookie": "a=1"}, path="/other")[0] == 404
    assert cookiestore.load(store) is None  # nothing was stored by any of those


def test_receiver_health_needs_no_token_and_leaks_no_cookie(receiver):
    base, _ = receiver
    post(base, {"cookie": "BakaAuth=secretvalue"})
    with urllib.request.urlopen(base + "/health", timeout=5) as r:
        text = r.read().decode()
    assert json.loads(text)["has_cookie"] is True and "secretvalue" not in text


def test_receiver_refuses_short_tokens(tmp_path):
    with pytest.raises(ConfigError):
        make_server("127.0.0.1", 0, "short", tmp_path / "s.json", None)


# --------------------------------------------------------- client side


def cfg(store, **kw):
    return Config(url="https://school.test", cookie_file=store, cache_ttl=0, **kw)


def test_client_uses_the_newest_pushed_cookie(tmp_path):
    store = tmp_path / "session.json"
    fake = FakeBakalari()
    client = BakalariClient(cfg(store), transport=fake.transport())
    cookiestore.save(store, "sid=good")
    assert run(client.get("user"))["FullName"] == "Karel Test"
    cookiestore.save(store, "sid=expired")  # the extension pushed a new (here: bad) cookie
    with pytest.raises(AuthError):
        run(client.get("user"))
    assert fake.logins == 0


def test_client_explains_missing_and_stale_browser_cookie(tmp_path):
    store = tmp_path / "session.json"
    client = BakalariClient(cfg(store, cookie_max_age=60), transport=FakeBakalari().transport())
    with pytest.raises(AuthError, match="No school cookie"):
        run(client.get("user"))
    cookiestore.save(store, "sid=good", now=time.time() - 3600)
    with pytest.raises(AuthError, match="60 minutes|has not sent"):
        run(client.get("user"))


def test_config_accepts_cookie_file_alone(tmp_path):
    c = Config.load(environ={"BAKALARI_COOKIE_FILE": str(tmp_path / "s.json")}, env_file=tmp_path / "none")
    assert c.uses_cookie and c.cookie_file == tmp_path / "s.json" and "s.json" not in repr(c)


def test_session_status_reports_age_and_remaining_time(tmp_path):
    store = tmp_path / "session.json"
    fake = FakeBakalari()
    client = BakalariClient(cfg(store), transport=fake.transport())
    cookiestore.save(store, "sid=good", now=time.time() - 120)
    status = run(client.session_status())
    assert status["alive"] is True and status["remaining_minutes"] == 14.7 and 1.9 <= status["cookie_age_minutes"] <= 2.1
    fake.session_dead = True
    assert run(client.session_status())["alive"] is False


def test_messages_in_cookie_mode_use_the_website_never_the_rest_api(tmp_path):
    import server

    store = tmp_path / "session.json"
    cookiestore.save(store, "sid=good")
    fake = FakeBakalari()
    config = cfg(store)
    server._config, server._client = config, BakalariClient(config, transport=fake.transport())
    try:
        with pytest.raises(Exception) as err:
            asyncio.run(server.mcp.call_tool("get_messages_received", {}))
        assert "komens.aspx" in str(err.value)  # this fake school has no such page; the website was asked
        assert not any(p.startswith("/api/3/") for _, p in fake.calls)  # the REST API is never tried with a cookie
    finally:
        server._config = server._client = None


def test_extension_logic_under_node():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = Path(__file__).parent / "extension_test.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "extension tests passed" in result.stdout


# ---------------------------------------------------------------- keeper


def run_keeper_for(fake, store, iterations):
    from bakalari_mcp.keeper import run_keeper

    lines: list[str] = []

    async def no_sleep(_):
        pass

    asyncio.run(run_keeper("https://school.test", store, 0, lines.append, transport=fake.transport(),
                           iterations=iterations, sleep=no_sleep))
    return lines


def test_keeper_renews_and_saves_the_cookie_the_school_sends_back(tmp_path):
    store = tmp_path / "session.json"
    cookiestore.save(store, "sid=good", now=1.0)
    lines = run_keeper_for(FakeBakalari(), store, 2)
    cookie, received = cookiestore.load(store)
    assert cookie.startswith("sid=good") and "BakaAuth=ROTATEDSECRET" in cookie  # rotated cookie persisted
    assert received > 1.0  # "last confirmed alive" moves forward, so the MCP server never sees it as stale
    text = "\n".join(lines)
    assert "renewed" in text and "14.7 min" in text and "ROTATEDSECRET" not in text and "sid=good" not in text


def test_keeper_reports_expiry_once_and_recovers_when_a_new_cookie_is_pasted(tmp_path):
    store = tmp_path / "session.json"
    cookiestore.save(store, "sid=good", now=1.0)
    fake = FakeBakalari()
    fake.session_dead = True
    lines = run_keeper_for(fake, store, 3)
    assert sum("SESSION EXPIRED" in line for line in lines) == 1  # not repeated every cycle
    assert cookiestore.load(store) == ("sid=good", 1.0)  # a dead cookie is left alone

    fake.session_dead = False
    cookiestore.save(store, "sid=good2; extra=1", now=5.0)  # the user pastes a fresh cookie
    lines = run_keeper_for(fake, store, 1)
    assert any("using the cookie from the cookie file" in line for line in lines)
    assert cookiestore.load(store)[0].startswith("sid=good2; extra=1")


def test_keeper_waits_when_there_is_no_cookie_yet(tmp_path):
    lines = run_keeper_for(FakeBakalari(), tmp_path / "none.json", 2)
    assert sum("no cookie yet" in line for line in lines) == 1


def test_store_cookie_command_reads_stdin(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    store = tmp_path / "session.json"
    env = {**os.environ, "BAKALARI_ENV_FILE": str(tmp_path / "none"), "BAKALARI_COOKIE_FILE": str(store)}
    done = subprocess.run([sys.executable, str(root / "server.py"), "--store-cookie"], input="Cookie: BakaAuth=abc; x=1",
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0 and "abc" not in done.stdout + done.stderr
    assert cookiestore.load(store)[0] == "BakaAuth=abc; x=1"


def test_check_in_cookie_mode_reports_the_session_instead_of_calling_the_api(tmp_path, capsys, monkeypatch):
    import server

    store = tmp_path / "session.json"
    cookiestore.save(store, "sid=good", now=time.time() - 60)
    fake = FakeBakalari()
    fake.accept_cookie = False
    config = cfg(store)
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_client", BakalariClient(config, transport=fake.transport()))
    assert asyncio.run(server._check()) == 0
    out = capsys.readouterr().out
    assert "OK: cookie session alive" in out and "14.7 min left" in out and "1.0 min ago" in out
    assert not any(p.startswith("/api/3/") for _, p in fake.calls)  # the REST API was never asked
    fake.session_dead = True
    assert asyncio.run(server._check()) == 1
    assert "session is over" in capsys.readouterr().out


# ------------------------------------------- get_marks through the website (cookie mode)


def _site(request):
    import httpx
    from pathlib import Path

    if request.url.path == "/next/prubzna.aspx":
        if request.headers.get("Cookie") != "sid=good":
            return httpx.Response(302, headers={"Location": "/login?ReturnUrl=%2Fnext%2Fprubzna.aspx"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              text=(Path(__file__).parent / "pages" / "marks_sample.html").read_text(encoding="utf-8"))
    if request.url.path.startswith("/login"):
        return httpx.Response(200, text="<html>login form</html>")
    return httpx.Response(404)


def _cookie_server(tmp_path, cookie="sid=good"):
    import httpx
    import server

    store = tmp_path / "session.json"
    cookiestore.save(store, cookie)
    config = cfg(store)
    server._config = config
    server._client = BakalariClient(config, transport=httpx.MockTransport(_site))
    return server


def test_get_marks_tool_reads_the_website_page_in_cookie_mode(tmp_path):
    server = _cookie_server(tmp_path)
    try:
        result = asyncio.run(server.mcp.call_tool("get_marks", {"summary_only": True}))
        text = str(result)
        assert "Matematika" in text and "1,75" in text and "Tělesná výchova" in text
        result = asyncio.run(server.mcp.call_tool("get_marks", {"subject": "cesky", "only_new": False}))
        assert "Diktát" in str(result) and "Matematika" not in str(result)
        result = asyncio.run(server.mcp.call_tool("get_marks", {"only_new": True}))
        assert "Test zlomky" in str(result) and "Diktát" not in str(result)
    finally:
        server._config = server._client = None


def test_get_marks_explains_an_ended_website_session(tmp_path):
    server = _cookie_server(tmp_path, cookie="sid=old")
    try:
        with pytest.raises(Exception) as err:
            asyncio.run(server.mcp.call_tool("get_marks", {}))
        assert "logged the session out" in str(err.value)
    finally:
        server._config = server._client = None


# ------------------------------------------------------------ HTTP mode (Cloudflare tunnel)

HTTP_TOKEN = "s3cret" * 6


def _rpc(client, path, method, params=None, host="127.0.0.1:8770", id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post(path, json=body, headers={"Accept": "application/json, text/event-stream", "Host": host})


def test_http_app_serves_mcp_only_on_the_secret_path_and_allowed_hosts():
    import server
    from starlette.testclient import TestClient

    app = server.build_http_app(HTTP_TOKEN, ["mcp.example.com"])
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    with TestClient(app) as client:
        ok = _rpc(client, f"/{HTTP_TOKEN}/mcp", "initialize", init)
        assert ok.status_code == 200 and "bakalari-mcp" in ok.text
        tools = _rpc(client, f"/{HTTP_TOKEN}/mcp", "tools/list", id_=2)
        assert tools.status_code == 200 and "get_marks" in tools.text
        via_tunnel = _rpc(client, f"/{HTTP_TOKEN}/mcp", "tools/list", host="mcp.example.com", id_=3)
        assert via_tunnel.status_code == 200  # the public tunnel host name is accepted
        assert _rpc(client, "/mcp", "tools/list").status_code == 404  # no secret, no service
        assert _rpc(client, "/wrong-token/mcp", "tools/list").status_code == 404
        assert _rpc(client, f"/{HTTP_TOKEN}/mcp", "tools/list", host="evil.example.org").status_code in (400, 403, 421)


def test_http_app_refuses_a_weak_token():
    import server

    with pytest.raises(ConfigError):
        server.build_http_app("short", [])


def test_http_app_answers_claude_ai_preflight_but_not_other_origins():
    import server
    from starlette.testclient import TestClient

    app = server.build_http_app(HTTP_TOKEN, ["mcp.example.com"])
    path = f"/{HTTP_TOKEN}/mcp"
    with TestClient(app) as client:
        def preflight(origin):
            return client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "content-type,mcp-protocol-version",
                                                 "Host": "mcp.example.com"})

        good = preflight("https://claude.ai")
        assert good.status_code == 200 and good.headers["access-control-allow-origin"] == "https://claude.ai"
        assert preflight("https://evil.example.org").status_code in (400, 403)
        # the wrong secret is still a 404 even for claude.ai
        assert client.post("/nope/mcp", json={}, headers={"Origin": "https://claude.ai", "Host": "mcp.example.com"}).status_code == 404


def test_request_log_masks_the_secret_and_shows_what_was_asked_for():
    import server
    from starlette.testclient import TestClient

    lines: list[str] = []
    app = server.build_http_app(HTTP_TOKEN, ["mcp.example.com"])
    logged = server._RequestLog(app, HTTP_TOKEN, write=lines.append)
    with TestClient(logged) as client:
        client.post(f"/{HTTP_TOKEN}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Host": "mcp.example.com", "Accept": "application/json, text/event-stream",
                             "User-Agent": "claude-checker/1"})
        client.get("/mcp", headers={"Host": "mcp.example.com"})
    text = "\n".join(lines)
    assert "POST /<secret>/mcp -> 200" in text and "ua=claude-checker/1" in text
    assert "GET /mcp -> 404" in text
    assert HTTP_TOKEN not in text


def test_a_secret_split_by_a_pasted_line_break_still_works_but_a_wrong_one_does_not():
    import server
    from starlette.testclient import TestClient

    app = server.build_http_app(HTTP_TOKEN, ["mcp.example.com"])
    hdr = {"Host": "mcp.example.com", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    half = len(HTTP_TOKEN) // 2
    with TestClient(app) as client:
        for glue in ("%20", "%0A", "%0d%0a", " ", "%09"):
            path = f"/{HTTP_TOKEN[:half]}{glue}{HTTP_TOKEN[half:]}/mcp"
            assert client.post(path, json=body, headers=hdr).status_code == 200, glue
        assert client.post(f"/{HTTP_TOKEN[:half]}%20x{HTTP_TOKEN[half:]}/mcp", json=body, headers=hdr).status_code == 404
        assert client.post(f"/{HTTP_TOKEN[:-1]}%20/mcp", json=body, headers=hdr).status_code == 404
