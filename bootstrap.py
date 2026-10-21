"""Подготовка окружения. Импортировать до faster_whisper и huggingface_hub.

Решает три проблемы Windows-установки:

1. CTranslate2 ищет cublas64_12.dll и cudnn*.dll в системном PATH, а pip кладёт их
   внутрь venv. Без этого расшифровка на GPU падает с «Library ... is not found».
2. Модели по умолчанию качаются в профиль на диск C, где мало места. Уводим их
   в папку models рядом со скриптом, если только HF_HOME не задан самим человеком.
3. Файлы внутри site-packages портятся: папка пакета на месте, часть модулей
   исчезает или превращается в мусор. Сначала грешили на антивирус, 21.08.2026
   выяснилось, что на диске D виноват сам диск. Приложение проверяет себя при
   запуске и молча переставляет побитое, вместо того чтобы падать с ImportError.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

_SUBDIRS = ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")

HERE = Path(__file__).resolve().parent
MODELS_DIR = HERE / "models"

# Модуль для проверки → пакет для переустановки. Проверяются только те, без
# которых приложение не запустится, иначе старт превратится в долгий ритуал.
CRITICAL: tuple[tuple[str, str], ...] = (
    ("numpy", "numpy"),
    ("numpy.fft", "numpy"),
    ("yaml", "pyyaml"),
    ("soxr", "soxr"),
    ("onnxruntime", "onnxruntime"),
    ("av", "av"),
    ("ctranslate2", "ctranslate2"),
    ("tokenizers", "tokenizers"),
    ("tqdm", "tqdm"),
    ("tqdm.auto", "tqdm"),
    ("huggingface_hub", "huggingface-hub"),
    ("httpx", "httpx"),
    ("keyring", "keyring"),
    ("pyaudiowpatch", "PyAudioWPatch"),
    ("webview", "pywebview"),
    # Проверяется последним и целиком: тянет за собой почти всё остальное,
    # поэтому ловит поломки в зависимостях, которых нет в списке поимённо
    ("faster_whisper", "faster-whisper"),
    ("faster_whisper.vad", "faster-whisper"),
)

# Без них не откроется окно. Импорт у них проходит, а побитая DLL роняет окно
# позже, внутри webview.start(): так 21.09.2026 умер WebView2.Core.dll на диске D.
# Поэтому их файлы сверяются по хэшам на каждом старте, это доли секунды.
WINDOW_STACK = ("pywebview", "pythonnet", "clr-loader")

# Сюда фоновая проверка пишет пакеты с битыми файлами, следующий старт их чинит
BROKEN_MARK = HERE / ".broken_packages"
# Оба файла идут в переустановку только как ограничения версий (-c). Библиотек CUDA
# из requirements-gpu.txt может не быть вовсе: Установка.bat их не ставит, их ставит
# кнопка «Ускорить на видеокарте» в окне. Ограничение на непоставленный пакет ничего
# не требует, а проверка файлов (damaged_packages) смотрит только на установленное
# и CUDA в CRITICAL не держит. Поэтому самолечение CUDA не ждёт и не ставит сама
REQUIREMENTS = (HERE / "requirements.txt", HERE / "requirements-gpu.txt")

# Переменная окружения, которую получает перезапущенный после починки процесс.
# Если и он не может импортировать пакеты, лечить второй раз нечем: показываем
# ошибку, а не перезапускаемся по кругу
HEALED_ENV = "DUBL_HEALED"


def use_local_model_cache() -> None:
    """Держит веса рядом со скриптом, а не в профиле пользователя.

    Если HF_HOME задан человеком, берём его: модель на несколько гигабайт там
    уже может лежать, и качать её заново в models незачем.
    """
    os.environ.setdefault("HF_HOME", str(MODELS_DIR))
    # Без этого huggingface_hub раз в сутки отправляет сведения об установке
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def enable_cuda_dlls() -> list[Path]:
    """Регистрирует каталоги с CUDA DLL. Возвращает список найденных путей."""
    if sys.platform != "win32":
        return []

    found: list[Path] = []
    for site in sys.path:
        nvidia = Path(site) / "nvidia"
        if not nvidia.is_dir():
            continue
        for sub in _SUBDIRS:
            bindir = nvidia / sub / "bin"
            if bindir.is_dir():
                os.add_dll_directory(str(bindir))
                found.append(bindir)
        if found:
            break

    # PATH нужен на случай, если DLL подтягивает другая DLL, а не сам Python
    if found:
        os.environ["PATH"] = (
            os.pathsep.join(str(p) for p in found) + os.pathsep + os.environ.get("PATH", "")
        )
    return found


# ------------------------------------------------------- защита от антивируса


def import_errors() -> dict[str, str]:
    """Пакеты, которые не импортируются, и первая ошибка импорта каждого."""
    import importlib

    errors: dict[str, str] = {}
    for module, package in CRITICAL:
        try:
            importlib.import_module(module)
        except Exception as exc:
            text = " ".join(f"{type(exc).__name__}: {exc}".split())
            errors.setdefault(package, f"{module}: {text}")
    return errors


def find_broken() -> list[str]:
    """Пакеты, которые не импортируются. Пустой список значит, что всё в порядке."""
    return list(import_errors())


def _canon(name: str) -> str:
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_name(dist) -> str:
    """Имя пакета. Если побит сам METADATA, берём его из имени папки dist-info."""
    try:
        name = dist.metadata["Name"]
    except Exception:
        name = None  # METADATA не читается как текст
    if name:
        return name
    # METADATA прочитался, но поля Name в мусоре нет
    return Path(str(getattr(dist, "_path", "unknown"))).name.split("-")[0]


def _file_ok(path: Path, expected: str) -> bool:
    import base64
    import hashlib

    try:
        with open(path, "rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").digest()
    except OSError:
        return False
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == expected


def damaged_packages(only: tuple[str, ...] | None = None) -> list[str]:
    """Пакеты, у которых файлы не совпадают с RECORD, записанным при установке.

    Ловит то, мимо чего проходит проверка импортом: битые DLL, данные и модули,
    которые подгружаются не на старте. Порча диска меняет содержимое файла, но
    оставляет размер и дату, поэтому сверяется именно хэш. Весь venv занимает
    3–12 секунд, поэтому целиком он проверяется в фоне, а на старте только only.
    """
    from importlib import metadata

    wanted = {_canon(n) for n in only} if only else None
    broken: list[str] = []
    for dist in metadata.distributions():
        name = _dist_name(dist)
        if wanted is not None and _canon(name) not in wanted:
            continue
        try:
            files = dist.files or []
        except Exception:
            files = None  # не читается RECORD, пакет точно битый
        if files is None or not all(
            _file_ok(Path(f.locate()), f.hash.value)
            for f in files
            if f.hash and f.hash.mode == "sha256"
        ):
            if name not in broken:
                broken.append(name)
    return broken


def mark_broken(packages: list[str]) -> None:
    BROKEN_MARK.write_text("\n".join(packages), encoding="utf-8")


def marked_broken() -> list[str]:
    try:
        text = BROKEN_MARK.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def clear_mark() -> None:
    BROKEN_MARK.unlink(missing_ok=True)


def pypi_reachable(timeout: float = 3.0, host: str = "pypi.org", port: int = 443) -> bool:
    """Есть ли связь с PyPI. Быстрая проверка перед починкой пакетов.

    Без неё каждый пакет стоил бы около 15 секунд отказа соединения у самого uv.
    Время ограничено целиком, вместе с поиском адреса: он тоже может зависнуть.
    Проверяется то место, куда реально пойдёт uv: прокси из окружения или из
    настроек Windows, свой индекс пакетов или pypi.org. Живое соединение с прокси
    не доказывает интернет, но мёртвый прокси ловит сразу.
    """
    import socket

    target = _connect_target(host, port)
    if target is None:
        return True  # непонятно, куда пойдёт uv: пусть пробует сам

    result: list[bool] = []

    def probe() -> None:
        try:
            socket.create_connection(target, timeout=timeout).close()
            result.append(True)
        except OSError:
            result.append(False)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout)
    return bool(result and result[0])


def _connect_target(host: str, port: int) -> tuple[str, int] | None:
    """Адрес, к которому подключится uv: прокси, свой индекс или сам host:port.

    None значит, что настройка есть, но разобрать её не вышло.
    """
    from urllib.parse import urlparse

    def split(url: str) -> tuple[str, int] | None:
        parts = urlparse(url if "://" in url else "http://" + url)
        if not parts.hostname:
            return None
        default = {"https": 443, "socks4": 1080, "socks5": 1080, "socks5h": 1080}.get(parts.scheme, 80)
        return parts.hostname, parts.port or default

    def env(*names: str) -> str:
        for name in names:
            value = os.environ.get(name) or os.environ.get(name.lower())
            if value:
                return value
        return ""

    proxy = env("HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY")
    if proxy:
        return split(proxy)

    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            if winreg.QueryValueEx(handle, "ProxyEnable")[0] == 1:
                server = str(winreg.QueryValueEx(handle, "ProxyServer")[0])
                # либо один адрес для всех, либо «http=a:1;https=b:2»
                entries = dict(item.split("=", 1) for item in server.split(";") if "=" in item)
                return split(entries.get("https") or entries.get("http") or server.split(";")[0])
    except (ImportError, OSError, ValueError):
        pass

    index = env("UV_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_INDEX_URL")
    if index:
        return split(index)
    return host, port


_ONLINE: bool | None = None


def online() -> bool:
    """pypi_reachable() с запоминанием: за один запуск проверяем связь один раз."""
    global _ONLINE
    if _ONLINE is None:
        _ONLINE = pypi_reachable()
    return _ONLINE


def find_uv() -> str | None:
    """Путь к uv. Свой, из папки .uv рядом с программой, идёт первым: его ставит
    Установка.bat по закреплённой версии и хэшу. Дальше uv из PATH и из
    %USERPROFILE%\\.local\\bin, куда его клал прежний установщик."""
    from shutil import which

    own = HERE / ".uv" / "uv.exe"
    if own.is_file():
        return str(own)
    return which("uv") or which(str(Path.home() / ".local" / "bin" / "uv"))


def _installer() -> list[str]:
    """Начало команды установки пакетов. Пустой список, если uv не найден.

    pip тут не годится: в окружении, которое делает uv, pip не ставится.
    Версии держатся по requirements.txt: переустановка не должна молча
    притащить свежий релиз, с которым приложение ещё не проверено.
    """
    uv = find_uv()
    if not uv:
        return []
    cmd = [uv, "pip", "install", "--python", sys.executable]
    # Короткие имена, а команда идёт из папки программы (см. repair): uv делит
    # значение -c по пробелам, и путь вида «D:\Мои программы\Дубль» рвался
    for req in REQUIREMENTS:
        if req.exists():
            cmd += ["-c", req.name]
    return cmd


def repair(packages: list[str], log=print) -> list[str]:
    """Переставляет побитые пакеты. Возвращает те, что починить не удалось.

    Только сам пакет, без зависимостей: --force-reinstall переставил бы всё дерево
    (у faster-whisper это 25 пакетов), а зависимости побитые проверяются и
    чинятся отдельно, по своему имени.
    """
    base = _installer()
    if not base:
        log("  не вышло: не найден uv")
        return list(packages)

    failed: list[str] = []
    for pkg in packages:
        log(f"Переустанавливаю {pkg}…")
        cmd = base + ["--no-cache", "--reinstall-package", pkg, "--no-deps", pkg]
        try:
            res = subprocess.run(
                cmd, cwd=str(HERE), capture_output=True, text=True, timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode != 0:
                failed.append(pkg)
                log(f"  не вышло: {res.stderr.strip()[-200:]}")
        except Exception as exc:
            failed.append(pkg)
            log(f"  не вышло: {exc}")
    return failed


def manual_repair_command(packages: list[str]) -> str:
    """Та же переустановка одной командой, чтобы человек выполнил её сам."""
    uv = r".uv\uv.exe" if (HERE / ".uv" / "uv.exe").is_file() else "uv"
    flags = " ".join(f"--reinstall-package {p}" for p in packages)
    return (
        rf"{uv} pip install --python .venv\Scripts\python.exe --no-deps "
        f"-c requirements.txt {flags} {' '.join(packages)}"
    )


def self_heal(log=print) -> tuple[bool, list[str]]:
    """Проверяет окружение и чинит его. Возвращает (нужен ли перезапуск, что не спаслось).

    Перезапуск нужен, потому что битый модуль мог быть частично загружен в память:
    доверять такому процессу нельзя, чище стартовать заново.
    """
    broken = find_broken()
    if not broken:
        return False, []

    log(f"Повреждены пакеты: {', '.join(broken)}. Обычно это антивирус.")
    failed = repair(broken, log)
    return True, failed


# --------------------------------------------------- целостность исходников

BACKUP_DIR = HERE / ".backup"
SOURCE_GLOBS = ("*.py", "*.yaml", "*.txt", "web/*")


def source_files() -> list[Path]:
    """Файлы самой программы. Личные дополнения (*.local.*, их README велит
    создавать вручную, и не всегда в UTF-8) и папка .backup сюда не входят:
    проверка не должна ни считать их порчей, ни подменять старой копией."""
    out: list[Path] = []
    for pattern in SOURCE_GLOBS:
        out += [
            p for p in HERE.glob(pattern)
            if p.is_file() and ".local." not in p.name and BACKUP_DIR not in p.parents
        ]
    return sorted(out)


def is_corrupted(path: Path) -> bool:
    """Файл считается битым, если в нём нулевые байты или он не читается как UTF-8.

    Ровно так выглядят следы антивируса: часть файла или весь файл заменяется
    двоичным мусором, причём размер и дата могут остаться правдоподобными.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return True
    if b"\x00" in raw:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def check_sources() -> list[Path]:
    """Битые файлы приложения. Пустой список значит, что всё цело."""
    return [p for p in source_files() if is_corrupted(p)]


def backup_sources() -> int:
    """Складывает копии целых файлов рядом. Возвращает число сохранённых.

    Бэкап обновляется только когда всё цело: иначе однажды сохраним мусор
    поверх последней хорошей копии, как это уже случилось с git.
    """
    import shutil

    files = source_files()
    if any(is_corrupted(p) for p in files):
        return 0

    BACKUP_DIR.mkdir(exist_ok=True)
    (BACKUP_DIR / "web").mkdir(exist_ok=True)
    saved = 0
    for p in files:
        target = BACKUP_DIR / p.relative_to(HERE)
        try:
            shutil.copy2(p, target)
            saved += 1
        except OSError:
            pass
    return saved


def restore_sources(broken: list[Path]) -> tuple[list[Path], list[Path]]:
    """Возвращает (что восстановили, что не удалось)."""
    import shutil

    healed, failed = [], []
    for p in broken:
        backup = BACKUP_DIR / p.relative_to(HERE)
        if backup.exists() and not is_corrupted(backup):
            try:
                shutil.copy2(backup, p)
                healed.append(p)
                continue
            except OSError:
                pass
        failed.append(p)
    return healed, failed


def already_healed() -> bool:
    """Запущены ли мы после починки: тогда второй раз лечить и перезапускаться нельзя."""
    return os.environ.get(HEALED_ENV) == "1"


def restart_self() -> None:
    """Перезапускает приложение тем же интерпретатором и с теми же аргументами.

    Новый процесс получает HEALED_ENV: если пакеты и после починки не импортируются,
    он покажет ошибку вместо ещё одного круга.
    """
    script = str(Path(sys.argv[0]).resolve())
    subprocess.Popen(
        [sys.executable, script, *sys.argv[1:]],
        cwd=str(HERE),
        env={**os.environ, HEALED_ENV: "1"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


use_local_model_cache()
enable_cuda_dlls()
