#!/usr/bin/env sh
set -eu

export SERVER_HOST="${SERVER_HOST:-0.0.0.0}"
export SERVER_PORT="${SERVER_PORT:-8000}"
export SERVER_WORKERS="${SERVER_WORKERS:-1}"

echo "Starting Grok2API on http://${SERVER_HOST}:${SERVER_PORT}"

if command -v uv >/dev/null 2>&1; then
  uv sync
  exec uv run granian --interface asgi --host "${SERVER_HOST}" --port "${SERVER_PORT}" --workers "${SERVER_WORKERS}" app.main:app
fi

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
exec .venv/bin/python -m granian --interface asgi --host "${SERVER_HOST}" --port "${SERVER_PORT}" --workers "${SERVER_WORKERS}" app.main:app
