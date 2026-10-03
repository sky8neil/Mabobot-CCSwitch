@echo off
cd /d C:\Users\mabobot\Mabobot
".venv\Scripts\pythonw.exe" "scripts\cleanup_idle_service.py" --once --log-file "C:\Users\mabobot\Mabobot\logs\cleanup.log"
