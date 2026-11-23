@echo off
rem Opens the fast review page (A approve, E edit, R reject). Starts the workers and page if needed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_workers.ps1"
timeout /t 3 >nul
start "" http://127.0.0.1:8502
