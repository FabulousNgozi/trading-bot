#!/usr/bin/env bash
# start.sh — one-click launcher for the Deriv Volatility Bot web panel (macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")"

echo "=============================="
echo " Deriv Volatility Bot — launcher"
echo "=============================="
echo

# 1. find python
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python 3 not found."
  echo "  macOS:  brew install python   (or python.org)"
  echo "  Ubuntu: sudo apt install python3 python3-venv python3-pip"
  exit 1
fi
echo "Using: $($PY --version 2>&1)"

# 2. venv
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv || { echo "ERROR: venv creation failed. On Ubuntu install python3-venv."; exit 1; }
fi
# shellcheck disable=SC1091
. .venv/bin/activate

# 3. deps
echo "Installing dependencies..."
python -m pip install -q --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
python -m pip install -q --disable-pip-version-check -r requirements.txt
echo "Dependencies ready."
echo

# 4. .env from example, first run only
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  chmod 600 .env 2>/dev/null || true
  echo "Created .env from .env.example."
  echo "  -> Add DERIV_APP_ID and a DEMO-scope DERIV_API_TOKEN to trade."
  echo "  -> Without them the panel still loads; Start will explain what is missing."
  echo
fi

# 5. open browser shortly after server starts
URL="http://127.0.0.1:8000/"
(
  sleep 3
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
  fi
) &

echo "Starting panel at $URL"
echo "Press Ctrl-C to stop."
echo
exec python server.py
