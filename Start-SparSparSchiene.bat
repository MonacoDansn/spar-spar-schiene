@echo off
cd /d "%~dp0"
start "" http://localhost:8325
python server.py
pause
