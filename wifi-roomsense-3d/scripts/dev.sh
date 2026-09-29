#!/usr/bin/env bash
# Development mode (Linux/macOS): backend + Vite dev server with hot reload.
#
# The backend runs on http://127.0.0.1:8765 and the Vite dev server on
# http://127.0.0.1:5173 (it proxies /api and the /api/ws WebSocket to the
# backend). Ctrl-C stops both.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v uv >/dev/null 2>&1 || { echo "dev: uv not found; run scripts/setup.sh first" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "dev: npm not found (Node.js >= 22.12 required)" >&2; exit 1; }
[ -d "$REPO/frontend/node_modules" ] || { echo "dev: run 'cd frontend && npm ci' (or scripts/setup.sh) first" >&2; exit 1; }

(cd "$REPO/backend" && exec uv run --frozen roomsense serve "$@") &
BACKEND_PID=$!

cleanup() {
  trap - EXIT INT TERM
  local children
  children="$(pgrep -P "$BACKEND_PID" 2>/dev/null || true)"
  # shellcheck disable=SC2086
  kill -TERM ${children:-$BACKEND_PID} 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$BACKEND_PID" 2>/dev/null || break
    sleep 0.5
  done
}
trap cleanup EXIT INT TERM

echo "Backend: http://127.0.0.1:8765   UI (dev): http://127.0.0.1:5173"
cd "$REPO/frontend"
npm run dev
