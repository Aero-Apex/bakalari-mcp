"""v1: per-OS paths, settings files, the command line, the setup wizard and background services.

Windows, macOS and Linux behaviour is simulated (platform names are passed in); no service or scheduled task is
registered and no real settings are touched (tests/conftest.py isolates them)."""

import io
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakalari_mcp import __version__, cli, cookiestore, services, wizard
from bakalari_mcp.config import Config, ConfigError, load_settings, parse_env_file, validate, write_settings
from bakalari_mcp.paths import config_dir, config_file_in_use, data_dir


def completed(code=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


class FakeRunner:
    def __init__(self, answers=None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return self.answers.get(command[1] if len(command) > 1 else "", completed())


# ------------------------------------------------------------------ paths


def test_config_folder_follows_each_operating_system(tmp_path):
    win = config_dir({"APPDATA": str(tmp_path / "Roaming")}, platform="win32")
    assert win == tmp_path / "Roaming" / "bakalari-mcp"
    mac = config_dir({}, platform="darwin")
    assert mac == Path.home() / "Library" / "Application Support" / "bakalari-mcp"
    linux = config_dir({"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, platform="linux")
    assert linux == tmp_path / "xdg" / "bakalari-mcp"


def test_data_folder_can_be_moved_and_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert data_dir({"BAKALARI_DATA_DIR": "~/school-data"}) == tmp_path / "school-data"
    assert data_dir({}, platform="linux") == config_dir({}, platform="linux")


def test_settings_file_lookup_order(tmp_path):
    default = tmp_path / "cfg" / "bakalari-mcp" / "config.env"
    legacy = tmp_path / "old.env"
    explicit = tmp_path / "explicit.env"
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    assert config_file_in_use({**env, "BAKALARI_CONFIG": str(explicit)}, "linux", legacy) == explicit
    assert config_file_in_use(env, "linux", legacy) == default  # nothing exists yet: where setup will write
    legacy.write_text('BAKALARI_URL="https://old.example"\n', encoding="utf-8")
    assert config_file_in_use(env, "linux", legacy) == legacy  # an older checkout's .env is still used
    default.parent.mkdir(parents=True)
    default.write_text("", encoding="utf-8")
    assert config_file_in_use(env, "linux", legacy) == default  # the per-user file wins once it exists


# ------------------------------------------------------------------ settings file


def test_settings_survive_awkward_values_and_removals(tmp_path):
    path = tmp_path / "config.env"
    tricky = 'pa"ss\\word $HOME `x` # not a comment ěščř'
    write_settings(path, {"BAKALARI_PASSWORD": tricky, "BAKALARI_URL": "https://school.example.cz"})
    assert parse_env_file(path)["BAKALARI_PASSWORD"] == tricky
    write_settings(path, {"BAKALARI_PASSWORD": None})
    assert "BAKALARI_PASSWORD" not in parse_env_file(path)
    assert parse_env_file(path)["BAKALARI_URL"] == "https://school.example.cz"
    with pytest.raises(ConfigError):
        write_settings(path, {"BAKALARI_URL": "two\nlines"})


@pytest.mark.skipif(sys.platform.startswith("win"), reason="file modes are not enforced on Windows")
def test_settings_file_is_private_on_unix(tmp_path):
    path = write_settings(tmp_path / "config.env", {"BAKALARI_PASSWORD": "x"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_environment_overrides_the_settings_file(tmp_path):
    path = write_settings(tmp_path / "config.env", {"BAKALARI_TZ": "Europe/Prague", "BAKALARI_CACHE_TTL": "120"})
    settings = load_settings({"BAKALARI_CACHE_TTL": "0", "BAKALARI_TZ": ""}, path)
    assert settings["BAKALARI_CACHE_TTL"] == "0" and settings["BAKALARI_TZ"] == "Europe/Prague"


def test_config_expands_a_home_relative_cookie_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = Config.load({"BAKALARI_COOKIE_FILE": "~/session.json"}, tmp_path / "none.env")
    assert cfg.cookie_file == tmp_path / "session.json" and cfg.uses_cookie


def test_validate_reports_errors_and_warnings(tmp_path):
    good = {"BAKALARI_URL": "https://school.example.cz", "BAKALARI_COOKIE_FILE": str(tmp_path / "s.json")}
    errors, warnings = validate(good, cookie_file_exists=False)
    assert errors == [] and any("does not exist yet" in w for w in warnings)
    errors, _ = validate({"BAKALARI_URL": "nonsense", "BAKALARI_TZ": "Mars/Base", "BAKALARI_RECEIVER_PORT": "99999",
                          "BAKALARI_HTTP_TOKEN": "short", "BAKALARI_KEEPER_INTERVAL": "5"})
    text = " | ".join(errors)
    assert "web address" in text and "time zone" in text and "65535" in text and "24 characters" in text
    assert "KEEPER_INTERVAL" in text and "No login is configured" in text


# ------------------------------------------------------------------ command line


def test_old_server_flags_map_to_the_new_commands():
    assert cli._translate_legacy(["--check"]) == ["check"]
    assert cli._translate_legacy(["--keepalive-test", "240", "480"]) == ["keepalive-test", "240", "480"]
    assert cli._translate_legacy(["--config", "x.env", "--keeper"]) == ["--config", "x.env", "keeper"]


def test_every_command_is_registered_and_version_is_reported(capsys):
    parser = cli.build_parser()
    for command in ("setup", "check", "config", "store-cookie", "keeper", "receiver", "http", "http-setup",
                    "show-url", "service", "probe", "keepalive-test", "serve"):
        assert parser.parse_args([command] + (["install", "keeper"] if command == "service" else [])).command == command
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0 and __version__ in capsys.readouterr().out


def test_config_show_hides_secrets(tmp_path, monkeypatch, capsys):
    write_settings(Path(tmp_path / "config.env"), {"BAKALARI_PASSWORD": "TOP-SECRET-PW", "BAKALARI_URL": "https://a.cz"})
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "config.env"))
    assert cli.main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "TOP-SECRET-PW" not in out and "BAKALARI_PASSWORD = set (hidden)" in out and "https://a.cz" in out


# ------------------------------------------------------------------ setup wizard


def test_scripted_password_login_stores_the_password_and_clears_the_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "config.env"))
    monkeypatch.setenv("BAKALARI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("pw-123\n"))
    args = cli.build_parser().parse_args(["setup", "--login", "password", "--url", "school.example.cz",
                                          "--username", "demo", "--password-stdin", "--no-check"])
    assert wizard.setup(args) == 0
    settings = parse_env_file(tmp_path / "config.env")
    assert settings["BAKALARI_URL"] == "https://school.example.cz" and settings["BAKALARI_PASSWORD"] == "pw-123"
    assert "BAKALARI_COOKIE_FILE" not in settings and "BAKALARI_COOKIE" not in settings


def test_scripted_cookie_login_saves_the_cookie_in_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "config.env"))
    monkeypatch.setenv("BAKALARI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("Cookie: BakaAuth=FAKE-VALUE; x=1\n"))
    args = cli.build_parser().parse_args(["setup", "--login", "cookie", "--url", "https://school.example.cz",
                                          "--cookie-stdin", "--no-check"])
    assert wizard.setup(args) == 0
    stored = cookiestore.load(tmp_path / "data" / "session.json")
    assert stored[0] == "BakaAuth=FAKE-VALUE; x=1"
    assert parse_env_file(tmp_path / "config.env")["BAKALARI_COOKIE_FILE"] == str(tmp_path / "data" / "session.json")


def test_setup_without_a_terminal_or_login_explains_what_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "config.env"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["setup", "--url", "https://school.example.cz", "--no-check"]) == 1
    assert "--login" in capsys.readouterr().err


def test_extension_login_generates_a_receiver_token_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "config.env"))
    monkeypatch.setenv("BAKALARI_DATA_DIR", str(tmp_path / "data"))
    args = cli.build_parser().parse_args(["setup", "--login", "extension", "--url", "https://school.example.cz",
                                          "--no-check"])
    wizard.setup(args)
    first = parse_env_file(tmp_path / "config.env")["BAKALARI_RECEIVER_TOKEN"]
    assert len(first) >= 16
    wizard.setup(args)
    assert parse_env_file(tmp_path / "config.env")["BAKALARI_RECEIVER_TOKEN"] == first  # kept, not regenerated


def test_http_setup_creates_keeps_and_rotates_the_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "config.env"))
    http = cli.build_parser().parse_args(["http-setup", "--host", "mcp.example.com"])
    assert wizard.http_setup(http) == 0
    token = parse_env_file(tmp_path / "config.env")["BAKALARI_HTTP_TOKEN"]
    assert len(token) >= 24
    assert wizard.http_setup(http) == 0
    assert parse_env_file(tmp_path / "config.env")["BAKALARI_HTTP_TOKEN"] == token
    assert wizard.http_setup(cli.build_parser().parse_args(["http-setup", "--new-token"])) == 0  # hosts kept
    assert parse_env_file(tmp_path / "config.env")["BAKALARI_HTTP_TOKEN"] != token
    assert parse_env_file(tmp_path / "config.env")["BAKALARI_HTTP_HOSTS"] == "mcp.example.com"
    capsys.readouterr()
    assert wizard.show_url(None) == 0
    assert capsys.readouterr().out.strip().startswith("https://mcp.example.com/")


def test_show_url_explains_when_there_is_no_connector(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "missing.env"))
    assert wizard.show_url(None) == 1
    assert "http-setup" in capsys.readouterr().err


# ------------------------------------------------------------------ background services


def test_systemd_unit_quotes_arguments_and_escapes_percent():
    unit = services.systemd_unit("keeper", ["/opt/my python/bin/python", "-m", "bakalari_mcp", "--config", "/x/%h.env"])
    assert 'ExecStart="/opt/my python/bin/python" -m bakalari_mcp --config /x/%%h.env' in unit
    assert "Restart=always" in unit and "WantedBy=default.target" in unit


def test_linux_service_writes_a_unit_and_uses_systemctl(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    runner = FakeRunner()
    lines = services.install("keeper", platform="linux", run=runner, config_file=tmp_path / "config.env")
    unit = tmp_path / "xdg" / "systemd" / "user" / "bakalari-keeper.service"
    assert unit.exists() and "keeper" in unit.read_text(encoding="utf-8")
    assert ["systemctl", "--user", "restart", "bakalari-keeper.service"] in runner.calls
    assert any("journalctl" in line for line in lines)
    services.remove("keeper", platform="linux", run=runner)
    assert not unit.exists()


def test_macos_service_writes_a_launch_agent_and_bootstraps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(services.os, "getuid", lambda: 501, raising=False)
    runner = FakeRunner()
    services.install("http", platform="darwin", run=runner, config_file=tmp_path / "config.env")
    plist = tmp_path / "Library" / "LaunchAgents" / "eu.bakalari-mcp.http.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == "eu.bakalari-mcp.http" and data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert data["ProgramArguments"][-1] == "http"
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in runner.calls


def test_windows_task_runs_at_logon_with_the_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BAKALARI_DATA_DIR", str(tmp_path / "data"))
    runner = FakeRunner()
    services.install("receiver", platform="win32", run=runner, config_file=tmp_path / "config.env")
    create = next(c for c in runner.calls if c[:2] == ["schtasks", "/Create"])
    assert create[create.index("/TN") + 1] == "bakalari-mcp-receiver"
    assert create[create.index("/SC") + 1] == "ONLOGON" and create[create.index("/RL") + 1] == "LIMITED"
    command = create[create.index("/TR") + 1]
    assert "-m bakalari_mcp" in command and "--config" in command and command.endswith("receiver")
    services.remove("receiver", platform="win32", run=runner)
    assert ["schtasks", "/Delete", "/TN", "bakalari-mcp-receiver", "/F"] in runner.calls


def test_unknown_service_and_unsupported_platform_fail_clearly():
    with pytest.raises(services.ServiceError, match="unknown service"):
        services.install("backup", platform="linux", run=FakeRunner())
    with pytest.raises(services.ServiceError, match="no background service support"):
        services.install("keeper", platform="freebsd", run=FakeRunner())


def test_console_less_background_runs_log_to_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BAKALARI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cli._ensure_console_streams("keeper")
    print("hello from the background", file=sys.stdout, flush=True)
    assert "hello from the background" in (tmp_path / "data" / "logs" / "keeper.log").read_text(encoding="utf-8")
    sys.stdout.close()
    sys.stderr.close()


def test_interactive_setup_asks_each_question_and_saves(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "config.env"))
    answers = iter(["school.example.cz", "", "2", "demo", "n"])  # url, time zone, password login, username, no test
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "pw-typed")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    assert cli.main(["setup"]) == 0
    settings = parse_env_file(tmp_path / "config.env")
    assert settings["BAKALARI_URL"] == "https://school.example.cz" and settings["BAKALARI_TZ"] == "Europe/Prague"
    assert settings["BAKALARI_USERNAME"] == "demo" and settings["BAKALARI_PASSWORD"] == "pw-typed"
    assert "Settings saved to" in capsys.readouterr().out
