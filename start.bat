@echo off
REM Startet die Testplattform lokal: http://localhost:8080
cd /d "%~dp0"
if not exist .venv (
  echo Lege Python-Umgebung an ...
  py -3.12 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r backend\requirements.txt
if not exist frontend\dist\index.html (
  where npm >nul 2>nul && (cd frontend && npm install && npm run build && cd ..)
)
if "%PORT%"=="" set PORT=8080
start "" http://localhost:%PORT%
echo Testplattform laeuft auf http://localhost:%PORT%  (Beenden: Strg+C)
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
