#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "============================================================"
echo "  University Club Timetable & Availability Analyzer"
echo "============================================================"

if [ ! -d ".venv" ]; then
    echo "[1/3] Creating local Python virtual environment (.venv)..."
    python3 -m venv .venv
    echo "[2/3] Installing dependencies..."
    ./.venv/bin/pip install -r requirements.txt
fi

source ./.venv/bin/activate

echo "[Starting] Launching FastAPI on http://127.0.0.1:8000 ..."
(
    sleep 2
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:8000" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:8000" >/dev/null 2>&1 || true
    fi
) &

exec python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

