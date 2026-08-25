@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_erp_pi.ps1" %*
exit /b %ERRORLEVEL%
