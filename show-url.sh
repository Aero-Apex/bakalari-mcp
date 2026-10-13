#!/usr/bin/env bash
# Prints the address to paste into Claude (Settings -> Connectors -> Add custom connector).
# The address contains the secret - don't share it.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
get() { grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | head -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
host="$(get BAKALARI_HTTP_HOSTS | cut -d, -f1)"
token="$(get BAKALARI_HTTP_TOKEN)"
if [ -z "$host" ] || [ -z "$token" ]; then echo "Run: bash $APP_DIR/http-service.sh" >&2; exit 1; fi
if [ "${1:-}" = "-n" ]; then printf '%s' "https://$host/$token/mcp"; else echo "https://$host/$token/mcp"; fi
