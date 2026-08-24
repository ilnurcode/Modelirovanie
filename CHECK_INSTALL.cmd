@echo off
chcp 65001 >nul
pushd "%~dp0"

if not exist "%~dp0consultant.exe" (
  echo [ERROR] Не найден consultant.exe.
  pause
  exit /b 1
)

echo [1/2] Версия приложения
consultant.exe --version || goto :failed

echo [2/2] Проверка открытия базы
consultant.exe --json --repo "%~dp0" list >nul || goto :failed

echo.
echo [OK] Комплект готов к работе. Запустите START.cmd.
popd
pause
exit /b 0

:failed
echo.
echo [ERROR] Проверка не пройдена.
popd
pause
exit /b 1
