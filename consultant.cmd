@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src"
py -3 -m consultant_cli %*
