#!/usr/bin/env bash
# WiFi RoomSense 3D: one-time setup (Linux/macOS).
#
# * checks Python >= 3.11, Node.js >= 22.12 and uv (explains how to get them;
#   installs nothing system-wide and never uses sudo)
# * installs the backend's pinned dependencies:  cd backend && uv sync --frozen
# * builds the web UI:                          cd frontend && npm ci && npm run build
# * copies configs/roomsense.example.toml to configs/roomsense.toml only if missing
#
# Usage: scripts/setup.sh [--skip-frontend]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_FRONTEND=0
for arg in "$@"; do
  case "$arg" in
    --skip-frontend) SKIP_FRONTEND=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "setup: unknown option $arg" >&2; exit 2 ;;
  esac
done

die() { echo "setup: $*" >&2; exit 1; }
info() { echo "setup: $*"; }

if [ "$(id -u)" -eq 0 ] && [ "${ROOMSENSE_ALLOW_ROOT:-0}" != "1" ]; then
  # Files created as root (virtualenv, data/) would later be unwritable for your user.
  die "do not run this as root or with sudo: RoomSense needs no elevated privileges (for serial access on Linux, add your user to the 'dialout' group yourself). In a container that only has root, set ROOMSENSE_ALLOW_ROOT=1."
fi

# --- uv ------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  die "uv is required to install the pinned backend dependencies. See https://docs.astral.sh/uv/getting-started/installation/ (it installs into your home directory; no sudo)."
fi
info "uv OK ($(uv --version))"

# --- Python ----------------------------------------------------------------------
# Either a Python >= 3.11 on PATH or one managed by uv is fine: uv sync picks it.
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  info "Python OK ($("$PY" -V 2>&1))"
elif UV_PY="$(uv python find '>=3.11' 2>/dev/null)" && [ -n "$UV_PY" ]; then
  info "Python OK (found by uv: $UV_PY)"
else
  die "Python 3.11 or newer is required (found on PATH: ${PY:-none}${PY:+ $("$PY" -V 2>&1)}). Install it from your package manager or python.org, or let uv provide one (per user, no sudo): 'uv python install 3.11'."
fi

# --- Node.js ---------------------------------------------------------------------
if [ "$SKIP_FRONTEND" -eq 0 ]; then
  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    die "Node.js 22.12 or newer (with npm) is required to build the web UI. Install it from nodejs.org or your package manager, or re-run with --skip-frontend (the API still works)."
  fi
  NODE_V="$(node -v | sed 's/^v//')"
  if ! node -e '
    const [a, b] = process.versions.node.split(".").map(Number);
    process.exit(a > 22 || (a === 22 && b >= 12) ? 0 : 1);
  '; then
    die "Node.js >= 22.12 is required (found $NODE_V)."
  fi
  info "Node.js OK ($NODE_V)"
fi

# --- backend -------------------------------------------------------------------
info "installing backend dependencies (uv sync --frozen)"
(cd "$REPO/backend" && uv sync --frozen)

# --- frontend --------------------------------------------------------------------
if [ "$SKIP_FRONTEND" -eq 0 ]; then
  info "building the web UI (npm ci && npm run build)"
  (cd "$REPO/frontend" && npm ci && npm run build)
else
  info "skipping the web UI build (--skip-frontend)"
fi

# --- config and local folders ------------------------------------------------------
if [ ! -f "$REPO/configs/roomsense.toml" ]; then
  # The example's receiver block names a sample port (/dev/ttyUSB0) that may not
  # exist here (macOS uses /dev/cu.*). Copy it commented out, so no receiver
  # looks configured until the user sets a real port. Ports are never guessed.
  TMP_CFG="$REPO/configs/.roomsense.toml.tmp"
  awk '
    /^\[\[acquisition\.receivers\]\]/ {
      if (!noted) {
        print "# NOT CONFIGURED YET: set each receiver'"'"'s serial port (list ports with: cd backend && uv run roomsense ports),"
        print "# then remove the leading \"# \" from its block. Until then no receiver is configured and"
        print "# starting the LIVE source is refused with NO_RECEIVERS_CONFIGURED."
        noted = 1
      }
      in_rx = 1
      print "# " $0
      next
    }
    in_rx && (/^[[:space:]]*$/ || /^\[/) { in_rx = 0 }
    { if (in_rx) print "# " $0; else print }
  ' "$REPO/configs/roomsense.example.toml" >"$TMP_CFG"
  if (cd "$REPO/backend" && uv run --frozen python -c 'import sys; from roomsense.config import load_config; c = load_config(sys.argv[1]); sys.exit(0 if not c.acquisition.receivers else 1)' "$TMP_CFG"); then
    mv "$TMP_CFG" "$REPO/configs/roomsense.toml"
    info "created configs/roomsense.toml from the example with the receiver block commented out; set your boards' serial ports there"
  else
    rm -f "$TMP_CFG"
    die "could not prepare configs/roomsense.toml from the example; copy configs/roomsense.example.toml by hand and edit the receiver ports"
  fi
else
  info "configs/roomsense.toml exists; left unchanged"
fi
mkdir -p "$REPO/data" "$REPO/logs"

info "done. Start with scripts/start.sh (or scripts/dev.sh for development)."
