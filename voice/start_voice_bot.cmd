@echo off
rem the agent Voice autostart wrapper (Task Scheduler: "the agentVoiceBot").
rem PYTHONUTF8: pipecat's runner prints emoji; Windows consoles default to cp1252.
set PYTHONUTF8=1
cd /d %~dp0
.venv\Scripts\python.exe voice_bot.py -t webrtc --port 8010 >> logs\voice_bot.log 2>&1
