@echo off
rem the agent heartbeat scheduler autostart (Task Scheduler: "AgentHeartbeat").
rem W0.1: resurrected 2026-07-05 under proper service management — the old
rem fleet-spawned detached process died silently when stopped. Seasons-aware:
rem during a quiet season the scheduler stays alive but cycles at the keeper
rem cadence (see src/agent/seasons.py).
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d %~dp0..
if not exist logs\services mkdir logs\services
.venv\Scripts\python.exe -u -m scripts.run_heartbeat_scheduler >> logs\services\heartbeat.log 2>&1
