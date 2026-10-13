"""Keeping `keeper`, `http` and `receiver` running in the background.

Linux uses systemd (user service), macOS launchd (LaunchAgent), Windows Task Scheduler (a task that starts at logon).
Each platform's commands are built here and run through an injectable runner, so they can be tested anywhere.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from bakalari_mcp.paths import config_file_in_use, data_dir

NAMES = ("keeper", "http", "receiver")
DESCRIPTIONS = {
    "keeper": "Bakalari MCP session keeper (renews the school cookie)",
    "http": "Bakalari MCP over HTTP (for a Cloudflare tunnel)",
    "receiver": "Bakalari MCP cookie receiver (the browser extension sends the cookie here)",
}
LABEL_PREFIX = "eu.bakalari-mcp"

Runner = Callable[..., subprocess.CompletedProcess]


class ServiceError(Exception):
    """A service command failed or the platform has no service support."""


def program_args(name: str, config_file: Path, platform: str = sys.platform) -> list[str]:
    """The command a service runs: this Python, the package, the config file in use, then the command name."""
    exe = sys.executable
    if platform.startswith("win"):
        pythonw = Path(exe).with_name("pythonw.exe")
        if pythonw.exists():
            exe = str(pythonw)  # no console window at logon
    return [exe, "-m", "bakalari_mcp", "--config", str(config_file), name]


def _run(run: Runner, command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = run(command, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ServiceError(f"{' '.join(command[:3])} failed: {detail or 'exit code ' + str(proc.returncode)}")
    return proc


# ----------------------------------------------------------------- Linux (systemd --user)


def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _systemd_arg(arg: str) -> str:
    arg = arg.replace("%", "%%")  # systemd treats % as a specifier
    if re.fullmatch(r"[A-Za-z0-9_./:=@+%-]+", arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd_unit(name: str, args: list[str]) -> str:
    return "\n".join([
        "[Unit]",
        f"Description={DESCRIPTIONS[name]}",
        "After=network-online.target",
        "",
        "[Service]",
        "ExecStart=" + " ".join(_systemd_arg(a) for a in args),
        "Restart=always",
        "RestartSec=10",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _install_linux(name: str, args: list[str], run: Runner) -> list[str]:
    unit_dir = _xdg_config() / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / f"bakalari-{name}.service"
    unit.write_text(systemd_unit(name, args), encoding="utf-8")
    _run(run, ["systemctl", "--user", "daemon-reload"])
    _run(run, ["systemctl", "--user", "enable", unit.name])
    _run(run, ["systemctl", "--user", "restart", unit.name])
    return [
        f"Installed {unit}",
        f"Logs: journalctl --user -u {unit.name} -f",
        "It starts at login. To keep it running without a login session: sudo loginctl enable-linger $USER",
    ]


def _remove_linux(name: str, run: Runner) -> list[str]:
    unit = _xdg_config() / "systemd" / "user" / f"bakalari-{name}.service"
    _run(run, ["systemctl", "--user", "disable", "--now", unit.name], check=False)
    if unit.exists():
        unit.unlink()
    _run(run, ["systemctl", "--user", "daemon-reload"], check=False)
    return [f"Removed {unit.name}."]


def _status_linux(name: str, run: Runner) -> list[str]:
    unit = f"bakalari-{name}.service"
    active = _run(run, ["systemctl", "--user", "is-active", unit], check=False).stdout.strip() or "unknown"
    enabled = _run(run, ["systemctl", "--user", "is-enabled", unit], check=False).stdout.strip() or "unknown"
    return [f"{unit}: {active}, starts at login: {enabled}"]


# ------------------------------------------------------------- macOS (launchd LaunchAgent)


def launch_agent_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.{name}.plist"


def log_folder() -> Path:
    return Path.home() / "Library" / "Logs" / "bakalari-mcp"


def launchd_plist(name: str, args: list[str]) -> bytes:
    log = str(log_folder() / f"{name}.log")
    return plistlib.dumps({
        "Label": f"{LABEL_PREFIX}.{name}",
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _install_macos(name: str, args: list[str], run: Runner) -> list[str]:
    plist = launch_agent_path(name)
    plist.parent.mkdir(parents=True, exist_ok=True)
    log_folder().mkdir(parents=True, exist_ok=True)
    plist.write_bytes(launchd_plist(name, args))
    domain = f"gui/{os.getuid()}"
    _run(run, ["launchctl", "bootout", domain, str(plist)], check=False)  # replaces an older copy
    _run(run, ["launchctl", "bootstrap", domain, str(plist)])
    return [f"Installed {plist}", f"Logs: {log_folder() / (name + '.log')}"]


def _remove_macos(name: str, run: Runner) -> list[str]:
    plist = launch_agent_path(name)
    _run(run, ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False)
    if plist.exists():
        plist.unlink()
    return [f"Removed {plist.name}."]


def _status_macos(name: str, run: Runner) -> list[str]:
    proc = _run(run, ["launchctl", "print", f"gui/{os.getuid()}/{LABEL_PREFIX}.{name}"], check=False)
    return [f"{LABEL_PREFIX}.{name}: " + ("loaded" if proc.returncode == 0 else "not loaded")]


# ------------------------------------------------------ Windows (Task Scheduler, at logon)


def task_name(name: str) -> str:
    return f"bakalari-mcp-{name}"


def _windows_command(args: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in args)


def _install_windows(name: str, args: list[str], run: Runner) -> list[str]:
    _run(run, ["schtasks", "/Create", "/TN", task_name(name), "/TR", _windows_command(args),
               "/SC", "ONLOGON", "/RL", "LIMITED", "/F"])
    _run(run, ["schtasks", "/Run", "/TN", task_name(name)], check=False)
    return [f"Installed scheduled task {task_name(name)} (starts at logon).",
            f"Logs: {data_dir() / 'logs' / (name + '.log')}"]


def _remove_windows(name: str, run: Runner) -> list[str]:
    _run(run, ["schtasks", "/Delete", "/TN", task_name(name), "/F"], check=False)
    return [f"Removed scheduled task {task_name(name)}."]


def _status_windows(name: str, run: Runner) -> list[str]:
    proc = _run(run, ["schtasks", "/Query", "/TN", task_name(name), "/FO", "LIST"], check=False)
    if proc.returncode != 0:
        return [f"{task_name(name)}: not installed"]
    state = [line.strip() for line in proc.stdout.splitlines() if line.lower().strip().startswith("status")]
    return [f"{task_name(name)}: " + (state[0] if state else "installed")]


# ------------------------------------------------------------------ public functions


def _check_name(name: str) -> None:
    if name not in NAMES:
        raise ServiceError(f"unknown service {name!r}; choose one of: {', '.join(NAMES)}")


def install(name: str, platform: str = sys.platform, run: Runner = subprocess.run, config_file: Path | None = None) -> list[str]:
    _check_name(name)
    args = program_args(name, config_file or config_file_in_use(), platform)
    if platform == "darwin":
        return _install_macos(name, args, run)
    if platform.startswith("win"):
        return _install_windows(name, args, run)
    if platform.startswith("linux"):
        return _install_linux(name, args, run)
    raise ServiceError(f"no background service support on {platform}; run `bakalari-mcp {name}` yourself.")


def remove(name: str, platform: str = sys.platform, run: Runner = subprocess.run) -> list[str]:
    _check_name(name)
    if platform == "darwin":
        return _remove_macos(name, run)
    if platform.startswith("win"):
        return _remove_windows(name, run)
    if platform.startswith("linux"):
        return _remove_linux(name, run)
    raise ServiceError(f"no background service support on {platform}.")


def status(name: str, platform: str = sys.platform, run: Runner = subprocess.run) -> list[str]:
    _check_name(name)
    if platform == "darwin":
        return _status_macos(name, run)
    if platform.startswith("win"):
        return _status_windows(name, run)
    if platform.startswith("linux"):
        return _status_linux(name, run)
    raise ServiceError(f"no background service support on {platform}.")


def run_command(args) -> int:
    """`bakalari-mcp service install|remove|status keeper|http|receiver`."""
    action = {"install": install, "remove": remove, "status": status}[args.action]
    try:
        lines = action(args.name)
    except ServiceError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0
