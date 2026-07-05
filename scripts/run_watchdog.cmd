@echo off
rem Dead-man's alarm (Task Scheduler: "AgentWatchdog", every 30 minutes).
rem W0.2: independent failure domain — its own process, direct Telegram.
rem Exempt from Seasons: resting-by-choice and broken-by-accident must never
rem look alike.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d %~dp0..
.venv\Scripts\python.exe scripts\watchdog.py
