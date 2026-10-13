"""`bakalari-mcp setup`, `http-setup` and `show-url`: the same on Windows, macOS and Linux.

Run without arguments in a terminal for questions; or pass flags (secrets come from stdin) for scripts and servers.
"""

from __future__ import annotations

import asyncio
import getpass
import re
import secrets
import sys
from pathlib import Path

from bakalari_mcp import cookiestore, services
from bakalari_mcp.config import (
    HTTP_TOKEN_MIN,
    RECEIVER_TOKEN_MIN,
    ConfigError,
    load_settings,
    normalize_cookie,
    normalize_url,
    parse_env_file,
    write_settings,
)
from bakalari_mcp.paths import config_file_in_use, data_dir
from bakalari_mcp.util import get_tz

LOGINS = (
    ("cookie", "Paste a browser cookie once; the keeper service keeps it alive (recommended on a server)"),
    ("password", "Username and password (REST API, no cookie)"),
    ("extension", "Browser extension; your computer keeps the cookie fresh"),
)
COOKIE_HOWTO = (
    "Log in to the school website in your browser. Open the developer tools (F12, or Cmd+Option+I on a Mac),\n"
    "Network tab, reload the page, click the first request and copy the value of the 'Cookie' request header.\n"
    "Then close the tab WITHOUT logging out (logging out ends the session)."
)


# ------------------------------------------------------------------ prompts


def _ask(prompt: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip() or default
        if answer or not required:
            return answer
        print("  please enter a value")


def _ask_secret(prompt: str, keep: str = "") -> str:
    hint = " (Enter keeps the current one)" if keep else ""
    return getpass.getpass(f"{prompt}{hint}: ").strip() or keep


def _choose(prompt: str, options: tuple[tuple[str, str], ...]) -> str:
    print(prompt)
    for number, (_, label) in enumerate(options, 1):
        print(f"  {number}) {label}")
    while True:
        answer = input("Choose [1]: ").strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        print("  please enter a number from the list")


def _yes(prompt: str, default: bool = True) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


# ------------------------------------------------------------------ settings


def _school_url(text: str) -> str:
    url = normalize_url(text)
    if not re.match(r"^https?://[^/]+\.[^/]+", url):
        raise ConfigError(f"{text!r} does not look like a web address (for example https://school.example.cz).")
    return url


def _time_zone(text: str) -> str:
    if get_tz(text) is None:
        raise ConfigError(f"{text!r} is not a known time zone (for example Europe/Prague).")
    return text


def _login_updates(
    login: str,
    current: dict[str, str],
    *,
    cookie: str | None = None,
    username: str | None = None,
    password: str | None = None,
    receiver_host: str | None = None,
    receiver_port: str | None = None,
) -> tuple[dict[str, str | None], str | None, list[str]]:
    """The settings for one login, the background service it needs (or None), and notes to show the user."""
    default_file = str(data_dir() / "session.json")
    if login == "cookie":
        if not cookie:
            raise ConfigError("A cookie is needed for the cookie login (pipe it in with --cookie-stdin).")
        file = current.get("BAKALARI_COOKIE_FILE") or default_file
        cookiestore.save(Path(file).expanduser(), normalize_cookie(cookie))
        updates = {"BAKALARI_COOKIE_FILE": file, "BAKALARI_COOKIE": None, "BAKALARI_USERNAME": None, "BAKALARI_PASSWORD": None}
        return updates, "keeper", [f"Cookie saved to {file}."]
    if login == "password":
        user = username or current.get("BAKALARI_USERNAME", "")
        secret = password or current.get("BAKALARI_PASSWORD", "")
        if not user or not secret:
            raise ConfigError("A username and a password are needed for the password login.")
        updates = {"BAKALARI_USERNAME": user, "BAKALARI_PASSWORD": secret, "BAKALARI_COOKIE_FILE": None, "BAKALARI_COOKIE": None}
        return updates, None, ["The password is stored only in the settings file (mode 600 where the system supports it)."]
    if login == "extension":
        token = current.get("BAKALARI_RECEIVER_TOKEN", "")
        if len(token) < RECEIVER_TOKEN_MIN:
            token = secrets.token_urlsafe(24)
        host = receiver_host or current.get("BAKALARI_RECEIVER_HOST") or "127.0.0.1"
        port = str(receiver_port or current.get("BAKALARI_RECEIVER_PORT") or 8765)
        file = current.get("BAKALARI_COOKIE_FILE") or default_file
        updates = {
            "BAKALARI_COOKIE_FILE": file, "BAKALARI_COOKIE": None, "BAKALARI_USERNAME": None, "BAKALARI_PASSWORD": None,
            "BAKALARI_RECEIVER_HOST": host, "BAKALARI_RECEIVER_PORT": port, "BAKALARI_RECEIVER_TOKEN": token,
        }
        notes = [
            "In the browser extension's options enter:",
            f"  Server address: http://{host}:{port}",
            f"  Token:          {token}",
            "Keep the token private.",
        ]
        return updates, "receiver", notes
    raise ConfigError(f"unknown login {login!r}")


def _finish(path: Path, updates: dict[str, str | None], service: str | None, install_service: bool,
            check: bool, notes: list[str]) -> int:
    write_settings(path, updates)
    print(f"Settings saved to {path}")
    for note in notes:
        print(note)
    if check:
        print("")
        from bakalari_mcp import server

        asyncio.run(server._check())  # prints OK or FAILED with the reason
    if service and install_service:
        for line in services.install(service):
            print(line)
    elif service:
        print(f"To run it in the background later: bakalari-mcp service install {service}")
    return 0


# ------------------------------------------------------------------ setup


def setup(args) -> int:
    path = config_file_in_use()
    current = parse_env_file(path)
    scripted = any([args.login, args.url, args.cookie_stdin, args.password_stdin, args.timezone])
    if not scripted and not sys.stdin.isatty():
        print("FAILED: setup needs a terminal, or flags such as --login cookie --cookie-stdin (see --help).", file=sys.stderr)
        return 1
    try:
        if scripted:
            login, url, tz, login_args = _scripted(args, current)
            install_service = bool(args.service)
            check = not args.no_check
        else:
            login, url, tz, login_args = _interactive(current, path)
            install_service = None  # decided below, after the login is saved
            check = None
        updates, service, notes = _login_updates(login, current, **login_args)
        updates.update({"BAKALARI_URL": url, "BAKALARI_TZ": tz})
        if not scripted:
            install_service = _yes(f"Start the {service} service in the background now (it starts at login)?") if service else False
            check = _yes("Test the login now?")
        return _finish(path, updates, service, install_service, check, notes)
    except ConfigError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


def _interactive(current: dict[str, str], path: Path) -> tuple[str, str, str, dict]:
    print("bakalari-mcp setup")
    print(f"Settings file: {path}")
    print("")
    url = _school_url(_ask("School website address, e.g. https://school.example.cz", current.get("BAKALARI_URL", ""), required=True))
    tz = _time_zone(_ask("Time zone", current.get("BAKALARI_TZ") or "Europe/Prague"))
    login = _choose("How should the server log in to Bakaláři?", LOGINS)
    args: dict = {}
    if login == "cookie":
        print(COOKIE_HOWTO)
        args["cookie"] = _ask_secret("Paste the Cookie header value (hidden)")
    elif login == "password":
        args["username"] = _ask("Username", current.get("BAKALARI_USERNAME", ""), required=True)
        args["password"] = _ask_secret("Password", keep=current.get("BAKALARI_PASSWORD", ""))
    else:
        args["receiver_host"] = _ask("Address the receiver listens on (127.0.0.1 = this computer only)",
                                     current.get("BAKALARI_RECEIVER_HOST") or "127.0.0.1")
        args["receiver_port"] = _ask("Port", current.get("BAKALARI_RECEIVER_PORT") or "8765")
    return login, url, tz, args


def _scripted(args, current: dict[str, str]) -> tuple[str, str, str, dict]:
    if not args.login:
        raise ConfigError("--login cookie, password or extension is required when there is no terminal.")
    url_text = args.url or current.get("BAKALARI_URL", "")
    if not url_text:
        raise ConfigError("--url is required: the school website address.")
    url = _school_url(url_text)
    tz = _time_zone(args.timezone or current.get("BAKALARI_TZ") or "Europe/Prague")
    values: dict = {}
    if args.login == "cookie":
        if not args.cookie_stdin:
            raise ConfigError("--cookie-stdin is needed for the cookie login (the cookie is read from standard input).")
        values["cookie"] = sys.stdin.read()
    elif args.login == "password":
        values["username"] = args.username
        if args.password_stdin:
            values["password"] = sys.stdin.readline().rstrip("\r\n")
    else:
        values["receiver_host"] = args.receiver_host
        values["receiver_port"] = str(args.receiver_port) if args.receiver_port else None
    return args.login, url, tz, values


# ------------------------------------------------------------------ connector address for a tunnel


def _hosts(text: str) -> list[str]:
    return [h.strip().removeprefix("https://").removeprefix("http://").split("/")[0] for h in text.split(",") if h.strip()]


def print_connector(host: str, port: int, token: str) -> None:
    print("")
    print("Connector address (give this to Claude; anyone who has it can read your school data):")
    print(f"  https://{host}/{token}/mcp")
    print("Local address (for testing on this computer):")
    print(f"  http://127.0.0.1:{port}/{token}/mcp")
    print("")
    print("Cloudflare tunnel (config.yml of cloudflared):")
    print("  ingress:")
    print(f"    - hostname: {host}")
    print(f"      service: http://localhost:{port}")
    print("    - service: http_status:404")
    print("")
    print("Start the HTTP service in the background: bakalari-mcp service install http")


def http_setup(args) -> int:
    path = config_file_in_use()
    current = parse_env_file(path)
    try:
        hosts = _hosts(",".join(args.hosts or [])) or _hosts(current.get("BAKALARI_HTTP_HOSTS", ""))
        if not hosts and sys.stdin.isatty():
            hosts = _hosts(_ask("Public host name of your Cloudflare tunnel, e.g. mcp.example.com", required=True))
        if not hosts:
            raise ConfigError("give the public host name of the tunnel: bakalari-mcp http-setup --host mcp.example.com")
        port = int(args.port or current.get("BAKALARI_HTTP_PORT") or 8770)
        token = current.get("BAKALARI_HTTP_TOKEN", "")
        if args.new_token or len(token) < HTTP_TOKEN_MIN:
            token = secrets.token_urlsafe(32)
            print("Created a new secret." if not args.new_token else "Replaced the secret.")
        write_settings(path, {"BAKALARI_HTTP_HOSTS": ",".join(hosts), "BAKALARI_HTTP_PORT": str(port),
                              "BAKALARI_HTTP_TOKEN": token})
    except (ConfigError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Settings saved to {path}")
    print_connector(hosts[0], port, token)
    return 0


def show_url(args) -> int:
    settings = load_settings()
    hosts = _hosts(settings.get("BAKALARI_HTTP_HOSTS", ""))
    token = settings.get("BAKALARI_HTTP_TOKEN", "")
    if not hosts or not token:
        print("FAILED: no connector address yet. Run: bakalari-mcp http-setup --host mcp.example.com", file=sys.stderr)
        return 1
    print(f"https://{hosts[0]}/{token}/mcp")
    return 0
