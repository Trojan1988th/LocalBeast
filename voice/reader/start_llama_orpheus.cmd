@echo off
rem Orpheus 3B GGUF server (Phase B reader). Port 5006 — llama.cpp default 8080
rem may already be in use on your machine.
cd /d %~dp0
bin\llama\llama-server.exe -m models\Orpheus-3b-FT-Q8_0.gguf ^
  --port 5006 --host 127.0.0.1 -ngl 99 -c 8192 --no-webui
