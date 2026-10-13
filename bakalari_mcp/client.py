"""Async Bakaláři API client: token reuse/refresh, retries, response cache."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Sequence
from urllib.parse import unquote

import httpx

from . import cookiestore
from .config import Config

log = logging.getLogger("bakalari_mcp.client")

CLIENT_ID = "ANDR"
MAX_ATTEMPTS = 3
RETRY_STATUS = {429, 500, 502, 503, 504}
TOKEN_SKEW = 60.0  # renew this many seconds before the token really expires


NO_BROWSER_COOKIE = (
    "No school cookie is stored yet. Run `bakalari-mcp setup` and choose the cookie login, or let the browser extension send one."
)

SESSION_ENDED = (
    "The school website logged the session out. Log in again in the browser and paste a fresh cookie "
    "(bakalari-mcp setup), or use the username + password login."
)

COOKIE_REJECTED = (
    "The school's REST API does not accept website cookies (it only takes its own access tokens). The cookie-mode "
    "tools read the website's pages instead; for the REST API use the username + password login (`bakalari-mcp setup`, password option)."
)


class BakalariError(Exception):
    """Anything that went wrong talking to Bakaláři. The message is safe to show."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(BakalariError):
    """Wrong credentials or the account is locked."""


class NotFoundError(BakalariError):
    """The server does not know this endpoint."""


class BakalariClient:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._http = httpx.AsyncClient(
            base_url=config.url,
            timeout=config.timeout,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": "bakalari-mcp/2.0", "Accept": "application/json"},
        )
        self._lock = asyncio.Lock()
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self._endpoint_hint: dict[str, str] = {}
        self.login_count = 0  # exposed for tests / diagnostics

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth

    async def _token_request(self, data: dict[str, str]) -> None:
        try:
            resp = await self._http.post("/api/login", data=data)
        except httpx.TimeoutException as exc:
            raise BakalariError(f"Timed out connecting to {self.config.url}.") from exc
        except httpx.HTTPError as exc:
            raise BakalariError(f"Cannot reach {self.config.url} ({type(exc).__name__}). Check BAKALARI_URL.") from exc

        if resp.status_code in (400, 401):
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("error_description") or body.get("error") or "")
            except ValueError:
                pass
            raise AuthError(
                "Bakaláři rejected the login" + (f": {detail}" if detail else "") +
                ". Check BAKALARI_USERNAME / BAKALARI_PASSWORD.",
                resp.status_code,
            )
        if resp.status_code == 404:
            raise BakalariError(
                f"{self.config.url}/api/login not found - BAKALARI_URL probably points to the wrong "
                "place (it should be the base address of the school's Bakaláři, e.g. https://bakalari.skola.cz).",
                404,
            )
        if resp.status_code >= 400:
            raise BakalariError(f"Login failed with HTTP {resp.status_code}.", resp.status_code)

        try:
            body = resp.json()
            self._access = body["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BakalariError("Login response from Bakaláři was not understood.") from exc
        self._refresh = body.get("refresh_token") or None
        try:
            lifetime = float(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._expires_at = time.monotonic() + max(30.0, lifetime - TOKEN_SKEW)

    async def _ensure_token(self) -> str:
        if self.config.uses_cookie:
            return ""
        async with self._lock:
            if self._access and time.monotonic() < self._expires_at:
                return self._access
            if self._refresh:
                try:
                    await self._token_request(
                        {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": self._refresh}
                    )
                    return self._access  # type: ignore[return-value]
                except AuthError:
                    log.info("Refresh token rejected, logging in again.")
                    self._refresh = None
            self.login_count += 1
            await self._token_request(
                {
                    "client_id": CLIENT_ID,
                    "grant_type": "password",
                    "username": self.config.username,
                    "password": self.config.password,
                }
            )
            return self._access  # type: ignore[return-value]

    def current_cookie(self) -> str:
        """The cookie to send: a fixed one, or the newest one the browser extension pushed."""
        cfg = self.config
        if cfg.cookie:
            return cfg.cookie
        if not cfg.cookie_file:
            return ""
        stored = cookiestore.load(cfg.cookie_file)
        if stored is None:
            raise AuthError(NO_BROWSER_COOKIE)
        cookie, received_at = stored
        age = time.time() - received_at
        if age > cfg.cookie_max_age:
            raise AuthError(
                f"The stored school cookie has not been renewed for {age / 60:.0f} minutes, so the school session has "
                "probably ended. Paste a fresh cookie with `bakalari-mcp setup`. The keeper log says whether it really ended: "
                "`bakalari-mcp service status keeper`, or the log in the data folder."
            )
        return cookie

    def _bakaauth_value(self) -> str | None:
        for part in self.current_cookie().split(";"):
            name, _, value = part.strip().partition("=")
            if name == "BakaAuth" and value:
                return unquote(value)
        return None

    def _auth_headers(self, token: str) -> dict[str, str]:
        if not self.config.uses_cookie:
            return {"Authorization": f"Bearer {token}"}
        headers = {"Cookie": self.current_cookie()}
        bearer = self._bakaauth_value()  # BakaAuth might itself be the API access token - harmless if not
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _forget_token(self) -> None:
        self._access = None
        self._expires_at = 0.0

    # -------------------------------------------------------------- requests

    async def _request(
        self, method: str, endpoint: str, params: dict | None = None, body: dict | None = None
    ) -> Any:
        reauthed = False
        attempt = 0
        while True:
            attempt += 1
            token = await self._ensure_token()
            auth = self._auth_headers(token)
            try:
                resp = await self._http.request(
                    method, f"/api/3/{endpoint}", params=params, json=body, headers=auth
                )
            except httpx.HTTPError as exc:
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise BakalariError(f"Bakaláři did not answer in time ({endpoint}).") from exc
                raise BakalariError(f"Network error while calling {endpoint} ({type(exc).__name__}).") from exc

            status = resp.status_code
            if status == 401 and self.config.uses_cookie:
                raise AuthError(COOKIE_REJECTED, 401)
            if status == 401:
                if reauthed:
                    raise AuthError("Bakaláři keeps refusing the access token.", 401)
                reauthed = True
                attempt -= 1
                self._forget_token()
                continue
            if status in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                delay = 0.5 * attempt
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = min(float(retry_after), 5.0)
                await asyncio.sleep(delay)
                continue
            if status in (404, 405, 501):
                raise NotFoundError(f"Endpoint {endpoint} is not available (HTTP {status}).", status)
            if status == 403:
                raise BakalariError(f"This account has no permission for {endpoint} (HTTP 403).", 403)
            if status >= 400:
                raise BakalariError(f"Bakaláři returned HTTP {status} for {endpoint}.", status)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise BakalariError(f"Bakaláři returned invalid JSON for {endpoint}.") from exc

    async def get(self, endpoint: str, params: dict | None = None, ttl: float | None = None) -> Any:
        """GET /api/3/<endpoint> with a small time-based cache."""
        ttl = self.config.cache_ttl if ttl is None else ttl
        key = endpoint + "?" + json.dumps(params or {}, sort_keys=True)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        data = await self._request("GET", endpoint, params)
        if ttl > 0:
            self._cache[key] = (now + ttl, data)
            if len(self._cache) > 200:  # drop expired entries
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return data

    async def get_any(self, endpoints: Sequence[str], params: dict | None = None) -> Any:
        """Try alternative endpoint names (they differ between Bakaláři versions).

        The first one that works is remembered.
        """
        group = "|".join(endpoints)
        ordered = list(endpoints)
        if group in self._endpoint_hint:
            ordered.sort(key=lambda e: e != self._endpoint_hint[group])
        last: BakalariError | None = None
        for endpoint in ordered:
            try:
                data = await self.get(endpoint, params)
            except NotFoundError as exc:
                last = exc
                continue
            self._endpoint_hint[group] = endpoint
            return data
        raise last or NotFoundError("No matching endpoint.")

    async def get_or_post(self, endpoint: str, params: dict | None = None) -> Any:
        """GET, falling back to POST with an empty filter for versions that require it."""
        key = endpoint + "?" + json.dumps(params or {}, sort_keys=True)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if self._endpoint_hint.get("POST:" + endpoint):
            data = await self._request("POST", endpoint, params, body={})
        else:
            try:
                data = await self._request("GET", endpoint, params)
            except NotFoundError as exc:
                if exc.status != 405:
                    raise
                self._endpoint_hint["POST:" + endpoint] = "post"
                data = await self._request("POST", endpoint, params, body={})
        if self.config.cache_ttl > 0:
            self._cache[key] = (time.monotonic() + self.config.cache_ttl, data)
        return data

    async def get_html(self, path: str, ttl: float | None = None) -> str:
        """A page of the website (cookie mode), cached briefly. Raises AuthError when the session is over."""
        ttl = self.config.cache_ttl if ttl is None else ttl
        key = "html:" + path
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            resp = await self._http.get(path, headers={"Cookie": self.current_cookie()}, follow_redirects=True)
        except httpx.TimeoutException as exc:
            raise BakalariError(f"The school website did not answer in time ({path}).") from exc
        except httpx.HTTPError as exc:
            raise BakalariError(f"Cannot reach the school website ({type(exc).__name__}).") from exc
        if "login" in resp.url.path.lower():
            raise AuthError(
                "The school website logged the session out. Log in again in the browser and paste a fresh cookie "
                "(bakalari-mcp setup), or use the username + password login."
            )
        if resp.status_code >= 400:
            raise BakalariError(f"The school website returned HTTP {resp.status_code} for {path}.", resp.status_code)
        if ttl > 0:
            self._cache[key] = (time.monotonic() + ttl, resp.text)
        return resp.text

    async def web_json(
        self, method: str, path: str, *, json_body: Any = None, params: dict | None = None, ttl: float | None = None
    ) -> Any:
        """Call one of the website's own JSON endpoints with the cookie; returns its 'data' part."""
        ttl = self.config.cache_ttl if ttl is None else ttl
        key = f"web:{method}:{path}:{json.dumps([json_body, params], sort_keys=True, default=str)}"
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            resp = await self._http.request(
                method, path, params=params, json=json_body, follow_redirects=False,
                headers={"Cookie": self.current_cookie(), "X-Requested-With": "XMLHttpRequest"},
            )
        except httpx.TimeoutException as exc:
            raise BakalariError(f"The school website did not answer in time ({path}).") from exc
        except httpx.HTTPError as exc:
            raise BakalariError(f"Cannot reach the school website ({type(exc).__name__}).") from exc
        if resp.status_code in (301, 302, 303, 307, 308) or "login" in resp.url.path.lower():
            raise AuthError(SESSION_ENDED, resp.status_code)
        if resp.status_code >= 400:
            raise BakalariError(f"The school website returned HTTP {resp.status_code} for {path}.", resp.status_code)
        try:
            body = resp.json()
        except ValueError as exc:
            raise AuthError(SESSION_ENDED, resp.status_code) from exc  # an HTML page instead of data = not logged in
        if isinstance(body, dict) and body.get("success") is False:
            raise BakalariError(str(body.get("error") or f"The school website refused {path}."))
        data = body.get("data") if isinstance(body, dict) and "data" in body else body
        if ttl > 0:
            self._cache[key] = (time.monotonic() + ttl, data)
        return data

    async def web_post_form(self, path: str, form: dict[str, str]) -> str:
        """POST a web form (an ASP.NET callback) with the cookie; returns the answer text."""
        try:
            resp = await self._http.post(
                path, data=form, follow_redirects=False,
                headers={"Cookie": self.current_cookie(), "X-Requested-With": "XMLHttpRequest"},
            )
        except httpx.TimeoutException as exc:
            raise BakalariError(f"The school website did not answer in time ({path}).") from exc
        except httpx.HTTPError as exc:
            raise BakalariError(f"Cannot reach the school website ({type(exc).__name__}).") from exc
        if resp.status_code in (301, 302, 303, 307, 308) or "login" in resp.url.path.lower():
            raise AuthError(SESSION_ENDED, resp.status_code)
        if resp.status_code >= 400:
            raise BakalariError(f"The school website returned HTTP {resp.status_code} for {path}.", resp.status_code)
        return resp.text

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------ high level

    async def verify(self) -> dict:
        """Log in and fetch the user profile (used by `--check`)."""
        return await self.get("user", ttl=0)

    async def session_status(self) -> dict:
        """State of the cookie session (cookie mode): {'alive', 'remaining_minutes', 'cookie_age_minutes'}."""
        cookie = self.current_cookie()
        age = None
        if self.config.cookie_file:
            stored = cookiestore.load(self.config.cookie_file)
            age = round((time.time() - stored[1]) / 60, 1) if stored else None
        try:
            r = await self._http.get("/sessioninfo", headers={"Cookie": cookie})
            seconds = float(r.json()["data"]["remainingTime"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise BakalariError(f"Could not ask the school about the session ({type(exc).__name__}).") from exc
        return {"alive": seconds > 0, "remaining_minutes": round(seconds / 60, 1), "cookie_age_minutes": age}

    async def probe_site(self) -> tuple[list[str], list[str]]:
        """Structure-only report of the logged-in website: (screen summary, full report)."""
        from .probe import build_report
        from .util import today

        return await build_report(self._http, self.current_cookie(), today(self.config.timezone).isoformat() + "T00:00:00")

    async def diagnose(self) -> list[str]:
        """Explain what the server does with the configured credentials (used by `--check` on failure)."""
        lines: list[str] = []
        try:
            info = await self._http.get("/api")
            version = info.json()[0] if info.status_code == 200 else {}
            lines.append(
                f"API: {self.config.url}/api -> HTTP {info.status_code}"
                + (f", API {version.get('ApiVersion')}, Bakaláři {version.get('ApplicationVersion')}" if version else "")
            )
        except (httpx.HTTPError, ValueError, IndexError, AttributeError) as exc:
            lines.append(f"API: cannot read {self.config.url}/api ({type(exc).__name__}) - check BAKALARI_URL")
            return lines
        if not self.config.uses_cookie:
            return lines
        try:
            cookie = self.current_cookie()
        except BakalariError as exc:
            lines.append(str(exc))
            return lines
        try:
            r = await self._http.get("/api/3/user", headers={"Cookie": cookie})
            lines.append(
                f"REST API with your cookie: HTTP {r.status_code}"
                + (f" ({r.headers.get('WWW-Authenticate')} required)" if r.status_code == 401 else "")
            )
            bearer = self._bakaauth_value()
            if bearer:
                rb = await self._http.get("/api/3/user", headers={"Authorization": f"Bearer {bearer}"})
                lines.append(f"REST API with BakaAuth used as Bearer token: HTTP {rb.status_code}")
            web = await self._http.get("/", headers={"Cookie": cookie}, follow_redirects=False)
            where = web.headers.get("location", "")
            valid = web.status_code == 200 or (300 <= web.status_code < 400 and "login" not in where.lower())
            lines.append(
                f"Website with your cookie: HTTP {web.status_code}"
                + (f" -> {where}" if where else "")
                + ("  => the cookie IS a valid web session" if valid else "  => the cookie is expired/invalid")
            )
            if valid and r.status_code == 401:
                lines.append(
                    "The website accepts the cookie but the REST API only accepts Bearer tokens, so this "
                    "school cannot be read through the API with a cookie alone."
                )
        except httpx.HTTPError as exc:
            lines.append(f"Diagnosis failed ({type(exc).__name__}).")
        return lines
