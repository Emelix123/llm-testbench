#!/usr/bin/env bash
# Startet die Testplattform: http://localhost:8080 (auf einem Server per SSH-Tunnel, siehe docs/SERVER.md)
#
#   ./start.sh                 im Vordergrund (Beenden: Strg+C)
#   ./start.sh --hintergrund   im Hintergrund (Log: data/testbench.log) – z. B. auf einem Server
#   ./start.sh --stopp         Hintergrund-Instanz beenden
#   PORT=8081 ./start.sh       anderer Port
#
# Braucht Python >= 3.11. Fehlt es, legt das Skript die Umgebung mit uv an (lädt Python 3.12 ohne
# root ins Home-Verzeichnis); uv selbst wird nach Rückfrage installiert.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
PORT=${PORT:-8080}
HOST=${LLMTB_HOST:-127.0.0.1}
PIDFILE="$ROOT/data/testbench.pid"
LOG="$ROOT/data/testbench.log"
mkdir -p "$ROOT/data"

running_pid() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && cat "$PIDFILE"; }

case "${1:-}" in
  --stopp|--stop)
    if pid=$(running_pid); then kill "$pid" && rm -f "$PIDFILE" && echo "Testplattform (PID $pid) beendet."
    else echo "Keine Hintergrund-Instanz gefunden."; fi
    exit 0;;
  --hintergrund|--background|"") ;;
  *) sed -n '2,10p' "$0"; exit 1;;
esac
MODE=${1:-}

if pid=$(running_pid); then
  echo "Testplattform läuft bereits (PID $pid): http://localhost:$PORT"; exit 0
fi

# ------------------------------------------------------------------ Python-Umgebung
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
find_uv() { command -v uv 2>/dev/null || { [[ -x "$HOME/.local/bin/uv" ]] && echo "$HOME/.local/bin/uv"; } || true; }
UV=$(find_uv)

if [[ -d .venv ]] && ! { [[ -x .venv/bin/python ]] && py_ok .venv/bin/python; }; then
  echo "Vorhandene .venv ist unbrauchbar oder älter als Python 3.11 – lege sie neu an."
  rm -rf .venv
fi
if [[ ! -d .venv ]]; then
  PY=""
  for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$c" ]] && command -v "$c" >/dev/null 2>&1 && py_ok "$c" && { PY="$c"; break; }
  done
  if [[ -n "$PY" ]] && "$PY" -m venv .venv 2>/dev/null; then
    echo "Python-Umgebung angelegt ($("$PY" --version))."
  else
    rm -rf .venv
    if [[ -z "$UV" ]]; then
      echo "Python >= 3.11 fehlt (hier: $(python3 --version 2>&1 || echo keins))."
      echo "Empfohlen ist uv: installiert Python 3.12 ohne root nur für dieses Projekt (~60 MB)."
      if [[ -t 0 ]]; then
        read -r -p "uv jetzt von https://astral.sh/uv installieren? [J/n] " a
        case "${a:-j}" in n*|N*) exit 1;; esac
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
        UV="$HOME/.local/bin/uv"
      else
        echo "Ohne Terminal keine Rückfrage möglich. Einmalig ausführen:"
        echo "  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh"
        exit 1
      fi
    fi
    echo "Lege Python-Umgebung mit uv an (Python 3.12) …"
    "$UV" venv --python 3.12 .venv
  fi
fi

# Pakete nur installieren, wenn sich backend/requirements.txt geändert hat
REQ_SHA=$(sha256sum backend/requirements.txt | cut -c1-16)
if [[ "$(cat .venv/.req-sha 2>/dev/null || true)" != "$REQ_SHA" ]]; then
  echo "Installiere Python-Pakete …"
  if [[ -x .venv/bin/pip ]]; then .venv/bin/pip install -q -r backend/requirements.txt
  elif [[ -n "$UV" ]]; then "$UV" pip install -q --python .venv/bin/python -r backend/requirements.txt
  else .venv/bin/python -m ensurepip -q && .venv/bin/python -m pip install -q -r backend/requirements.txt; fi
  echo "$REQ_SHA" > .venv/.req-sha
fi

if [[ ! -f frontend/dist/index.html ]]; then
  if command -v npm >/dev/null; then (cd frontend && npm install && npm run build)
  else echo "Hinweis: frontend/dist fehlt und npm ist nicht installiert." >&2; fi
fi

[[ "$HOST" == "127.0.0.1" ]] || echo "WARNUNG: LLMTB_HOST=$HOST – die Oberfläche kann Docker-Container starten. Nur hinter eigener Absicherung!" >&2

# ------------------------------------------------------------------ Start
cd backend
if [[ "$MODE" == --hintergrund || "$MODE" == --background ]]; then
  nohup ../.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "Testplattform läuft im Hintergrund (PID $!): http://localhost:$PORT  · Log: data/testbench.log · Beenden: ./start.sh --stopp"
  exit 0
fi
( sleep 2; (command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT") || (command -v open >/dev/null && open "http://localhost:$PORT") || true ) >/dev/null 2>&1 &
echo "Testplattform läuft auf http://localhost:$PORT  (Beenden: Strg+C)"
echo "Auf einem Server: ssh -L $PORT:localhost:$PORT <server>, dann http://localhost:$PORT im eigenen Browser."
exec ../.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
