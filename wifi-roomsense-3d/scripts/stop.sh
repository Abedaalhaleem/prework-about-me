#!/usr/bin/env bash
# Stop RoomSense started by scripts/start.sh (Linux/macOS).
#
# Sends SIGTERM (graceful: the source is stopped, serial ports are closed, an
# active recording is finalised, the database is closed), waits up to 15 s,
# and only then sends SIGKILL with a warning. Removes data/roomsense.pid.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO/data/roomsense.pid"
GRACE_S=15

if [ ! -f "$PID_FILE" ]; then
  echo "RoomSense is not running (no $PID_FILE)."
  exit 0
fi
PID="$(cat "$PID_FILE" 2>/dev/null || true)"
case "$PID" in
  ''|*[!0-9]*) echo "stop: $PID_FILE does not contain a process id; removing it" >&2; rm -f "$PID_FILE"; exit 1 ;;
esac
if ! kill -0 "$PID" 2>/dev/null; then
  echo "RoomSense is not running (stale pid $PID); removing $PID_FILE."
  rm -f "$PID_FILE"
  exit 0
fi
# Never signal a process that merely reused the pid.
CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
case "$CMD" in
  *roomsense*|*uv*) ;;
  *) echo "stop: pid $PID is not RoomSense ('$CMD'); not signalling it. Remove $PID_FILE if it is stale." >&2; exit 1 ;;
esac

# start.sh records the pid of 'uv run'; the server is its child. Signal the
# child directly so it gets exactly one SIGTERM (uv exits when it does).
CHILDREN="$(pgrep -P "$PID" 2>/dev/null || true)"
TARGETS="${CHILDREN:-$PID}"
echo "Stopping RoomSense (pid $PID)..."
# shellcheck disable=SC2086
kill -TERM $TARGETS 2>/dev/null || true

alive() {
  kill -0 "$PID" 2>/dev/null && return 0
  for c in $CHILDREN; do kill -0 "$c" 2>/dev/null && return 0; done
  return 1
}

for _ in $(seq 1 $((GRACE_S * 2))); do
  if ! alive; then
    rm -f "$PID_FILE"
    echo "RoomSense stopped."
    exit 0
  fi
  sleep 0.5
done

echo "WARNING: RoomSense did not stop within ${GRACE_S} s; sending SIGKILL." >&2
echo "WARNING: an active recording is left without its closing record. It stays replayable and is marked as interrupted (ERROR) at the next start." >&2
# shellcheck disable=SC2086
kill -KILL $CHILDREN "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1
