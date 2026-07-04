@echo off
rem the agent Reader autostart wrapper (Task Scheduler: "the agentReader").
rem Starts BOTH halves of the render stack:
rem   1. llama-server (Orpheus 3B GGUF) on 5006 — skipped if already listening
rem   2. reader_service.py (FastAPI render service) on 5005
rem PYTHONUTF8: same cp1252-vs-unicode guard as the voice bot.
set PYTHONUTF8=1
cd /d %~dp0
if not exist logs mkdir logs

rem -- llama-server: only start if 5006 is not already serving --
powershell -NoProfile -Command "try { Invoke-RestMethod -Uri http://127.0.0.1:5006/health -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  start "orpheus-llama" /min cmd /c "start_llama_orpheus.cmd >> logs\llama_server.log 2>&1"
)

rem -- wait for llama-server health (up to ~60s; model load takes a while) --
powershell -NoProfile -Command "$d=(Get-Date).AddSeconds(60); while((Get-Date) -lt $d){ try { Invoke-RestMethod -Uri http://127.0.0.1:5006/health -TimeoutSec 2 | Out-Null; exit 0 } catch { Start-Sleep -Seconds 2 } }; exit 1"

rem -- render service (foreground; Task Scheduler owns this process) --
.venv\Scripts\python.exe reader_service.py >> logs\reader_service.log 2>&1
