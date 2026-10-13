# Bakaláři MCP

Gives Claude (Desktop, Code, or any MCP client) read-only access to your school's
[Bakaláři](https://bakalari.cz/) data: timetable, marks, homework, messages, noticeboard, absence and events.
Works on Windows, macOS and Linux.

## What it can do

- **Daily briefing:** `get_overview` gives today's lessons, the next school day, new marks, homework due, unread
  messages and unsolved absence in one call.
- **Timetable:** the day, the week, or the permanent timetable, with substitutions and cancelled lessons flagged.
- **Marks, homework, subjects and events**, with subject filters that ignore accents and case.
- **Messages:** received and sent Komens messages with full text and attachment names, plus the noticeboard.
  Nothing is ever marked as read.
- **Two ways to log in:** a browser cookie kept alive in the background, or a username and password.
- **Remote access:** an HTTP connector for a Cloudflare tunnel, so claude.ai can reach it too.

## Install

You need Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/).

Install `uv` (once):

- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS and Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Install `bakalari-mcp` (works the same on every system):

```bash
uv tool install git+https://github.com/Aero-Apex/bakalari-mcp
bakalari-mcp --version
```

Update later with `uv tool upgrade bakalari-mcp`. From a clone of this repository, `uv sync` and then
`uv run bakalari-mcp ...` also work.

## Set up the school login

```bash
bakalari-mcp setup
```

The wizard asks for the school's web address and time zone, then how to log in:

1. **Paste a browser cookie once.** The keeper service renews it in the background. Best on a server or an always-on
   computer.
2. **Username and password.** Uses the school's REST API, no cookie.
3. **Browser extension.** Your computer keeps the cookie fresh (see [Browser extension](#browser-extension)).

Then it tests the login. The settings are saved to the file shown in [Settings](#settings).

For scripts and servers, pass the answers as flags. Secrets are read from standard input, so they never appear in the
command line or shell history:

```bash
printf '%s' "$PASSWORD" | bakalari-mcp setup --url https://school.example.cz --login password --username me --password-stdin
```

```bash
echo "Cookie: BakaAuth=..." | bakalari-mcp setup --url https://school.example.cz --login cookie --cookie-stdin --service
```

Run `bakalari-mcp setup --help` for every flag.

## Connect Claude

**Claude Desktop** keeps its settings in `claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{ "mcpServers": { "bakalari": { "command": "bakalari-mcp" } } }
```

Desktop apps on macOS may not see `~/.local/bin`, so if it doesn't start, replace `"bakalari-mcp"` with the full path
from `which bakalari-mcp` (or `where bakalari-mcp` on Windows).

**Claude Code:**

```bash
claude mcp add bakalari -- bakalari-mcp
```

**claude.ai** cannot reach your computer directly. Use the HTTP connector through a Cloudflare tunnel:

```bash
bakalari-mcp http-setup --host mcp.example.com
bakalari-mcp service install http
```

`http-setup` prints the connector address and a `cloudflared` ingress snippet. Add the snippet to your tunnel, then give
claude.ai the connector address. Anyone who has that address can read your school data, so keep it private. Run
`bakalari-mcp http-setup --new-token` to replace it. `bakalari-mcp show-url` prints it again.

## Background services

Three programs can run in the background, and `bakalari-mcp service` installs or removes them:

| Name | What it does |
|---|---|
| `keeper` | Renews the pasted school cookie every few minutes |
| `receiver` | Receives cookies from the browser extension |
| `http` | Serves the connector address for a tunnel |

```bash
bakalari-mcp service install keeper
bakalari-mcp service status keeper
bakalari-mcp service remove keeper
```

Each system uses its own mechanism, and the service starts at login:

- **Linux:** a systemd user service, `~/.config/systemd/user/bakalari-<name>.service`. Logs: `journalctl --user -u bakalari-<name> -f`.
  To keep it running without a login session: `sudo loginctl enable-linger $USER`.
- **macOS:** a LaunchAgent, `~/Library/LaunchAgents/eu.bakalari-mcp.<name>.plist`. Logs in `~/Library/Logs/bakalari-mcp/`.
- **Windows:** a scheduled task named `bakalari-mcp-<name>`, started at logon. Logs in the data folder (`logs/`).

## Settings

Settings live in one file, `config.env`, in a per-user folder:

- Windows: `%APPDATA%\bakalari-mcp\config.env`
- macOS: `~/Library/Application Support/bakalari-mcp/config.env`
- Linux: `$XDG_CONFIG_HOME/bakalari-mcp/config.env` (default `~/.config/bakalari-mcp/config.env`)

Environment variables with the same names override the file. `BAKALARI_CONFIG=/path/file.env` (or `--config`) uses a
different file. The session cookie and logs go to the data folder, the same folder unless `BAKALARI_DATA_DIR` says
otherwise.

```bash
bakalari-mcp config path       # which file is used
bakalari-mcp config show       # every setting; secrets are hidden
bakalari-mcp config validate   # reports mistakes
```

| Setting | Default | Meaning |
|---|---|---|
| `BAKALARI_URL` | `https://school.example.cz` | School website address |
| `BAKALARI_COOKIE_FILE` | `<data folder>/session.json` | Where the session cookie is kept |
| `BAKALARI_COOKIE` | | A cookie copied from the browser (expires with the website session) |
| `BAKALARI_USERNAME` | | Username for the password login |
| `BAKALARI_PASSWORD` | | Password for the password login |
| `BAKALARI_TZ` | `Europe/Prague` | Time zone used for "today" |
| `BAKALARI_TIMEOUT` | `30` | HTTP timeout in seconds |
| `BAKALARI_CACHE_TTL` | `120` | Seconds to cache responses (`0` turns the cache off) |
| `BAKALARI_COOKIE_MAX_AGE` | `1200` | Seconds without a renewed cookie before the session counts as ended |
| `BAKALARI_KEEPER_INTERVAL` | `180` | Seconds between the keeper's renewals (at least 30) |
| `BAKALARI_RECEIVER_HOST` | `127.0.0.1` | Address the browser-extension receiver listens on |
| `BAKALARI_RECEIVER_PORT` | `8765` | Port of the receiver |
| `BAKALARI_RECEIVER_TOKEN` | | Secret the extension sends to the receiver (at least 16 characters) |
| `BAKALARI_HTTP_HOST` | `127.0.0.1` | Address the HTTP connector listens on |
| `BAKALARI_HTTP_PORT` | `8770` | Port of the HTTP connector (point the tunnel here) |
| `BAKALARI_HTTP_HOSTS` | | Public host names of the tunnel, comma-separated |
| `BAKALARI_HTTP_TOKEN` | | Secret path of the connector (at least 24 characters) |
| `BAKALARI_DATA_DIR` | the config folder | Folder for the session cookie and logs |
| `BAKALARI_LOG_LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `BAKALARI_CONFIG` | | Use this settings file instead of the default one |

`.env.example` in this repository lists the same settings.

## Commands

| Command | What it does |
|---|---|
| `bakalari-mcp` or `bakalari-mcp serve` | The MCP server over stdio (what Claude starts) |
| `bakalari-mcp setup` | Set up the login (questions, or flags) |
| `bakalari-mcp check` | Test the login and show how long the session has left |
| `bakalari-mcp config show\|validate\|path` | Show, check or locate the settings |
| `bakalari-mcp service install\|remove\|status NAME` | Run keeper, receiver or http in the background |
| `bakalari-mcp keeper` / `receiver` / `http` | Run one of those in the foreground |
| `bakalari-mcp http-setup` / `show-url` | Create or show the connector address |
| `bakalari-mcp store-cookie` | Save a cookie read from standard input |
| `bakalari-mcp keepalive-test` | Measure how long a cookie session survives |

The old `server.py` entry point and its flags (`--check`, `--keeper`, `--http`, ...) keep working.

## Browser extension

`extension/` is a Chrome or Edge extension. It sends the school's session cookie to the receiver on your computer and
calls the school's `/sessionextend` while the browser is open, so the session does not time out.

1. Run `bakalari-mcp setup` and choose the browser extension option. It prints the receiver's address and a token.
2. In Chrome or Edge open `chrome://extensions` (or `edge://extensions`), turn on Developer mode, click Load unpacked
   and choose the `extension` folder.
3. Open the extension's options and enter the address and token from step 1.
4. Log in to the school website in that browser. The popup should report the cookies it sent.

The receiver accepts only requests with the token. Keep the token private.

## Platform support

| System | Tested |
|---|---|
| Windows 10 | the command, setup, settings, the tests and the built package; the scheduled-task commands are unit-tested and the status check has run, but a real install has not |
| Linux (Ubuntu, systemd) | everything, including a live school login and the background services |
| macOS | the command and the tests; the LaunchAgent commands are unit-tested but have not been run on a Mac yet |

Tests run on every system with `uv run pytest` (see [Development](#development)).

## Privacy and security

- The session cookie, passwords and tokens are stored only in the settings file and the data folder. On Linux and macOS
  both are created with mode 600. On Windows they sit in your user profile.
- The program never sends your data anywhere except the school's own servers and, for the HTTP connector, your tunnel.
- Reading messages never marks them as read.
- Subjects never include teachers' phone numbers.

## Development

```bash
uv sync
uv run pytest
uv build      # wheel and sdist in dist/
```

The tests run against fake school websites and the fake REST API, so no real account is needed.

## License

No license has been chosen yet. Until one is added, the code is all rights reserved by its author.
