#!/usr/bin/env bash
# Startet die Testplattform lokal: http://localhost:8080
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "Lege Python-Umgebung an (.venv) …"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r backend/requirements.txt
if [ ! -f frontend/dist/index.html ]; then
  if command -v npm >/dev/null; then
    (cd frontend && npm install && npm run build)
  else
    echo "Hinweis: frontend/dist fehlt und npm ist nicht installiert." >&2
  fi
fi
PORT=${PORT:-8080}
( sleep 2; (command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT") || (command -v open >/dev/null && open "http://localhost:$PORT") || true ) >/dev/null 2>&1 &
echo "Testplattform läuft auf http://localhost:$PORT  (Beenden: Strg+C)"
cd backend && exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
