#!/usr/bin/env bash
# Runs the MCP server as an HTTP service on 127.0.0.1 so a Cloudflare tunnel (cloudflared) can publish it.
# The address contains a long random secret; anyone who knows the full URL can read the school data,
# so treat it like a password. Run it again to change the hostname or to create a NEW secret.
#   bash http-service.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
NAME="bakalari-mcp-http"
export PATH="$HOME/.local/bin:$PATH"

if [ ! -t 0 ]; then echo "Run this with a terminal (ssh -t ...)." >&2; exit 1; fi
if [ ! -f "$ENV_FILE" ]; then echo "Run deploy.sh first." >&2; exit 1; fi

read_old() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true; }

old_hosts="$(read_old BAKALARI_HTTP_HOSTS)"
read -r -p "Public hostname of your Cloudflare tunnel, e.g. mcp.example.com [${old_hosts:-none}]: " hosts
hosts="${hosts:-$old_hosts}"
if [ -z "$hosts" ]; then echo "A hostname is required (only these names are accepted)." >&2; exit 1; fi
hosts="${hosts#https://}"; hosts="${hosts%%/*}"

port="$(read_old BAKALARI_HTTP_PORT)"; port="${port:-8770}"
read -r -p "Local port [$port]: " answer
port="${answer:-$port}"

token="$(read_old BAKALARI_HTTP_TOKEN)"
if [ "${#token}" -ge 24 ]; then
  read -r -p "Keep the existing secret? [Y/n] " keep
  case "${keep:-Y}" in [nN]*) token="" ;; esac
fi
if [ "${#token}" -lt 24 ]; then token="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"; echo "Created a new secret."; fi

umask 077
grep -vE '^BAKALARI_HTTP_[A-Z]+=' "$ENV_FILE" > "$ENV_FILE.new" || true
{
  echo "BAKALARI_HTTP_HOSTS=\"$hosts\""
  echo "BAKALARI_HTTP_PORT=\"$port\""
  echo "BAKALARI_HTTP_TOKEN=\"$token\""
} >> "$ENV_FILE.new"
mv "$ENV_FILE.new" "$ENV_FILE"
chmod 600 "$ENV_FILE"

unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
cat > "$unit_dir/$NAME.service" <<UNIT
[Unit]
Description=Bakalari MCP over HTTP (for the Cloudflare tunnel)
After=network-online.target

[Service]
ExecStart=$HOME/.local/bin/uv --directory $APP_DIR run --no-dev server.py --http
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now "$NAME"
systemctl --user restart "$NAME"
sudo loginctl enable-linger "$USER" || echo "(run 'sudo loginctl enable-linger $USER' yourself so it survives logout/reboot)"
sleep 3

code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$port/wrong/mcp" || true)"
if [ "$code" = "404" ]; then echo "Service is running."; else echo "WARNING: unexpected answer ($code) - see: journalctl --user -u $NAME -n 20"; fi

cat <<MSG

== Done ==
Listens only on this machine:   http://127.0.0.1:$port/$token/mcp

Publish it with your Cloudflare tunnel, e.g. in the tunnel's config:
    ingress:
      - hostname: $hosts
        service: http://localhost:$port
      - service: http_status:404

The address to give Claude (Settings -> Connectors -> add custom connector):
    https://$hosts/$token/mcp

Keep that address private. To replace it with a new secret, run this script again and answer 'n'.
MSG
