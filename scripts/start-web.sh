#!/usr/bin/env bash
set -e

echo "=== Running database migrations ==="
alembic upgrade head

echo "=== Starting web server ==="
exec uvicorn main:app --host 0.0.0.0 --port 8000
