@echo off
cd /d "%~dp0"
rem Auf 0.0.0.0 lauschen, damit auch das Handy im WLAN zugreifen kann
set HOST=0.0.0.0
start "" http://localhost:8325
python server.py
pause
