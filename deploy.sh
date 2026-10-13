#!/usr/bin/env bash
# Installs / updates bakalari-mcp where this script lives, asks how to log in to Bakalari,
# verifies it and creates a launcher.
#   Run interactively:  bash deploy.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
RUNNER="$APP_DIR/run-bakalari.sh"
KEEPER="bakalari-keeper"
RECEIVER="bakalari-receiver"
export PATH="$HOME/.local/bin:$PATH"

if [ ! -t 0 ]; then
  echo "This script asks questions - run it with a terminal (ssh -t ...)." >&2
  exit 1
fi

echo "== Installing system packages (sudo password may be requested) =="
if ! command -v pipx >/dev/null 2>&1 && ! command -v uv >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y pipx
fi

echo "== Installing uv =="
command -v uv >/dev/null 2>&1 || pipx install uv

echo "== Installing Python dependencies =="
(cd "$APP_DIR" && uv sync --no-dev)

# ---- helpers -----------------------------------------------------------------
read_old() { # read_old KEY -> current value from .env (quotes stripped), or empty
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true
}
escape() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }
keep_other_settings() { # copy optional settings (timeout, cache ttl, ...) the user added by hand
  grep -vE '^(BAKALARI_URL|BAKALARI_USERNAME|BAKALARI_PASSWORD|BAKALARI_COOKIE|BAKALARI_COOKIE_FILE|BAKALARI_RECEIVER_[A-Z]+)=' \
    "$ENV_FILE" 2>/dev/null >> "$ENV_FILE.new" || true
  mv "$ENV_FILE.new" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}
stop_service() { systemctl --user disable --now "$1" >/dev/null 2>&1 || true; }

install_service() { # install_service NAME "description" server.py-flag
  local name="$1" description="$2" flag="$3" unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/$name.service" <<UNIT
[Unit]
Description=$description
After=network-online.target

[Service]
ExecStart=$HOME/.local/bin/uv --directory $APP_DIR run --no-dev server.py $flag
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT
  if systemctl --user daemon-reload 2>/dev/null && systemctl --user enable --now "$name" 2>/dev/null; then
    systemctl --user restart "$name"
    echo "Service '$name' started (log: journalctl --user -u $name -f)."
    echo "Keeping it running after you log out and after reboots needs one sudo command:"
    sudo loginctl enable-linger "$USER" || echo "  (skipped - run 'sudo loginctl enable-linger $USER' yourself later)"
  else
    echo "systemd --user is not available here. Start it yourself with:"
    echo "  nohup $APP_DIR/run-bakalari.sh $flag > $APP_DIR/$name.log 2>&1 &"
  fi
}

ask_credentials() {
  local old_url url method cookie user pass old_pass_line token host port detected answer
  old_url="$(read_old BAKALARI_URL)"; old_url="${old_url:-https://school.example.cz}"
  echo
  echo "== Bakalari login (stored only in $ENV_FILE and session.json, mode 600) =="
  read -r -p "Bakalari address [$old_url]: " url
  url="${url:-$old_url}"

  echo "Login method:"
  echo "  1) paste a cookie ONCE - this server keeps the session alive by itself (default)"
  echo "  2) username + password (fully standalone, all data tools work)"
  echo "  3) browser extension (your PC keeps the cookie fresh)"
  read -r -p "Choose [1]: " method
  umask 077
  case "${method:-1}" in
    1)
      stop_service "$RECEIVER"
      cat <<MSG

How to get the cookie (do this right before pasting - an unused session ends after 15 minutes):
  1. Log in to $url in your browser.
  2. Press F12 -> Network tab -> reload the page -> click the first request.
  3. In Request Headers find "Cookie:" and copy its whole value.
  4. Afterwards just close the tab. Do NOT click "Log out" - that would end the session.
MSG
      read -r -s -p "Paste cookie (hidden): " cookie; echo
      if [ -z "$cookie" ]; then echo "Cookie is required." >&2; return 1; fi
      {
        echo "BAKALARI_URL=\"$(escape "$url")\""
        echo "BAKALARI_COOKIE_FILE=\"$APP_DIR/session.json\""
      } > "$ENV_FILE.new"
      keep_other_settings
      printf '%s' "$cookie" | (cd "$APP_DIR" && uv run --no-dev server.py --store-cookie)
      install_service "$KEEPER" "Bakalari MCP session keeper (renews the pasted school cookie)" "--keeper"
      ;;
    2)
      stop_service "$RECEIVER"; stop_service "$KEEPER"
      read -r -p "Username [$(read_old BAKALARI_USERNAME)]: " user
      user="${user:-$(read_old BAKALARI_USERNAME)}"
      read -r -s -p "Password (Enter = keep current): " pass; echo
      old_pass_line="$(grep -E '^BAKALARI_PASSWORD=' "$ENV_FILE" 2>/dev/null | head -n1 || true)"
      if [ -z "$user" ] || { [ -z "$pass" ] && [ -z "$old_pass_line" ]; }; then echo "Username and password are required." >&2; return 1; fi
      {
        echo "BAKALARI_URL=\"$(escape "$url")\""
        echo "BAKALARI_USERNAME=\"$(escape "$user")\""
        if [ -n "$pass" ]; then echo "BAKALARI_PASSWORD=\"$(escape "$pass")\""; else echo "$old_pass_line"; fi
      } > "$ENV_FILE.new"
      keep_other_settings
      ;;
    *)
      stop_service "$KEEPER"
      detected="$(hostname -I 2>/dev/null | awk '{print $1}')"
      echo
      echo "The receiver is a small service your browser extension sends the cookie to."
      echo "It must listen on an address your PC can reach. Anyone on that network who knows the secret"
      echo "token could send a cookie, so keep the token private. Use 127.0.0.1 to keep it local only."
      host="$(read_old BAKALARI_RECEIVER_HOST)"; host="${host:-$detected}"
      read -r -p "Listen address [${host:-127.0.0.1}]: " answer
      host="${answer:-${host:-127.0.0.1}}"
      port="$(read_old BAKALARI_RECEIVER_PORT)"; port="${port:-8765}"
      read -r -p "Port [$port]: " answer
      port="${answer:-$port}"
      token="$(read_old BAKALARI_RECEIVER_TOKEN)"
      if [ "${#token}" -lt 16 ]; then token="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"; fi
      {
        echo "BAKALARI_URL=\"$(escape "$url")\""
        echo "BAKALARI_COOKIE_FILE=\"$APP_DIR/session.json\""
        echo "BAKALARI_RECEIVER_HOST=\"$host\""
        echo "BAKALARI_RECEIVER_PORT=\"$port\""
        echo "BAKALARI_RECEIVER_TOKEN=\"$token\""
      } > "$ENV_FILE.new"
      keep_other_settings
      install_service "$RECEIVER" "Bakalari MCP cookie receiver (browser extension pushes the school cookie here)" "--receiver"
      ;;
  esac
}

verify_login() {
  if grep -q '^BAKALARI_RECEIVER_TOKEN=' "$ENV_FILE"; then
    local host port token
    host="$(read_old BAKALARI_RECEIVER_HOST)"; port="$(read_old BAKALARI_RECEIVER_PORT)"
    token="$(read_old BAKALARI_RECEIVER_TOKEN)"
    sleep 2
    echo
    if curl -fsS -m 5 "http://$host:$port/health" >/dev/null 2>&1; then
      echo "Receiver is running on http://$host:$port"
    else
      echo "WARNING: the receiver does not answer on http://$host:$port yet (see: systemctl --user status $RECEIVER)."
    fi
    cat <<MSG

== Now set up the browser extension (Chrome or Edge) ==
  1. Open chrome://extensions (edge://extensions), switch on "Developer mode",
     click "Load unpacked" and choose the bakalari-extension folder.
  2. Open the extension's Options and enter:
       Server address:  http://$host:$port
       Token:           $token
  3. Log in to $(read_old BAKALARI_URL) in that browser. The popup should say "Sent ... cookies".
  4. If the popup says it cannot reach the server and the server runs a firewall (ufw), allow your network, e.g.:
       sudo ufw allow from <your PC network, e.g. 192.168.88.0/24> to any port $port proto tcp
MSG
    return 0
  fi
  local rc=0
  sleep 4  # give the keeper a moment to renew once
  (cd "$APP_DIR" && uv run --no-dev server.py --check) || rc=$?
  if grep -q '^BAKALARI_COOKIE_FILE=' "$ENV_FILE"; then
    echo
    echo "Keeper log (the session should keep renewing itself):"
    journalctl --user -u "$KEEPER" -n 5 --no-pager 2>/dev/null || true
  fi
  return $rc
}

until ask_credentials && verify_login; do
  echo
  read -r -p "Login test failed. Enter the details again? [Y/n] " again
  case "${again:-Y}" in [nN]*) echo "Saved anyway; run deploy.sh again to fix it."; break ;; esac
done

# ---- launcher ----------------------------------------------------------------
cat > "$RUNNER" <<EOF
#!/usr/bin/env bash
# Starts the MCP server on stdio (or another mode, e.g. --check). Settings come from $ENV_FILE.
export PATH="\$HOME/.local/bin:\$PATH"
exec uv --directory "$APP_DIR" run --no-dev server.py "\$@"
EOF
chmod 700 "$RUNNER"
cat > "$APP_DIR/check.sh" <<EOF
#!/usr/bin/env bash
# Tests the login and lists the tools.
export PATH="\$HOME/.local/bin:\$PATH"
exec uv --directory "$APP_DIR" run --no-dev server.py --check
EOF
chmod 700 "$APP_DIR/check.sh"

echo
echo "== Done =="
echo "Launcher: $RUNNER"
echo "Change the login any time by running: bash $APP_DIR/deploy.sh"
if grep -q '^BAKALARI_PASSWORD=' "$ENV_FILE" 2>/dev/null; then
  echo
  echo "Claude Desktop config (needs SSH key login: ssh-copy-id $USER@$(hostname -I | awk '{print $1}')):"
  echo "  \"bakalari\": { \"command\": \"ssh\", \"args\": [\"-o\", \"BatchMode=yes\", \"$USER@$(hostname -I | awk '{print $1}')\", \"$RUNNER\"] }"
fi
