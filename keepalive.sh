#!/usr/bin/env bash
# Measures how long a Bakalari website session (cookie) can be kept alive with the site's own
# "extend session" call. Runs in the background and writes keepalive.log.
#   Start:  bash keepalive.sh
#   Watch:  cat ~/bakalari-mcp/keepalive.log
#   Stop:   pkill -f keepalive-test
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$APP_DIR/keepalive.log"
export PATH="$HOME/.local/bin:$PATH"

if [ ! -t 0 ]; then
  echo "Run this with a terminal (ssh -t ...)." >&2
  exit 1
fi

cat <<'MSG'
1. In your browser log in to https://school.example.cz
2. F12 -> Network tab -> reload -> click the first request -> Request Headers -> copy the value of "Cookie:"
3. Paste it here right away (a session dies after 15 minutes without activity).
4. Afterwards close that browser tab WITHOUT logging out and don't use the site there:
   any click in the browser would also extend the session and spoil the measurement.
MSG
read -r -s -p "Paste fresh cookie (hidden): " cookie
echo
if [ -z "$cookie" ]; then echo "Nothing pasted." >&2; exit 1; fi

pkill -f "keepalive-test" 2>/dev/null || true

tmp="$(mktemp)"
chmod 600 "$tmp"
value=${cookie//\\/\\\\}
value=${value//\"/\\\"}
printf 'BAKALARI_COOKIE="%s"\n' "$value" > "$tmp"

# ping every 4 minutes for up to 8 hours
BAKALARI_ENV_FILE="$tmp" setsid nohup uv --directory "$APP_DIR" run --no-dev server.py --keepalive-test 240 480 > "$LOG" 2>&1 < /dev/null &
sleep 10
rm -f "$tmp"

echo
echo "== first readings =="
cat "$LOG"
echo
echo "Running in the background. Check later with:  cat $LOG"
