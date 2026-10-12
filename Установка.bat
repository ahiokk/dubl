@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Установка Дубля

echo.
echo   Установка Дубля
echo.

rem uv ставит нужный Python и библиотеки. Сам Python заранее ставить не надо
set "UV=uv"
where uv >nul 2>nul
if not errorlevel 1 goto :venv
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if exist "%UV%" goto :venv
echo   Ставлю uv, через него ставятся Python и библиотеки
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if not exist "%UV%" goto :no_uv

:venv
rem Кэш скачанного и Python, если его придётся качать, держим в папке программы,
rem а не в профиле на диске C. Кэш после установки удаляется: uv ставит пакеты
rem жёсткими ссылками на него, и файлы в .venv остаются на месте.
rem Если свой кэш у человека уже настроен, берём его
set "OWN_CACHE="
if not defined UV_CACHE_DIR set "OWN_CACHE=1"
if not defined UV_CACHE_DIR set "UV_CACHE_DIR=%~dp0.uv-cache"
if not defined UV_PYTHON_INSTALL_DIR set "UV_PYTHON_INSTALL_DIR=%~dp0.python"

echo.
echo   [1/3] Окружение Python 3.12 в папке .venv
if exist ".venv\Scripts\python.exe" goto :packages
"%UV%" venv --python 3.12 .venv
if errorlevel 1 goto :fail

:packages
echo.
echo   [2/3] Библиотеки, около 250 МБ
"%UV%" pip install --python .venv\Scripts\python.exe -r requirements.txt
if errorlevel 1 goto :fail

echo.
where nvidia-smi >nul 2>nul
if errorlevel 1 goto :no_gpu
echo   [3/3] Нашлась видеокарта NVIDIA, ставлю библиотеки CUDA, около 2 ГБ
"%UV%" pip install --python .venv\Scripts\python.exe -r requirements-gpu.txt
if errorlevel 1 goto :fail
goto :shortcut

:no_gpu
echo   [3/3] Видеокарты NVIDIA нет, библиотеки CUDA не нужны.
echo         Речь будет распознаваться на процессоре или через интернет.

:shortcut
if defined OWN_CACHE if exist ".uv-cache" rmdir /s /q ".uv-cache"
if /i "%~1"=="--no-shortcut" goto :done
set "DUBL_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = $env:DUBL_DIR; $s = (New-Object -ComObject WScript.Shell).CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'Дубль.lnk')); $s.TargetPath = $d + '.venv\Scripts\pythonw.exe'; $s.Arguments = 'run.py'; $s.WorkingDirectory = $d; $s.IconLocation = $d + 'web\img\icon.ico'; $s.Save()"
if errorlevel 1 echo   Ярлык создать не вышло, запускай Дубль.bat из этой папки.

:done
echo.
echo   Готово. Запуск ярлыком «Дубль» на рабочем столе или файлом Дубль.bat
echo   в этой папке. Модель распознавания скачается при первой записи.
echo.
pause
exit /b 0

:no_uv
echo.
echo   Не получилось поставить uv. Поставь его по инструкции
echo   https://docs.astral.sh/uv/getting-started/installation/
echo   и запусти Установка.bat ещё раз.
echo.
pause
exit /b 1

:fail
echo.
echo   Установка прервалась, ошибка выше. Чаще всего это обрыв интернета.
echo   Запусти Установка.bat ещё раз, скачанное заново качаться не будет.
echo.
pause
exit /b 1
