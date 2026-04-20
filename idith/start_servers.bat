@echo off
REM start_servers.bat - avvia SSE (8888) e HTTP (8000) in nuove finestre

REM cambia directory alla cartella del progetto
cd /d "%~dp0"

REM apri una finestra per il server SSE
start "SSE 8888" cmd /k "python sse_events.py --host 127.0.0.1 --port 8888"

REM apri una finestra per il server HTTP (static files)
start "HTTP 8000" cmd /k "python -m http.server 8000"

echo Avviati comandi: SSE su 127.0.0.1:8888 e HTTP su 127.0.0.1:8000
pause