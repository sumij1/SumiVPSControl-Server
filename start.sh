#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "[setup] Creating Python virtual environment"
  "$PYTHON_BIN" -m venv "$ROOT/.venv"
fi

VENV_PYTHON="$ROOT/.venv/bin/python"
DEPS_STAMP="$ROOT/.venv/.requirements-installed"
if [[ "${SKIP_INSTALL:-0}" != "1" ]]; then
  if [[ ! -f "$DEPS_STAMP" || "$ROOT/requirements.txt" -nt "$DEPS_STAMP" ]]; then
    echo "[setup] Installing dependencies"
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r "$ROOT/requirements.txt"
    touch "$DEPS_STAMP"
  fi
fi

if [[ ! -s "$ROOT/.env" ]]; then
  echo "[setup] Generating .env and random access keys"
  PYTHON_BIN="$PYTHON_BIN" bash "$ROOT/scripts/create_env.sh" "$ROOT"
fi

if [[ ! -f "$ROOT/frontend/index.html" ]]; then
  if command -v node >/dev/null 2>&1; then
    echo "[setup] Building the web client"
    node "$ROOT/scripts/build_frontend.js"
  else
    echo "frontend/index.html is missing and Node.js is unavailable." >&2
    exit 1
  fi
fi

BIND_HOST="$("$VENV_PYTHON" -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("BIND_HOST") or "0.0.0.0")')"
BIND_PORT="$("$VENV_PYTHON" -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("BIND_PORT") or "8080")')"

echo
echo "Web client: http://127.0.0.1:${BIND_PORT}/"
echo "Health check: http://127.0.0.1:${BIND_PORT}/api/health"
echo "ADMIN_KEY and AGENT_KEY are stored in: $ROOT/.env"
echo

exec "$VENV_PYTHON" -m uvicorn main:app \
  --host "$BIND_HOST" \
  --port "$BIND_PORT" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips=127.0.0.1
