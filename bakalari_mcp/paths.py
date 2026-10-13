"""Where bakalari-mcp keeps its files, on Windows, macOS and Linux.

The settings file is searched in this order: BAKALARI_CONFIG (or the older BAKALARI_ENV_FILE), the per-user config file,
the `.env` of an older source checkout. When none exists, `setup` writes the per-user file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

APP_DIR_NAME = "bakalari-mcp"
CONFIG_FILE_NAME = "config.env"
LEGACY_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"  # a source checkout made before v1


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def config_dir(environ: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """%APPDATA%\\bakalari-mcp on Windows, ~/Library/Application Support/bakalari-mcp on macOS,
    $XDG_CONFIG_HOME/bakalari-mcp (default ~/.config/bakalari-mcp) elsewhere."""
    env, plat = _env(environ), sys.platform if platform is None else platform
    if plat.startswith("win"):
        base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path(env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / APP_DIR_NAME


def data_dir(environ: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Where the session cookie and logs live: BAKALARI_DATA_DIR, or the config directory."""
    env = _env(environ)
    explicit = env.get("BAKALARI_DATA_DIR")
    return Path(explicit).expanduser() if explicit else config_dir(env, platform)


def default_config_file(environ: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    return config_dir(environ, platform) / CONFIG_FILE_NAME


def config_file_in_use(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    legacy: Path = LEGACY_ENV_FILE,
) -> Path:
    """The settings file that is read (and that `setup` writes to)."""
    env = _env(environ)
    explicit = env.get("BAKALARI_CONFIG") or env.get("BAKALARI_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    default = default_config_file(env, platform)
    if default.exists():
        return default
    if legacy.exists():
        return legacy
    return default
