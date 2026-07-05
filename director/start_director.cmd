@echo off
rem Director service autostart (Task Scheduler: "AgentDirector").
rem Quarantined secret-keeper for RPG mystery stories — port 5008.
rem Its Fernet key lives in director\.director_key, NEVER in the agent's env.
set PYTHONUTF8=1
cd /d %~dp0
if not exist logs mkdir logs
..\.venv\Scripts\python.exe director_service.py >> logs\director.log 2>&1
