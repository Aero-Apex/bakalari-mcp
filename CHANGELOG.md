# Changelog

## 1.0.0

First stable release.

### New

- **One command on every system:** `bakalari-mcp` works on Windows, macOS and Linux. Install it with
  `uv tool install git+https://github.com/Aero-Apex/bakalari-mcp`.
- **Setup wizard:** `bakalari-mcp setup` asks questions, or takes flags for scripts. Secrets come from standard input.
  Login options: a pasted cookie kept alive by the keeper, username and password, or the browser extension.
- **Settings:** one settings file per user, a `config show | validate | path` command, and every setting documented.
  Environment variables override the file.
- **Background services:** `bakalari-mcp service install | remove | status` for the keeper, the receiver and the HTTP
  connector. systemd on Linux, a LaunchAgent on macOS, a scheduled task at logon on Windows.
- **HTTP connector:** `bakalari-mcp http-setup` creates the secret and prints the connector address and the Cloudflare
  tunnel snippet. `show-url` prints it again.
- **Time zone and cache settings** are validated; `config validate` reports mistakes before the server starts.

### Changed

- The command line lives in `bakalari_mcp.cli`. The root `server.py` and its old flags (`--check`, `--keeper`,
  `--http`, `--receiver`, `--store-cookie`, `--probe`, `--keepalive-test`) still work.
- The server code moved into the package (`bakalari_mcp/server.py`), so it installs with the package.
- `deploy.sh` is now a thin wrapper around `bakalari-mcp setup`. `http-service.sh`, `show-url.sh` and `keepalive.sh`
  were removed; their jobs are the `http-setup`, `show-url` and `keepalive-test` commands.
- Error messages refer to the new commands instead of the bash scripts.

### Fixed

- Setting up and running the services only worked on Linux (bash, systemd, `hostname -I`, `/dev/urandom`).
- The settings file was only found inside the project folder, so an installed package could not find it.
- A cookie file path starting with `~` was not expanded, so the cookie was silently not found.
- `probe` wrote its report into the package folder, which an installed package cannot write to. It now writes to the
  data folder.
- The permanent timetable request used the computer's clock date instead of the configured time zone, which could ask
  for the wrong day for an hour or two around midnight.
- Command output now uses UTF-8 on every system, so Czech text in help and messages does not crash consoles with other
  code pages.

### Not in this release

- No licence has been chosen yet.
- The macOS background service is tested by unit tests only.
