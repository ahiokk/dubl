@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
rem Запуск окна. Если окно не появилось, смотри ошибка.log
cd /d "%~dp0" || exit /b 1
if not exist ".venv\Scripts\pythonw.exe" goto :no_venv
start "" ".venv\Scripts\pythonw.exe" "run.py"
exit /b 0

:no_venv
echo.
echo   Дубль ещё не установлен: в этой папке нет окружения .venv.
echo   Сначала запусти Установка.bat, потом открывай Дубль.bat или ярлык.
echo.
pause
exit /b 1
