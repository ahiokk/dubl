"""Окно живой расшифровки созвона.

Слушает микрофон и звук из наушников, показывает диалог по ходу разговора и
одновременно дописывает markdown-файл. Путь к файлу виден в окне и копируется
одной кнопкой: его можно отдать ИИ-агенту прямо во время разговора.

Интерфейс это обычная веб-страница из папки web, поднятая в нативном окне
через WebView2. Питон отдаёт ей события, она зовёт питон через pywebview.api.

    python app.py
"""

from __future__ import annotations

import ctypes
import functools
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

import webview

import api_keys
import archive
import bootstrap
from asr import (
    CUDA_DLLS,
    LANGUAGES,
    LOCAL_MODELS,
    PROVIDERS,
    DownloadCancelled,
    ModelDownloadError,
    Recognizer,
    RecognizerError,
    _cuda_libs_found,
    _GPU_ORDER,
    check_server_url,
    cuda_available,
    default_local_model,
    describe,
    ensure_model,
    make_recognizer,
    mask_secrets,
    model_ready,
    normalize_config,
    probe,
    read_glossary,
)
from capture import Capture, Segment, list_devices
from interview import (
    BANKS_DIR,
    Bank,
    BankError,
    Session,
    describe_levels,
    fix_request,
    parse_bank_reply,
    save_bank,
    summarize_history,
)
from interview import bank_prompt as build_bank_prompt
from speech_metrics import SpeechAnalyzer, analyze_session, session_report, session_ui

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "расшифровки"
SETTINGS_FILE = HERE / "настройки.json"
ICON = HERE / "web" / "img" / "icon.ico"
APP_ID = "ahiokk.dubl"
ME = "Я"
THEM = "Собеседник"

# Границы усиления микрофона. Ниже единицы опускать незачем, а выше восьми
# вытянутый шум начинает мешать распознаванию сильнее, чем помогает громкость
GAIN_MIN, GAIN_MAX = 1.0, 8.0
# Звук из компьютера можно тянуть сильнее: это чистый цифровой сигнал без шума
# комнаты, а тихим он бывает, когда в приложении созвона убавлена громкость
THEM_GAIN_MAX = 12.0

# Как часто отправлять в окно накопившиеся реплики и уровни звука
PUMP_SEC = 0.1
# Как часто заглядывать в папку с банками вопросов
BANKS_POLL_SEC = 2.0
# Сколько ждать хвост расшифровки после остановки. Процессор и далёкий сервер
# отстают от речи, и к остановке в очереди ещё лежат реплики
STOP_WAIT_SEC = 600
# Одна и та же ошибка распознавания повторяется в окне не чаще этого
ERROR_REPEAT_SEC = 30
# Крестик нажали ещё раз, а запись всё дописывается: после этого срока (от первого
# нажатия) окно закрывается как есть. Реплики, которые успели расшифроваться,
# к этому времени уже лежат в файле, теряется только недорасшифрованный хвост
CLOSE_FORCE_SEC = 20
# Банк, который писали меньше этого назад, ещё могут писать: читаем на следующем круге
BANK_SETTLE_NS = 1_200_000_000
# Часы файловой системы и time_ns() расходятся на миллисекунды, так что «свежий»
# файл может оказаться чуть в будущем. Дальше этого запаса время считается чужим
BANK_FUTURE_SLACK_NS = 5_000_000_000

# Сколько окно ждёт ответа потока устройств, пока тот занят чем-то другим. Список
# устройств, саундчек и проверка микрофона укладываются в секунды. Старт ответа
# может упереться в загрузку модели, поэтому ему минута. Остановка ответа это
# расшифровка записанного, она идёт столько же, сколько хвост после встречи
DEVICES_WAIT_SEC = 15
MONITOR_WAIT_SEC = 20
MIC_CHECK_WAIT_SEC = 20
ANSWER_START_WAIT_SEC = 60
ANSWER_STOP_WAIT_SEC = STOP_WAIT_SEC
# Ответ расшифровывается по ходу речи. Столько неудач подряд после «Закончить ответ»
# значат, что распознавание не отвечает (лёг сервер или сеть): остаток очереди не
# пробуем, иначе пришлось бы ждать таймаут на каждую недоделанную реплику
ANSWER_GIVE_UP_FAILS = 3
# Закрытие окна посреди ответа ждёт реплику, которую поток расшифровки уже разбирает:
# живой вызов распознавателя в потоке-демоне при выходе процесса может его уронить
ANSWER_ABORT_WAIT_SEC = 5

# Что окно просит запомнить между запусками: имя в окне, ключ в настройки.json.
# Браузерное хранилище окна для этого не годится: pywebview открывает страницу
# в приватном режиме, и всё сохранённое там стирается при выходе. Другие ключи
# окно записать не может
REMEMBER_KEYS = {
    "micName": "микрофон",
    "spkName": "устройство_собеседника",
    "ivBank": "банк_тренажёра",
}
# Имя устройства в Windows или файла банка короче этого с большим запасом
REMEMBER_MAX_LEN = 300

# Отметка в настройки.json: человек уже выбрал, с чего начать. Пока её нет, окно
# открывается экраном выбора «Тренажёр или запись»
FIRST_RUN_KEY = "первый_запуск_пройден"

# Библиотеки CUDA (около 2 ГБ) для видеокарты NVIDIA ставит кнопка «Ускорить на
# видеокарте»: Установка.bat их не качает. Ставятся тем же uv, что и остальные пакеты
GPU_REQUIREMENTS = HERE / "requirements-gpu.txt"
GPU_SIZE_TEXT = "около 2 ГБ"
# Дольше установка не идёт: uv сам обрывает зависший запрос, а это страховка от вечного ожидания
GPU_INSTALL_MAX_SEC = 45 * 60
# Что установка добавляет к окружению программы. Тот же набор знает audit hook (_SpawnPermit):
# любые другие переменные у uv он не пускает
GPU_ENV_EXTRA = {"NO_COLOR": "1", "UV_NO_PROGRESS": "1"}
# Кэш uv на время установки лежит в папке программы и потом удаляется, как в
# Установка.bat: иначе 2 ГБ колёс CUDA осели бы в профиле Windows на диске C.
# Свой UV_CACHE_DIR человека не трогаем
GPU_CACHE_DIR = HERE / ".uv-cache"
GPU_DROP_CACHE = "UV_CACHE_DIR" not in os.environ
if GPU_DROP_CACHE:
    GPU_ENV_EXTRA["UV_CACHE_DIR"] = str(GPU_CACHE_DIR)

# Запись настроек идёт из потоков окна и аудио, временный файл у них общий
_SETTINGS_LOCK = threading.Lock()

# Кто может держать устройства, и что сказать человеку, когда он мешает. Одновременно
# устройствами пользуется одна вещь, см. Recorder._holders
_BUSY_TEXT = {
    "start": "Запись встречи запускается, подожди",
    "meeting": "Идёт запись встречи, сначала останови её",
    "answer": "Идёт ответ в тренажёре, сначала закончи его",
    "check": "Идёт проверка микрофона, сначала закончи её",
    "monitor": "Идёт проверка звука на экране записи",
}
# Кто мешает занять устройства под каждое дело. Саундчек и проверку микрофона новое
# дело вытесняет само, поэтому их в списках нет, где вытеснение допустимо
_BLOCKED_BY = {
    "meeting": ("start", "meeting", "answer", "check"),
    "answer": ("start", "meeting", "answer"),
    "check": ("start", "meeting", "answer"),
    "monitor": ("start", "meeting", "answer", "check"),
}
# Команды потоку устройств, которые открывают что-то. Если на ответ уже не ждут
# (вышел срок), открывать незачем
_OPENING = frozenset({"devices", "monitor", "mic_check", "answer_start"})


class _Discard:
    """Очередь, которая ничего не хранит. Саундчеку реплики не нужны,
    а обычная очередь за час проверки набрала бы гигабайт звука."""

    def put(self, *args, **kwargs) -> None:
        pass


_DISCARD = _Discard()


class DeviceNotFound(Exception):
    """Выбранного устройства больше нет. Текст идёт человеку как есть."""


class _Reply(queue.Queue):
    """Очередь для ответа потока устройств плюс отметка «ждать перестали».

    Вызов из окна кладёт команду и ждёт ответ с таймаутом. Вышел срок, значит
    окно уже получило ошибку, и команда, дождавшаяся своей очереди позже, не
    должна ничего открывать: человек об этом не знает и закрывать некому.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = threading.Event()


class _AnswerJob:
    """Расшифровка ответа в тренажёре, которая идёт, пока человек ещё говорит.

    Источник кладёт в очередь каждую реплику, как только детектор речи её
    отрезал, а поток расшифровки (Recorder._answer_loop) забирает их сразу, не
    дожидаясь «Закончить ответ». После остановки остаётся хвост: реплика, которую
    источник отдаёт при закрытии. У каждого ответа свой экземпляр: не успевший
    кончить поток прошлого ответа не подмешает свои реплики в новый.
    """

    def __init__(self, segments: "queue.Queue", transcriber: Recognizer) -> None:
        self.segments = segments
        self.transcriber = transcriber
        # Распознанное: (начало, длительность, текст), в порядке расшифровки
        self.parts: list[tuple[float, float, str]] = []
        # Причины, по которым реплики не распознались, и где в ответе они были: речь
        # на этих отрезках была, это не пауза
        self.lost: list[str] = []
        self.lost_spans: list[tuple[float, float]] = []
        self.fails_in_row = 0  # неудач подряд на конец разобранного
        self.seen = 0  # сколько реплик разобрано, считая пустые и упавшие
        self.abort = threading.Event()  # ответ не нужен: бросить, не доделывая
        self.finishing = threading.Event()  # «Закончить ответ» нажато, новых реплик не будет
        self.finished = threading.Event()  # поток расшифровки кончил

    def end(self) -> None:
        """Знак конца в очередь: после него реплик не будет, а ждущий поток просыпается."""
        self.finishing.set()
        self.segments.put(None)


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def clean_text(value) -> str:
    """Убирает то, что не кодируется в UTF-8: одиночные суррогаты из сбойного
    распознавания или чужого текста ошибки. С ними падает и запись в файл, и
    вызов окна, а реплика пропадает вместе с потоком, который её нёс."""
    return str(value).encode("utf-8", "replace").decode("utf-8")


def sentence(text: str) -> str:
    """Текст ошибки с заглавной буквы. Ошибки хранилища ключей начинаются со строчной
    («не удалось сохранить ключ…») и читаются как продолжение фразы, а в окне стоят
    сами по себе."""
    text = str(text).strip()
    return text[:1].upper() + text[1:]


def write_text_atomic(path: Path, text: str) -> None:
    """Пишет файл целиком или не пишет совсем.

    Сначала временный файл в той же папке (на другом диске os.replace не
    атомарен), потом подмена. Обрыв посреди записи оставляет прежний файл
    целым, а не обрезанным до нуля, как делает write_text.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", errors="replace") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        # Антивирус или чужая программа может держать файл доли секунды, и замена
        # получает отказ в доступе
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def create_unique(directory: Path, stem: str, suffix: str, text: str) -> Path:
    """Создаёт новый файл с текстом и никогда не трогает существующий.

    Имя «stem + suffix», занято, значит «stem (2) + suffix», «stem (3)» и так
    дальше, как в interview.Session.save. Режим "x" проверяет и создаёт одним
    шагом: две записи в одну минуту не затрут друг друга.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        name = f"{stem}{suffix}" if n == 1 else f"{stem} ({n}){suffix}"
        path = directory / name
        try:
            f = path.open("x", encoding="utf-8", errors="replace")
        except FileExistsError:
            continue
        try:
            with f:
                f.write(text)
        except BaseException:
            path.unlink(missing_ok=True)  # пустой файл после неудачи не оставляем
            raise
        return path
    raise OSError(f"в папке «{directory.name}» слишком много файлов «{stem}»")


# -------------------------------------------------- модель и видеокарта (помощники)


def _auto_model() -> str:
    """Модель, которую берём, пока человек сам ни разу не выбирал её в настройках.

    Это default_local_model() из asr, с одной поправкой. На видеокарте small не
    годится: она лежит на диске с установки, когда карты ещё не касались библиотеки
    CUDA, и после кнопки «Ускорить на видеокарте» должна смениться на turbo, а не
    остаться, потому что «первая из скачанных».
    """
    model = default_local_model()
    if model == "small" and cuda_available():
        return "large-v3-turbo"
    return model


def _model_chosen(raw) -> bool:
    """Выбрал ли человек модель сам: в сохранённых настройках есть своё поле «модель»."""
    return isinstance(raw, dict) and raw.get("модель") in {m["id"] for m in LOCAL_MODELS}


@functools.cache
def gpu_present() -> bool:
    """Есть ли в компьютере видеокарта NVIDIA, на которой CTranslate2 мог бы считать.
    Библиотек CUDA это не касается: их наличие проверяет cuda_available()."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _cuda_libs_on_disk() -> bool:
    """Лежат ли библиотеки CUDA в пакетах nvidia-* окружения. Сразу после установки
    их ещё нет в PATH процесса (bootstrap добавляет их при старте), но на диске они есть."""
    for site in sys.path:
        nvidia = Path(site) / "nvidia"
        try:
            if nvidia.is_dir() and all(any(nvidia.glob(f"*/bin/{dll}")) for dll in CUDA_DLLS):
                return True
        except OSError:
            continue
    return False


def _venv_python() -> str:
    """Интерпретатор окружения для uv. Под pythonw у процесса нет консоли, и uv не
    получает от него ответ на разведку, поэтому берём python.exe из той же папки."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if console.is_file() else exe)


def gpu_install_plan() -> tuple[list[str] | None, str]:
    """Команда установки библиотек CUDA и, если её нет, причина для человека.

    Команда это uv по полному пути: свой из папки .uv, иначе из PATH. Тот же список
    разрешает audit hook (см. _SpawnPermit), поэтому строится в одном месте и один раз
    при старте. Хук сверяет командную строку, а не содержимое файла: разрешён любой
    файл по этому пути в тот момент, когда нажали кнопку.

    Ставим только в .venv программы. Запущенная чужим интерпретатором (python app.py
    из глобального Python или другого venv), она положила бы 2 ГБ не туда. uv из PATH
    берём, только если это настоящий .exe и лежит он не в текущей папке и не в папке
    программы: which на Windows ищет сначала в текущей папке и знает uv.cmd с uv.bat.
    """
    if Path(sys.prefix).resolve() != (HERE / ".venv").resolve():
        return None, ("Программа открыта не через Дубль.bat, поэтому ставить библиотеки "
                      "некуда. Открой её через Дубль.bat и нажми кнопку ещё раз")
    uv = bootstrap.find_uv()
    path = Path(uv).resolve() if uv else None
    if path is not None and path != (HERE / ".uv" / "uv.exe").resolve():
        try:
            cwd = Path.cwd().resolve()
        except OSError:
            cwd = HERE
        if path.suffix.lower() != ".exe" or path.parent in (cwd, HERE):
            path = None
    if path is None:
        return None, ("Не нашёл установщик пакетов. Запусти Установка.bat, она его "
                      "поставит, потом открой программу заново")
    return [str(path), "pip", "install", "--python", _venv_python(),
            "-r", str(GPU_REQUIREMENTS)], ""


def gpu_install_argv() -> list[str] | None:
    """Команда установки библиотек CUDA или None (см. gpu_install_plan)."""
    return gpu_install_plan()[0]


def restart_argv() -> list[str]:
    """Перезапуск программы: тот же интерпретатор и run.py, как в Дубль.bat."""
    return [sys.executable, str(HERE / "run.py")]


def gpu_manual_command() -> str:
    """Та же установка командой, чтобы человек выполнил её сам, если кнопка не справилась."""
    uv = r".uv\uv.exe" if (HERE / ".uv" / "uv.exe").is_file() else "uv"
    return rf"{uv} pip install --python .venv\Scripts\python.exe -r requirements-gpu.txt"


class _GpuError(Exception):
    """Сбой установки CUDA: human это фраза для окна, detail строка установщика для подсказки."""

    def __init__(self, human: str, detail: str = "") -> None:
        super().__init__(human)
        self.human = human
        self.detail = detail


def _one_line(text: str) -> str:
    """Строка вывода uv в одну строку без секретов (в адресе индекса бывает логин и пароль)."""
    return " ".join(mask_secrets(clean_text(text)).split())[:400]


class Api:
    """Тонкий мост для окна: только то, что вправе звать JavaScript.

    Объект в js_api окна не отдаётся. Диспетчер pywebview ищет вызов из
    страницы по точкам через getattr без всякого фильтра на «_», и через
    ссылку на рекордер любой скрипт в окне доходил до os.system. Поэтому окно
    создаётся с js_api=None, а методы ниже открываются по одному через
    window.expose (см. expose_api): страница достаёт только имена из API_NAMES.

    Новый метод для окна: добавить его здесь и в API_NAMES. Метод, которого в
    API_NAMES нет, из окна не виден, и это сделано нарочно.
    """

    def __init__(self, recorder: "Recorder") -> None:
        self._rec = recorder

    def get_devices(self) -> dict:
        return self._rec.get_devices()

    def start(self, mic_index: int, spk_index: int,
              mic_name: str = "", spk_name: str = "") -> dict:
        return self._rec.start(mic_index, spk_index, mic_name, spk_name)

    def stop(self) -> dict:
        return self._rec.stop()

    def monitor_start(self, mic_index: int, spk_index: int,
                      mic_name: str = "", spk_name: str = "") -> dict:
        return self._rec.monitor_start(mic_index, spk_index, mic_name, spk_name)

    def monitor_stop(self) -> dict:
        return self._rec.monitor_stop()

    def open_dir(self) -> dict:
        return self._rec.open_dir()

    def open_url(self, url: str) -> dict:
        return self._rec.open_url(url)

    def open_banks_dir(self) -> dict:
        return self._rec.open_banks_dir()

    # -------------------------------------------------------- распознавание

    def asr_settings(self) -> dict:
        return self._rec.asr_settings()

    def asr_save(self, cfg: dict, key: str = "", model_chosen: bool = False) -> dict:
        return self._rec.asr_save(cfg, key, model_chosen)

    def asr_test(self, cfg: dict, key: str = "") -> dict:
        return self._rec.asr_test(cfg, key)

    def asr_forget_key(self, provider: str) -> dict:
        return self._rec.asr_forget_key(provider)

    # ------------------------------------------------ быстрый старт и модель

    def first_run_done(self) -> dict:
        return self._rec.first_run_done()

    def model_retry(self) -> dict:
        return self._rec.model_retry()

    def gpu_install(self) -> dict:
        return self._rec.gpu_install()

    def restart_app(self) -> dict:
        return self._rec.restart_app()

    # ------------------------------------------------------------- архив

    def archive_list(self) -> list:
        return archive.listing()

    def archive_search(self, query: str) -> list:
        return archive.search(query)

    def archive_read(self, path: str) -> dict:
        return archive.read(path)

    def archive_delete(self, path: str) -> dict:
        return archive.delete(path)

    def archive_rename(self, path: str, name: str) -> dict:
        return archive.rename(path, name)

    def archive_digest(self, path: str) -> dict:
        return self._rec.archive_digest(path)

    def archive_open(self, path: str) -> dict:
        return self._rec.archive_open(path)

    def get_settings(self) -> dict:
        return self._rec.get_settings()

    def remember(self, key: str, value: str) -> dict:
        return self._rec.remember(key, value)

    def set_mic_gain(self, value: float) -> dict:
        return self._rec.set_mic_gain(value)

    def set_them_gain(self, value: float) -> dict:
        return self._rec.set_them_gain(value)

    # ---------------------------------------------------------- мок-собес

    def interview_setup(self) -> dict:
        return self._rec.interview_setup()

    def bank_prompt(self, vacancy: str = "") -> dict:
        return self._rec.bank_prompt(vacancy)

    def bank_save(self, reply: str) -> dict:
        return self._rec.bank_save(reply)

    def interview_start(self, bank_file: str, level: str, mic_index: int,
                        mic_name: str = "", categories: list[str] | None = None,
                        shuffle: bool = False, only_ids: list[str] | None = None) -> dict:
        return self._rec.interview_start(
            bank_file, level, mic_index, mic_name, categories, shuffle, only_ids
        )

    def mic_check_start(self, mic_index: int, mic_name: str = "") -> dict:
        return self._rec.mic_check_start(mic_index, mic_name)

    def mic_check_stop(self) -> dict:
        return self._rec.mic_check_stop()

    def answer_start(self) -> dict:
        return self._rec.answer_start()

    def followup_start(self, question_text: str) -> dict:
        return self._rec.followup_start(question_text)

    def answer_stop(self) -> dict:
        return self._rec.answer_stop()

    def interview_retry(self) -> dict:
        return self._rec.interview_retry()

    def interview_next(self) -> dict:
        return self._rec.interview_next()

    def interview_finish(self) -> dict:
        return self._rec.interview_finish()


# Что окну позволено звать: ровно то, что web/app.js берёт через api().<имя>.
# Сверка: grep -o "api()\.[a-z_]*" web/app.js | sort -u
API_NAMES = (
    "get_devices", "start", "stop", "monitor_start", "monitor_stop",
    "open_dir", "open_url", "open_banks_dir",
    "asr_settings", "asr_save", "asr_test", "asr_forget_key",
    "first_run_done", "model_retry", "gpu_install", "restart_app",
    "archive_list", "archive_search", "archive_read", "archive_delete",
    "archive_rename", "archive_digest", "archive_open",
    "get_settings", "remember", "set_mic_gain", "set_them_gain",
    "interview_setup", "bank_prompt", "bank_save",
    "interview_start", "mic_check_start", "mic_check_stop",
    "answer_start", "followup_start", "answer_stop",
    "interview_retry", "interview_next", "interview_finish",
)


class _NoAttributes:
    """Корень обхода для диспетчера pywebview вместо None.

    Имя, которого нет среди открытых функций, pywebview ищет обходом атрибутов
    от js_api окна. При js_api=None обход идёт от None, и цепочка
    __class__.__base__ доходит до object и его методов. У этого объекта нет ни
    одного атрибута: обход кончается на первом шаге, и диспетчер отвечает, что
    такой функции нет. dir() пустой, поэтому заглушек для JS он тоже не даёт.
    """

    __slots__ = ()

    def __getattribute__(self, name):
        return None

    def __dir__(self):
        return []


def expose_api(window: webview.Window, api: Api) -> None:
    """Открывает окну методы Api по точным именам из API_NAMES.

    Берём не bound method, а functools.partial: pywebview строит заглушку
    для JS по списку аргументов, и у bound method туда попадает self. Вызову
    это не мешает (заглушка передаёт все аргументы как есть), но список
    параметров с partial выходит тот же, что был у обхода через js_api.
    """
    funcs = []
    for name in API_NAMES:
        fn = functools.partial(getattr(Api, name), api)
        fn.__name__ = name  # expose берёт имя заглушки отсюда
        funcs.append(fn)
    window.expose(*funcs)
    # Внутреннее поле pywebview 6.2.1: от него диспетчер начинает обход
    # неизвестных имён (см. _NoAttributes)
    window._js_api = _NoAttributes()


class Recorder:
    """Вся работа, кроме интерфейса.

    Устройства открываются и закрываются в одном выделенном потоке: PortAudio
    не переносит обращений из разных потоков и падает без traceback.
    """

    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self.analyzer = SpeechAnalyzer()
        self.transcriber: Recognizer | None = None
        # С какими настройками собран текущий распознаватель. Сравниваем при каждом
        # старте записи: поменяли в настройках, значит собираем заново
        self.transcriber_cfg: dict | None = None

        self.segments: "queue.Queue[Segment]" = queue.Queue()
        self.ready: "queue.Queue[tuple]" = queue.Queue()
        self.commands: "queue.Queue[tuple]" = queue.Queue()

        self.levels = {ME: 0.0, THEM: 0.0}
        self.lines: list[tuple[float, str, str, float]] = []
        self.capture: Capture | None = None
        self.out_path: Path | None = None
        # Шапка, с которой создан файл записи. По ней видно, что в файле больше ничего
        # нет: пустую запись (ни реплик, ни дыр) после остановки удаляем
        self.header_text = ""
        self.session_start = 0.0
        # Реплики, которые не распознались: (время, кто, причина). В файле остаются
        # видимой дырой, иначе кусок разговора пропадёт молча
        self.gaps: list[tuple[float, str, str]] = []
        self.last_error = ("", 0.0)
        # Что запись сейчас: "meeting" (встреча), "answer" (ответ в тренажёре) или
        # None. Решает, что делать при остановке: итоговый файл собирается только
        # у встречи
        self.rec_kind: str | None = None
        # Итог по встрече уже записан в файл: второй раз его переписывать нельзя,
        # он затёр бы выжимку, которую успели дописать, или воскресил удалённый файл
        self.finalized = False
        # Хоть одну реплику не удалось дописать в файл по ходу записи. Если после
        # этого не вышла и итоговая перезапись, часть реплик есть только в окне
        self.append_failed = False

        # режим тренажёра
        self.session: Session | None = None
        self.interview_mic = -1
        self.interview_mic_name = ""
        # если заполнено, следующий записанный ответ идёт как уточнение к прошлому
        self.pending_followup: str | None = None
        # Расшифровка идущего ответа (см. _AnswerJob). Распознаватель общий для ответов
        # и записи встречи: поток прошлого ответа, не успевший кончить, не работает с ним
        # одновременно с потоком нового ответа или встречи
        self.answer_job: _AnswerJob | None = None
        self._asr_lock = threading.Lock()

        # Предупреждения, которые до загрузки страницы показать некому: pump
        # отдаёт их окну, когда оно готово
        self._startup_warnings: list[str] = []
        self.settings = self._load_settings()
        self.mic_gain = self._gain_setting("усиление_микрофона", GAIN_MAX)
        self.them_gain = self._gain_setting("усиление_собеседника", THEM_GAIN_MAX)
        self.asr_cfg = self._startup_asr_config()

        # Локальная модель: что окну показать про её загрузку (см. _ensure_local).
        # Качает один поток за раз, ход идёт в окно через очередь ready
        self._model_lock = threading.Lock()
        self._model_info = {"state": "idle", "model": "", "done": 0, "total": 0,
                            "source": "huggingface", "error": ""}
        self._dl_lock = threading.Lock()
        self._model_cancel = threading.Event()
        self._model_thread: threading.Thread | None = None
        # Библиотеки CUDA для видеокарты (см. gpu_install). Команду uv считаем один раз:
        # audit hook разрешает ровно её
        self._gpu_lock = threading.Lock()
        self._gpu = {"state": "idle", "text": "", "detail": "", "since": 0.0}
        self._gpu_thread: threading.Thread | None = None
        self._gpu_proc: subprocess.Popen | None = None
        self._gpu_argv, self._gpu_missing = gpu_install_plan()
        # Крестик при идущей установке один раз предупреждает, второй закрывает (on_closing)
        self._gpu_close_warned = False
        # Отпечаток папки банков, который уже показан окну (см. _banks_loop, bank_save)
        self._banks_last: list[tuple] = []

        self.recording = threading.Event()
        self.checking = threading.Event()  # открыт микрофон ради проверки уровня
        # Саундчек на экране записи: обе дорожки открыты, но ничего не пишется
        self.monitoring = threading.Event()
        # Идёт команда «старт»: от нажатия кнопки до конца _do_start. Устройства
        # ещё могут быть не открыты, но уже заняты, и закрывать окно в это время
        # нельзя. Ставится в потоке окна под _claim_lock, снимается потоком устройств
        self.starting = threading.Event()
        self._claim_lock = threading.Lock()
        # Номер последнего вызова саундчека (старт или стоп). Команда с устаревшим
        # номером ничего не открывает: см. monitor_start
        self._monitor_gen = 0
        self._monitor_lock = threading.Lock()
        self.mic_peak = 0.0
        self.worker_stop = threading.Event()
        self.worker: threading.Thread | None = None
        # Остановка уже идёт: закрытие окна должно дождаться её, а не решить, что
        # раз recording сброшен, то ждать нечего
        self.stopping = threading.Event()
        # Окно просят закрыть посреди записи: закрываем его сами, когда допишем
        self.closing = threading.Event()
        # Процесс выходит: загрузки и установка больше не начинаются (см. shutdown)
        self.exiting = threading.Event()
        self.close_asked_at = 0.0
        # Хвост дописан, и теперь закрытие окна пропускаем без вопросов
        self.close_ok = threading.Event()

        threading.Thread(target=self._audio_loop, daemon=True, name="audio").start()
        threading.Thread(target=self._pump_loop, daemon=True, name="pump").start()
        threading.Thread(target=self._banks_loop, daemon=True, name="banks").start()

    # -------------------------------------------------------------- настройки

    def _load_settings(self) -> dict:
        """Читает настройки.json. Битый файл или неверная форма не роняют запуск:
        что не прочиталось, берётся по умолчанию, а окно скажет об этом, когда
        загрузится."""
        try:
            raw = SETTINGS_FILE.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except UnicodeError as exc:  # битая кодировка: файл не наш, не затираем его
            self._startup_warnings.append(
                f"Файл настройки.json не прочитался ({exc}), взяты значения по умолчанию"
                + self._set_settings_aside()
            )
            return {}
        except (OSError, ValueError) as exc:
            self._startup_warnings.append(
                f"Файл настройки.json не прочитался ({exc}), взяты значения по умолчанию"
            )
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            # Не только ValueError: файл из ста тысяч «[» даёт RecursionError, а
            # на любой чужой файл запуск падать не должен
            self._startup_warnings.append(
                "Файл настройки.json повреждён, взяты значения по умолчанию"
                + self._set_settings_aside()
            )
            return {}
        if not isinstance(data, dict):
            self._startup_warnings.append(
                "Файл настройки.json устроен не так, как ждёт программа, "
                "взяты значения по умолчанию" + self._set_settings_aside()
            )
            return {}

        # Верхний уровень цел, но внутри могло быть что угодно: приводим к форме,
        # с которой работает остальной код
        bad = []
        if data.get("распознавание") is not None and not isinstance(data["распознавание"], dict):
            bad.append("распознавание")
        themes = data.get("темы")
        if themes is not None:
            if isinstance(themes, dict):
                data["темы"] = {
                    k: [str(x) for x in v]
                    for k, v in themes.items()
                    if isinstance(k, str) and isinstance(v, list)
                }
            else:
                bad.append("темы")
        for key in bad:
            data.pop(key)
        if bad:
            self._startup_warnings.append(
                "В файле настройки.json неверно заполнено: " + ", ".join(bad)
                + ". Эти значения взяты по умолчанию"
            )
        return data

    @staticmethod
    def _set_settings_aside() -> str:
        """Файл, который не удалось разобрать, уходит в сторону, а не ждёт первой записи
        настроек: она переписала бы его целиком, и всё, что человек там правил руками
        (микрофон, усиление, банк), пропало бы без следа. Возвращает фразу для
        предупреждения."""
        bad = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".bad")
        try:
            os.replace(SETTINGS_FILE, bad)
        except OSError:
            return ""
        return f". Старый файл сохранён как {bad.name}"

    def _startup_asr_config(self) -> dict:
        """Настройки распознавания из файла. Форма внутри раздела может быть любой
        ({"язык": ["ru"]}, {"провайдер": {"a": 1}}), и normalize_config на ней падает
        с TypeError: окно из-за этого не открывалось бы вовсе."""
        try:
            return self._asr_config(self.settings.get("распознавание"))
        except Exception:
            self.settings.pop("распознавание", None)
            self._startup_warnings.append(
                "В файле настройки.json неверно заполнено: распознавание. "
                "Эти значения взяты по умолчанию"
            )
            return self._asr_config(None)

    @staticmethod
    def _asr_config(raw, chosen: bool | None = None) -> dict:
        """normalize_config плюс правило про модель. Пока человек сам не выбрал модель,
        берётся автоматическая (_auto_model), и она может смениться: скачалась другая,
        поставили CUDA. Выбранная им остаётся навсегда. chosen None значит «посмотреть
        в raw, есть ли там своё поле «модель»»."""
        cfg = normalize_config(raw)
        if not (_model_chosen(raw) if chosen is None else chosen):
            cfg["модель"] = _auto_model()
        return cfg

    def _gain_setting(self, key: str, hi: float) -> float:
        """Усиление из настроек: null, строка и бесконечность не роняют старт."""
        value = self.settings.get(key, 1.0)
        try:
            gain = float(value)
        except (TypeError, ValueError, OverflowError):
            gain = math.nan
        if not math.isfinite(gain):
            self.settings.pop(key, None)
            self._startup_warnings.append(
                f"В файле настройки.json неверно заполнено «{key}», взято значение по умолчанию"
            )
            return 1.0
        return max(GAIN_MIN, min(hi, gain))

    def _save_settings(self) -> None:
        try:
            text = json.dumps(self.settings, ensure_ascii=False, indent=1)
            with _SETTINGS_LOCK:
                write_text_atomic(SETTINGS_FILE, text)
        except Exception as exc:
            # Не смогли запомнить, но ронять из-за этого запись незачем. Молчать
            # тоже не стоит: человек решит, что настройка сохранилась
            self._warn(f"Настройки не сохранились: {exc}")

    def set_mic_gain(self, value: float) -> dict:
        """Усиление микрофона. Применяется сразу, в том числе посреди записи."""
        try:
            gain = float(value)
        except (TypeError, ValueError, OverflowError):
            gain = math.nan
        if not math.isfinite(gain):
            return {"error": "Усиление должно быть числом"}
        self.mic_gain = max(GAIN_MIN, min(GAIN_MAX, gain))
        # Захват закрывают в другом потоке, между проверкой и вызовом он может
        # стать None: ссылку берём один раз
        capture = self.capture
        if capture is not None:
            capture.set_gain(ME, self.mic_gain)
        self.settings["усиление_микрофона"] = self.mic_gain
        self._save_settings()
        return {"ok": True, "gain": self.mic_gain}

    def set_them_gain(self, value: float) -> dict:
        """Усиление звука собеседника. Тихий созвон распознаётся с пропусками:
        VAD не признаёт речью слишком тихий голос, и реплика теряется целиком."""
        try:
            gain = float(value)
        except (TypeError, ValueError, OverflowError):
            gain = math.nan
        if not math.isfinite(gain):
            return {"error": "Усиление должно быть числом"}
        self.them_gain = max(GAIN_MIN, min(THEM_GAIN_MAX, gain))
        capture = self.capture
        if capture is not None:
            capture.set_gain(THEM, self.them_gain)
        self.settings["усиление_собеседника"] = self.them_gain
        self._save_settings()
        return {"ok": True, "gain": self.them_gain}

    def get_settings(self) -> dict:
        return {
            "gain": self.mic_gain,
            "gain_min": GAIN_MIN,
            "gain_max": GAIN_MAX,
            "them_gain": self.them_gain,
            "them_gain_max": THEM_GAIN_MAX,
            "asr_label": describe(self.asr_cfg),
            "asr": self._asr_state(),
            "remembered": self._remembered(),
            # Первый запуск: пока человек не выбрал, с чего начать, окно показывает выбор
            "first_run": self.settings.get(FIRST_RUN_KEY) is not True,
            "model": self._model_snapshot(),
            "gpu": self._gpu_info(),
            "patterns_note": self._patterns_note(),
        }

    def first_run_done(self) -> dict:
        """Человек выбрал, с чего начать: больше экран выбора не показываем."""
        if self.settings.get(FIRST_RUN_KEY) is not True:
            self.settings[FIRST_RUN_KEY] = True
            self._save_settings()
        return {"ok": True}

    def _patterns_note(self) -> str:
        """Что не прочиталось в словарях разбора речи (speech_patterns*.yaml), одной строкой.
        Разбор работает по тому, что прочиталось, но человек должен знать, что файл не весь."""
        problems = [" ".join(str(p).split()) for p in self.analyzer.problems if str(p).strip()]
        if not problems:
            return ""
        shown = problems[:3]
        if len(problems) > len(shown):
            shown.append(f"Ещё замечаний: {len(problems) - len(shown)}")
        return clean_text(". ".join(shown))

    def _asr_state(self) -> dict:
        """Готово ли распознавание на самом деле: для строки «Распознавание» под
        дорожками. Галочка там не должна стоять просто так.

        state: "ok" (можно писать), "wait" (локальная модель ещё не скачана, скачается
        при первой записи), "bad" (нет ключа или адреса сервера или загрузка сорвалась:
        запись не начнётся).
        """
        cfg = self.asr_cfg
        label = describe(cfg)
        ok = {"state": "ok", "label": label, "note": ""}
        if cfg["режим"] != "api":
            if model_ready(cfg["модель"]):
                return ok
            info = self._model_snapshot()
            if info["model"] == cfg["модель"]:
                if info["state"] == "loading":
                    return {"state": "wait", "label": label, "note": "готовлю распознавание"}
                if info["state"] == "error":
                    return {"state": "bad", "label": label,
                            "note": "не скачалось, нажми «Попробовать снова»"}
            return {"state": "wait", "label": label,
                    "note": "распознавание подготовится при первой записи"}
        pid = cfg["провайдер"]
        if pid == "custom":
            # Ключ у своего сервера бывает не нужен, а адрес нужен всегда
            if not cfg["адрес"]:
                return {"state": "bad", "label": label, "note": "не указан адрес сервера"}
            return ok
        if not api_keys.has(pid):
            return {"state": "bad", "label": label, "note": "нужен ключ, вставь его в настройках"}
        return ok

    def _url_warning(self, cfg: dict) -> str:
        """Предупреждение про адрес своего сервера в сохранённых настройках, если оно есть."""
        if cfg["режим"] == "api" and cfg["провайдер"] == "custom" and cfg["адрес"]:
            return check_server_url(cfg["адрес"]).get("warning", "")
        return ""

    def _glossary_note(self) -> str:
        """Причина, по которой глоссарий пропущен, одной строкой. Пусто, если всё прочиталось."""
        return " ".join(read_glossary(HERE / "glossary.txt", self.asr_cfg["язык"]).warnings)

    def _remembered(self) -> dict:
        """Запомненный выбор в именах окна. Файл правят руками, поэтому в окно уходит
        только строка разумной длины, а всё остальное считается незаданным."""
        out = {}
        for key, name in REMEMBER_KEYS.items():
            value = self.settings.get(name)
            out[key] = value if isinstance(value, str) and len(value) <= REMEMBER_MAX_LEN else ""
        return out

    def remember(self, key: str, value: str) -> dict:
        """Запоминает выбор человека (микрофон, устройство собеседника, банк
        тренажёра) в настройки.json. Принимаются только ключи из REMEMBER_KEYS и
        только строки: окно не вправе записать в файл что-то другое."""
        name = REMEMBER_KEYS.get(key) if isinstance(key, str) else None
        if name is None:
            return {"error": "Это значение не запоминается"}
        if not isinstance(value, str):
            return {"error": "Запомнить можно только строку"}
        value = clean_text(value).strip()
        if len(value) > REMEMBER_MAX_LEN:
            return {"error": "Значение слишком длинное"}
        if any(ord(ch) < 32 for ch in value):
            return {"error": "В значении лишние управляющие символы"}
        if value:
            if self.settings.get(name) == value:
                return {"ok": True}
            self.settings[name] = value
        elif name in self.settings:
            del self.settings[name]  # пустое значение это «забыть выбор»
        else:
            return {"ok": True}
        self._save_settings()
        return {"ok": True}

    # ---------------------------------------------------------- распознавание

    def asr_settings(self) -> dict:
        """Всё для экрана настроек: текущий выбор, варианты, есть ли ключи."""
        return {
            "config": self.asr_cfg,
            "providers": PROVIDERS,
            "local_models": LOCAL_MODELS,
            "languages": LANGUAGES,
            "cuda": cuda_available(),
            "gpu": self._gpu_info(),
            # Модель в списке выбрана человеком или подставлена программой: от этого
            # зависит, запомнится ли она (см. asr_save)
            "model_chosen": _model_chosen(self.settings.get("распознавание")),
            "keys": {pid: api_keys.has(pid) for pid in PROVIDERS},
            "label": describe(self.asr_cfg),
            "url_warning": self._url_warning(self.asr_cfg),
            "glossary_note": self._glossary_note(),
        }

    def asr_save(self, cfg: dict, key: str = "", model_chosen: bool = False) -> dict:
        # Модель уже в работе или грузится: менять распознавание посреди записи нельзя.
        # Окно в это время закрывает поля, этот отказ страхует от запоздавшего сохранения
        if self.recording.is_set() or self.starting.is_set() or self.stopping.is_set():
            return {"error": "Сначала останови запись"}
        key = key if isinstance(key, str) else ""
        # Модель попадает в настройки.json только если человек сам выбрал её в списке (окно
        # присылает model_chosen) или выбрал раньше. Иначе в файле застряла бы та, что
        # программа подставила при первом запуске (small), и после установки CUDA
        # перезапуск не сменил бы её на turbo
        chosen = model_chosen is True or _model_chosen(self.settings.get("распознавание"))
        try:
            clean = self._asr_config(cfg, chosen)
        except Exception:
            return {"error": "Настройки распознавания заполнены неверно"}
        url_warning = ""
        if clean["режим"] == "api" and clean["провайдер"] == "custom":
            checked = check_server_url(clean["адрес"])
            if checked.get("error"):
                return {"error": checked["error"]}
            clean["адрес"] = checked["url"]  # без лишнего «/» в конце
            url_warning = checked["warning"]
        if key.strip():
            try:
                api_keys.put(clean["провайдер"], key)
            except api_keys.KeyStoreError as exc:
                # Текст уже говорит, что случилось и что делать, и ключа в нём нет.
                # Приставка «Ключ не сохранился:» вышла бы дублем
                return {"error": sentence(str(exc))}
            except Exception as exc:
                return {"error": f"Ключ не сохранился: {mask_secrets(str(exc), key)}"}
        self.asr_cfg = clean
        saved = dict(clean)
        if not chosen:
            saved.pop("модель")
        self.settings["распознавание"] = saved
        self._save_settings()
        # Ключ мог смениться при тех же настройках, поэтому собираем заново всегда
        self.transcriber_cfg = None
        self._model_after_settings()
        return {
            "ok": True,
            "label": describe(clean),
            "keys": {pid: api_keys.has(pid) for pid in PROVIDERS},
            "asr": self._asr_state(),
            "url_warning": url_warning,
        }

    def asr_test(self, cfg: dict, key: str = "") -> dict:
        """Пробный запрос с тем, что сейчас введено в окне, ещё до сохранения."""
        key = key if isinstance(key, str) else ""
        try:
            clean = normalize_config(cfg)
        except Exception:
            return {"error": "Настройки распознавания заполнены неверно"}
        if clean["режим"] != "api":
            return {"error": "Проверка нужна только для распознавания через интернет"}
        warning = ""
        if clean["провайдер"] == "custom":
            checked = check_server_url(clean["адрес"])
            if checked.get("error"):
                return {"error": checked["error"]}
            clean["адрес"] = checked["url"]
            warning = checked["warning"]
        try:
            recognizer = make_recognizer(clean, None, key)
        except RecognizerError as exc:
            return {"error": str(exc), "warning": warning}
        try:
            seconds = probe(recognizer)
        except RecognizerError as exc:
            return {"error": str(exc), "warning": warning}
        except Exception as exc:
            return {"error": f"Сбой: {mask_secrets(str(exc), key)}", "warning": warning}
        finally:
            recognizer.close()
        return {"ok": True, "ms": round(seconds * 1000), "warning": warning}

    def asr_forget_key(self, provider: str) -> dict:
        if not isinstance(provider, str) or provider not in PROVIDERS:
            return {"error": "Нет такого сервиса"}
        try:
            api_keys.forget(provider)
        except api_keys.KeyStoreError as exc:
            return {"error": sentence(str(exc))}
        except Exception as exc:
            return {"error": f"Ключ не удалился: {exc}"}
        self.transcriber_cfg = None
        return {
            "ok": True,
            "keys": {pid: api_keys.has(pid) for pid in PROVIDERS},
            "asr": self._asr_state(),
        }

    # ------------------------------------------------------ локальная модель
    #
    # Модель весит от 0.5 до 3 ГБ. Её качает отдельный поток, а не поток устройств:
    # тому в это время нужно отвечать саундчеку и проверке микрофона. Ход идёт в окно
    # через очередь ready (событие "model"), окно рисует его в строке состояния того
    # раздела, где человек сейчас. Скачивание одно за раз (_dl_lock): запись, тренировка
    # и фоновая подготовка не качают одно и то же втроём.

    def _model_snapshot(self) -> dict:
        with self._model_lock:
            return dict(self._model_info)

    def _model_publish(self, **fields) -> None:
        """Обновляет сведения о загрузке и шлёт их окну, если что-то изменилось."""
        with self._model_lock:
            before = dict(self._model_info)
            self._model_info.update(fields)
            after = dict(self._model_info)
        if after != before:
            self.ready.put(("model", after))

    def _model_progress(self, done: int, total: int, source: str) -> None:
        """Ход загрузки из asr.ensure_model: не чаще четырёх раз в секунду."""
        self._model_publish(state="loading", done=int(done), total=int(total),
                            source=source or "huggingface", error="")

    def _ensure_local(self, size: str) -> None:
        """Файлы локальной модели на диске. Нет их, качает с ходом в окно.

        Второй вызов во время загрузки ждёт первого и находит модель готовой. Сбой
        пробрасывается ModelDownloadError с текстом для человека; окно к этому
        времени уже получило тот же текст и кнопку «Попробовать снова».
        """
        with self._dl_lock:
            if model_ready(size):
                self._model_publish(state="ready", model=size, error="")
                return
            self._model_cancel = cancel = threading.Event()
            # Окно закрывают (или программа уже выходит): вторую загрузку не начинаем.
            # Без этого запись, ждавшая чужую загрузку, после её отмены качала бы всё
            # заново, пока окно ждёт. Флаг ставится раньше, чем снимается cancel выхода
            # (см. shutdown), поэтому проверка идёт после создания cancel, а не до
            if self.closing.is_set() or self.exiting.is_set():
                raise DownloadCancelled("Загрузка модели остановлена.")
            self._model_publish(state="loading", model=size, done=0, total=0,
                                source="huggingface", error="")
            try:
                ensure_model(size, self._model_progress, cancel=cancel)
            except DownloadCancelled:
                self._model_publish(state="idle", error="")
                raise
            except Exception as exc:
                if not isinstance(exc, ModelDownloadError):
                    exc = ModelDownloadError(f"Распознавание не скачалось ({size}): {exc}")
                text = " ".join(mask_secrets(clean_text(str(exc))).split())
                self._model_publish(state="error", error=text)
                raise ModelDownloadError(text) from None
            self._model_publish(state="ready", error="")

    def _model_start_job(self, size: str) -> None:
        """Запускает загрузку в своём потоке, если она ещё не идёт."""
        with self._model_lock:
            if self._model_thread is not None and self._model_thread.is_alive():
                return
            self._model_thread = threading.Thread(
                target=self._model_job, args=(size,), daemon=True, name="model"
            )
            self._model_thread.start()

    def _model_job(self, size: str) -> None:
        try:
            self._ensure_local(size)
        except Exception:
            pass  # причина уже в окне: _ensure_local сообщила её сама

    def prepare_model(self) -> None:
        """Окно открылось: модель готовим заранее, пока человек выбирает, с чего начать.
        Устройства не трогаем. Модели нет на диске, качаем её в фоне, ход виден в окне."""
        cfg = self.asr_cfg
        if cfg["режим"] == "api":
            return
        size = cfg["модель"]
        if model_ready(size):
            self._model_publish(state="ready", model=size, error="")
            return
        self._model_start_job(size)

    def model_retry(self) -> dict:
        """Кнопка «Попробовать снова» после ошибки загрузки."""
        cfg = self.asr_cfg
        if cfg["режим"] != "api":
            self._model_start_job(cfg["модель"])
        return {"ok": True}

    def _model_after_settings(self) -> None:
        """Настройки распознавания сохранены. Скачивание не выбранной больше модели
        прекращаем, ошибка о ней забывается, а готовая выбранная снимает любую тревогу."""
        cfg = self.asr_cfg
        want = cfg["модель"] if cfg["режим"] != "api" else None
        info = self._model_snapshot()
        if info["state"] == "loading" and info["model"] != want:
            self._model_cancel.set()
        elif info["state"] == "error" and info["model"] != want:
            self._model_publish(state="idle", error="")
        if want is not None and model_ready(want):
            self._model_publish(state="ready", model=want, error="")

    def _queue_when_model_ready(self, cmd: tuple) -> None:
        """Кладёт команду потоку устройств, когда локальная модель уже на диске.

        Модели нет: ждёт её в своём потоке, а не в потоке устройств, иначе тот на
        минуты занят скачиванием и не отвечает саундчеку. Режим «через интернет»
        и готовая модель проходят сразу, как было.
        """
        cfg = self.asr_cfg
        if cfg["режим"] == "api" or model_ready(cfg["модель"]):
            self.commands.put(cmd)
            return
        threading.Thread(target=self._wait_model_then, args=(cmd, cfg["модель"]),
                         daemon=True, name="model-wait").start()

    def _wait_model_then(self, cmd: tuple, size: str) -> None:
        try:
            self._ensure_local(size)
        except DownloadCancelled:
            if cmd[0] == "start":
                # Отмена из on_closing: окно закрывают, говорить уже не с кем
                self._start_failed(None if self.closing.is_set() else "Загрузка модели отменена")
            return
        except Exception as exc:
            if cmd[0] == "start":
                self._start_failed(self._not_started(exc))
            return
        self.commands.put(cmd)

    @staticmethod
    def _not_started(exc: Exception) -> str:
        """Почему не запустилось распознавание. Ошибка загрузки модели уже написана для
        человека целиком (что случилось и что делать), приставка к ней только удлинила бы
        строку, которую окно режет на четыре строки."""
        if isinstance(exc, ModelDownloadError):
            return str(exc)
        return f"Распознавание не запустилось: {exc}"

    def _start_failed(self, text: str | None) -> None:
        """Старт записи не дошёл до потока устройств. Снимаем «запускаюсь» так же, как это
        делает поток устройств в конце команды, и закрываем окно, если его просили."""
        try:
            if text:
                self._call("error", text)
        finally:
            self.starting.clear()
            if self.closing.is_set():
                self._finish_closing()

    # -------------------------------------------------- видеокарта: библиотеки CUDA
    #
    # Установщик CUDA не качает (это 2 ГБ, а нужна она не всем). Если в компьютере есть
    # видеокарта NVIDIA, а библиотек нет, окно предлагает «Ускорить на видеокарте». Кнопка
    # ставит пакеты из requirements-gpu.txt тем же uv, что и остальное, в окружение
    # программы. Потом нужен перезапуск: DLL подхватываются при старте (bootstrap).

    def _gpu_info(self) -> dict:
        """Что окну знать про ускорение: state это none (предлагать нечего), offer
        (карта есть, библиотек нет), installing, restart (поставили, нужен перезапуск),
        error. since это когда началась установка."""
        with self._gpu_lock:
            run = dict(self._gpu)
        present = gpu_present()
        if cuda_available() or not present:
            state = "none"
        elif run["state"] in ("installing", "restart", "error"):
            state = run["state"]
        elif _cuda_libs_on_disk() and not _cuda_libs_found():
            state = "restart"  # поставили в прошлый раз и закрыли окно, не перезапустив
        else:
            state = "offer"
        mine = state == run["state"]
        return {
            "present": present,
            "state": state,
            "text": run["text"] if mine else "",
            # Сырая строка установщика: в окно она идёт только мелким шрифтом в настройках
            # и по наведению, красным пишется человеческая фраза из text
            "detail": run["detail"] if mine else "",
            "since": run["since"],
            "size": GPU_SIZE_TEXT,
            "after_model": self._model_after_gpu() if state != "none" else "",
            "uv": self._gpu_argv is not None,
            "manual": gpu_manual_command(),
        }

    def _model_after_gpu(self) -> str:
        """Размер модели, которая докачается после установки CUDA и перезапуска, или пусто.

        Повторяет выбор _auto_model для видеокарты: первая готовая модель из списка для
        карты, а нет ни одной или это small (она лежит с первого запуска, до CUDA), turbo.
        Если человек выбрал модель сам, она остаётся его, и её загрузка от видеокарты
        не зависит.
        """
        if self.asr_cfg["режим"] == "api" or _model_chosen(self.settings.get("распознавание")):
            return ""
        first = next((size for size in _GPU_ORDER if model_ready(size)), None)
        if first not in (None, "small"):
            return ""  # medium, large-v3 или turbo уже на диске, ничего не качается
        return next((m["size"] for m in LOCAL_MODELS if m["id"] == _GPU_ORDER[0]), "")

    def _gpu_publish(self, state: str, text: str = "", detail: str = "") -> None:
        with self._gpu_lock:
            if state == "installing" and self._gpu["state"] != "installing":
                self._gpu["since"] = time.time()
            if (self._gpu["state"], self._gpu["text"], self._gpu["detail"]) == (state, text, detail):
                return
            self._gpu["state"] = state
            self._gpu["text"] = text
            self._gpu["detail"] = detail
        self.ready.put(("gpu", self._gpu_info()))

    def _busy_for_install(self) -> str | None:
        if self.recording.is_set() or self.starting.is_set() or self.stopping.is_set():
            return "Сначала останови запись"
        return None

    def gpu_install(self) -> dict:
        """Кнопка «Ускорить на видеокарте». Ставит библиотеки CUDA в окружение программы."""
        why = self._busy_for_install()
        if why:
            return {"error": why}
        info = self._gpu_info()
        if info["state"] == "installing":
            return {"ok": True}
        if info["state"] not in ("offer", "error"):
            return {"error": "Ускорять нечего: видеокарты NVIDIA нет или библиотеки уже стоят"}
        if self._gpu_argv is None:
            self._gpu_publish("error", self._gpu_missing)
            return {"ok": True}
        with self._gpu_lock:
            if self._gpu["state"] == "installing" or (
                    self._gpu_thread is not None and self._gpu_thread.is_alive()):
                return {"ok": True}
            self._gpu_thread = threading.Thread(target=self._gpu_job, daemon=True, name="gpu")
            self._gpu["state"] = "installing"
            self._gpu["text"] = "Проверяю связь с интернетом"
            self._gpu["detail"] = ""
            self._gpu["since"] = time.time()
            self._gpu_close_warned = False
        # Первое событие уходит раньше запуска потока: его собственные события не должны
        # обогнать это и затем затереться устаревшим текстом
        self.ready.put(("gpu", self._gpu_info()))
        self._gpu_thread.start()
        return {"ok": True}

    @staticmethod
    def _uv_stage(line: str) -> tuple[str, str] | None:
        """Что написать по строке вывода uv: («Качаю…», пакет) или («Ставлю…», ""). Имя
        пакета человеку ничего не говорит, оно идёт только в подсказку по наведению.
        None, если строка ничего не меняет."""
        low = line.lower()
        if low.startswith(("prepared", "installed", "installing", "uninstalled", "+ ")):
            return "Ставлю библиотеки для видеокарты", ""
        m = re.match(r"(?:downloading|downloaded)\s+([\w.\-]+)", low)
        if m:
            return "Качаю библиотеки для видеокарты", m.group(1)
        if low.startswith("resolved"):
            return "Качаю библиотеки для видеокарты", ""
        return None

    def _gpu_job(self) -> None:
        """Поток установки: запускает uv и пересказывает его вывод окну.

        Человеку идёт одна понятная фраза (что случилось и что делать), а строка самого
        uv, обычно английская, уходит рядом как detail: в подсказку и в настройки."""
        argv = self._gpu_argv
        tail: list[str] = []
        try:
            if not bootstrap.pypi_reachable():
                raise _GpuError(
                    "Нет связи с сервером библиотек. Проверь интернет или VPN и нажми "
                    "кнопку ещё раз", "pypi.org не отвечает")
            self._gpu_publish("installing", "Качаю библиотеки для видеокарты")
            env = {**os.environ, **GPU_ENV_EXTRA}
            proc = subprocess.Popen(
                argv, cwd=str(HERE), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._gpu_lock:
                self._gpu_proc = proc  # shutdown убирает его, если окно закрыли посреди установки
            # Страховка от вечной установки: через GPU_INSTALL_MAX_SEC процесс убирается,
            # чтение вывода тогда кончается само
            guard = threading.Timer(GPU_INSTALL_MAX_SEC, proc.kill)
            guard.daemon = True
            guard.start()
            try:
                for raw in proc.stdout:
                    line = " ".join(raw.split())
                    if not line:
                        continue
                    tail = (tail + [line])[-6:]
                    stage = self._uv_stage(line)
                    if stage:
                        self._gpu_publish("installing", *stage)
                code = proc.wait()
            finally:
                guard.cancel()
                if proc.poll() is None:
                    proc.kill()
            if code != 0:
                raise _GpuError(
                    "Не получилось поставить библиотеки для видеокарты. Проверь интернет "
                    "и нажми кнопку ещё раз",
                    tail[-1] if tail else f"установщик закончил с кодом {code}")
            if not _cuda_libs_on_disk():
                raise _GpuError(
                    "Установка закончилась, но библиотек для видеокарты на месте нет. "
                    "Нажми кнопку ещё раз", "uv закончил без ошибки, файлов CUDA нет")
        except _GpuError as exc:
            self._gpu_publish("error", exc.human, _one_line(exc.detail))
            return
        except Exception as exc:
            self._gpu_publish("error", "Не получилось запустить установку. Нажми кнопку ещё раз",
                              _one_line(str(exc)))
            return
        finally:
            with self._gpu_lock:
                self._gpu_proc = None
            if GPU_DROP_CACHE:
                shutil.rmtree(GPU_CACHE_DIR, ignore_errors=True)
        self._gpu_publish("restart", "Готово")

    def restart_app(self) -> dict:
        """Кнопка «Перезапустить программу» после установки CUDA: запускает ту же программу
        заново и закрывает это окно. Новый процесс подхватит библиотеки при старте."""
        if (self.rec_kind is not None or self.recording.is_set()
                or self.starting.is_set() or self.stopping.is_set()):
            return {"error": "Сначала останови запись"}
        # Переменную «меня уже лечили» новому процессу не передаём: он стартует с чистого листа
        env = {k: v for k, v in os.environ.items() if k != bootstrap.HEALED_ENV}
        try:
            subprocess.Popen(
                restart_argv(), cwd=str(HERE), env=env, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return {"error": clean_text(f"Не получилось перезапустить: {exc}. "
                                        "Закрой окно и открой его заново")}
        threading.Thread(target=self._exit_for_restart, daemon=True, name="restart").start()
        return {"ok": True}

    def _exit_for_restart(self) -> None:
        # Ответ на вызов из окна должен успеть уйти, потом окно закрывается
        time.sleep(0.4)
        self._save_unfinished_session()
        self._release_devices_for_exit()
        self._close_window()

    def open_banks_dir(self) -> dict:
        """Папка, куда кладут свои банки вопросов. Там же лежит пример."""
        BANKS_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(BANKS_DIR)
        return {"ok": True}

    def open_url(self, url: str) -> dict:
        """Ссылки открываются в обычном браузере, а не в окне программы."""
        if not url.startswith(("https://", "http://")):
            return {"error": "Это не ссылка"}
        webbrowser.open(url)
        return {"ok": True}

    # ------------------------------------------------------------ мост в окно

    def _js(self, code: str) -> None:
        if self.window is None:
            return
        try:
            # Одиночный суррогат в тексте не переваривает уже .NET на той стороне
            self.window.evaluate_js(clean_text(code))
        except Exception:
            pass  # окно закрывают, доставлять уже некому

    def _call(self, fn: str, *args) -> None:
        # default=str: в данных из YAML бывают даты и прочее, что json не знает
        payload = ", ".join(json.dumps(a, ensure_ascii=False, default=str) for a in args)
        self._js(f"window.ui.{fn}({payload})")

    def _warn(self, text: str) -> None:
        """Предупреждение в окно, не чаще раза в ERROR_REPEAT_SEC для одного текста:
        легла сеть, и каждая реплика падает с той же ошибкой, а сыпать одинаковыми
        сообщениями каждые две секунды незачем."""
        text = clean_text(text)
        last_text, last_at = self.last_error
        now = time.time()
        if text != last_text or now - last_at > ERROR_REPEAT_SEC:
            self.ready.put(("warn", text))
            self.last_error = (text, now)

    def _status(self, text: str, kind: str = "idle", sub: str = "", queued: bool = False) -> None:
        """Состояние записи для окна.

        kind красит точку и рамку: idle, loading, ready, live, stopping, done, error.
        sub это строка объяснения под ним. Во время записи окно пишет её само,
        подставляя время и число реплик. queued: через очередь реплик, по порядку с ними.
        """
        if queued:
            self.ready.put(("call", ("setStatus", (text, kind, sub))))
        else:
            self._call("setStatus", text, kind, sub)

    # -------------------------------------------------------- вызовы из окна

    @staticmethod
    def _index(value) -> int:
        """Номер устройства из окна. Всё непонятное значит «устройства нет», это -1.
        Ноль это настоящий номер («Microsoft Sound Mapper»), поэтому пустой список
        нельзя превращать в 0, как делал Number('')."""
        if isinstance(value, bool):
            return -1
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            value = int(value)
        if isinstance(value, int) and value >= 0:
            return value
        return -1

    @staticmethod
    def _name(value) -> str:
        return clean_text(value) if isinstance(value, str) else ""

    def _holders(self) -> set[str]:
        """Что держит устройства прямо сейчас: единый признак занятости.

        Одновременно ими пользуется одна вещь: запись встречи ("meeting"), ответ
        в тренажёре ("answer"), проверка микрофона ("check") или саундчек
        ("monitor"). "start" это команда старта, которая уже принята, но ещё не
        открыла устройства. Состояния ставятся и снимаются в потоке устройств,
        кроме "start": его ставит поток окна под _claim_lock. Решение «можно ли
        открывать» окончательное только в потоке устройств, проверки в потоках
        окна дают человеку быстрый отказ, не больше.
        """
        held = set()
        if self.rec_kind is not None:
            held.add(self.rec_kind)
        if self.starting.is_set():
            held.add("start")
        if self.checking.is_set():
            held.add("check")
        if self.monitoring.is_set():
            held.add("monitor")
        return held

    def _busy_reason(self, task: str, ignore: tuple = ()) -> str | None:
        """Почему устройства нельзя занять под task, или None, если можно."""
        held = self._holders()
        for who in _BLOCKED_BY[task]:
            if who in held and who not in ignore:
                if who == "meeting" and self.stopping.is_set():
                    return "Запись ещё останавливается, подожди"
                return _BUSY_TEXT[who]
        return None

    def _wait(self, reply: _Reply, timeout: float, on_timeout: str,
              cleanup: str | None = None) -> dict:
        """Ждёт ответ потока устройств. Вышел срок: команду помечаем как ненужную,
        а если она могла успеть открыть устройства, следом кладём команду-уборщика
        (она встанет в очередь после неё)."""
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            reply.cancelled.set()
            if cleanup == "monitor_stop":
                self._queue_monitor_stop()
            elif cleanup:
                self.commands.put((cleanup, None, _Reply()))
            return {"error": on_timeout}

    def get_devices(self) -> dict:
        """Список устройств. Читает поток устройств, а не поток вызова из окна:
        PortAudio не переносит обращений из разных потоков, а параллельный
        PyAudio() рядом с закрытием захвата роняет процесс без traceback."""
        reply = _Reply()
        self.commands.put(("devices", None, reply))
        try:
            return reply.get(timeout=DEVICES_WAIT_SEC)
        except queue.Empty:
            reply.cancelled.set()
            return {"mics": [], "loopbacks": [],
                    "error": "Список устройств не прочитался: звук занят другим делом. "
                             "Попробуй ещё раз через минуту"}

    @staticmethod
    def _resolve(index: int, name: str, kind: str, devices: dict | None = None) -> int:
        """Находит устройство заново, по имени. Зовётся только из потока устройств.

        Номера устройств в Windows плавают: стоит появиться или исчезнуть
        виртуальному входу (Steam поднимает свой вместе с собой), и весь список
        сдвигается. Номер, выбранный при открытии окна, к моменту записи может
        указывать уже на соседнее устройство.

        Имя задано, а такого устройства нет: ошибка, а не старый номер, который
        теперь означает другое устройство. Устройство должно быть нужного типа
        (вход или loopback): микрофон не годится в «Собеседника». Без имени
        годится номер, но только если он есть в списке нужного типа. index < 0
        значит «устройства не выбрано» и возвращается как есть.
        """
        if index < 0:
            return -1
        if devices is None:
            try:
                devices = list_devices()
            except Exception as exc:
                raise DeviceNotFound(f"список устройств не прочитался: {exc}") from exc
        items = devices["mics" if kind == "mic" else "loopbacks"]
        if name:
            same = [item for item in items if item["name"] == name]
            # Два одинаковых устройства: берём то, чей номер выбран, иначе первое
            for item in same:
                if item["index"] == index:
                    return item["index"]
            if same:
                return same[0]["index"]
            raise DeviceNotFound(f"устройство „{name}“ не найдено, выбери другое")
        if any(item["index"] == index for item in items):
            return index
        raise DeviceNotFound(f"устройство №{index} не найдено, выбери другое")

    def _pick_devices(self, mic: tuple, spk: tuple) -> list[tuple[int, str, float]]:
        """Выбранные окном устройства, найденные заново: [(номер, кто, усиление)].
        Пусто, если не выбрано ничего. Нет выбранного устройства: DeviceNotFound."""
        if mic[0] < 0 and spk[0] < 0:
            return []
        try:
            found = list_devices()
        except Exception as exc:
            raise DeviceNotFound(f"список устройств не прочитался: {exc}") from exc
        devices = []
        if mic[0] >= 0:
            devices.append((self._resolve(mic[0], mic[1], "mic", found), ME, self.mic_gain))
        if spk[0] >= 0:
            devices.append((self._resolve(spk[0], spk[1], "loopback", found), THEM, self.them_gain))
        return devices

    def start(self, mic_index: int, spk_index: int,
              mic_name: str = "", spk_name: str = "") -> dict:
        mic = (self._index(mic_index), self._name(mic_name))
        spk = (self._index(spk_index), self._name(spk_name))
        if mic[0] < 0 and spk[0] < 0:
            return {"error": "Не выбрано ни одного устройства: подключи микрофон или наушники"}
        if self.closing.is_set():
            return {"error": "Окно закрывается, запись не начата"}
        # Проверка и «старт принят» одним шагом: два быстрых вызова подряд не пройдут оба
        with self._claim_lock:
            why = self._busy_reason("meeting")
            if why:
                return {"error": why}
            self.starting.set()
        # Номера превращаются в устройства в потоке устройств, там же, где они
        # открываются: список читается заново, без параллельного PyAudio. Модель
        # ещё качается: команда ждёт её не в потоке устройств, а в своём потоке
        self._queue_when_model_ready(("start", mic, spk))
        return {"ok": True}

    def stop(self) -> dict:
        if self.recording.is_set():
            self.commands.put(("stop", None, None))
        return {"ok": True}

    def monitor_start(self, mic_index: int, spk_index: int,
                      mic_name: str = "", spk_name: str = "") -> dict:
        """Саундчек: открывает оба источника, чтобы до записи было видно, что их слышно.

        Звук никуда не пишется и не распознаётся. Вызывается заново при смене
        устройства, старые источники при этом закрываются.
        """
        mic = (self._index(mic_index), self._name(mic_name))
        spk = (self._index(spk_index), self._name(spk_name))
        if mic[0] < 0 and spk[0] < 0:
            return {"error": "Нет ни микрофона, ни наушников"}
        why = self._busy_reason("monitor")
        if why:
            return {"error": why}
        # Каждый вызов саундчека (старт и стоп) получает свой номер. Команда, чей
        # номер к моменту её очереди уже не последний, устарела и ничего не откроет
        with self._monitor_lock:
            self._monitor_gen += 1
            gen = self._monitor_gen
        reply = _Reply()
        self.commands.put(("monitor", (mic, spk, gen), reply))
        return self._wait(reply, MONITOR_WAIT_SEC, "устройства не отозвались",
                          cleanup="monitor_stop")

    def _queue_monitor_stop(self, reply: "_Reply | None" = None) -> None:
        """Кладёт в очередь закрытие саундчека и делает все прежние старты устаревшими."""
        with self._monitor_lock:
            self._monitor_gen += 1
            self.commands.put(("monitor_stop", None, reply if reply is not None else _Reply()))

    def monitor_stop(self) -> dict:
        # Всегда в очередь, даже если саундчек сейчас не отмечен открытым: его старт
        # мог не дойти до своей очереди, и без стопа он открыл бы устройства позже
        reply = _Reply()
        self._queue_monitor_stop(reply)
        try:
            return reply.get(timeout=MONITOR_WAIT_SEC)
        except queue.Empty:
            return {"error": "не удалось закрыть устройства"}

    def open_dir(self) -> dict:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(self.out_path.parent if self.out_path else OUT_DIR)
        return {"ok": True}

    def archive_open(self, path: str) -> dict:
        """Открывает файл в том, чем в системе открываются markdown.

        Только .md из папок архива: путь приходит из окна, и без проверки
        os.startfile запустил бы по нему что угодно, хоть .exe.
        """
        try:
            p = Path(path).resolve()
        except (OSError, TypeError, ValueError):
            return {"error": "Открыть можно только расшифровку .md из архива"}
        if p.suffix.lower() != ".md" or not archive._inside_archive(p):
            return {"error": "Открыть можно только расшифровку .md из архива"}
        if not p.exists():
            return {"error": "файла больше нет"}
        try:
            os.startfile(p)
        except OSError as exc:
            return {"error": str(exc)}
        return {"ok": True}

    def archive_digest(self, path: str) -> dict:
        """Считает выжимку и дописывает её в конец файла, если её там ещё нет.

        Выжимка возвращается всегда. written в ответе говорит, дописана ли она в файл
        именно сейчас, а note объясняет, почему нет: окно не должно писать «дописана»,
        когда это неправда.
        """
        data = archive.read(path)
        if data.get("error"):
            return data

        text = data["text"]
        result = archive.digest(text)
        md = archive.digest_markdown(data["name"], result)
        out = {"ok": True, "digest": result, "markdown": md, "written": False}

        if "## Выжимка" in text:
            out["note"] = "Выжимка готова. В файле она уже есть, второй раз не дописываю"
        elif data.get("damaged"):
            # Текст прочитан с заменой нечитаемых байтов на «�». Записать его назад значит
            # сделать эту замену навсегда, поэтому файл не трогаем
            out["note"] = ("Выжимка готова, но в файл не записана: часть знаков в нём не читается, "
                           "и перезапись испортила бы их окончательно")
        else:
            try:
                # archive.read сохраняет CRLF из файла, а запись в текстовом режиме
                # превратила бы каждый \n ещё раз в \r\n: вышло бы \r\r\n
                text = text.replace("\r\n", "\n")
                write_text_atomic(Path(data["path"]), text.rstrip() + "\n\n---\n\n" + md)
                out["written"] = True
            except OSError as exc:
                out["note"] = clean_text(f"Выжимка готова, но в файл не записана: {exc}")
        return out

    # --------------------------------------------------------- поток устройств

    def _audio_loop(self) -> None:
        while True:
            cmd, a, b = self.commands.get()
            try:
                if cmd in _OPENING and isinstance(b, _Reply) and b.cancelled.is_set():
                    # На ответ уже не ждут (вышел срок), человек получил ошибку.
                    # Открывать устройства по такой команде нельзя
                    b.put({"error": "устройства уже не нужны"})
                    continue
                if cmd == "start":
                    self._do_start(a, b)
                elif cmd == "stop":
                    self._do_stop()
                elif cmd == "load_model":
                    self._load_model()
                elif cmd == "devices":
                    b.put(self._do_devices())
                elif cmd == "answer_start":
                    b.put(self._do_answer_start(a))
                elif cmd == "answer_stop":
                    b.put(self._do_answer_stop(b.cancelled))
                elif cmd == "answer_abort":
                    self._do_answer_abort()
                elif cmd == "mic_check":
                    b.put(self._do_mic_check(a))
                elif cmd == "mic_check_stop":
                    b.put(self._do_mic_check_stop())
                elif cmd == "monitor":
                    b.put(self._do_monitor(*a))
                elif cmd == "monitor_stop":
                    b.put(self._end_monitor())
                elif cmd == "release":
                    b.put(self._release_devices())
            except Exception:
                last = traceback.format_exc(limit=2).strip().split("\n")[-1]
                # Ответ ждут на той стороне: без него интерфейс висит до таймаута
                if isinstance(b, queue.Queue):
                    b.put({"error": last})
                else:
                    self._call("error", last)
            finally:
                if cmd == "start":
                    # Только после того, как старт целиком отработал (в том числе
                    # recording.set()): закрытие окна смотрит на оба признака
                    self.starting.clear()
                # Окно просили закрыть, пока шёл старт или остановка, и мы отказали
                # (см. on_closing). Теперь команда кончилась, ждать больше нечего
                if cmd in ("start", "stop") and self.closing.is_set():
                    self._finish_closing()

    def _load_model(self) -> None:
        """Держим распознаватель между записями: локальная модель грузится секунд пять."""
        cfg = dict(self.asr_cfg)
        if self.transcriber is not None and self.transcriber_cfg == cfg:
            return
        if self.transcriber is not None:
            self.transcriber.close()
            self.transcriber = None

        local = cfg["режим"] != "api"
        if local:
            self._status("Готовлю распознавание", "loading", describe(cfg))
            # Обычно модель скачана заранее (prepare_model, _queue_when_model_ready), и
            # это мгновенно. Иначе (удалили, подсунули другую) качаем здесь: ход виден в
            # окне так же, как при заблаговременной загрузке
            self._ensure_local(cfg["модель"])
        else:
            self._status("Подключаю распознавание", "loading", describe(cfg))
        glossary = read_glossary(HERE / "glossary.txt", cfg["язык"])
        if glossary.warnings:
            # Личный глоссарий не прочитался (не та кодировка, нет доступа): без него
            # имена и термины распознаются хуже. Причина видна и в настройках
            self._warn(" ".join(glossary.warnings))
        self.transcriber = make_recognizer(
            cfg, glossary.prompt, on_progress=self._model_progress if local else None
        )
        self.transcriber_cfg = cfg
        if local:
            self._model_publish(state="ready", error="")  # снимает ход, если он был
        self._status("Распознавание готово", "ready", "можно начинать")

    def _open_capture(self, devices: list[tuple[int, str, float]],
                      out_queue, session_start: float) -> Capture:
        """Открывает и запускает источники: devices это [(номер, кто, усиление)].

        Любой сбой на полпути закрывает всё, что успело открыться, и летит
        дальше. Без этого первый поток и PyAudio остаются висеть, а микрофон
        занят до перезапуска программы.
        """
        capture = None
        try:
            capture = Capture()
            for index, speaker, gain in devices:
                capture.add(index, speaker, out_queue, session_start,
                            on_level=self._on_level, on_error=self._on_source_error)
                capture.set_gain(speaker, gain)
            capture.start()
            return capture
        except Exception:
            if capture is not None:
                capture.close()
            raise

    def _on_source_error(self, speaker: str, exc: Exception) -> None:
        """Источник звука отвалился посреди работы: выдернули наушники, устройство
        пропало. Зовётся из потока источника, поэтому только кладёт в очередь."""
        self.levels[speaker] = 0.0  # иначе дорожка так и светит последним уровнем
        what = "микрофон" if speaker == ME else "звук из наушников"
        self.ready.put(("source_lost", {
            "speaker": speaker,
            "text": clean_text(f"Источник «{speaker}» ({what}) отвалился: {exc}"),
        }))

    def _closing_abort(self) -> bool:
        """Окно просили закрыть, пока шёл старт: записи не будет. True, если так."""
        if not self.closing.is_set():
            return False
        self._call("error", "Окно закрывается, запись не начата")
        return True

    def _do_start(self, mic: tuple, spk: tuple) -> None:
        if self._closing_abort():
            return
        # Вторая команда «старт» подряд, запись ответа в тренажёре, проверка микрофона:
        # второй раз открывать устройства нельзя, первый захват потерялся бы. Саундчек
        # не мешает, его мы закроем сами. Свой же признак "start" не в счёт
        why = self._busy_reason("meeting", ignore=("start",))
        if why:
            self._call("error", why)
            return
        try:
            self._load_model()
        except Exception as exc:
            self._call("error", self._not_started(exc))
            return
        # Модель могла грузиться минутами, и крестик нажали за это время
        if self._closing_abort():
            return

        # Саундчек закрываем до чтения списка: PortAudio держит список, прочитанный
        # при первой инициализации, пока жив хоть один PyAudio. Читать надо свежий,
        # тот же, с которым откроются устройства
        self._end_monitor()
        try:
            devices = self._pick_devices(mic, spk)
        except DeviceNotFound as exc:
            self._call("error", str(exc))
            return
        if not devices:
            self._call("error", "Не выбрано ни одного источника звука")
            return

        # Сначала устройства, потом файл: если они не откроются, на диске не
        # должно остаться пустой расшифровки (а прошлая не должна быть затёрта).
        # Очередь реплик новая: «раз два три» из проверки микрофона в запись не попадёт
        self.segments = queue.Queue()
        session_start = time.time()
        try:
            capture = self._open_capture(devices, self.segments, session_start)
        except Exception as exc:
            self._call("error", f"Устройство не открылось: {exc}")
            return
        # Последняя проверка до файла: после неё запись уже настоящая, и её придётся
        # остановить как положено (см. _finish_closing)
        if self._closing_abort():
            capture.close()
            return

        now = datetime.now()
        # Шапку пишем полями «ключ: значение»: в файле это обычный markdown,
        # а окно раскладывает её карточкой вместо служебных строк в тексте
        header = (
            f"# Расшифровка {now.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"- Начата: {now.strftime('%d.%m.%Y %H:%M')}\n"
            f"- Распознавание: {self.transcriber.label}\n"
            f"- Спикеры: «{ME}» это микрофон, «{THEM}» это звук из наушников\n"
            "- Состояние: файл дополняется по ходу разговора\n\n---\n\n"
        )
        try:
            # Две записи в одну минуту получают разные имена: «… расшифровка.md»,
            # «… расшифровка (2).md». Существующий файл не перезаписывается никогда
            path = create_unique(
                OUT_DIR, f"{now.strftime('%Y-%m-%d_%H-%M')} расшифровка", ".md", header
            )
        except Exception as exc:
            capture.close()
            self._call("error", f"Файл записи не создался: {exc}")
            return

        self.out_path = path
        self.header_text = header
        self.lines.clear()
        self.gaps.clear()
        self.finalized = False
        self.append_failed = False
        self.session_start = session_start
        self._adopt(capture)
        self.rec_kind = "meeting"
        self.recording.set()
        self.worker_stop.clear()
        self.worker = threading.Thread(
            target=self._transcribe_loop, args=(self.segments,), daemon=True, name="asr"
        )
        self.worker.start()

        self._call("started", str(path))
        # подпись под статусом окно ведёт само: там время и число реплик
        self._status("Идёт запись", "live")

    def _adopt(self, capture: Capture) -> None:
        """Запоминает новый захват. Старый, если он остался, закрывается: два
        захвата вместе это занятый микрофон и поток, которого уже никто не читает."""
        old, self.capture = self.capture, capture
        if old is not None and old is not capture:
            old.close()

    def _close_capture(self) -> list[str]:
        """Закрывает текущий захват, если он есть. Возвращает зависшие источники."""
        capture, self.capture = self.capture, None
        return capture.close() if capture is not None else []

    def _do_stop(self) -> None:
        kind = self.rec_kind
        if kind is None:
            self.recording.clear()
            return  # остановка уже прошла: вторая команда «стоп» подряд ничего не делает
        # stopping поднимаем раньше, чем сбрасываем recording: закрытие окна смотрит
        # на них по очереди и не должно застать миг, когда один уже сброшен, а
        # другой ещё не поднят
        self.stopping.set()
        try:
            if kind == "answer":
                self._abort_answer()
            else:
                self._stop_meeting()
        except Exception as exc:
            if kind == "answer":
                raise  # ответ в тренажёре ждёт не stopped, а свой ответ; ошибку покажет цикл команд
            # Окно ждёт stopped. Без него оно висело бы на «Останавливаю» насовсем: ошибка
            # в этой фазе только сбрасывает запись, а новую начать ему не даёт
            self._call("error", clean_text(f"Остановка не закончилась: {exc}"))
            self._call("stopped", None, False)
        finally:
            self.rec_kind = None
            self.stopping.clear()

    def _abort_answer(self) -> None:
        """Остановка посреди ответа в тренажёре: так бывает, когда закрывают окно.

        Ответ ещё не расшифрован и не оценён, сохранять нечего. Файл встречи тут
        не при чём: его итог собирается только для записи встречи.
        """
        job = self._drop_answer_job()
        self.recording.clear()
        self._close_capture()
        self.pending_followup = None
        if job is not None and self.closing.is_set():
            # Окно закрывается, процесс выйдет следом. Реплика, которую поток
            # расшифровки уже разбирает, дойдёт до конца: выйти посреди вызова
            # распознавателя в потоке-демоне значит рисковать падением при выходе.
            # Ждёт поток устройств, окно не зависает. Дольше срока не ждём: вызов
            # прервать нельзя, а закрытие не должно застрять
            job.finished.wait(ANSWER_ABORT_WAIT_SEC)

    def _do_answer_abort(self) -> None:
        """Закрывает ответ, который остался открытым после таймаута answer_start или
        answer_stop. Трогает только ответ: встреча и всё остальное не при чём."""
        if self.rec_kind == "answer":
            self._abort_answer()
            self.rec_kind = None

    def _stop_meeting(self) -> None:
        self.recording.clear()
        if self.closing.is_set():
            self._status("Дописываю и закрываюсь", "stopping",
                         "дописываю последние реплики в файл")
        else:
            self._status("Останавливаю", "stopping", "дописываю последние реплики в файл")

        capture, self.capture = self.capture, None
        if capture is not None:
            stuck = capture.close()
            if stuck:
                # Предупреждение, а не ошибка: запись остановлена, хвост дописывается дальше
                self._call("warn", clean_text(f"Источник «{', '.join(stuck)}» не отозвался"))

        self.worker_stop.set()
        worker, self.worker = self.worker, None
        worker_alive = False
        if worker is not None:
            # Медленный распознаватель отстаёт от речи. Ждём, пока он дорасшифрует
            # очередь, и показываем, сколько осталось: иначе хвост разговора
            # потеряется, а итоговый файл перезапишется поверх недописанного
            deadline = time.time() + STOP_WAIT_SEC
            shown = None
            while worker.is_alive() and time.time() < deadline:
                left = self.segments.qsize()
                state = (left, self.closing.is_set())
                if left and state != shown:
                    if state[1]:
                        self._status("Дописываю и закрываюсь", "stopping",
                                     f"осталось реплик: {left}, потом окно закроется")
                    else:
                        self._status("Дорасшифровываю", "stopping", f"осталось реплик: {left}")
                    shown = state
                worker.join(timeout=0.5)
            worker_alive = worker.is_alive()
        self._drain_leftovers(worker_alive)

        # Итог собирается один раз за запись: после него файл могли дополнить
        # (выжимка) или удалить, и переписывать его вслепую нельзя
        summary = None
        in_file = True  # реплики лежат в файле, пусть и не в итоговом виде
        removed = False  # запись оказалась пустой, и файл удалён
        if not self.finalized:
            try:
                if self.lines or self.gaps or worker_alive:
                    summary = self._finalize()
                else:
                    # Ни реплики, ни дыры: в файле одна шапка «файл дополняется по ходу
                    # разговора». Она врёт и ничего не хранит. Поток расшифровки, который
                    # не уложился в срок, мог ещё что-то дописать, тогда файл остаётся
                    removed = self._drop_empty_file()
                self.finalized = True
            except Exception as exc:
                # Итоговая перезапись не вышла. Если по ходу записи всё дописывалось,
                # файл цел в том виде, как писался вживую. Если и дописывание
                # сбоило (диск полон, нет прав), часть реплик только в окне.
                # finalized не ставим: итога нет, и говорить так нельзя
                in_file = not self.append_failed
                tail = ("Реплики остались в файле как были записаны" if in_file else
                        "Реплики остались только в окне, сохрани их вручную (Ctrl+A в ленте)")
                self._call("warn", clean_text(f"Итог не собрался: {exc}. {tail}"))
        try:
            report = session_ui(summary)
        except Exception:
            report = None
        # Устройства свободны с этого момента: окно получит «Новая запись» и вправе
        # сразу начать следующую
        self.rec_kind = None
        # Итог и статус идут той же очередью, что реплики: хвостовая реплика, которую
        # расшифровали уже после «Стоп», лежит там раньше и должна встать в ленту до
        # блока «Как ты говорил», а не под ним
        self.ready.put(("call", ("stopped", (report, removed))))
        if not in_file:
            self._status("Запись не сохранилась", "error",
                         "реплики только в окне, сохрани их вручную (Ctrl+A в ленте)", queued=True)
        elif self.lines:
            lost = f", не распознано {len(self.gaps)}" if self.gaps else ""
            self._status(
                "Записано", "done",
                f"реплик {len(self.lines)}{lost}, файл в папке «расшифровки»", queued=True,
            )
        elif self.gaps:
            self._status("Ничего не распозналось", "error", self.gaps[-1][2], queued=True)
        else:
            self._status(
                "Речи не было", "error",
                "проверь, тот ли выбран вход и слышно ли собеседника в наушниках", queued=True,
            )

    def _drain_leftovers(self, worker_alive: bool) -> None:
        """Добирает реплики, оставшиеся в очереди после остановки.

        Обычно их уже разобрал поток расшифровки. Если он умер, расшифровываем
        здесь сами. Если он ещё жив, но не уложился в срок, трогать
        распознаватель из второго потока нельзя, и остаток честно помечаем дырой
        в файле, а не теряем молча.
        """
        if not worker_alive and self.segments.qsize():
            self._status("Дорасшифровываю", "stopping",
                         f"осталось реплик: {self.segments.qsize()}")
        while True:
            try:
                seg = self.segments.get_nowait()
            except queue.Empty:
                return
            if worker_alive:
                self._mark_lost(seg, "не успели расшифровать до остановки")
            else:
                self._handle_safe(seg)

    def _on_level(self, speaker: str, level: float) -> None:
        self.levels[speaker] = level
        if self.checking.is_set() and speaker == ME:
            self.mic_peak = max(self.mic_peak, level)

    # ------------------------------------------------------- поток расшифровки

    def _transcribe_loop(self, segments: "queue.Queue[Segment] | None" = None) -> None:
        # Очередь поток получает при создании: следующая запись заводит свою, и
        # поток, который не успел закончиться, не должен забирать её реплики
        if segments is None:
            segments = self.segments
        # Сбой одной реплики не должен убить поток: он молча встанет, окно будет
        # писать «Идёт запись», а хвост разговора пропадёт. Поэтому каждая
        # реплика идёт через _handle_safe
        try:
            while not self.worker_stop.is_set():
                try:
                    seg = segments.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._handle_safe(seg)

            # добираем то, что источники успели дописать при остановке
            while True:
                try:
                    seg = segments.get_nowait()
                except queue.Empty:
                    break
                self._handle_safe(seg)
        except Exception as exc:
            # Сюда не должно попасть ничего. Если попало, _do_stop увидит мёртвый
            # поток и дочитает очередь сам
            self._warn(f"Поток расшифровки остановился: {exc}")

    def _handle_safe(self, seg: Segment) -> None:
        try:
            self._handle(seg)
        except Exception as exc:
            try:
                self._mark_lost(seg, f"сбой обработки реплики: {exc}")
            except Exception:
                pass  # даже метку поставить не вышло, но поток живёт дальше

    def _append_file(self, text: str) -> None:
        """Дописывает в файл записи. Сбой записи не бросает: реплика остаётся в
        памяти, и итог при остановке соберёт файл заново вместе с ней."""
        path = self.out_path
        if path is None:
            return
        try:
            with path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except (OSError, ValueError) as exc:
            self.append_failed = True
            self._warn(f"Не получилось дописать файл записи: {exc}. "
                       "Реплики сохранятся при остановке")

    def _handle(self, seg: Segment) -> None:
        try:
            # Замок общий с расшифровкой ответа: брошенный поток прошлого ответа
            # может ещё доделывать свою реплику тем же распознавателем
            with self._asr_lock:
                text = self.transcriber.transcribe(seg.audio)
        except Exception as exc:
            self._lost(seg, exc)
            return
        # Суррогаты и прочее, что не кодируется в UTF-8, убираем до всего остального
        text = clean_text(text or "").strip()
        if not text:
            return

        self.lines.append((seg.t_start, seg.speaker, text, seg.duration))
        # Пишем сразу и закрываем файл: агент, читающий его параллельно,
        # должен видеть последнюю реплику через секунду, а не в конце созвона
        self._append_file(f"**[{hhmmss(seg.t_start)}] {seg.speaker}:** {text}\n\n")

        self.ready.put((
            "line",
            {
                "t": seg.t_start,
                "speaker": seg.speaker,
                "text": text,
                "who": "me" if seg.speaker == ME else "them",
            },
        ))

    def _lost(self, seg: Segment, exc: Exception) -> None:
        """Реплика не распозналась: отмечаем дыру в файле и в ленте."""
        reason = str(exc) if isinstance(exc, RecognizerError) else f"сбой расшифровки: {exc}"
        self._mark_lost(seg, reason)

    def _mark_lost(self, seg: Segment, reason: str) -> None:
        # Причина идёт в одну строку markdown, поэтому без переводов строки
        reason = " ".join(clean_text(reason).split())[:300]
        self.gaps.append((seg.t_start, seg.speaker, reason))
        self._append_file(
            f"**[{hhmmss(seg.t_start)}] {seg.speaker}:** _(не распознано: {reason})_\n\n"
        )

        self.ready.put((
            "line",
            {
                "t": seg.t_start,
                "speaker": seg.speaker,
                "text": "не распознано",
                "who": "me" if seg.speaker == ME else "them",
                "gap": True,
            },
        ))
        self._warn(reason)

    # ------------------------------------------------------------ поток вывода

    def _page_loaded(self) -> bool:
        try:
            return self.window is not None and self.window.events.loaded.is_set()
        except Exception:
            return False

    def _deliver(self, kind: str, payload) -> None:
        if kind == "line":
            self._call("addLine", payload)
        elif kind == "warn":
            self._call("warn", payload)
        elif kind == "error":
            self._call("error", payload)
        elif kind == "source_lost":
            self._source_lost(payload)
        elif kind == "model":
            self._call("modelState", payload)
        elif kind == "gpu":
            self._call("gpuState", payload)
        elif kind == "call":
            name, args = payload
            self._call(name, *args)

    def _source_lost(self, info: dict) -> None:
        """Источник отвалился: говорим об этом окну, и статус записи перестаёт врать."""
        self._call("warn", info["text"])
        if self.rec_kind != "meeting" or not self.recording.is_set():
            return  # саундчек, проверка микрофона или запись уже кончилась
        capture = self.capture
        sources = list(capture.sources) if capture is not None else []
        if sources and all(s.error is not None for s in sources):
            self._status("Запись не идёт", "error",
                         "все источники отвалились. Нажми «Стоп»: записанное сохранится")
        else:
            self._status(f"Идёт запись без «{info['speaker']}»", "error",
                         "источник отвалился, проверь устройство. Остальное пишется")

    def _pump_once(self, last_levels):
        if self._startup_warnings and self._page_loaded():
            # Одним сообщением: следующая всплывашка вытеснила бы предыдущую
            warnings, self._startup_warnings = self._startup_warnings, []
            self.ready.put(("warn", ". ".join(warnings)))

        while True:
            try:
                kind, payload = self.ready.get_nowait()
            except queue.Empty:
                break
            try:
                self._deliver(kind, payload)
            except Exception as exc:
                # Одно непрошедшее сообщение не должно отнять у окна остальные
                self._warn(f"Сбой вывода в окно: {exc}")

        if not (self.recording.is_set() or self.checking.is_set() or self.monitoring.is_set()):
            return None

        # В тишине уровень дрожит в шестом знаке. Округляем и молчим, пока
        # картинка не изменилась, иначе шлём в окно десять пустых вызовов в секунду
        now = (round(self.levels.get(ME, 0.0), 3), round(self.levels.get(THEM, 0.0), 3))
        if now != last_levels:
            self._call("setLevels", now[0], now[1])
            return now
        return last_levels

    def _pump_loop(self) -> None:
        last_levels = None
        while True:
            time.sleep(PUMP_SEC)
            try:
                last_levels = self._pump_once(last_levels)
            except Exception as exc:
                # Без этого потока окно слепнет: ни реплик, ни уровней, ни ошибок
                self._warn(f"Сбой вывода в окно: {exc}")

    # ------------------------------------------------------- слежение за банками

    @staticmethod
    def _banks_stamp() -> list[tuple]:
        """Отпечаток папки с банками: имя, время правки и размер каждого файла."""
        BANKS_DIR.mkdir(parents=True, exist_ok=True)
        out = []
        for p in sorted(BANKS_DIR.glob("*.yaml")):
            try:
                st = p.stat()
                out.append((p.name, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        return out

    def _banks_loop(self) -> None:
        """Подхватывает новые банки без перезапуска окна.

        Новый банк кладут в папку, пока программа открыта, и список должен
        обновиться сам: жать «обновить» никто не будет.
        """
        try:
            self._banks_last = self._banks_stamp()
        except Exception:
            self._banks_last = []
        while True:
            time.sleep(BANKS_POLL_SEC)
            try:
                self._banks_last = self._banks_step(self._banks_last)
            except Exception as exc:
                # Битый банк или сбой чтения папки не должны останавливать слежение
                self._warn(f"Не удалось обновить список банков: {exc}")

    def _banks_step(self, last: list[tuple]) -> list[tuple]:
        stamp = self._banks_stamp()
        if stamp == last:
            return last

        # Файл могут писать прямо сейчас: дочитаем на следующем круге,
        # иначе поймаем YAML на середине и покажем «битый файл». Время правки
        # в далёком будущем (часы сбились, файл пришёл из архива) свежим не
        # считается, иначе список не обновился бы никогда
        now = time.time_ns()
        if any(-BANK_FUTURE_SLACK_NS < now - mtime < BANK_SETTLE_NS for _, mtime, _ in stamp):
            return last

        names = {x[0] for x in last}
        appeared = [n for n, _, _ in stamp if n not in names]
        try:
            self._call("banksChanged", Bank.available(),
                       self.settings.get("темы", {}), appeared)
        except Exception as exc:
            self._warn(f"Не удалось обновить список банков: {exc}")
        # Отпечаток запоминаем и после неудачи: повторять то же самое каждые две
        # секунды незачем, следующая попытка будет после очередной правки файла
        return stamp

    # ---------------------------------------------------------------- итог

    def _finalize(self):
        """Пересобирает файл по времени реплик и считает разбор речи.

        Живой вывод идёт в порядке готовности расшифровки, а он не всегда
        совпадает с порядком реплик: длинная фраза считается дольше короткой.
        """
        if self.finalized or not (self.lines or self.gaps) or self.out_path is None:
            return None

        ordered = sorted(self.lines, key=lambda x: x[0])
        # Дыры встают в ленту на своё время, но в разбор речи не идут
        rows = [(t, who, txt) for t, who, txt, _ in ordered]
        rows += [(t, who, f"_(не распознано: {why})_") for t, who, why in self.gaps]
        rows.sort(key=lambda x: x[0])
        body = "".join(f"**[{hhmmss(t)}] {who}:** {txt}\n\n" for t, who, txt in rows)

        # Разбираем только свою речь: собеседника оценивать смысла нет. Разбор
        # вторичен: если он упал, расшифровка всё равно должна лечь в файл
        try:
            summary = analyze_session(
                self.analyzer, [(txt, dur) for _, who, txt, dur in ordered if who == ME]
            ) if ordered else None
            report = session_report(summary) if summary else ""
        except Exception as exc:
            summary, report = None, ""
            self._call("warn", clean_text(f"Разбор речи не получился: {exc}"))

        lost = f"- Не распознано: {len(self.gaps)}\n" if self.gaps else ""
        label = self.transcriber.label if self.transcriber else describe(self.asr_cfg)
        write_text_atomic(
            self.out_path,
            f"# Расшифровка {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"- Длительность: {hhmmss(rows[-1][0])}\n"
            f"- Реплик: {len(ordered)}\n"
            + lost
            + f"- Распознавание: {label}\n"
            f"- Спикеры: «{ME}» это микрофон, «{THEM}» это звук из наушников\n\n---\n\n"
            + body
            + ("\n---\n\n" + report if report else ""),
        )
        return summary

    def _drop_empty_file(self) -> bool:
        """Удаляет файл записи, в котором нет ничего, кроме нашей шапки.

        Запись без единой реплики и без дыр оставляет файл с «Состояние: файл дополняется
        по ходу разговора», а _finalize на такой выходит сразу. Файл ничего не хранит и
        обещает то, чего не будет. Удаляем только если там по-прежнему ровно наша шапка:
        если туда что-то дописали (агент, человек, поздняя реплика), это уже не наш мусор.
        True, если файл удалён.
        """
        path = self.out_path
        if path is None or not self.header_text:
            return False
        try:
            if path.read_text(encoding="utf-8", errors="replace") != self.header_text:
                return False
            path.unlink()
        except OSError:
            return False
        self.out_path = None
        return True

    # ------------------------------------------------------------- мок-собес

    def interview_setup(self) -> dict:
        """Что показать на экране выбора: банки, уровни, прошлые сессии."""
        return {
            "banks": Bank.available(),
            "levels": describe_levels(),
            "history": summarize_history(),
            "saved_categories": self.settings.get("темы", {}),
        }

    def bank_prompt(self, vacancy: str = "") -> dict:
        """Запрос для нейросети, по которому она делает банк под вакансию. Текст запроса
        один и живёт в interview.bank_prompt: окно только копирует то, что вернул питон."""
        vacancy = clean_text(vacancy) if isinstance(vacancy, str) else ""
        return {"ok": True, "text": build_bank_prompt(vacancy)}

    def bank_save(self, reply: str) -> dict:
        """Ответ нейросети, вставленный в окно: разбирает и, если банк прочитался целиком,
        сохраняет его в папку «вопросы». Иначе возвращает список ошибок человеческими
        словами и готовую просьбу исправить, которую окно копирует в буфер."""
        reply = clean_text(reply) if isinstance(reply, str) else ""
        try:
            parsed = parse_bank_reply(reply)
        except Exception as exc:  # разбор не должен падать, но окно не должно и зависать
            return {"ok": False, "errors": [clean_text(f"Ответ не разобрался: {exc}")],
                    "notes": [], "fix": ""}
        info = parsed.info()
        notes = self._bank_notes(info["notes"])
        if not parsed.ok:
            errors = list(parsed.errors) or ["Ответ не читается как банк вопросов"]
            return {"ok": False, "errors": errors, "notes": notes,
                    "fix": fix_request(errors)}
        try:
            file = save_bank(parsed)
        except BankError as exc:
            return {"ok": False, "errors": [str(exc)], "notes": notes, "fix": ""}
        except Exception as exc:
            return {"ok": False, "errors": [clean_text(f"Банк не сохранился: {exc}")],
                    "notes": notes, "fix": ""}
        # Окно получает свежий список в этом же ответе. Слежение за папкой не должно
        # через две секунды сообщить о том же файле второй раз
        try:
            self._banks_last = self._banks_stamp()
        except Exception:
            pass
        return {"ok": True, "file": file, "name": info["name"], "count": info["count"],
                "notes": notes, "banks": Bank.available()}

    @staticmethod
    def _bank_notes(notes: list[str]) -> list[str]:
        """Что программа поправила в ответе, для человека. Про JSON и YAML (interview.py
        пишет «В JSON были комментарии, убрал», «ответ прочитан мягче, как YAML») ему знать
        нечего: это одна фраза, и повторять её незачем. Замечания к вопросам остаются."""
        out: list[str] = []
        for note in notes:
            if note.startswith("В JSON"):
                note = "Ответ нейросети был с мелкими ошибками, программа их поправила"
            if note not in out:
                out.append(note)
        return out

    def interview_start(self, bank_file: str, level: str, mic_index: int,
                        mic_name: str = "", categories: list[str] | None = None,
                        shuffle: bool = False, only_ids: list[str] | None = None) -> dict:
        # Без микрофона тренировка не начинается: ответы уходили бы в пустоту
        mic = self._index(mic_index)
        if mic < 0:
            return {"error": "Не выбран микрофон: подключи его, список обновится сам"}
        # Имя приходит из окна. Годится только то, что сейчас лежит в папке
        # банков: абсолютный путь или «..» подменили бы папку, а текст ошибки
        # YAML понёс бы в окно кусок чужого файла
        if not isinstance(bank_file, str) or bank_file not in {
            b["file"] for b in Bank.available()
        }:
            return {"error": "Банк не найден"}
        try:
            bank = Bank.load(BANKS_DIR / bank_file)
        except Exception as exc:
            return {"error": f"Банк вопросов не читается: {exc}"}
        if not bank.questions:
            return {"error": "В банке нет вопросов"}

        self.session = Session(bank, level, self.analyzer, categories, shuffle, only_ids)
        if not self.session.questions:
            self.session = None
            return {"error": "На этих темах и уровне вопросов не осталось"}

        # Запоминаем выбор тем по каждому банку: под техническую тренировку
        # и под поведенческую наборы разные, переключаться придётся часто.
        # Прогон слабых вопросов выбор тем не меняет
        if not only_ids:
            self.settings.setdefault("темы", {})[bank_file] = list(categories or [])
            self._save_settings()

        self.interview_mic = mic
        self.interview_mic_name = self._name(mic_name)
        # Модель ещё качается: загрузка ждёт её в своём потоке, а поток устройств остаётся
        # свободным для проверки микрофона
        self._queue_when_model_ready(("load_model", None, None))
        return {"ok": True, **self._question_payload()}

    def _question_payload(self) -> dict:
        s = self.session
        q = s.current()
        if q is None:
            return {"finished": True, "progress": s.progress()}
        target, limit = s.limits(q)
        return {
            "finished": False,
            "progress": s.progress(),
            "question": {
                "id": q.id,
                "text": q.text,
                "category": q.category,
                "note": q.note,
                "theses": [t.text for t in q.theses],
                "target": [round(target[0]), round(target[1])] if target else None,
                "fillers_limit": limit,
            },
        }

    def mic_check_start(self, mic_index: int, mic_name: str = "") -> dict:
        """Открывает микрофон просто чтобы показать уровень.

        Нужно потому, что тихий вход выглядит как «программа сломалась»: ответы
        уходят в пустоту, а понять это можно только по исчезающей подсказке.
        """
        mic = (self._index(mic_index), self._name(mic_name))
        if mic[0] < 0:
            return {"error": "Не выбран микрофон: подключи его, список обновится сам"}
        why = self._busy_reason("check")
        if why:
            return {"error": why}
        reply = _Reply()
        self.commands.put(("mic_check", mic, reply))
        return self._wait(reply, MIC_CHECK_WAIT_SEC, "Микрофон не отозвался",
                          cleanup="mic_check_stop")

    def mic_check_stop(self) -> dict:
        reply = _Reply()
        self.commands.put(("mic_check_stop", None, reply))
        try:
            return reply.get(timeout=MIC_CHECK_WAIT_SEC)
        except queue.Empty:
            return {"error": "Не удалось закрыть микрофон"}

    def followup_start(self, question_text: str) -> dict:
        """Запись ответа на уточняющий вопрос. Пишется так же, разбирается иначе."""
        if self.session is None:
            return {"error": "сессия не начата"}
        self.pending_followup = question_text
        result = self.answer_start()
        if result.get("error"):
            self.pending_followup = None  # ответ не начался, уточнение никого не ждёт
        return result

    def answer_start(self) -> dict:
        """Начинает запись ответа. Слушаем только микрофон: собеседника тут нет."""
        if self.session is None:
            return {"error": "сессия не начата"}
        mic = (self.interview_mic, self.interview_mic_name)
        if mic[0] < 0:
            return {"error": "Не выбран микрофон: подключи его, список обновится сам"}
        why = self._busy_reason("answer")
        if why:
            return {"error": why}
        # Окно держит кнопку закрытой, пока модель качается. Этот отказ страхует: ждать
        # скачивание здесь значило бы упереться в минуту ожидания старта ответа
        cfg = self.asr_cfg
        if cfg["режим"] != "api" and not model_ready(cfg["модель"]):
            # Другую модель выбрали в настройках посреди тренировки, и сама она не качается
            # (см. _model_after_settings): запускаем загрузку, окно закроет кнопку и покажет ход
            self._model_start_job(cfg["модель"])
            return {"error": "Распознавание ещё готовится, подожди"}
        reply = _Reply()
        self.commands.put(("answer_start", mic, reply))
        # Минута: сюда входит и загрузка модели, если она ещё не кончилась. Не
        # дождались, значит окно получает ошибку, а запись, которая могла успеть
        # открыться, закрывает команда answer_abort
        return self._wait(
            reply, ANSWER_START_WAIT_SEC,
            "Микрофон или распознавание не отозвались за минуту. Попробуй ещё раз",
            cleanup="answer_abort",
        )

    def answer_stop(self) -> dict:
        """Останавливает запись, дожидается расшифровки хвоста и разбирает ответ."""
        if self.session is None:
            return {"error": "сессия не начата"}
        reply = _Reply()
        self.commands.put(("answer_stop", None, reply))
        # Обычно ждать секунды: всё, кроме последней реплики, расшифровано по ходу
        # ответа. Но медленный распознаватель отстаёт от речи, и очередь минутами
        # расшифровывается после остановки: обрывать её раньше времени нельзя,
        # записанное просто пропадёт. Но не вечно: кнопка в окне не должна висеть,
        # пока поток устройств занят
        return self._wait(
            reply, ANSWER_STOP_WAIT_SEC,
            "Расшифровка ответа не уложилась в десять минут, ответ не засчитан",
            cleanup="answer_abort",
        )

    def interview_retry(self) -> dict:
        return {"ok": True, **self._question_payload()}

    def interview_next(self) -> dict:
        if self.session is None:
            return {"error": "сессия не начата"}
        self.session.advance()
        return {"ok": True, **self._question_payload()}

    def interview_finish(self) -> dict:
        if self.session is None:
            return {"error": "сессия не начата"}
        path = self.session.save()
        prompt = self.session.prompt_for_review() if path else ""
        summary = self.session.summary()
        self.session = None
        return {"ok": True, "path": str(path) if path else "", "prompt": prompt, "summary": summary}

    def _save_unfinished_session(self) -> None:
        """Окно закрывают или перезапускают посреди тренировки: данные ответы
        ложатся в мок-собесы, как после «Завершить». Только запись файла, без окна."""
        session, self.session = self.session, None
        if session is None or not session.attempts:
            return
        try:
            session.save()
        except Exception:
            # Окно уже закрывается, сказать некому: причина остаётся в ошибка.log
            try:
                with open(HERE / "ошибка.log", "a", encoding="utf-8") as f:
                    f.write("Разбор незаконченной тренировки не сохранился\n" + traceback.format_exc())
            except OSError:
                pass

    # ----------------------------------------------------- запись одного ответа

    def _do_devices(self) -> dict:
        """Читает список устройств. Зовётся только из потока устройств.

        PortAudio читает список при первой инициализации и держит его, пока жив
        хоть один PyAudio. Саундчек держит свой, и с ним список остался бы тем,
        что был при его открытии: наушники, подключённые позже, не появились бы.
        Поэтому на время чтения саундчек закрывается, а окно по monitor_closed
        открывает его заново. Запись, ответ и проверка микрофона закрыться не
        могут, для них список остаётся прежним.
        """
        closed = self.monitoring.is_set()
        if closed:
            self._end_monitor()
        try:
            d = list_devices()
        except Exception as exc:
            return {"mics": [], "loopbacks": [], "monitor_closed": closed,
                    "error": clean_text(f"Список устройств не прочитался: {exc}")}
        return {
            "mics": d["mics"],
            "loopbacks": d["loopbacks"],
            "default_mic": d["default_mic"],
            "default_loopback": d["default_loopback"],
            "monitor_closed": closed,
        }

    def _do_monitor(self, mic: tuple, spk: tuple, gen: int) -> dict:
        """Открывает оба источника ради уровня. Реплики выбрасываются сразу."""
        # Вызов мог устареть, пока ждал в очереди: после него был ещё один старт
        # или стоп саундчека, либо окно уже не дождалось ответа
        if gen != self._monitor_gen:
            return {"error": "устройства уже не нужны"}
        # Запись могла начаться, пока команда ждала в очереди
        why = self._busy_reason("monitor")
        if why or self.closing.is_set():
            return {"error": why or "Окно закрывается"}
        self._end_monitor()
        try:
            devices = self._pick_devices(mic, spk)
            if not devices:
                return {"error": "Нет ни микрофона, ни наушников"}
            capture = self._open_capture(devices, _DISCARD, time.time())
        except DeviceNotFound as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Устройство не открылось: {exc}"}
        self._adopt(capture)
        self.monitoring.set()
        return {"ok": True}

    def _end_monitor(self) -> dict:
        """Закрывает саундчек, если он открыт. Зовётся только из потока устройств."""
        if self.monitoring.is_set():
            self.monitoring.clear()
            self._close_capture()
            self.levels = {ME: 0.0, THEM: 0.0}
        return {"ok": True}

    def _end_check(self) -> None:
        """Закрывает проверку микрофона, если она открыта. Только из потока устройств.

        Очередь реплик после неё новая: проверочное «раз два три» никто не
        расшифровывал, и в следующую запись оно попасть не должно.
        """
        if self.checking.is_set():
            self.checking.clear()
            self._close_capture()
            self.segments = queue.Queue()
            self.levels = {ME: 0.0, THEM: 0.0}

    def _release_devices(self) -> dict:
        """Отдаёт устройства, которые держат саундчек или проверка микрофона."""
        self._end_monitor()
        self._end_check()
        return {"ok": True}

    def _do_mic_check(self, mic: tuple) -> dict:
        """Открывает вход и гонит уровень в окно. Расшифровка не нужна."""
        why = self._busy_reason("check")
        if why or self.closing.is_set():
            return {"error": why or "Окно закрывается"}
        self._end_monitor()
        # Повторное «Проверить»: прежний захват закрывается, второго не будет
        self._end_check()
        self.segments = queue.Queue()
        try:
            devices = self._pick_devices(mic, (-1, ""))
            capture = self._open_capture(devices, self.segments, time.time())
        except DeviceNotFound as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Микрофон не открылся: {exc}"}
        self._adopt(capture)
        self.checking.set()
        self.mic_peak = 0.0
        return {"ok": True}

    def _do_mic_check_stop(self) -> dict:
        # Закрываем только свою проверку: захват может принадлежать записи встречи
        if not self.checking.is_set():
            return {"error": "Проверка микрофона не идёт"}
        peak = self.mic_peak
        self._end_check()
        # Речь обычного человека даёт пик около 0.1. Всё, что тише сотой доли,
        # VAD речью не признает, и ответы будут уходить в пустоту
        verdict = "хорошо" if peak > 0.05 else "тихо" if peak > 0.01 else "почти ничего"
        return {"ok": True, "peak": round(peak, 4), "verdict": verdict}

    def _do_answer_start(self, mic: tuple) -> dict:
        why = self._busy_reason("answer")
        if why or self.closing.is_set():
            return {"error": why or "Окно закрывается"}
        # Зовём всегда: если настройки распознавания поменялись, соберётся заново,
        # а если нет, вернётся сразу
        try:
            self._load_model()
        except Exception as exc:
            return {"error": self._not_started(exc)}

        # Забытая проверка микрофона и саундчек уступают: человек начал отвечать
        self._end_monitor()
        self._end_check()
        self._drop_answer_job()
        self.segments = queue.Queue()
        try:
            devices = self._pick_devices(mic, (-1, ""))
            # Отсчёт ответа идёт от открытия захвата: чтение списка устройств в
            # длительность и в смещения реплик не входит
            self.session_start = time.time()
            capture = self._open_capture(devices, self.segments, self.session_start)
        except DeviceNotFound as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Микрофон не открылся: {exc}"}

        self._adopt(capture)
        # Реплики расшифровываются по ходу ответа, а не после «Закончить ответ»
        job = _AnswerJob(self.segments, self.transcriber)
        self.answer_job = job
        threading.Thread(target=self._answer_loop, args=(job,), daemon=True,
                         name="asr-answer").start()
        self.rec_kind = "answer"
        self.recording.set()
        return {"ok": True}

    def _answer_loop(self, job: _AnswerJob) -> None:
        """Поток расшифровки ответа: забирает реплики по мере появления.

        Кончает по знаку конца (job.end() после закрытия источников) или по
        отмене. Сбой одной реплики его не останавливает.
        """
        try:
            while not job.abort.is_set():
                try:
                    seg = job.segments.get(timeout=0.5)
                except queue.Empty:
                    continue
                if seg is None:
                    break
                self._answer_one(job, seg)
        finally:
            job.finished.set()

    def _answer_one(self, job: _AnswerJob, seg: Segment) -> None:
        """Расшифровывает одну реплику ответа. Сбой записывается и ответ не роняет.

        Пока человек говорит, пробуем каждую реплику: сеть могла вернуться. После
        «Закончить ответ» ждёт человек, поэтому подряд упавшие ANSWER_GIVE_UP_FAILS
        реплик значат, что распознавание лежит, и остаток очереди идёт в потери без
        вызова: иначе ожидание вышло бы по таймауту на каждую недоделанную реплику.
        """
        text = None
        failed = None
        if job.finishing.is_set() and job.fails_in_row >= ANSWER_GIVE_UP_FAILS:
            failed = job.lost[-1]
        else:
            try:
                with self._asr_lock:
                    if job.abort.is_set():
                        return
                    text = job.transcriber.transcribe(seg.audio)
            except Exception as exc:
                failed = str(exc) or type(exc).__name__
        job.seen += 1
        if failed is not None:
            job.lost.append(failed)
            job.lost_spans.append((seg.t_start, seg.t_start + seg.duration))
            job.fails_in_row += 1
            return
        job.fails_in_row = 0
        if text:
            job.parts.append((seg.t_start, seg.duration, text))

    def _finish_answer(self, job: _AnswerJob, cancel: threading.Event | None) -> bool:
        """Ждёт, пока поток расшифровки разберёт всё, что осталось после остановки.

        Источники уже закрыты, поэтому новых реплик не будет: получив знак конца,
        поток дорабатывает хвост и кончает. False значит, что окно перестало ждать
        (вышел срок) и расшифровку надо бросить.
        """
        job.end()
        while not job.finished.wait(0.2):
            if cancel is not None and cancel.is_set():
                return False
        # Поток мог умереть раньше времени и оставить реплики в очереди: разбираем их сами
        while True:
            try:
                seg = job.segments.get_nowait()
            except queue.Empty:
                return True
            if cancel is not None and cancel.is_set():
                return False
            if seg is not None:
                self._answer_one(job, seg)

    def _drop_answer_job(self) -> "_AnswerJob | None":
        """Бросает расшифровку ответа, которая больше не нужна. Поток кончит после
        реплики, которую уже разбирает. Возвращает брошенное задание, чтобы можно
        было дождаться этой реплики."""
        job, self.answer_job = self.answer_job, None
        if job is not None:
            job.abort.set()
            job.end()
        return job

    def _do_answer_stop(self, cancel: threading.Event | None = None) -> dict:
        # Останавливаем только ответ: если сейчас идёт запись встречи, признак
        # rec_kind и захват принадлежат ей, и трогать их нельзя
        if self.rec_kind != "answer":
            return {"error": "Ответ сейчас не записывается"}
        started = self.session_start
        job, self.answer_job = self.answer_job, None
        self.recording.clear()
        self.rec_kind = None
        self._close_capture()

        # Длительность меряем по часам, а не по сумме кусков речи:
        # паузы и заминки это часть ответа, за них тоже спрашивают. Ожидание хвоста
        # сюда не входит
        duration = max(0.1, time.time() - started)

        # Реплики, которые детектор речи отрезал по ходу ответа, поток расшифровки
        # уже разобрал. Источники закрыты, недоговорённая реплика лежит в очереди:
        # ждём её, и больше почти ничего
        try:
            finished = self._finish_answer(job, cancel)
        finally:
            job.abort.set()  # поток к этому времени кончил, флаг нужен на случай сбоя
        if not finished:
            # Окно перестало ждать и уже получило ошибку по времени
            return {"error": "Расшифровка прервана: вышло время ожидания"}

        # Порядок по времени начала, как и раньше: потоку всё равно, в каком порядке
        # реплики пришли в очередь
        parts = sorted(job.parts, key=lambda p: p[0])
        # Сервер мог обрезать эмодзи посередине: одиночный суррогат уронил бы запись разбора
        answer = clean_text(" ".join(text for _, _, text in parts)).strip()
        lost = job.lost
        if lost and not answer:
            # Текста нет, а сбои были: назвать такой ответ пустым значило бы соврать
            return {"error": f"Сбой расшифровки: {lost[0]}"}
        # Отрезок упавшей реплики тоже в spans: человек там говорил, это не пауза.
        # Иначе дыра между соседями стала бы ложной длинной паузой в разборе
        spans = sorted([(start, start + length) for start, length, _ in parts] + job.lost_spans,
                       key=lambda span: span[0])
        followup_q, self.pending_followup = self.pending_followup, None
        if cancel is not None and cancel.is_set():
            # Окно уже получило ошибку по времени: в сессию ответ не записываем
            return {"error": "Расшифровка прервана: вышло время ожидания"}
        if not answer:
            return {"empty": True, "duration": round(duration), "was_followup": bool(followup_q)}

        if lost:
            # Часть реплик не распозналась, ответ разобран по остальным: человеку надо
            # знать, что тезисы и заминки посчитаны не по всему сказанному
            # Мимо _warn: он глушит одинаковый текст на ERROR_REPEAT_SEC, а второй ответ
            # подряд с тем же сбоем остался бы без пометки, и непрозвучавшие тезисы
            # выглядели бы виной человека
            why = " ".join(clean_text(lost[0]).split())[:200]
            self.ready.put(("warn", clean_text(
                f"Не распозналось реплик: {len(lost)} из {job.seen}. "
                f"Разбор сделан по остальному. Причина: {why}")))

        if followup_q:
            v = self.session.answer_followup(followup_q, answer, duration)
        else:
            v = self.session.answer(answer, duration, spans)
        result = {
            "ok": True,
            "text": answer,
            "verdict": {
                "attempt": v.attempt_no,
                "duration": round(v.duration),
                "target": [round(v.target[0]), round(v.target[1])] if v.target else None,
                "time_verdict": v.time_verdict,
                "fillers": v.fillers,
                "fillers_limit": v.fillers_limit,
                "hedges": v.hedges,
                "covered": v.covered,
                "missing": [t.text for t in v.missing_theses],
                "followup": v.followup,
                "should_retry": v.should_retry,
                "can_retry": v.can_retry,
                "delta": v.delta,
                "clean": v.clean,
                "just_closed": v.just_closed,
                "after_followup": v.after_followup,
                "versus_past": v.versus_past,
            },
        }
        if lost:
            result["lost"] = len(lost)
        return result

    # ------------------------------------------------------- закрытие окна

    def on_closing(self) -> bool | None:
        """Обработчик закрытия окна: False отменяет закрытие, None пропускает.

        Зовётся синхронно, в потоке окна, и это две ловушки. Первая: любой
        evaluate_js отсюда и из всего, что мы тут ждём, виснет насмерть. Он
        через Control.Invoke ждёт поток окна, а поток окна стоит здесь и ждёт
        его: раньше так и выходило, и девяносто секунд окно не отвечало, а хвост
        записи не дописывался. Вторая: устройства принадлежат аудио-потоку,
        останавливать запись напрямую нельзя.

        Занято значит одно из трёх: запись идёт (recording), запись
        останавливается (stopping) или запускается (starting: модель грузится,
        устройства открываются, файла ещё может не быть). Тогда закрытие
        отменяется, аудио-потоку уходит команда «стоп», а он, дописав хвост в
        файл, сам закрывает окно через window.destroy() (см. _close_window).
        Окно за это время живое и показывает «Дописываю и закрываюсь». Если
        крестик нажали во время запуска, «стоп» не нужен: запуск сам увидит
        просьбу, не создаст файл или остановит уже начавшуюся запись (см.
        _finish_closing). Крестик, нажатый ещё раз через CLOSE_FORCE_SEC после
        первого, закрывает окно как есть: расшифрованное к этому времени уже
        лежит в файле.

        Идущая установка CUDA записью не считается, но закрытие её оборвёт: первый
        крестик предупреждает и ничего не закрывает, второй закрывает окно, а
        shutdown останавливает установку.
        """
        if self.close_ok.is_set():
            return None  # хвост дописан, окно закрываем мы сами
        first = not self.closing.is_set()
        if first:
            self.close_asked_at = time.monotonic()
        # Сначала флаг, потом проверка занятости. Аудио-поток снимает starting и
        # потом смотрит на флаг: при обратном порядке просьба могла бы проскочить
        # между его проверкой и нашей, и окно не закрыл бы никто
        self.closing.set()
        recording = self.recording.is_set()
        stopping = self.stopping.is_set()
        starting = self.starting.is_set()
        if not (recording or stopping or starting):
            self.closing.clear()
            self.close_asked_at = 0.0
            if self._gpu_installing() and not self._gpu_close_warned:
                # Установка CUDA идёт в дочернем процессе, и закрытие окна её оборвёт на
                # полпути. Один раз спрашиваем, второй крестик закрывает (см. shutdown)
                self._gpu_close_warned = True
                self.ready.put(("warn", "Идёт установка библиотек для видеокарты. "
                                        "Закрой окно ещё раз, чтобы прервать её"))
                return False
            self._save_unfinished_session()
            self._release_devices_for_exit()
            return None
        if first:
            if recording and not stopping:
                # Остановку уже могли запустить кнопкой: тогда второй раз не просим
                self.commands.put(("stop", None, None))
            elif starting and not (recording or stopping):
                # Запуск мог встать на скачивании модели: оно бросается, дольше ждать незачем
                self._model_cancel.set()
                # evaluate_js отсюда нельзя, но очередь для окна безопасна
                self.ready.put(("warn", "Окно закроется, как только запись запустится. "
                                        "Крестик ещё раз через 20 секунд закроет его сразу"))
            return False
        if time.monotonic() - self.close_asked_at > CLOSE_FORCE_SEC:
            return None
        return False

    def _gpu_installing(self) -> bool:
        with self._gpu_lock:
            return self._gpu["state"] == "installing"

    def shutdown(self) -> None:
        """Окно закрыто, процесс выходит (зовёт main). Дочерние дела бросаем, иначе
        невидимый процесс живёт дальше.

        Загрузка модели: интерпретатор при выходе ждёт рабочие потоки huggingface_hub, то
        есть до конца файла (до 3 ГБ), хотя окна уже нет. Установка CUDA: uv дочерний
        процесс и сам не умирает, а читать его вывод больше некому. Прерванная установка
        чинится при следующем запуске (bootstrap сверяет пакеты с RECORD), а кнопка
        предложит её снова."""
        # Сначала флаг, потом cancel: загрузка, начавшаяся между ними, видит флаг
        self.exiting.set()
        self._model_cancel.set()
        with self._gpu_lock:
            proc = self._gpu_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _release_devices_for_exit(self) -> None:
        """Закрывает саундчек и проверку микрофона перед выходом. Аудио-поток при
        этом evaluate_js не зовёт, так что поток окна может дождаться его без
        взаимной блокировки. Исключение: аудио-поток как раз грузит модель и шлёт
        статусы в окно, тогда мы упрёмся в таймаут, и окно закроется без ожидания."""
        if not (self.monitoring.is_set() or self.checking.is_set()):
            return
        closed = _Reply()
        self.commands.put(("release", None, closed))
        try:
            closed.get(timeout=5)
        except queue.Empty:
            pass

    def _finish_closing(self) -> None:
        """Крестик нажали, пока шла команда старта или остановки, и окно осталось
        открытым. Команда кончилась. Если запись при этом всё же началась, её
        останавливаем как положено (хвост, итог), и только потом закрываем окно."""
        if self.rec_kind is not None:
            try:
                self._do_stop()
            except Exception:
                pass  # окно всё равно закрывается, а сбой остановки уже виден в файле
        self._close_window()

    def _close_window(self) -> None:
        """Закрывает окно после остановки. Зовётся из аудио-потока: destroy()
        сама переходит в поток окна, и там on_closing пропустит закрытие."""
        self.close_ok.set()
        window = self.window
        if window is None:
            return
        try:
            window.destroy()
        except Exception:
            # Окно уже закрыто. А если нет, следующий щелчок по крестику его
            # закроет: close_ok поднят
            pass


def check_packages_later(rec: Recorder) -> None:
    """Сверяет весь venv с RECORD в фоне, пока окно уже работает.

    Целиком это 3–12 секунд, на старте столько не ждут. Найденное пишется в
    метку, и следующий запуск чинит эти пакеты ещё до открытия окна.
    """

    def work() -> None:
        broken = bootstrap.damaged_packages()
        if not broken:
            return
        bootstrap.mark_broken(broken)
        text = ("Повреждены файлы пакетов: " + ", ".join(broken)
                + ". Починю при следующем запуске")
        rec._js(f"toast({json.dumps(text, ensure_ascii=False)})")

    threading.Thread(target=work, daemon=True).start()


# ----------------------------------------------- второй слой: запреты в процессе
#
# Первый слой это сам мост (Api, API_NAMES): из окна достижимы только имена из
# списка. Этот слой вторичный и неполный. Он ловит запуск программ по событиям
# интерпретатора (os.system, os.exec*, os.spawn*, subprocess, os.startfile,
# _winapi.CreateProcess) и самый очевидный запуск через ctypes (событие
# ctypes.dlsym на WinExec, CreateProcess*, ShellExecute*).
#
# Чего он не делает. Тот, у кого в руках произвольный питон, обойдёт ctypes
# через GetProcAddress, порядковый номер функции или адрес в памяти. Вызовы
# .NET через pythonnet (System.Diagnostics.Process.Start и подобные) событий
# аудита не дают совсем. Так что хук страхует от простых цепочек, а граница
# безопасности здесь мост и CSP окна.
#
# Сама программа после старта запускает процессы ровно в двух случаях, оба по кнопке
# в окне и оба разрешены точным совпадением командной строки, рабочей папки (папка
# программы) и среды (см. _SpawnPermit):
#   1. uv по полному пути, вычисленному при старте, с командой установки библиотек
#      CUDA («Ускорить на видеокарте»);
#   2. перезапуск после этого: тот же интерпретатор и run.py («Перезапустить
#      программу»).
# Любая другая команда, в том числе тот же uv.exe с другими аргументами (uv умеет
# запускать что угодно) и любой другой exe, по-прежнему закрыта. Сверяется путь к uv,
# а не содержимое файла по нему. Хук не защищает от того, кто может сам вызвать Popen
# с разрешённой строкой из подходящего кадра или поменять os.environ: он страхует
# мост окна, а не процесс от собственного кода. Ссылки и папки
# открываются через os.startfile (webbrowser.open на Windows делает то же самое),
# а установка и починка пакетов при старте живут в run.py и bootstrap.py и работают
# до этого хука.
#
# Всё нужное хуку передаётся аргументами по умолчанию. При выходе интерпретатора
# глобальные имена модуля становятся None, а хук вызывается на каждое событие,
# в том числе из финализаторов: обращение к глобалу там падает с TypeError.

_PROCESS_EVENTS = frozenset({
    "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "subprocess.Popen",
    # Нижний уровень: через него стартует и multiprocessing, минуя subprocess
    "_winapi.CreateProcess",
})
_STARTFILE_EVENTS = frozenset({"os.startfile", "os.startfile/2"})
# Событие ctypes.dlsym приходит с (библиотека, имя функции) при первом
# обращении к функции из DLL. Ни одна из этих функций программе не нужна
_DLSYM_EVENTS = frozenset({"ctypes.dlsym", "ctypes.dlsym/handle"})
_DLSYM_DENIED = frozenset({
    "WinExec",
    "CreateProcessA", "CreateProcessW",
    "CreateProcessAsUserA", "CreateProcessAsUserW",
    "CreateProcessWithLogonW", "CreateProcessWithTokenW", "CreateProcessInternalW",
    "ShellExecuteA", "ShellExecuteW", "ShellExecuteExA", "ShellExecuteExW",
})
_WATCHED_EVENTS = _PROCESS_EVENTS | _STARTFILE_EVENTS | _DLSYM_EVENTS
_audit_installed = False


class BlockedAction(PermissionError):
    """Действие, которое запретил audit hook. Это OSError, чтобы код вроде
    перебора браузеров в webbrowser принял отказ как обычную неудачу."""


class _SpawnPermit:
    """Какие запуски процессов программа разрешает себе сама. Их ровно два:
    установка библиотек CUDA через uv и перезапуск (список команд задаёт main).

    Разрешает точное совпадение всей командной строки, а не имя файла: «uv.exe
    разрешён» открыл бы всё, что uv умеет запускать (uv run, uv tool run). Событие
    subprocess.Popen приходит с готовой командной строкой, по ней и сверяем (в
    Python 3.12 на Windows это строка после list2cmdline, в других версиях список,
    поэтому приводим к строке сами). Рядом сверяются рабочая папка (обе команды
    запускаются из папки программы: uv читает uv.toml из рабочей папки) и среда
    (только то, что есть в os.environ, плюс GPU_ENV_EXTRA: через PYTHONPATH или UV_*
    чужой индекс пакетов или sitecustomize подсунуть нельзя).

    Следом в том же потоке приходит _winapi.CreateProcess, но его командную строку из
    аргументов события не прочесть (CPython отдаёт там мусор вместо строки), поэтому
    оно пускается один раз и только сразу за разрешённым Popen этого же потока, и только
    если его зовёт сам subprocess.Popen._execute_child. Вызов _winapi.CreateProcess
    напрямую закрыт, и ручной sys.audit("subprocess.Popen", ...) перед ним ничего не
    открывает: кадр вызывающего не тот.

    Чего это не даёт. Тот, кто может сам вызвать Popen из подходящего кадра с
    разрешённой строкой, исполнит её, а тот, кто может менять os.environ или файлы по
    разрешённым путям (в том числе сам uv.exe: сверяется путь, а не содержимое), обойдёт
    и это. Граница безопасности здесь мост окна, а не хук.
    """

    def __init__(self) -> None:
        self.commands: frozenset[str] = frozenset()
        self.cwd = ""
        self.extra_env: dict[str, str] = {}
        self._local = threading.local()
        # На Windows процессы стартует этот метод; на другой системе его нет, и разрешать нечего
        self._spawner = getattr(subprocess.Popen, "_execute_child", None)

    def allow(self, *argvs, cwd: str = "", extra_env: dict | None = None) -> None:
        """Задаёт разрешённые команды (списки аргументов), рабочую папку для них и
        добавки к среде. Пустые команды пропускаются."""
        self.commands = frozenset(
            subprocess.list2cmdline([str(a) for a in argv]) for argv in argvs if argv
        )
        self.cwd = str(cwd)
        self.extra_env = dict(extra_env or {})

    def popen_ok(self, args: tuple) -> bool:
        """args события subprocess.Popen: (executable, args, cwd, env)."""
        self._local.after_popen = False
        try:
            executable, command, cwd, env = args[0], args[1], args[2], args[3]
            if executable is not None:
                return False  # своя подмена исполняемого файла обошла бы сверку командной строки
            if not self.cwd or cwd is None or str(cwd) != self.cwd:
                return False
            if env is not None:
                for key, value in dict(env).items():
                    if os.environ.get(key) != value and self.extra_env.get(key) != value:
                        return False
            if isinstance(command, (list, tuple)):
                command = subprocess.list2cmdline([str(a) for a in command])
            ok = isinstance(command, str) and command in self.commands
        except Exception:
            return False
        self._local.after_popen = ok
        return ok

    def create_ok(self) -> bool:
        """Событие _winapi.CreateProcess: пускаем, только если прямо перед ним был
        разрешённый Popen в этом потоке и зовёт его сам subprocess. Разрешение
        одноразовое."""
        ok = getattr(self._local, "after_popen", False)
        self._local.after_popen = False
        if not ok or self._spawner is None:
            return False
        try:
            # Кадры хука (этот файл) пропускаем, первый чужой должен быть _execute_child.
            # Глубину не считаем: хук могут обернуть, и счёт тихо разойдётся с жизнью
            here = type(self).create_ok.__code__.co_filename
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_filename == here:
                frame = frame.f_back
            return frame is not None and frame.f_code is self._spawner.__code__
        except Exception:
            return False


_PERMIT = _SpawnPermit()


def _startfile_denial(args: tuple, _fsdecode=os.fsdecode, _path=Path, _here=HERE) -> str | None:
    """Почему os.startfile не пускаем, или None, если можно.

    Можно: ссылка http(s); существующая папка внутри папки программы; файл
    .md внутри неё. Всё остальное, включая любые .exe и .bat рядом с
    программой, закрыто: ShellExecute запустил бы их как программу.
    """
    target = args[0] if args else None
    operation = args[1] if len(args) > 1 else None
    arguments = args[2] if len(args) > 2 else None
    if operation not in (None, "", "open") or arguments:
        return "os.startfile принимает только обычное открытие без аргументов"
    try:
        text = _fsdecode(target)
    except (TypeError, ValueError):
        return "os.startfile получил не путь"
    if text.lower().startswith(("http://", "https://")):
        return None
    try:
        path = _path(text)
        # Относительный путь ShellExecute отсчитывал бы от текущей папки, а
        # так выглядят и схемы вроде ms-settings:, поэтому только абсолютные
        if not path.is_absolute():
            return "os.startfile открывает только ссылки http(s) и пути в папке программы"
        path = path.resolve()
        if path.is_relative_to(_here) and (path.suffix.lower() == ".md" or path.is_dir()):
            return None
    except (OSError, ValueError):
        pass
    return "os.startfile открывает только ссылки http(s), папки и файлы .md в папке программы"


def _audit_denial(event: str, args: tuple, _process=_PROCESS_EVENTS,
                  _startfile=_STARTFILE_EVENTS, _startfile_check=_startfile_denial,
                  _dlsym=_DLSYM_EVENTS, _dlsym_denied=_DLSYM_DENIED,
                  _permit=_PERMIT) -> str | None:
    """Текст отказа для события аудита, или None, если событие разрешено."""
    if event in _process:
        # Единственное исключение: две команды самой программы, см. _SpawnPermit
        if event == "subprocess.Popen" and _permit.popen_ok(args):
            return None
        if event == "_winapi.CreateProcess" and _permit.create_ok():
            return None
        return f"Дубль не запускает сторонние программы ({event})"
    if event in _startfile:
        reason = _startfile_check(args)
        return None if reason is None else f"{reason} ({event})"
    if event in _dlsym:
        name = args[1] if len(args) > 1 else None
        if isinstance(name, bytes):
            name = name.decode("ascii", "replace")
        if name in _dlsym_denied:  # у порядкового номера name это int, в множестве его нет
            return f"Дубль не запускает сторонние программы (ctypes: {name})"
    return None


def _audit_hook(event: str, args: tuple, _watched=_WATCHED_EVENTS, _denial=_audit_denial,
                _blocked=BlockedAction, _sys=sys) -> None:
    # Вызывается на каждое событие интерпретатора, поэтому выход быстрый
    if event not in _watched:
        return
    reason = _denial(event, args)
    if reason is None:
        return
    try:
        print(f"[Дубль] заблокировано: {reason}", file=_sys.stderr)
    except (OSError, ValueError):
        pass  # под pythonw stderr нет, а закрытый поток не повод терять отказ
    raise _blocked(reason)


def install_audit_hook() -> None:
    """Ставит запреты на запуск процессов. Снять хук после этого нельзя."""
    global _audit_installed
    if not _audit_installed:
        sys.addaudithook(_audit_hook)
        _audit_installed = True


def use_own_taskbar_identity() -> None:
    """Своё имя процесса для панели задач Windows.

    Без него окно числится за pythonw.exe: Windows группирует его с другими
    питоновскими окнами и может показать значок интерпретатора вместо своего.
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass


def window_background() -> str:
    """Фон окна до загрузки страницы, в цвет темы Windows.

    Без этого светлая тема на долю секунды вспыхивает тёмным прямоугольником.
    Цвета те же, что --bg в web/style.css.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            light = winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 1
    except OSError:
        light = True
    return "#f7f7f4" if light else "#141518"


def main() -> None:
    rec: Recorder | None = None
    try:
        use_own_taskbar_identity()
        rec = Recorder()
        window = webview.create_window(
            "Дубль",
            str(HERE / "web" / "index.html"),
            # js_api=None нарочно: объект целиком окну не отдаём, см. Api
            js_api=None,
            width=1120,
            height=780,
            min_size=(860, 580),
            background_color=window_background(),
        )
        expose_api(window, Api(rec))
        rec.window = window
        # Две команды, которые хук пустит (установка CUDA и перезапуск), задаются до хука
        _PERMIT.allow(rec._gpu_argv, restart_argv(), cwd=str(HERE), extra_env=GPU_ENV_EXTRA)
        # Хук ставится не здесь, а на before_show. К этому моменту pywebview
        # уже импортировал pythonnet и WinForms: если .NET Framework не
        # поднялся, pywebview берёт coreclr, а clr_loader зовёт тогда
        # `dotnet --list-runtimes` через subprocess, и под хуком окно бы не
        # открылось. Страница при этом ещё не загружена: событие идёт
        # синхронно в потоке окна до цикла сообщений, а инициализация WebView2
        # и загрузка страницы идут через этот цикл и начнутся после хука
        window.events.before_show += install_audit_hook
        window.events.closing += rec.on_closing
        loaded_once = threading.Event()

        def on_loaded() -> None:
            if not loaded_once.is_set():
                loaded_once.set()
                check_packages_later(rec)
                # Модель готовится заранее, в своём потоке и без устройств: пока человек
                # решает, с чего начать, она уже качается
                rec.prepare_model()

        window.events.loaded += on_loaded
        # В справке pywebview иконка «только для GTK и QT», но на Windows
        # winforms её тоже берёт из этого параметра, нужен именно .ico
        webview.start(icon=str(ICON) if ICON.exists() else None)
    except Exception:
        (HERE / "ошибка.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        # Окно закрыто любым путём (крестик, перезапуск, сбой): скачивание и установка
        # не должны пережить его
        if rec is not None:
            rec.shutdown()


if __name__ == "__main__":
    main()
