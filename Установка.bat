@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || goto :bad_dir
title Установка Дубля

rem Из терминала с PowerShell 7 в окружение попадает его PSModulePath, и встроенный
rem Windows PowerShell перестаёт находить свои команды, например Get-FileHash
set "PSModulePath="
set "DUBL_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%DUBL_PS%" set "DUBL_PS=powershell"

rem uv ставит Python и библиотеки. Он берётся из архива конкретной версии с GitHub,
rem а перед распаковкой архив сверяется с хэшем SHA256 отсюда. Чтобы обновить uv:
rem поменять версию и взять новый хэш из файла uv-x86_64-pc-windows-msvc.zip.sha256
rem в том же релизе на https://github.com/astral-sh/uv/releases
set "DUBL_UV_VERSION=0.12.23"
set "DUBL_UV_SHA256=75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90"

echo.
echo   Установка Дубля
echo.

rem Запуск прямо из архива: Windows распаковывает один файл во временную папку
if not exist "requirements.txt" goto :not_unpacked
if not exist "run.py" goto :not_unpacked

rem Пути везде в кавычках и ни разу не внутри блока в скобках: так разбор не ломают
rem ни пробелы, ни кириллица, ни скобки, ни амперсанд в имени папки
set "DUBL_HOME=%~dp0"
if /i "%DUBL_HOME:OneDrive=%"=="%DUBL_HOME%" goto :uv
echo   Программа лежит в папке OneDrive. Здесь это плохо по двум причинам:
echo     - записи созвонов в папке расшифровки будут уходить в облако;
echo     - библиотеки и модель займут около 1 ГБ, а с ускорением на видеокарте до 4 ГБ,
echo       и установка в синхронизируемой папке замедлится.
echo   Лучше распаковать программу вне OneDrive, например в C:\Dubl или D:\Dubl.
echo.
set "ANS="
set /p "ANS=  Продолжить установку здесь? Введи y и нажми Enter, любой другой ответ отменит: "
if /i not "%ANS%"=="y" goto :cancel
echo.

:uv
rem uv лежит внутри программы, в папке .uv. Ни PATH, ни профиль не трогаются
set "DUBL_UV_DIR=%~dp0.uv"
set "DUBL_UV_ZIP=%~dp0.uv\uv.zip"
set "DUBL_UV_URL=https://github.com/astral-sh/uv/releases/download/%DUBL_UV_VERSION%/uv-x86_64-pc-windows-msvc.zip"
set "UV=%~dp0.uv\uv.exe"

echo   [1/4] uv, он ставит Python и библиотеки
if not exist "%UV%" goto :uv_get
"%UV%" --version >nul 2>nul && goto :uv_ready
echo         Старый uv не запускается, скачаю заново.
del /q "%UV%" >nul 2>nul

:uv_get
if not exist ".uv" mkdir ".uv"
echo         Скачиваю uv %DUBL_UV_VERSION% с GitHub, около 18 МБ
"%DUBL_PS%" -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { $w=New-Object Net.WebClient; $w.Proxy=[Net.WebRequest]::GetSystemWebProxy(); $w.Proxy.Credentials=[Net.CredentialCache]::DefaultNetworkCredentials; $w.DownloadFile($env:DUBL_UV_URL, $env:DUBL_UV_ZIP) } catch { Write-Host $_.Exception.Message; exit 1 }"
set "DUBL_RC=%errorlevel%"
if not "%DUBL_RC%"=="0" goto :uv_download_failed

rem Сверка хэша: чужой или битый архив не распаковывается и не запускается
"%DUBL_PS%" -NoProfile -Command "try { $h=(Get-FileHash -Algorithm SHA256 -LiteralPath $env:DUBL_UV_ZIP).Hash } catch { Write-Host $_.Exception.Message; exit 1 }; if ($h -ne $env:DUBL_UV_SHA256) { Write-Host ('        SHA256 получен:   ' + $h); Write-Host ('        SHA256 ожидался:  ' + $env:DUBL_UV_SHA256); exit 3 }"
set "DUBL_RC=%errorlevel%"
if "%DUBL_RC%"=="3" goto :uv_hash_bad
if not "%DUBL_RC%"=="0" goto :uv_download_failed

"%DUBL_PS%" -NoProfile -Command "try { Add-Type -AssemblyName System.IO.Compression.FileSystem; $z=[IO.Compression.ZipFile]::OpenRead($env:DUBL_UV_ZIP); try { foreach ($e in $z.Entries) { if ($e.Name) { [IO.Compression.ZipFileExtensions]::ExtractToFile($e, [IO.Path]::Combine($env:DUBL_UV_DIR, $e.Name), $true) } } } finally { $z.Dispose() } } catch { Write-Host $_.Exception.Message; exit 1 }"
set "DUBL_UNZIP=%errorlevel%"
del /q "%DUBL_UV_ZIP%" >nul 2>nul
if not "%DUBL_UNZIP%"=="0" goto :uv_unzip_failed
if not exist "%UV%" goto :uv_unzip_failed

:uv_ready
rem Кэш скачанного и Python держим в папке программы, а не в профиле на диске C.
rem Кэш после установки удаляется: uv ставит пакеты жёсткими ссылками на него,
rem и файлы в .venv остаются на месте. Если свой кэш у человека уже настроен,
rem берём его и не удаляем. Python всегда ставится в .python рядом с программой,
rem системный не используется
set "DROP_CACHE="
if not defined UV_CACHE_DIR set "DROP_CACHE=1"
if not defined UV_CACHE_DIR set "UV_CACHE_DIR=%~dp0.uv-cache"
set "UV_PYTHON_INSTALL_DIR=%~dp0.python"
set "UV_PYTHON_DOWNLOADS=automatic"

echo.
echo   [2/4] Python 3.12 и окружение в папке .venv
echo         Python uv ставит свой, в папку .python, около 20 МБ. Системный не нужен.
rem Окружение есть и запускается: оставляем. Если папку переносили или .python удалили,
rem python.exe в .venv указывает в пустоту, тогда окружение собирается заново
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "pass" >nul 2>nul && goto :packages
"%UV%" venv --python 3.12 --managed-python --clear .venv || goto :fail

:packages
echo.
echo   [3/4] Библиотеки, около 250 МБ
"%UV%" pip install --python .venv\Scripts\python.exe -r requirements.txt || goto :fail

rem Библиотеки CUDA для видеокарты (около 2 ГБ) здесь не ставятся. Их ставит кнопка
rem «Ускорить на видеокарте» в окне программы, по requirements-gpu.txt. Установщик
rem только запоминает, что видеокарта есть, чтобы сказать об этом в конце
set "DUBL_GPU="
where nvidia-smi >nul 2>nul && set "DUBL_GPU=1"

rem Модель распознавания скачивается заранее. Сбой тут установку не останавливает:
rem prepare_model.py сам объясняет, что модель скачается при первом запуске, а
rem итоговая фраза зависит от кода выхода (0 значит, что модель на диске)
echo.
echo   [4/4] Модель распознавания, чтобы первая запись началась сразу
".venv\Scripts\python.exe" prepare_model.py
set "DUBL_MODEL_RC=%errorlevel%"

:shortcut
if defined DROP_CACHE if exist ".uv-cache" rmdir /s /q ".uv-cache"
if /i "%~1"=="--no-shortcut" goto :done_no_shortcut
"%DUBL_PS%" -NoProfile -Command "$ErrorActionPreference='Stop'; $d=$env:DUBL_HOME; $l=[IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'Дубль.lnk'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut($l); $s.TargetPath=$d+'.venv\Scripts\pythonw.exe'; $s.Arguments='run.py'; $s.WorkingDirectory=$d; $s.IconLocation=$d+'web\img\icon.ico'; $s.Save(); if (-not (Test-Path -LiteralPath $l)) { exit 1 }"
set "DUBL_RC=%errorlevel%"
if not "%DUBL_RC%"=="0" goto :shortcut_failed

echo.
echo   Готово. Запуск ярлыком «Дубль» на рабочем столе или файлом Дубль.bat
echo   в этой папке.
call :summary
echo   Папку программы теперь нельзя переносить: ярлык и окружение помнят путь.
echo.
pause
exit /b 0

:done_no_shortcut
echo.
echo   Готово. Ярлык не создавался, запуск файлом Дубль.bat в этой папке.
call :summary
echo   Папку программы теперь нельзя переносить: окружение помнит путь.
echo.
pause
exit /b 0

:shortcut_failed
echo.
echo   Библиотеки установлены, но ярлык на рабочем столе создать не вышло.
echo   Программа работает: запускай файлом Дубль.bat из этой папки.
call :summary
echo.
pause
exit /b 1

:bad_dir
echo.
echo   Не получилось открыть папку, где лежит Установка.bat.
echo   Распакуй архив целиком на обычный диск, например в C:\Dubl, и запусти файл оттуда.
echo.
pause
exit /b 1

:not_unpacked
echo   Рядом с Установка.bat нет requirements.txt или run.py.
echo   Похоже, файл запущен прямо из архива. Распакуй архив целиком
echo   (правая кнопка на ZIP, «Извлечь всё») и запусти Установка.bat
echo   из распакованной папки.
echo.
pause
exit /b 1

:cancel
echo.
echo   Установка отменена. Распакуй программу вне OneDrive и запусти Установка.bat там.
echo.
pause
exit /b 1

:uv_download_failed
if exist "%DUBL_UV_ZIP%" del /q "%DUBL_UV_ZIP%" >nul 2>nul
echo.
echo   Не получилось скачать uv с GitHub. Проверь интернет, прокси и VPN
echo   и запусти Установка.bat ещё раз.
echo.
pause
exit /b 1

:uv_hash_bad
del /q "%DUBL_UV_ZIP%" >nul 2>nul
echo.
echo   Скачанный архив uv не прошёл проверку SHA256 и удалён. Установка остановлена.
echo   Так бывает при обрыве загрузки, а также если файл по дороге подменили
echo   (прокси, антивирус). Запусти Установка.bat ещё раз. Если ошибка повторится,
echo   сообщи об этом: https://github.com/ahiokk/dubl/issues
echo.
pause
exit /b 1

:uv_unzip_failed
echo.
echo   Архив uv прошёл проверку, но распаковать его не вышло.
echo   Проверь, что в папке .uv можно создавать файлы, и запусти Установка.bat ещё раз.
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

:summary
rem Итог по модели и видеокарте, одинаковый для всех вариантов «Готово»
if "%DUBL_MODEL_RC%"=="0" echo   Модель распознавания готова, первая запись начнётся сразу.
if not "%DUBL_MODEL_RC%"=="0" echo   Модель распознавания не скачана, она скачается при первом запуске.
if defined DUBL_GPU echo   Видеокарта NVIDIA найдена. Если драйвер подходит, программа предложит кнопку «Ускорить на видеокарте».
exit /b 0
