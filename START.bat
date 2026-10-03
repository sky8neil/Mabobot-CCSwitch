@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CODEX_PROXY_USE_WSL=true"
REM 2026-09-30: was 20; cold-start codex via WSL needs 40s+ (measured 41.7s).
REM Too short -> version probe timeout -> circuit open -> App Server never starts.
set "CODEX_VERSION_PROBE_TIMEOUT_SECONDS=90"
set "CODEX_SCHEMA_PROBE_TIMEOUT_SECONDS=180"
set "STARTUP_ARG="
if /I "%~1"=="--startup" set "STARTUP_ARG=-Startup"

start "" /b powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%ROOT%scripts\launcher\bootstrap.ps1" %STARTUP_ARG%
exit /b 0
