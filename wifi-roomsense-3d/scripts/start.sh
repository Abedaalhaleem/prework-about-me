#!/usr/bin/env bash
# Start RoomSense in the background (Linux/macOS).
#
# Runs 'uv run roomsense serve' with nohup, writes data/roomsense.pid, logs to
# logs/roomsense.log, waits until /api/health answers and prints the URL.
# Extra arguments are passed to 'roomsense serve' (e.g. --port 8766).
# Stop it with scripts/stop.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO/data/roomsense.pid"
LOG_DIR="$REPO/logs"
LOG_FILE="$LOG_DIR/roomsense.log"
mkdir -p "$REPO/data" "$LOG_DIR"

die() { echo "start: $*" >&2; exit 1; }

if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "RoomSense is already running (pid $OLD_PID). Stop it with scripts/stop.sh."
    exit 0
  fi
  rm -f "$PID_FILE"  # stale
fi

command -v uv >/dev/null 2>&1 || die "uv not found; run scripts/setup.sh first"

# Host/port for the health check and the printed URL (same defaults as the config).
HOST="127.0.0.1"
PORT="8765"
if [ -f "$REPO/configs/roomsense.toml" ]; then
  CFG_HOST="$(sed -n 's/^[[:space:]]*host[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$REPO/configs/roomsense.toml" | head -n1)"
  CFG_PORT="$(sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$REPO/configs/roomsense.toml" | head -n1)"
  [ -n "$CFG_HOST" ] && HOST="$CFG_HOST"
  [ -n "$CFG_PORT" ] && PORT="$CFG_PORT"
fi
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  case "${ARGS[$i]}" in
    --host) HOST="${ARGS[$((i + 1))]:-$HOST}" ;;
    --host=*) HOST="${ARGS[$i]#--host=}" ;;
    --port) PORT="${ARGS[$((i + 1))]:-$PORT}" ;;
    --port=*) PORT="${ARGS[$i]#--port=}" ;;
  esac
done
CHECK_HOST="$HOST"
case "$CHECK_HOST" in
  0.0.0.0|"::"|"") CHECK_HOST="127.0.0.1" ;;
esac
case "$CHECK_HOST" in
  *:*) URL_HOST="[$CHECK_HOST]" ;;
  *) URL_HOST="$CHECK_HOST" ;;
esac
URL="http://$URL_HOST:$PORT"

cd "$REPO/backend"
echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) starting roomsense serve $* ---" >>"$LOG_FILE"
nohup uv run --frozen roomsense serve "$@" >>"$LOG_FILE" 2>&1 </dev/null &
PID=$!
echo "$PID" >"$PID_FILE"

health_code() {
  # Any HTTP answer means the server is up; 401 just means a token is configured.
  # The token is never put on a command line (it would show up in 'ps').
  if command -v curl >/dev/null 2>&1; then
    curl -s -o /dev/null -m 2 -w '%{http_code}' "$URL/api/health" 2>/dev/null || true
  else
    uv run --frozen python - "$URL/api/health" <<'PY' 2>/dev/null || true
import sys, urllib.error, urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=2).status, end="")
except urllib.error.HTTPError as e:
    print(e.code, end="")
except Exception:
    print("000", end="")
PY
  fi
}

for _ in $(seq 1 60); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "RoomSense exited during startup. Last log lines:" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    exit 1
  fi
  CODE="$(health_code)"
  if [ "$CODE" = "200" ] || [ "$CODE" = "401" ]; then
    echo "RoomSense is running (pid $PID): $URL"
    echo "Logs: $LOG_FILE   Stop: scripts/stop.sh"
    exit 0
  fi
  sleep 0.5
done
echo "RoomSense did not answer on $URL/api/health within 30 s (pid $PID still running). Check $LOG_FILE." >&2
exit 1
