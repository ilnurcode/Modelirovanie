@echo off
chcp 65001 >nul
if exist "%~dp0consultant.exe" (
  if not defined CONSULTANT_DATA_DIR set "CONSULTANT_DATA_DIR=%~dp0..\.."
  "%~dp0consultant.exe" --repo "%~dp0" %*
  exit /b %errorlevel%
)
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src"
py -3 -m consultant_cli %*
