@echo off
chcp 65001 >nul
pushd "%~dp0"
consultant.exe
set "APP_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %APP_EXIT_CODE%

