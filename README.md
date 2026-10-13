# Bakaláři MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude Desktop, Claude Code, …) read your
[Bakaláři](https://bakalari.cz/) school data: timetable, marks, homework, messages, absence. Read-only.

## What's in 2.0

- **One login, reused**: the access token is cached and refreshed instead of logging in on every call.
- **Readable output**: names instead of IDs, times in the timetable, cancelled/substituted lessons flagged,
  weighted averages for marks, absence percentages.
- **Raw data**: `raw=true` on a tool gives the school's own data (the JSON the website or REST API returns). Pages
  that exist only as HTML (marks, user name, messages) return the parsed data instead. Subjects never include
  phone numbers.
- **Parameters**: subject filters (accent-insensitive), date arguments (`2026-10-09`, `9.10.`, `today`, `zítra`),
  unread-only / search / limit for messages, homework by deadline.
- **`get_overview`**: a daily briefing in one call (today and next school day, new marks, homework due, unread
  messages, unsolved absence). Partial failures are reported per section.
- **Robust**: retries on 429/5xx, one automatic re-login on 401, clear error messages (wrong password, wrong URL,
  school unreachable, session ended), short response cache, works around Bakaláři versions that need `POST` for
  Komens.
- **Prompts**: `daily_briefing`, `homework_plan`, `marks_report`.

## Tools

| Tool | Purpose |
|---|---|
| `get_overview(date)` | Daily briefing |
| `get_user` | Profile, class, school |
| `get_marks(subject, since, only_new, summary_only)` | Marks + averages |
| `get_timetable_day(date)` / `get_timetable_actual(date)` | One day / whole week, with changes |
| `get_timetable_permanent` | Regular timetable |
| `get_homeworks(include_done, subject, due_from, due_until)` | Homework by deadline |
| `get_subjects` | Subjects and teachers |
| `get_events(days_ahead)` | Upcoming school events |
| `get_absence` | Absence overview |
| `get_messages_received(limit, unread_only, search, text_chars)` | Komens inbox |
| `get_messages_sent`, `get_noticeboard` | Komens sent / noticeboard |

## Setup

Requires Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env     # then fill in your details (or run deploy.sh on a Linux server, it asks you)
uv run server.py --check # logs in and prints your name (or, with a cookie, whether the session is alive)
```

Configuration (environment variables win over `.env`; `.env.example` lists every setting):

| Variable | |
|---|---|
| `BAKALARI_URL` | Base address of the school's Bakaláři (default `https://school.example.cz`) |
| `BAKALARI_COOKIE_FILE` | Where the session cookie is kept (`deploy.sh` sets it to `session.json`); the keeper or the browser extension writes it |
| `BAKALARI_COOKIE` | A one-off `Cookie` header copied from the logged-in website (F12 → Network → any request → Request Headers); it expires with the website session |
| `BAKALARI_COOKIE_MAX_AGE` | Seconds without a renewed cookie before the tools report the session as ended (default 1200) |
| `BAKALARI_USERNAME` / `BAKALARI_PASSWORD` | Login without a cookie (REST API) |
| `BAKALARI_CACHE_TTL` | Seconds to cache responses (default 120, `0` disables) |
| `BAKALARI_TIMEOUT` | HTTP timeout in seconds (default 30) |
| `BAKALARI_TZ` | Time zone for "today" (default `Europe/Prague`) |
| `BAKALARI_ENV_FILE` | Use a different `.env` path |
| `BAKALARI_KEEPER_INTERVAL` | Seconds between the keeper's renewals (default 180) |
| `BAKALARI_RECEIVER_*` | The browser-extension receiver (`deploy.sh` option 3) |
| `BAKALARI_HTTP_*` | The HTTP service for a Cloudflare tunnel (`http-service.sh`) |

### Cookie login

With a cookie, the data tools read the school website's own pages. The REST API refuses website cookies, so it is not
used in this mode. Timetable, marks, homework, subjects, events, absence, messages and the noticeboard all work this
way.

A website session ends after 15 minutes without activity, so the cookie has to be renewed. The keeper below does that.
If the session ends anyway, the tools say so and you paste a fresh cookie. `uv run server.py --check` reports whether
the session is alive and how many minutes it has left.

Useful facts about cookie sessions (measured on the school server): the website session lasts 15 minutes without
activity; `GET /sessioninfo` shows the remaining time and `GET /sessionextend` renews it. `bash keepalive.sh`
measures how long a session can really be kept alive (log: `keepalive.log`).

## Paste a cookie once (the server keeps it alive)

`deploy.sh` option 1 stores the pasted cookie and starts the `bakalari-keeper` service (`server.py --keeper`). Every
3 minutes it calls the school's `/sessionextend`, saves the renewed cookie and logs how long the session has left
(`journalctl --user -u bakalari-keeper -f`). If the school ever ends the session it logs `SESSION EXPIRED`; paste a
fresh cookie with `bash deploy.sh` and it carries on. Whether a session can be renewed indefinitely is up to the
school; the log shows it.

## Browser extension (cookie login that keeps itself alive)

`extension/` is a small Chrome/Edge extension. Load it via `chrome://extensions` -> Developer mode -> Load unpacked.
It sends the school's session cookie to the receiver on your server (`server.py --receiver`, set up by `deploy.sh`
option 3) every few minutes and whenever the cookie changes, and calls the school's own `/sessionextend` so the
session does not time out while the browser is open. The receiver only listens where you tell it to
(default `127.0.0.1`) and only accepts requests carrying the secret token.

The keeper (option 1) and the receiver (option 3) write the same cookie file, so `deploy.sh` runs only one of them at a
time. Option 2 (username + password) does not use a cookie at all.

## Over HTTP for a Cloudflare tunnel

`bash http-service.sh` runs `server.py --http` as a service on `127.0.0.1:8770`. The MCP address is
`/<secret>/mcp`; every other path is a 404 and only the hostnames you list (`BAKALARI_HTTP_HOSTS`) are accepted.
Point the tunnel at `http://localhost:8770` and give Claude `https://<hostname>/<secret>/mcp`. Anyone who knows that
full address can read the school data, so keep it private; run the script again to rotate the secret.

## Claude Desktop

Local:

```json
{ "mcpServers": { "bakalari": { "command": "/absolute/path/to/uv",
  "args": ["--directory", "/absolute/path/to/bakalari-mcp", "run", "server.py"] } } }
```

On a server over SSH (key login required, no password prompt possible):

```json
{ "mcpServers": { "bakalari": { "command": "ssh",
  "args": ["-o", "BatchMode=yes", "user@server", "/home/user/bakalari-mcp/run-bakalari.sh"] } } }
```

## Deploy to a Debian/Ubuntu server

```bash
tar czf bakalari-mcp.tar.gz --exclude=.venv --exclude=.env --exclude=session.json --exclude=__pycache__ \
  --exclude=.pytest_cache --exclude=keepalive.log --exclude=probe-report.txt .
scp bakalari-mcp.tar.gz user@server:~/
ssh -t user@server "mkdir -p ~/bakalari-mcp && tar xzf ~/bakalari-mcp.tar.gz -C ~/bakalari-mcp && bash ~/bakalari-mcp/deploy.sh"
```

`deploy.sh` installs `uv`, syncs dependencies, asks how you log in (paste a cookie, username + password, or the browser
extension), tests the login, saves the settings in `.env` (mode 600) and writes the `run-bakalari.sh` launcher. Run it
again any time to change them (Enter keeps the current value).

## Development

```bash
uv run pytest
```

The tests run against a fake Bakaláři API (both the REST API and the school website), no real account needed.
