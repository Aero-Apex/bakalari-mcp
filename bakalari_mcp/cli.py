"""The `bakalari-mcp` command, also `python -m bakalari_mcp`. The same on Windows, macOS and Linux.

With no command it starts the MCP server over stdio, which is what Claude Desktop and Claude Code start. The old
flags of server.py (`--check`, `--keeper`, `--http`, ...) keep working.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from bakalari_mcp import __version__

LEGACY_FLAGS = {
    "--check": "check",
    "--http": "http",
    "--keeper": "keeper",
    "--receiver": "receiver",
    "--store-cookie": "store-cookie",
    "--probe": "probe",
    "--keepalive-test": "keepalive-test",
}


def _translate_legacy(argv: list[str]) -> list[str]:
    out = list(argv)
    for index, arg in enumerate(out):
        if arg in LEGACY_FLAGS:
            out[index] = LEGACY_FLAGS[arg]
            break
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bakalari-mcp",
        description="Bakaláři school data for Claude. Run without a command to start the MCP server (stdio).",
    )
    parser.add_argument("--config", metavar="FILE", help="settings file to use (same as BAKALARI_CONFIG)")
    parser.add_argument("--version", action="version", version=f"bakalari-mcp {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    commands.add_parser("serve", help="MCP server over stdio (the default)")

    setup = commands.add_parser("setup", help="set up the school login (asks questions, or use the flags)")
    setup.add_argument("--url", help="school website address, e.g. https://school.example.cz")
    setup.add_argument("--timezone", help="time zone for 'today' (default Europe/Prague)")
    setup.add_argument("--login", choices=["cookie", "password", "extension"], help="how to log in")
    setup.add_argument("--cookie-stdin", action="store_true", help="read the cookie from standard input (cookie login)")
    setup.add_argument("--username", help="username (password login)")
    setup.add_argument("--password-stdin", action="store_true", help="read the password from standard input (password login)")
    setup.add_argument("--receiver-host", help="address the browser-extension receiver listens on (default 127.0.0.1)")
    setup.add_argument("--receiver-port", type=int, help="port of the receiver (default 8765)")
    setup.add_argument("--service", action="store_true", help="also start the background service (keeper or receiver)")
    setup.add_argument("--no-check", action="store_true", help="do not test the login afterwards")

    commands.add_parser("check", help="test the login and show the session state")

    config = commands.add_parser("config", help="show, validate or locate the settings")
    config.add_argument("action", nargs="?", choices=["show", "validate", "path"], default="show")

    commands.add_parser("store-cookie", help="save a cookie read from standard input")
    commands.add_parser("keeper", help="keep the school session alive (runs until stopped)")
    commands.add_parser("receiver", help="receive cookies from the browser extension (runs until stopped)")
    commands.add_parser("http", help="serve over HTTP for a Cloudflare tunnel (runs until stopped)")

    http_setup = commands.add_parser("http-setup", help="create the HTTP secret and print the connector address")
    http_setup.add_argument("--host", action="append", dest="hosts", metavar="HOSTNAME",
                            help="public host name of the tunnel (repeat for more)")
    http_setup.add_argument("--port", type=int, help="local port (default 8770)")
    http_setup.add_argument("--new-token", action="store_true", help="replace the secret (the connector address changes)")

    commands.add_parser("show-url", help="print the connector address")

    service = commands.add_parser("service", help="run keeper, http or receiver in the background at login")
    service.add_argument("action", choices=["install", "remove", "status"])
    service.add_argument("name", choices=["keeper", "http", "receiver"])

    commands.add_parser("probe", help="write a structure report of the school website (no personal data)")
    keepalive = commands.add_parser("keepalive-test", help="measure how long a cookie session survives")
    keepalive.add_argument("interval", nargs="?", type=float, default=240.0, help="seconds between pings (default 240)")
    keepalive.add_argument("minutes", nargs="?", type=float, default=240.0, help="how long to test (default 240)")
    return parser


def _ensure_console_streams(command: str) -> None:
    """A background service started without a console (Windows pythonw) has no stdout or stderr: log to a file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from bakalari_mcp.paths import data_dir

    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = open(logs / f"{command}.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def _set_log_level() -> None:
    import importlib

    from bakalari_mcp.config import LOG_LEVELS, load_settings

    importlib.import_module("bakalari_mcp.server")  # importing the server sets up logging; the level is applied after

    level = (load_settings().get("BAKALARI_LOG_LEVEL") or "WARNING").upper()
    logging.getLogger().setLevel(level if level in LOG_LEVELS else "WARNING")


def _utf8_streams() -> None:
    """Czech text in help and output must not crash a console with another code page (cp1251, cp1252...); the MCP
    protocol is UTF-8 anyway."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _serve(args) -> int:
    from bakalari_mcp import server

    server.mcp.run(transport="stdio")
    return 0


def _check(args) -> int:
    from bakalari_mcp import server

    return asyncio.run(server._check())


def _probe(args) -> int:
    from bakalari_mcp import server

    return asyncio.run(server._probe())


def _keepalive(args) -> int:
    from bakalari_mcp import server

    return asyncio.run(server._keepalive([str(args.interval), str(args.minutes)]))


def _config(args) -> int:
    from pathlib import Path

    from bakalari_mcp.config import SETTINGS, load_settings, validate
    from bakalari_mcp.paths import config_file_in_use, data_dir

    path = config_file_in_use()
    if args.action == "path":
        print(f"settings file: {path} ({'exists' if path.exists() else 'not created yet'})")
        print(f"data folder:   {data_dir()}")
        return 0
    settings = load_settings()
    if args.action == "show":
        print(f"settings file: {path}")
        for name, default, _, secret in SETTINGS:
            value = settings.get(name, "")
            if secret:
                shown = "set (hidden)" if value else "not set"
            else:
                shown = value or f"(default: {default})"
            print(f"  {name} = {shown}")
        return 0
    cookie_file = settings.get("BAKALARI_COOKIE_FILE", "")
    exists = Path(cookie_file).expanduser().exists() if cookie_file else None
    errors, warnings = validate(settings, cookie_file_exists=exists)
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}")
    if not errors:
        print("OK: the settings are valid.")
    return 1 if errors else 0


def _store_cookie(args) -> int:
    from bakalari_mcp import server

    return server._store_cookie()


def _keeper(args) -> int:
    from bakalari_mcp import server

    return server._keeper()


def _receiver(args) -> int:
    from bakalari_mcp import server

    return server._receiver()


def _http(args) -> int:
    from bakalari_mcp import server

    return server._http()


def _setup(args) -> int:
    from bakalari_mcp import wizard

    return wizard.setup(args)


def _http_setup(args) -> int:
    from bakalari_mcp import wizard

    return wizard.http_setup(args)


def _show_url(args) -> int:
    from bakalari_mcp import wizard

    return wizard.show_url(args)


def _service(args) -> int:
    from bakalari_mcp import services

    return services.run_command(args)


COMMANDS = {
    "serve": _serve,
    "setup": _setup,
    "check": _check,
    "config": _config,
    "store-cookie": _store_cookie,
    "keeper": _keeper,
    "receiver": _receiver,
    "http": _http,
    "http-setup": _http_setup,
    "show-url": _show_url,
    "service": _service,
    "probe": _probe,
    "keepalive-test": _keepalive,
}


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    args = build_parser().parse_args(_translate_legacy(list(sys.argv[1:] if argv is None else argv)))
    if args.config:
        os.environ["BAKALARI_CONFIG"] = args.config
    command = args.command or "serve"
    _ensure_console_streams(command)
    _set_log_level()
    return COMMANDS[command](args)


if __name__ == "__main__":
    sys.exit(main())
