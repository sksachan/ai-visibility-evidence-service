#!/usr/bin/env sh
set -eu
PORT_VALUE="${PORT:-8080}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT_VALUE"
