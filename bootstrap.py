"""Подготовка окружения. Импортировать до faster_whisper и huggingface_hub.

Решает три проблемы Windows-установки:

1. CTranslate2 ищет cublas64_12.dll и cudnn*.dll в системном PATH, а pip кладёт их
   внутрь venv. Без этого расшифровка на GPU падает с «Library ... is not found».
2. Модели по умолчанию качаются в профиль на диск C, где мало места. Уводим их
   в папку models рядом со скриптом.
3. Файлы внутри site-packages портятся: папка пакета на месте, часть модулей
   исчезает или превращается в мусор. Сначала грешили на антивирус, 21.08.2026
   выяснилось, что на диске D виноват сам диск. Приложение проверяет себя при
   запуске и молча переставляет побитое, вместо того чтобы падать с ImportError.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
REQUIREMENTS = (HERE / "requirements.txt", HERE / "requirements-gpu.txt")


def use_local_model_cache() -> None:
    """Держит веса рядом со скриптом, а не в профиле пользователя."""
    os.environ.setdefault("HF_HOME", str(MODELS_DIR))


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


def find_broken() -> list[str]:
    """Пакеты, которые не импортируются. Пустой список значит, что всё в порядке."""
    import importlib

    broken: list[str] = []
    for module, package in CRITICAL:
        try:
            importlib.import_module(module)
        except Exception:
            if package not in broken:
                broken.append(package)
    return broken


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


def _installer() -> list[str]:
    """uv быстрее, но его может не быть, тогда обычный pip.

    Версии держатся по requirements.txt: переустановка не должна молча
    притащить свежий релиз, с которым приложение ещё не проверено.
    """
    from shutil import which

    # Установщик uv кладёт его сюда, а PATH у окна может быть ещё старый
    uv = which("uv") or which(str(Path.home() / ".local" / "bin" / "uv"))
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable]
    else:
        cmd = [sys.executable, "-m", "pip", "install"]
    for req in REQUIREMENTS:
        if req.exists():
            cmd += ["-c", str(req)]
    return cmd


def repair(packages: list[str], log=print) -> list[str]:
    """Переставляет побитые пакеты. Возвращает те, что починить не удалось."""
    failed: list[str] = []
    for pkg in packages:
        log(f"Переустанавливаю {pkg}…")
        cmd = _installer() + ["--force-reinstall", "--no-cache", pkg]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode != 0:
                failed.append(pkg)
                log(f"  не вышло: {res.stderr.strip()[-200:]}")
        except Exception as exc:
            failed.append(pkg)
            log(f"  не вышло: {exc}")
    return failed


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
    out: list[Path] = []
    for pattern in SOURCE_GLOBS:
        out += [p for p in HERE.glob(pattern) if p.is_file()]
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


def restart_self() -> None:
    """Перезапускает приложение тем же интерпретатором и с теми же аргументами."""
    subprocess.Popen(
        [sys.executable, *sys.argv],
        cwd=str(HERE),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


use_local_model_cache()
enable_cuda_dlls()
