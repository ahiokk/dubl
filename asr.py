"""Распознавание речи: Whisper на этом компьютере или сервер по API.

Снаружи все распознаватели одинаковые (см. Recognizer): подпись label и метод
transcribe, который берёт реплику и отдаёт строку. Окну и консоли всё равно,
где считается текст, на своей видеокарте, на процессоре или на чужом сервере.

Whisper на тишине и шуме любит выдумывать фразы из титров ютуба
(«Продолжение следует...», «Субтитры сделал...»). Здесь они отсекаются,
иначе расшифровка созвона обрастает репликами, которых никто не говорил.
"""

from __future__ import annotations

import codecs
import errno
import functools
import hashlib
import io
import ipaddress
import math
import re
import shutil
import threading
import time
import unicodedata
import wave
import zipfile
from pathlib import Path
from typing import Callable, NamedTuple, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
import numpy as np

import api_keys
import bootstrap  # CUDA DLL и путь к моделям, должен идти до faster_whisper

SAMPLE_RATE = 16000  # так режет capture.py, и так хочет Whisper

# Куски, по которым узнаётся выдуманное из титров. Именно куски, а не фразы
# целиком: модель тасует фамилии и порядок слов, и «Редактор субтитров
# А.Семкин Корректор А.Егорова» точному списку не равен.
#
# Реплика с таким куском выбрасывается, только если кусок занимает почти всю
# реплику (правило в _is_hallucination). «Мы делаем субтитры для видео к
# курсу» и «все права защищены за заказчиком, обсудим» это живая речь.
HALLUCINATION_MARKERS = (
    "субтитр",  # покрывает и «субтитры сделал», и «субтитры подогнал»
    "редактор субтитров",
    "корректор а.",
    "подписывайтесь на канал",
    "подписывайся на канал",
    "ставьте лайк",
    "спасибо за просмотр",
    "продолжение следует",
    "продолжение в следующей серии",
    "не забудьте подписаться",
    "все права защищены",
    "thanks for watching",
    "переведено на русский",
    "если вам понравилось видео",
    "с вами был",
)

# Устойчивые подписи, которые Whisper дописывает к титрам: фамилия автора, имя
# сообщества, готовая фраза целиком. Реплика с такой подписью выбрасывается
# при любом числе слов вокруг: «Субтитры делал Игорь Негода» это три слова вне
# маркера, а «Субтитры сделаны сообществом Amara.org» все четыре.
#
# Подписи узкие нарочно: «субтитры сделал», а не «субтитры» с любым глаголом,
# «игорь негода», а не «негода» (в «Из-за негоды перенесли рейс» нет титров),
# «подписаться на канал и поставить лайк», а не «поставить лайк» (под постом
# лайк ставят и живые люди). Но «Субтитры сделал Вася, проверь» в живом разговоре
# тоже уйдёт: Whisper выдумывает такое чаще, чем люди говорят.
HALLUCINATION_SIGNATURES = (
    "субтитры сделал",  # и «сделала», и «сделали»
    "субтитры сделан",  # «сделаны», «сделано»
    "субтитры делал",
    "субтитры создал",
    "субтитры создан",
    "субтитры подогнал",
    "субтитры добавил",
    "субтитры предоставлен",
    "amara.org",
    "сообществом amara",
    "игорь негода",
    "dimatorzok",
    "dima torzok",
    "торзок",
    "подписаться на канал и поставить лайк",
)

# Прощания из роликов, которые Whisper дописывает на хвосте тишины. Сами по
# себе это обычные слова на конце созвона: «Спасибо, до новых встреч!» скажет
# живой человек, и такая реплика остаётся. Режутся только одиночные (см.
# HALLUCINATION_EXACT) или рядом с настоящим маркером: «Спасибо за просмотр!
# До новых встреч!»
HALLUCINATION_FAREWELLS = (
    "до новых встреч",
    "на этом все спасибо за внимание",
)

# Столько слов вне маркеров ещё не мешает выбросить реплику.
# Известное ограничение: короткая живая фраза с маркером («Нужны субтитры?»,
# «А субтитры будут?», «Продолжение следует в понедельник») режется молча,
# отличить её от выдуманной по тексту нельзя. Порог поднимать не стоит: через
# него начнут проходить титры.
HALLUCINATION_MAX_EXTRA = 2

# Эти выбрасываются, только если составляют всю реплику: внутри живой фразы
# они совершенно нормальны
HALLUCINATION_EXACT = {
    "музыка",
    "аплодисменты",
    "смех",
    "конец",
    "титры",
    "you",
    "subscribe",
    "продолжение",
    *HALLUCINATION_FAREWELLS,
}


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    text = _PUNCT.sub("", text)
    return _SPACE.sub(" ", text).strip()


# Подписи в том виде, в каком их ищет _is_hallucination (после нормализации)
_SIGNATURES = tuple(_normalize(s) for s in HALLUCINATION_SIGNATURES)


def _cover(norm: str, spans: list[tuple[int, int]], phrases: tuple[str, ...]) -> set[int]:
    """Номера слов, которые накрыты любой из фраз.

    Совпадение растягивается до границ слов: маркер «субтитр» накрывает слово
    «субтитры» целиком, а «корректор а» слово «аегорова» (точка из «А.Егорова»
    при нормализации пропадает).
    """
    hit: set[int] = set()
    for phrase in phrases:
        m = _normalize(phrase)
        if not m:
            continue
        i = norm.find(m)
        while i != -1:
            j = i + len(m)
            hit.update(k for k, (a, b) in enumerate(spans) if a < j and b > i)
            i = norm.find(m, i + 1)
    return hit


def _is_hallucination(text: str) -> bool:
    """Выдумка ли это: пустой звук, реплика из титров или ролика.

    Подпись из HALLUCINATION_SIGNATURES режет реплику при любом числе слов.
    Маркер из HALLUCINATION_MARKERS режет, только когда слов вне маркеров не
    больше HALLUCINATION_MAX_EXTRA или маркеры занимают больше половины
    реплики. Так уходят «Субтитры сделал DimaTorzok» и «Продолжение
    следует...», а «Мы делаем субтитры для видео к курсу» остаётся.
    Прощания считаются маркерами, только если рядом есть настоящий маркер.
    """
    norm = _normalize(text)
    if not norm:
        return True
    if norm in HALLUCINATION_EXACT:
        return True
    if any(sig in norm for sig in _SIGNATURES):
        return True

    words = norm.split()
    spans, pos = [], 0
    for w in words:
        start = norm.index(w, pos)
        spans.append((start, start + len(w)))
        pos = start + len(w)

    strong = _cover(norm, spans, HALLUCINATION_MARKERS)
    if not strong:
        return False
    covered = len(strong | _cover(norm, spans, HALLUCINATION_FAREWELLS))
    extra = len(words) - covered
    return extra <= HALLUCINATION_MAX_EXTRA or covered > extra


def _is_looping(text: str) -> bool:
    """Ловит зацикливание вида «да да да да да» или повтор одной фразы подряд."""
    words = _normalize(text).split()
    if len(words) < 6:
        return False

    # одно и то же слово больше половины реплики
    if len(set(words)) <= max(2, len(words) // 6):
        return True

    # одна и та же короткая фраза повторяется 3+ раза подряд
    for size in (2, 3, 4):
        if len(words) < size * 3:
            continue
        chunks = [" ".join(words[i : i + size]) for i in range(0, len(words) - size + 1, size)]
        for i in range(len(chunks) - 2):
            if chunks[i] == chunks[i + 1] == chunks[i + 2]:
                return True
    return False


def _clean(pieces: list[tuple[str, float | None, float | None]]) -> str:
    """Склеивает куски ответа Whisper, выкидывая выдуманное.

    Кусок это (текст, no_speech_prob, avg_logprob). Локальная модель и часть
    серверов отдают обе оценки. Где их нет, там None, и остаются только
    проверки по самому тексту.
    """
    parts = []
    for text, no_speech, logprob in pieces:
        text = text.strip()
        if not text or _is_hallucination(text):
            continue
        # Модель сама говорит, что речи тут нет, но текст всё равно выдала.
        # Для коротких реплик это почти всегда шум, поэтому выбрасываем
        if no_speech is not None and no_speech > 0.75 and len(_normalize(text)) < 40:
            continue
        if logprob is not None and logprob < -1.1:
            continue
        if _is_looping(text):
            continue
        parts.append(text)

    result = " ".join(parts).strip()
    if not result or _is_hallucination(result):
        return ""
    return result


# ------------------------------------------------------------------ язык

LANGUAGES = {"ru": "Русский", "en": "Английский", "auto": "Определять самому"}

_GLOSSARY_LEAD = {
    "ru": "Разговор на русском языке. В речи встречаются: ",
    "en": "The conversation is in English. Terms used: ",
}


def local_glossary(path: Path) -> Path:
    """Личный глоссарий рядом с общим: glossary.txt → glossary.local.txt.

    В нём компания и имена под конкретную встречу. В git он не попадает,
    и обновление программы его не затирает.
    """
    return path.with_name(f"{path.stem}.local{path.suffix}")


def _decode_text(raw: bytes) -> str:
    """Байты текстового файла в строку: UTF-8, UTF-8 с BOM, UTF-16 по BOM, cp1251.

    Блокнот «ANSI» и PowerShell `>` дают именно cp1251 и UTF-16, а файл
    с кириллицей в другой кодировке не должен ронять распознавание.
    Не вышло: UnicodeDecodeError или ValueError, причину берёт вызывающий.
    """
    if raw.startswith(codecs.BOM_UTF8):
        text = raw.decode("utf-8-sig")
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16")  # BOM снимает сам
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251")
    # UTF-16 без BOM с кириллицей тоже проходит как «UTF-8», но с управляющими
    # знаками вместо букв. Настоящий текст их не содержит
    if any(c < " " and c not in "\t\r\n\f" for c in text):
        raise ValueError("в файле управляющие знаки или нулевые байты, это не текст")
    return text


def _read_terms(path: Path) -> tuple[list[str], str | None]:
    """Термины из файла и предупреждение, если файл пришлось пропустить."""
    if not path.exists():
        return [], None
    try:
        text = _decode_text(path.read_bytes())
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        return [], f"Глоссарий {path.name} пропущен: не удалось прочитать файл ({why})."
    except ValueError as exc:
        # UnicodeDecodeError это тоже ValueError
        why = (
            "не получилось определить кодировку" if isinstance(exc, UnicodeDecodeError)
            else str(exc)
        )
        return [], f"Глоссарий {path.name} пропущен: {why}. Пересохрани файл в UTF-8."
    terms = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return terms, None


class GlossaryResult(NamedTuple):
    prompt: str | None  # подсказка для модели или None, если терминов нет
    warnings: list[str]  # что пропущено и почему: окну есть что показать


# Предупреждения последнего load_glossary. Для тех, кто зовёт старую функцию
# и всё же хочет узнать, не пропущен ли личный глоссарий. Список меняется на месте,
# так что годится и `asr.LAST_GLOSSARY_WARNINGS`, и `from asr import ...`
LAST_GLOSSARY_WARNINGS: list[str] = []


def read_glossary(path: Path | None, language: str = "ru") -> GlossaryResult:
    """Собирает подсказку для модели и сообщает, что в глоссарии не удалось прочитать.

    Личные термины идут первыми. Подсказка ограничена по длине, и если она не
    влезает, обрезается конец списка (см. fit_prompt), а имена и компания остаются.
    Файл не в UTF-8 и не cp1251 пропускается целиком, второй файл при этом
    читается как обычно.
    """
    if path is None:
        return GlossaryResult(None, [])
    warnings, found = [], []
    for p in (local_glossary(path), path):
        terms, warning = _read_terms(p)
        found += terms
        if warning:
            warnings.append(warning)
    terms = list(dict.fromkeys(found))
    if not terms:
        return GlossaryResult(None, warnings)
    # Whisper смотрит на initial_prompt как на «предыдущий текст», поэтому оформляем
    # его обычным предложением, а не списком через запятую
    return GlossaryResult(_GLOSSARY_LEAD.get(language, "") + ", ".join(terms) + ".", warnings)


def load_glossary(path: Path | None, language: str = "ru") -> str | None:
    """Подсказка для модели (см. read_glossary). Предупреждения: LAST_GLOSSARY_WARNINGS."""
    result = read_glossary(path, language)
    LAST_GLOSSARY_WARNINGS[:] = result.warnings
    return result.prompt


def fit_prompt(prompt: str | None, fits) -> str | None:
    """Укорачивает подсказку по границе термина, пока fits(подсказка) не станет True.

    Режем с конца списка. Сам Whisper при переполнении режет с начала и
    выбрасывает вводную фразу и личные термины, то есть самое важное.
    """
    if not prompt or fits(prompt):
        return prompt
    body = prompt.rstrip(".")
    while ", " in body:
        body = body[: body.rfind(", ")]
        if fits(body + "."):
            return body + "."
    return None


# --------------------------------------------------------- общий контракт


# Имя, разделитель (пробел, «:», «=», возможно с кавычками, как в JSON) и значение.
# Значение кончается на пробеле или закрывающем знаке: скобка и кавычка вокруг
# заголовка в тексте ошибки остаются на месте
_SECRET = re.compile(
    r"\b(Bearer|Api-Key)(['\"]?[\s:=]+['\"]?)[^\s)\]'\"]+",
    re.IGNORECASE,
)


def mask_secrets(text: str, *keys: str | None) -> str:
    """Прячет ключи в строке, которая может попасть в окно или в лог.

    Заголовки целиком: `Bearer …`, `Api-Key …` и `X-API-Key: …`, как их
    печатают httpx и h11 в ошибках. Плюс сам ключ, если он известен: сервер
    вполне может вернуть его в тексте ответа.
    """
    for key in keys:
        key = (key or "").strip()
        if len(key) >= 3:
            text = text.replace(key, "***")
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class RecognizerError(Exception):
    """Ошибка распознавания, текст которой можно показать человеку как есть.

    Заголовки с ключами в тексте прячутся сразу при создании: ошибка уйдёт
    в окно и в журнал, а не только в консоль разработчика.
    """

    def __init__(self, message: object = "", *args: object) -> None:
        super().__init__(mask_secrets(str(message)), *args)


class ModelDownloadError(RecognizerError):
    """Модель не скачалась. Текст можно показать человеку как есть."""


class DownloadCancelled(ModelDownloadError):
    """Загрузку остановил сам человек (см. параметр cancel у ensure_model)."""


class Recognizer(Protocol):
    """Всё, что окну и консоли нужно от распознавателя."""

    label: str  # для людей: в шапку файла и в окно

    def transcribe(self, audio: np.ndarray) -> str:
        """Реплика float32 от -1 до 1, моно, 16 кГц → текст. Пустая строка, если речи нет."""
        ...

    def close(self) -> None: ...


# ------------------------------------------------------ на этом компьютере

# Что предлагать в окне. Размер это сколько скачается при первом запуске
LOCAL_MODELS = (
    {"id": "large-v3", "size": "3 ГБ", "note": "Лучшее качество. Для живой записи нужна видеокарта NVIDIA."},
    {
        "id": "large-v3-turbo",
        "size": "1.6 ГБ",
        "note": "По умолчанию на видеокарте. Почти как large-v3, но в разы быстрее.",
    },
    {"id": "medium", "size": "1.5 ГБ", "note": "Середина между качеством и скоростью."},
    {
        "id": "small",
        "size": "0.5 ГБ",
        "note": "По умолчанию на процессоре. Лёгкая, тянет и слабый компьютер, но путает термины.",
    },
)


# Без них CTranslate2 загрузит модель на видеокарту, а упадёт на первой реплике
CUDA_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")

# Сколько токенов подсказки Whisper берёт в контекст: половина окна в 448 минус один
PROMPT_TOKENS = 223


def _cuda_libs_found() -> bool:
    """Лежат ли библиотеки CUDA там, где их найдёт CTranslate2.

    bootstrap уже добавил в PATH папки из пакетов nvidia-*. Установщик ставит
    их только при видеокарте NVIDIA, а карту могли поставить позже.
    """
    import os

    dirs = [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    return all(any((d / dll).is_file() for d in dirs) for dll in CUDA_DLLS)


@functools.cache
def cuda_available() -> bool:
    """Есть ли видеокарта NVIDIA и библиотеки, чтобы CTranslate2 мог на ней считать."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() == 0:
            return False
    except Exception:
        return False
    return _cuda_libs_found()


# Без этих файлов модель из кэша не откроется или полезет в сеть за токенайзером
_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")

# Что качать: тот же список, что у faster_whisper.utils.download_model
_MODEL_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]


def _has_model_files(folder: Path) -> bool:
    return all((folder / name).is_file() for name in _MODEL_FILES) and any(
        f.is_file() for f in folder.glob("vocabulary.*")
    )


def _cached_folder(model_size: str, download_root: str | None = None) -> str | None:
    """Папка модели в кэше huggingface или None. В сеть не ходит.

    snapshot_download с local_files_only отдаёт папку снимка и тогда, когда
    скачивание оборвалось на половине, поэтому файлы проверяем сами. Любая
    ошибка значит «в кэше нет»: дальше всё идёт как при первом запуске.
    """
    try:
        if Path(model_size).is_dir():
            return str(model_size)  # путь к своей папке с моделью, качать нечего
        from faster_whisper.utils import download_model

        folder = Path(download_model(model_size, local_files_only=True, cache_dir=download_root))
        return str(folder) if _has_model_files(folder) else None
    except Exception:
        return None


def _model_cached(model_size: str, download_root: str | None = None) -> bool:
    """Лежит ли модель целиком в кэше huggingface (см. _cached_folder)."""
    return _cached_folder(model_size, download_root) is not None


# ---------------------------------------------- модель: зеркало, выбор, загрузка

# Запасная копия моделей small и large-v3-turbo в релизе GitHub: Hugging Face у
# части провайдеров встаёт на первых килобайтах. Размеры и SHA256 сняты с готовых
# архивов и сверены с тем, что насчитал GitHub. Внутри архива файлы модели прямо в
# корне, как на Hugging Face, без сжатия. «files» это размеры файлов после
# распаковки: по ним видно, что папка зеркала цела.
MIRROR_URL = "https://github.com/ahiokk/dubl/releases/download/models-v1/faster-whisper-{size}.zip"
MIRROR: dict[str, dict] = {
    "small": {
        "bytes": 486_212_794,
        "sha256": "f4071199e2b948df7b80d931beb5c9fb1f792234efcc5ad65a4330a43ee45175",
        "files": {
            "config.json": 2_370,
            "model.bin": 483_546_902,
            "tokenizer.json": 2_203_239,
            "vocabulary.txt": 459_861,
        },
    },
    "large-v3-turbo": {
        "bytes": 1_621_666_531,
        "sha256": "162c4508545d9efb6e62f2354cc2bcd11668fb7df5ddbdaca03569f5b67c2bab",
        "files": {
            "config.json": 2_263,
            "model.bin": 1_617_884_929,
            "preprocessor_config.json": 340,
            "tokenizer.json": 2_710_337,
            "vocabulary.json": 1_068_114,
        },
    },
}
_MIRROR_ALIASES = {"turbo": "large-v3-turbo"}

# Зеркальные модели лежат в models/mirror/<размер>/ рядом с программой, а не в
# HF_HOME: это обычная папка с файлами модели, которую WhisperModel открывает по пути
MIRROR_ROOT = bootstrap.MODELS_DIR / "mirror"

# Сколько ждём байт от Hugging Face, прежде чем перейти на зеркало. Для моделей
# без зеркала переходить некуда, там ждём дольше и только потом сдаёмся
HF_SILENCE_SEC = 20.0
HF_SILENCE_NO_MIRROR_SEC = 90.0
# Не чаще четырёх вызовов on_progress в секунду
PROGRESS_INTERVAL = 0.25
# Попыток скачать архив с GitHub (каждая следующая докачивает с места обрыва) и пауза между ними
MIRROR_TRIES = 3
MIRROR_RETRY_PAUSE = 2.0
# Кусок, которым читаем ответ сервера. Мелкий, чтобы на медленной связи прогресс шёл
# каждые секунды, а не раз в десять секунд
CHUNK = 128 << 10

ProgressCallback = Callable[[int, int, str], None]

# На CPU large-v3 не успевает за речью, поэтому среди готовых её не берём
_GPU_ORDER = ("large-v3-turbo", "large-v3", "medium", "small")
_CPU_ORDER = ("small", "medium", "large-v3-turbo")


def _mirror_key(model_size: str) -> str | None:
    key = _MIRROR_ALIASES.get(model_size, model_size)
    return key if key in MIRROR else None


def _mirror_folder(model_size: str) -> str | None:
    """Папка зеркальной копии, если она на месте целиком (по размерам файлов)."""
    key = _mirror_key(model_size)
    if key is None:
        return None
    folder = MIRROR_ROOT / key
    try:
        ok = all(
            (folder / name).stat().st_size == nbytes
            for name, nbytes in MIRROR[key]["files"].items()
        )
    except OSError:
        return None
    return str(folder) if ok else None


def _local_model(model_size: str, download_root: str | None = None) -> str | None:
    """Путь, по которому модель открывается без сети: кэш Hugging Face, потом зеркало."""
    return _cached_folder(model_size, download_root) or _mirror_folder(model_size)


def model_ready(size: str, download_root: str | None = None) -> bool:
    """Скачана ли модель: лежит в кэше Hugging Face или в папке зеркала. Без сети."""
    return _local_model(size, download_root) is not None


def default_local_model() -> str:
    """Какую локальную модель брать, если человек сам ничего не выбрал.

    Из уже скачанных берётся первая по порядку для этого железа, чтобы тем, у кого
    лежит large-v3, ничего не качалось заново. Нет ни одной: large-v3-turbo на
    видеокарте и small на процессоре. Выбор человека в настройках сильнее,
    его учитывает normalize_config.
    """
    on_gpu = cuda_available()
    for size in _GPU_ORDER if on_gpu else _CPU_ORDER:
        if model_ready(size):
            return size
    return "large-v3-turbo" if on_gpu else "small"


def _repo_id(model_size: str) -> str:
    """Репозиторий на Hugging Face по размеру модели, как это делает faster-whisper."""
    from faster_whisper.utils import _MODELS

    if "/" in model_size:
        return model_size
    repo = _MODELS.get(model_size)
    if repo is None:
        raise ValueError(
            f"Неизвестная модель «{model_size}». Есть такие: {', '.join(_MODELS)}"
        )
    return repo


class _Progress:
    """Передаёт ход загрузки вызывающему не чаще четырёх раз в секунду."""

    def __init__(self, callback: ProgressCallback | None) -> None:
        self.callback = callback
        self._last: float | None = None

    def _call(self, done: int, total: int, source: str) -> None:
        self._last = time.monotonic()
        try:
            self.callback(int(done), int(total), source)  # type: ignore[misc]
        except Exception:
            pass  # сбой в окне не должен ронять скачивание

    def send(self, done: int, total: int, source: str) -> None:
        if self.callback is None:
            return
        if self._last is not None and time.monotonic() - self._last < PROGRESS_INTERVAL:
            return
        self._call(done, total, source)

    def finish(self, total: int, source: str) -> None:
        """Последний вызов «скачано всё». Ждёт, если прошлый был совсем недавно."""
        if self.callback is None:
            return
        if self._last is not None:
            wait = PROGRESS_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
        self._call(total, total, source)


class _Sink:
    """Файл «в никуда» для полос tqdm: в консоль прогресс не пишем."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


class _Abort(Exception):
    """Бросается из полосы загрузки, когда её надо остановить."""


class _HfBars:
    """Байты, о которых snapshot_download сообщает через свои полосы tqdm.

    Полос с единицей B две: «transfer» (байты из сети) и «reconstruct» (байты,
    записанные на диск, у неё же известен общий размер). Различаем их по описанию
    и, если описание незнакомое, по порядку создания.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.transfer = None
        self.reconstruct = None
        self.cancelled = False

    def register(self, bar) -> None:
        desc = str(getattr(bar, "desc", "") or "").lower()
        with self.lock:
            if desc.startswith("reconstruct"):
                role = "reconstruct"
            elif desc.startswith("download"):
                role = "transfer"
            else:
                role = "transfer" if self.transfer is None else "reconstruct"
            setattr(self, role, bar)

    def snapshot(self) -> tuple[int, int]:
        """(скачано, всего) в байтах. Всего 0, пока полоса не узнала размеры."""
        with self.lock:
            rec, tr = self.reconstruct, self.transfer
            total = int(getattr(rec, "total", 0) or 0)
            # По сети байты приходят раньше, чем ложатся на диск: берём бо́льшее
            done = max(int(getattr(rec, "n", 0) or 0), int(getattr(tr, "n", 0) or 0))
        if total:
            done = min(done, total)
        return max(0, done), total


def _bar_class(bars: _HfBars):
    """Класс полосы для snapshot_download(tqdm_class=...): считает байты и молчит."""
    from tqdm import tqdm

    class ByteBar(tqdm):
        monitor_interval = 0  # фоновый поток tqdm здесь ни к чему

        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            # disable=True не годится: у выключенной полосы update ничего не считает
            kwargs["disable"] = False
            kwargs["file"] = _Sink()
            super().__init__(*args, **kwargs)
            if self.unit == "B":
                bars.register(self)

        def display(self, *args, **kwargs):  # рисовать нечего
            pass

        def update(self, n=1):
            if bars.cancelled:
                raise _Abort()
            with bars.lock:  # скачивают восемь потоков, а n += x не атомарно
                return super().update(n)

    return ByteBar


class _HfSilent(Exception):
    """Hugging Face не отдаёт байты. Причина короткая, для логов, не для окна."""


def _folder_bytes(folder: Path) -> int:
    """Сколько байт лежит в папке (без вложенных). Нет папки или она недоступна: 0."""
    try:
        return sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
    except OSError:
        return 0


def _abort_xet() -> None:
    """Останавливает передачу hf_xet, которая иначе дошла бы до конца в фоне.

    По умолчанию Hugging Face отдаёт эти модели через Xet, и загрузка идёт в
    Rust, а не в нашем потоке: исключение из полосы её не прерывает. Это та же
    функция, которой huggingface_hub отвечает на Ctrl+C. Она трогает только уже
    созданную сессию, а следующая загрузка создаёт новую.
    """
    try:
        from huggingface_hub.utils._xet import abort_xet_session

        abort_xet_session()
    except Exception:
        pass


def _download_from_hf(
    repo: str,
    download_root: str | None,
    report: _Progress,
    cancel: threading.Event | None,
    silence_sec: float,
) -> str:
    """Качает модель с Hugging Face, пока он отдаёт байты.

    snapshot_download идёт в своём потоке, а этот поток раз в 0,1 с смотрит на
    счётчики. Если за silence_sec не прибавилось ни байта (в том числе пока
    huggingface.co вообще не ответил), качать с него дальше нет смысла:
    поднимается _HfSilent, а полоса остановит оставшийся поток при следующем
    обновлении. Остальные сбои snapshot_download пробрасываются как есть.

    Живой считается и загрузка, у которой растут файлы в blobs/ кэша, даже если
    полосы молчат: так здоровую загрузку не бросим, если huggingface_hub
    когда-нибудь изменит то, как он сообщает о байтах.
    """
    import huggingface_hub
    from huggingface_hub import constants

    bars = _HfBars()
    box: dict = {}

    def work() -> None:
        kwargs = {"allow_patterns": _MODEL_PATTERNS, "tqdm_class": _bar_class(bars)}
        if download_root:
            kwargs["cache_dir"] = download_root
        try:
            box["path"] = huggingface_hub.snapshot_download(repo, **kwargs)
        except BaseException as exc:  # noqa: BLE001  разбирает вызывающий
            box["error"] = exc

    # По обычному HTTP huggingface_hub сообщает о байтах кусками по 10 МБ: на связи
    # в 100 КБ/с это раз в 100 секунд, и «тишина» была бы ложной. Через Xet, как
    # качают эти модели обычно, он сообщает чаще, но не везде Xet доступен.
    # Константа читается при каждой загрузке файла, так что хватает подмены на время работы
    blobs = Path(download_root or constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--")) / "blobs"
    chunk_was = constants.DOWNLOAD_CHUNK_SIZE
    constants.DOWNLOAD_CHUNK_SIZE = CHUNK
    thread = threading.Thread(target=work, name="model-download", daemon=True)
    thread.start()
    try:
        report.send(0, 0, "huggingface")
        seen, seen_disk, moved_at = -1, -1, time.monotonic()
        while thread.is_alive():
            thread.join(0.1)
            done, total = bars.snapshot()
            on_disk = _folder_bytes(blobs)
            now = time.monotonic()
            if done != seen or on_disk != seen_disk:
                seen, seen_disk, moved_at = done, on_disk, now
            report.send(done, total, "huggingface")
            if cancel is not None and cancel.is_set():
                bars.cancelled = True
                _abort_xet()
                raise DownloadCancelled("Загрузка модели остановлена.")
            if thread.is_alive() and now - moved_at > silence_sec:
                bars.cancelled = True
                _abort_xet()
                raise _HfSilent(f"ни одного байта за {silence_sec:.0f} с")
    finally:
        constants.DOWNLOAD_CHUNK_SIZE = chunk_was
    if "error" in box:
        raise box["error"]
    path = str(box.get("path") or "")
    if not path or not _has_model_files(Path(path)):
        raise _HfSilent("скачалось не всё")
    done, total = bars.snapshot()
    report.finish(max(done, total), "huggingface")
    return path


class _MirrorTrouble(Exception):
    """Сбой скачивания зеркала. text это то, что можно сказать человеку.

    retry: имеет смысл ещё одна попытка (обрыв, 5xx, битый архив). Для 404 нет.
    """

    def __init__(self, text: str, retry: bool = True) -> None:
        super().__init__(text)
        self.text = text
        self.retry = retry


def _gb(nbytes: float) -> str:
    return f"{nbytes / 1e9:.1f}".replace(".", ",") + " ГБ"


def _size_label(model_size: str) -> str:
    return next((m["size"] for m in LOCAL_MODELS if m["id"] == model_size), "")


def _disk_full(model_size: str, folder: Path | str, need: float = 0) -> ModelDownloadError:
    size = _gb(need) if need else _size_label(model_size)
    why = f" (нужно около {size})" if size else ""
    return ModelDownloadError(
        f"Не удалось скачать модель {model_size}: на диске не хватает места{why}. "
        f"Освободи место там, где лежат модели ({folder}), и попробуй ещё раз."
    )


def _net_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "сервер не ответил вовремя"
    if isinstance(exc, httpx.ConnectError):
        return "нет связи с GitHub"
    return f"обрыв связи ({type(exc).__name__})"


def _mirror_client() -> httpx.Client:
    # Редирект нужен: GitHub отдаёт файл релиза с другого адреса
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=15.0),
        headers={"User-Agent": "dubl-model-mirror"},
    )


def _file_digest(path: Path, cancel: threading.Event | None) -> "hashlib._Hash":
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Загрузка модели остановлена.")
    return digest


def _fetch_archive(
    client: httpx.Client,
    url: str,
    part: Path,
    spec: dict,
    report: _Progress,
    cancel: threading.Event | None,
) -> None:
    """Докачивает архив в part и сверяет SHA256. Битый файл удаляет.

    Если part остался от прошлой попытки, просит у GitHub остаток (Range). Сервер
    отказал в докачке: качает с нуля. Хэш считается на лету, поэтому проверка не
    стоит отдельного прохода по полутора гигабайтам. Оборванный архив остаётся на
    месте для следующей попытки.
    """
    total = spec["bytes"]
    # Архив скачан с нуля за один заход. Если он не сошёлся, виноват сервер или
    # прокси, и ещё два раза по полтора гигабайта ничего не изменят. Повтор нужен,
    # когда в файле был хвост от прошлой попытки: тогда мог испортиться он
    fresh = False
    for _ in range(2):  # второй заход нужен, только если докачка не вышла
        have = part.stat().st_size if part.exists() else 0
        if have > total:
            part.unlink()
            have = 0
        digest = _file_digest(part, cancel) if have else hashlib.sha256()
        report.send(have, total, "github")
        if have == total:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        with client.stream("GET", url, headers=headers) as resp:
            status = resp.status_code
            resumed = status == 206 and str(resp.headers.get("content-range", "")).startswith(
                f"bytes {have}-"
            )
            if have and (status == 416 or (status == 206 and not resumed)):
                part.unlink(missing_ok=True)  # хвост не принят: начнём сначала
                continue
            if status not in (200, 206):
                raise _MirrorTrouble(
                    f"ответ {status}" + (": такого файла в релизе нет" if status in (403, 404) else ""),
                    retry=status not in (403, 404),
                )
            if have and status == 200:  # докачку сервер не поддержал и прислал файл целиком
                have, digest = 0, hashlib.sha256()
            fresh = have == 0
            with part.open("ab" if have else "wb") as out:
                for chunk in resp.iter_bytes(CHUNK):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled("Загрузка модели остановлена.")
                    if have + len(chunk) > total:
                        out.close()
                        part.unlink(missing_ok=True)
                        raise _MirrorTrouble(
                            "архив оказался больше, чем должен быть, я его удалил", retry=not fresh
                        )
                    out.write(chunk)
                    digest.update(chunk)
                    have += len(chunk)
                    report.send(have, total, "github")
        break
    if have < total:
        raise _MirrorTrouble("соединение оборвалось, файл скачан не весь")
    if digest.hexdigest() != spec["sha256"]:
        part.unlink(missing_ok=True)
        raise _MirrorTrouble(
            "архив скачался повреждённым (контрольная сумма не сошлась), я его удалил",
            retry=not fresh,
        )


def _unpack_mirror(part: Path, key: str, spec: dict) -> Path:
    """Распаковывает проверенный архив в MIRROR_ROOT/<размер>/ и удаляет архив."""
    final = MIRROR_ROOT / key
    tmp = MIRROR_ROOT / f"{key}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with zipfile.ZipFile(part) as zf:
            infos = {info.filename: info for info in zf.infolist()}
            for name, nbytes in spec["files"].items():
                info = infos.get(name)  # берём только свои файлы и только из корня
                if info is None or info.file_size != nbytes:
                    raise zipfile.BadZipFile(name)
                with zf.open(info) as src, (tmp / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        shutil.rmtree(final, ignore_errors=True)
        tmp.replace(final)
    except zipfile.BadZipFile:
        shutil.rmtree(tmp, ignore_errors=True)
        part.unlink(missing_ok=True)
        raise _MirrorTrouble("в архиве не те файлы, я его удалил", retry=False) from None
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    part.unlink(missing_ok=True)
    return final


def _download_from_mirror(
    model_size: str,
    report: _Progress,
    cancel: threading.Event | None,
) -> str:
    """Качает архив с GitHub Releases и распаковывает его. Путь к папке модели."""
    key = _mirror_key(model_size)
    assert key is not None
    spec = MIRROR[key]
    part = MIRROR_ROOT / f"faster-whisper-{key}.zip.part"
    url = MIRROR_URL.format(size=key)

    def fail(why: str) -> ModelDownloadError:
        return ModelDownloadError(
            f"Не удалось скачать модель {model_size}. Hugging Face не отвечает, "
            f"запасная копия на GitHub тоже ({why}). Проверь интернет или VPN и попробуй ещё раз."
        )

    try:
        MIRROR_ROOT.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        # Архив и распакованные файлы лежат рядом, пока не удалён архив
        need = max(0, spec["bytes"] - have) + sum(spec["files"].values()) + (100 << 20)
        if shutil.disk_usage(MIRROR_ROOT).free < need:
            raise _disk_full(model_size, MIRROR_ROOT, need)
        why = "нет связи"
        with _mirror_client() as client:
            for attempt in range(MIRROR_TRIES):
                if attempt:
                    time.sleep(MIRROR_RETRY_PAUSE)
                try:
                    _fetch_archive(client, url, part, spec, report, cancel)
                    break
                except _MirrorTrouble as exc:
                    why = exc.text
                    if not exc.retry:
                        raise fail(why) from None
                except httpx.HTTPError as exc:
                    why = _net_reason(exc)
            else:
                raise fail(why)
        folder = _unpack_mirror(part, key, spec)
    except ModelDownloadError:
        raise
    except _MirrorTrouble as exc:
        raise fail(exc.text) from None
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise _disk_full(model_size, MIRROR_ROOT) from None
        raise ModelDownloadError(
            f"Не удалось сохранить модель {model_size} в папку {MIRROR_ROOT}: "
            f"{exc.strerror or type(exc).__name__}."
        ) from None
    except Exception as exc:  # страховка: наружу идёт только ModelDownloadError
        raise fail(f"сбой {type(exc).__name__}") from None
    report.finish(spec["bytes"], "github")
    return str(folder)


def _hf_cache_dir() -> str:
    try:
        from huggingface_hub import constants

        return str(constants.HF_HUB_CACHE)
    except Exception:
        return str(bootstrap.MODELS_DIR)


def _hf_trouble(exc: BaseException, folder: Path | str) -> tuple[bool, str]:
    """(это сеть?, короткая причина) для сбоя загрузки с Hugging Face.

    Нужна тем моделям, у которых нет зеркала: там человеку надо сказать, что именно
    не так, а не только «Hugging Face не отвечает».
    """
    if isinstance(exc, _HfSilent):
        return True, str(exc)
    if isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
    ):
        return False, f"нет прав на запись в папку {folder}"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return True, "сервер не ответил вовремя"
    if isinstance(exc, (httpx.ConnectError, ConnectionError)):
        return True, "нет связи"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status >= 500:
            return True, f"сервер ответил ошибкой {status}"
        return False, f"Hugging Face не отдал файлы модели (ответ {status})"
    if exc.__cause__ is not None and exc.__cause__ is not exc:
        # LocalEntryNotFoundError и подобные: настоящая причина лежит в цепочке
        return _hf_trouble(exc.__cause__, folder)
    if isinstance(exc, httpx.HTTPError):
        return True, f"ошибка связи ({type(exc).__name__})"
    if isinstance(exc, OSError):
        return False, f"не удалось записать файлы в папку {folder} ({exc.strerror or type(exc).__name__})"
    return False, f"сбой {type(exc).__name__}"


def _drop_orphans(model_size: str) -> None:
    """Убирает следы прерванной загрузки зеркала для модели, которая уже готова."""
    key = _mirror_key(model_size)
    if key is None:
        return
    try:
        (MIRROR_ROOT / f"faster-whisper-{key}.zip.part").unlink(missing_ok=True)
        shutil.rmtree(MIRROR_ROOT / f"{key}.tmp", ignore_errors=True)
    except OSError:
        pass


def ensure_model(
    size: str,
    on_progress: ProgressCallback | None = None,
    download_root: str | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Делает так, чтобы модель была на диске, и отдаёт то, что понимает WhisperModel.

    Уже скачанную (кэш Hugging Face или папка зеркала) возвращает сразу, без сети.
    Иначе качает с Hugging Face, а если он не дал ни байта за HF_SILENCE_SEC секунд
    или упал по сети, берёт архив из GitHub Releases (есть для small и
    large-v3-turbo). Возвращает путь к папке с моделью.

    on_progress(скачано_байт, всего_байт, источник) зовётся не чаще четырёх раз в
    секунду из потока, который вызвал ensure_model. Источник это "huggingface" или
    "github". Всего 0 значит «пока неизвестно». Последний вызов приходит с
    done == total. cancel это threading.Event: если его выставить, загрузка
    прервётся (DownloadCancelled), а недокачанный архив останется для докачки.

    Все сбои, которые можно показать человеку, это ModelDownloadError (он же
    RecognizerError). Неизвестное имя модели даёт ValueError, как у faster-whisper.
    После успеха убирает осиротевшие остатки прерванной загрузки зеркала этой модели.
    """
    path = _ensure_model(size, on_progress, download_root, cancel)
    _drop_orphans(size)
    return path


def _ensure_model(
    size: str,
    on_progress: ProgressCallback | None,
    download_root: str | None,
    cancel: threading.Event | None,
) -> str:
    found = _local_model(size, download_root)
    if found:
        return found
    repo = _repo_id(size)
    from huggingface_hub import constants

    if constants.HF_HUB_OFFLINE:
        # Человек сам отключил сеть для моделей: зеркало на GitHub тоже сеть
        raise ModelDownloadError(
            f"Модель {size} не скачана, а сеть для моделей отключена переменной HF_HUB_OFFLINE. "
            "Убери её или положи модель в кэш вручную."
        )
    report = _Progress(on_progress)
    has_mirror = _mirror_key(size) is not None
    try:
        return _download_from_hf(
            repo,
            download_root,
            report,
            cancel,
            HF_SILENCE_SEC if has_mirror else HF_SILENCE_NO_MIRROR_SEC,
        )
    except ModelDownloadError:
        raise
    except Exception as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise _disk_full(size, download_root or _hf_cache_dir()) from None
        if has_mirror:
            return _download_from_mirror(size, report, cancel)
        network, why = _hf_trouble(exc, download_root or _hf_cache_dir())
        if not network:
            raise ModelDownloadError(f"Не удалось скачать модель {size}: {why}.") from None
        raise ModelDownloadError(
            f"Не удалось скачать модель {size}: Hugging Face не отвечает ({why}), а запасной "
            "копии для неё нет. Проверь интернет или VPN. Для small и large-v3-turbo запасная "
            "копия на GitHub есть, можно выбрать одну из них."
        ) from None


def _open_model(whisper_model, model_size: str, download_root: str | None, **kwargs):
    """Открывает модель; скачанную берёт из кэша без запроса в сеть.

    Без local_files_only faster-whisper при каждой загрузке спрашивает
    huggingface.co, нет ли новой ревизии, и молча докачивает её. Возвращает
    модель и признак «открыта из кэша».
    """
    if _model_cached(model_size, download_root):
        try:
            model = whisper_model(
                model_size, download_root=download_root, local_files_only=True, **kwargs
            )
            return model, True
        except FileNotFoundError:
            # Кэш оказался неполным (LocalEntryNotFoundError это FileNotFoundError).
            # Докачиваем, как при первом запуске. Остальные ошибки, например
            # float16 без видеокарты, повтор из сети не лечит
            pass
    return whisper_model(model_size, download_root=download_root, **kwargs), False


class LocalWhisper:
    """faster-whisper на этом компьютере: на видеокарте, если она есть, иначе на процессоре."""

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "auto",
        language: str = "ru",
        glossary: str | None = None,
        download_root: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """on_progress(скачано, всего, источник) получает ход загрузки модели (см. ensure_model).

        Сам LocalWhisper прогресс не показывает: если модель не скачана, она
        докачивается здесь, и кто хочет видеть ход, передаёт on_progress.
        """
        # Импорт здесь, а не в начале файла: тем, кто распознаёт по API,
        # незачем грузить CTranslate2 и ждать CUDA
        from faster_whisper import WhisperModel

        if device == "auto":
            device = "cuda" if cuda_available() else "cpu"
        # На видеокарте float16 быстрее всего: int8_float16 на RTX 5060 Ti давал
        # ×8 к реальному времени против ×10. На процессоре float16 нет, там int8
        compute_type = "float16" if device == "cuda" else "int8"
        # Была ли модель на диске до нас: от этого зависит, понадобилась ли сеть
        self.from_cache = model_ready(model_size, download_root)
        source = ensure_model(model_size, on_progress, download_root)
        self.model, _ = _open_model(
            WhisperModel, source, download_root,
            device=device, compute_type=compute_type,
        )
        self.language = None if language == "auto" else language
        tok = self.model.hf_tokenizer
        self.glossary = fit_prompt(
            glossary,
            # Так подсказку кодирует сам faster-whisper
            lambda s: len(tok.encode(" " + s.strip(), add_special_tokens=False).ids) <= PROMPT_TOKENS,
        )
        # Перебор пяти гипотез на процессоре слишком дорог для живой записи,
        # там берём жадный разбор: чуть хуже на сложных словах, зато успевает
        self.beam = 5 if device == "cuda" else 1
        where = "видеокарта" if device == "cuda" else "процессор"
        self.label = f"faster-whisper {model_size}, {where}"

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam,
            best_of=self.beam,
            # Без этого модель тянет контекст из прошлой реплики и уходит в зацикливание
            condition_on_previous_text=False,
            initial_prompt=self.glossary,
            # Повторный проход на большей температуре, если первый вышел бракованным
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            # Второй рубеж после нашего VAD: срезает остатки тишины внутри реплики
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
            word_timestamps=False,
        )
        return _clean([(s.text, s.no_speech_prob, s.avg_logprob) for s in segments])

    def close(self) -> None:
        pass


# ------------------------------------------------------------- по API

# Реплика не длиннее 15 секунд, нормальный сервер отвечает за пару секунд.
# Ждать дольше 30 нет смысла: разговор за это время уедет далеко
TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# Подсказка серверу ограничена 224 токенами, так у OpenAI и Groq. Русское слово
# это два-три токена, поэтому режем с запасом, по границе термина
PROMPT_CHARS = 400


def _pcm16(audio: np.ndarray) -> bytes:
    """float32 от -1 до 1 в 16-битный PCM: такой звук понимает любой сервер."""
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _wav(audio: np.ndarray) -> bytes:
    """WAV в памяти, без временных файлов на диске."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(_pcm16(audio))
    return buf.getvalue()


def _short_prompt(glossary: str | None) -> str | None:
    # Токенизатора сервера у нас нет, поэтому меряем знаками
    return fit_prompt(glossary, lambda s: len(s) <= PROMPT_CHARS)


def _checked_key(service: str, key: str) -> str:
    """Ключ в том виде, в каком его можно положить в заголовок, иначе RecognizerError.

    Ключ с кириллицей или переводом строки отвергает h11, и в тексте его
    ошибки напечатан весь заголовок. Лучше сказать про это самим и без ключа.
    """
    key = (key or "").strip()
    if not key:
        return ""
    try:
        return api_keys.check_format(key, min_len=1)
    except api_keys.KeyStoreError as exc:
        raise RecognizerError(f"{service}: {exc}") from None


def _explain(service: str, status: int, detail: str, key: str = "") -> str:
    """Код ответа сервера → что случилось, человеческими словами.

    key нужен, чтобы спрятать его в тексте ответа: сервер может вернуть ключ,
    а эта строка уйдёт в окно.
    """
    if status in (401, 403):
        return f"{service}: ключ не подходит (ответ {status})"
    if status == 404:
        return f"{service}: нет такого адреса или модели (ответ 404)"
    if status == 413:
        return f"{service}: реплика слишком большая для сервера"
    if status == 429:
        return f"{service}: кончился лимит запросов, проверь тариф"
    if status >= 500:
        return f"{service}: сервер не справился (ответ {status})"
    detail = mask_secrets(" ".join(detail.split()), key)[:160]
    return f"{service}: ответ {status}. {detail}".strip()


def _error_text(value: object) -> str:
    """Текст ошибки из тела ответа: строка, {"message": ...} или что угодно."""
    if isinstance(value, dict):
        value = value.get("message") or value.get("error_message") or value.get("type") or value
    return " ".join(str(value).split())


def _error_detail(resp: httpx.Response) -> str:
    """Что сервер написал про ошибку: поле message, а не сырой JSON."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text
    if isinstance(data, dict):
        for field in ("error", "error_message", "message", "detail"):
            if data.get(field):
                return _error_text(data[field])
    return resp.text


def _json_object(service: str, resp: httpx.Response, key: str = "") -> dict:
    """Тело успешного ответа как объект JSON, иначе RecognizerError.

    Бывает и 200 с {"error": ...}, и список, и не-JSON: прокси и самодельные
    серверы так делают. Принять это за ответ без речи нельзя, проверка связи
    скажет «сервер отвечает», а в записи будут дыры.
    """
    try:
        data = resp.json()
    except ValueError:
        raise RecognizerError(f"{service}: ответ не похож на JSON") from None
    if not isinstance(data, dict):
        raise RecognizerError(f"{service}: ответ в неожиданном виде, ждали объект JSON")
    problem = data.get("error") or data.get("error_message") or data.get("error_code")
    if problem:
        detail = mask_secrets(_error_text(data.get("error_message") or problem), key)[:160]
        raise RecognizerError(f"{service}: сервер вернул ошибку. {detail}".strip())
    return data


def _score(value: object) -> float | None:
    """Оценка уверенности из ответа сервера: число или None, если пришло другое."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _is_local_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_server_url(url: str) -> dict:
    """Проверяет адрес «своего сервера», который ввёл человек.

    Пускает только http и https. Адрес с http:// разрешён (свой сервер в
    соседней комнате чаще без сертификата), но если сервер не на этом же
    компьютере, ключ и звук пойдут по сети открытым текстом. Об этом warning.

    Ответ, как у остальных методов окна:
      {"error": "что не так"}
      {"ok": True, "url": "адрес без лишнего / в конце", "warning": "" или "текст предупреждения"}
    """
    raw = (url or "").strip()
    if not raw:
        return {"error": "Не указан адрес сервера"}
    if any(c.isspace() or ord(c) < 32 for c in raw):
        return {"error": "В адресе не должно быть пробелов и переносов строк"}
    try:
        parts = urlsplit(raw)
        _ = parts.port  # кривой порт даёт ValueError только при обращении
    except ValueError:
        return {"error": "Адрес не разобрать: проверь порт и квадратные скобки"}
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return {"error": "Адрес сервера должен начинаться с http:// или https://"}
    host = parts.hostname
    if not host:
        return {"error": "В адресе нет имени сервера"}
    if "@" in parts.netloc:
        return {"error": "В адресе не должно быть логина и пароля: ключ вводится отдельно"}
    if parts.query or parts.fragment:
        return {"error": "Адрес без «?» и «#»: путь /audio/transcriptions программа допишет сама"}

    warning = ""
    if scheme == "http" and not _is_local_host(host):
        warning = (
            "Адрес начинается с http://, а не с https://: ключ и звук уйдут открытым текстом. "
            "Это безопасно только в сети, которой ты доверяешь."
        )
    clean = urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return {"ok": True, "url": clean, "warning": warning}


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(5.0, max(1.0, float(resp.headers.get("retry-after", 1))))
    except ValueError:
        return 1.0


def _post(client: httpx.Client, service: str, url: str, **kwargs) -> httpx.Response:
    """POST с одним повтором, если связь мигнула или сервер перегружен.

    По таймауту не повторяем: это ещё тридцать секунд тишины в ленте,
    а разговор тем временем идёт дальше.
    """
    for attempt in range(2):
        try:
            resp = client.post(url, **kwargs)
        except httpx.TimeoutException:
            raise RecognizerError(f"{service}: сервер не ответил за {TIMEOUT.read:.0f} с") from None
        except httpx.TransportError as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RecognizerError(f"{service}: нет связи с сервером ({type(exc).__name__})") from None
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(_retry_after(resp))
            continue
        return resp
    raise RecognizerError(f"{service}: нет связи с сервером")


class OpenAICompatible:
    """Любой сервер с OpenAI-совместимым /audio/transcriptions.

    Один класс на всех, у кого такой адрес: OpenAI, Groq, свой сервер
    (например speaches поверх того же faster-whisper в соседней комнате).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        language: str = "ru",
        glossary: str | None = None,
        service: str = "Сервер",
    ) -> None:
        if not base_url:
            raise RecognizerError(f"{service}: не указан адрес сервера")
        if not model:
            raise RecognizerError(f"{service}: не указана модель")
        checked = check_server_url(base_url)
        if "error" in checked:
            raise RecognizerError(f"{service}: {checked['error']}")
        api_key = _checked_key(service, api_key)
        self.url = checked["url"] + "/audio/transcriptions"
        self.model = model
        self.language = None if language == "auto" else language
        self.prompt = _short_prompt(glossary)
        self.service = service
        self._key = api_key
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(timeout=TIMEOUT, headers=headers)
        # verbose_json даёт по каждому куску те же оценки, что и локальная модель,
        # и по ним работает отсев выдуманного. Умеют его не все: у OpenAI модели
        # gpt-4o-*-transcribe отдают только json. Тогда откатываемся на текст
        self.verbose = True
        self.label = f"{service} {model}, по API"

    def _send(self, audio: np.ndarray) -> httpx.Response:
        data = {
            "model": self.model,
            "response_format": "verbose_json" if self.verbose else "json",
            "temperature": "0",
        }
        if self.language:
            data["language"] = self.language
        if self.prompt:
            data["prompt"] = self.prompt
        files = {"file": ("speech.wav", _wav(audio), "audio/wav")}
        return _post(self.client, self.service, self.url, data=data, files=files)

    def transcribe(self, audio: np.ndarray) -> str:
        try:
            return self._transcribe(audio)
        except RecognizerError:
            raise
        except Exception as exc:
            # Что-то не из httpx: текст может нести заголовок с ключом
            raise RecognizerError(
                mask_secrets(f"{self.service}: сбой ({type(exc).__name__}: {exc})", self._key)
            ) from None

    def _transcribe(self, audio: np.ndarray) -> str:
        resp = self._send(audio)
        if resp.status_code == 400 and self.verbose and "response_format" in resp.text:
            self.verbose = False
            resp = self._send(audio)
        if resp.status_code != 200:
            raise RecognizerError(
                _explain(self.service, resp.status_code, _error_detail(resp), self._key)
            )
        data = _json_object(self.service, resp, self._key)

        segments = data.get("segments")
        if segments is not None and not isinstance(segments, list):
            raise RecognizerError(f"{self.service}: ответ в неожиданном виде (segments не список)")
        if segments:
            pieces = []
            for s in segments:
                if not isinstance(s, dict):
                    raise RecognizerError(f"{self.service}: ответ в неожиданном виде (кусок не объект)")
                if isinstance(s.get("text"), str):
                    pieces.append((s["text"], _score(s.get("no_speech_prob")), _score(s.get("avg_logprob"))))
            return _clean(pieces)
        text = data.get("text")
        if isinstance(text, str):
            return _clean([(text, None, None)])
        if isinstance(segments, list) and "text" not in data:
            return ""  # verbose_json на тишине: пустой список кусков
        raise RecognizerError(f"{self.service}: в ответе нет текста распознавания")

    def close(self) -> None:
        self.client.close()


class YandexSpeechKit:
    """Яндекс SpeechKit, синхронное распознавание коротких аудио (API v1).

    Работает из России без VPN. Принимает сырой PCM без заголовка WAV,
    кусок до 30 секунд и до 1 МБ. Реплики capture.py режет по 15 секунд,
    так что влезают с запасом, но длинное всё равно делим: лимит нарезки
    могут поднять, а ошибка от сервера будет непонятной.
    """

    URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
    SERVICE = "Яндекс SpeechKit"
    MAX_SEC = 29
    # Автоопределение языка в v1 не берём, без указания он распознаёт русский
    LANG = {"ru": "ru-RU", "en": "en-US"}

    def __init__(self, api_key: str, language: str = "ru", folder_id: str = "",
                 topic: str = "general") -> None:
        if not api_key:
            raise RecognizerError(f"{self.SERVICE}: нужен API-ключ, вставь его в настройках")
        api_key = _checked_key(self.SERVICE, api_key)
        self._key = api_key
        self.params = {
            "lang": self.LANG.get(language, "ru-RU"),
            "topic": topic or "general",
            "format": "lpcm",
            "sampleRateHertz": str(SAMPLE_RATE),
        }
        # Каталог нужен только для ключа обычного пользователя,
        # с ключом сервисного аккаунта его не передают
        if folder_id:
            self.params["folderId"] = folder_id
        self.client = httpx.Client(timeout=TIMEOUT, headers={"Authorization": f"Api-Key {api_key}"})
        self.label = f"{self.SERVICE} {self.params['topic']}, по API"

    def transcribe(self, audio: np.ndarray) -> str:
        try:
            return self._transcribe(audio)
        except RecognizerError:
            raise
        except Exception as exc:
            raise RecognizerError(
                mask_secrets(f"{self.SERVICE}: сбой ({type(exc).__name__}: {exc})", self._key)
            ) from None

    def _transcribe(self, audio: np.ndarray) -> str:
        step = self.MAX_SEC * SAMPLE_RATE
        parts = []
        for i in range(0, len(audio), step):
            resp = _post(self.client, self.SERVICE, self.URL,
                         params=self.params, content=_pcm16(audio[i : i + step]))
            if resp.status_code != 200:
                raise RecognizerError(
                    _explain(self.SERVICE, resp.status_code, _error_detail(resp), self._key)
                )
            result = _json_object(self.SERVICE, resp, self._key).get("result")
            if not isinstance(result, str):
                raise RecognizerError(f"{self.SERVICE}: в ответе нет результата распознавания")
            parts.append(result.strip())
        # Фильтры галлюцинаций настроены на Whisper. SpeechKit титров из ютуба
        # не выдумывает, а живое «продолжение следует» резать незачем
        return " ".join(p for p in parts if p)

    def close(self) -> None:
        self.client.close()


# Готовые варианты для окна настроек. kind говорит, каким классом с ним разговаривать
PROVIDERS = {
    "groq": {
        "title": "Groq",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["whisper-large-v3-turbo", "whisper-large-v3"],
        "keys_url": "https://console.groq.com/keys",
        # Запрос короче 10 секунд Groq считает за 10, а реплики capture.py режет
        # от трети секунды до 15. Платишь больше, чем длится разговор
        "note": "Whisper large-v3-turbo или large-v3 на их серверах. Из России может понадобиться VPN.",
    },
    "openai": {
        "title": "OpenAI",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
        "keys_url": "https://platform.openai.com/api-keys",
        # У gpt-4o-*-transcribe нет оценок уверенности, мусор отсеивается только по тексту
        "note": "Из России работает только через VPN.",
    },
    "yandex": {
        "title": "Яндекс SpeechKit",
        "kind": "yandex",
        "base_url": YandexSpeechKit.URL,
        "models": ["general"],
        "keys_url": "https://console.yandex.cloud/",
        # Ключ сервисного аккаунта с ролью ai.speechkit-stt.user. Подсказок
        # в v1 нет, поэтому глоссарий здесь не действует
        "note": "Работает из России без VPN. Нужен ключ сервисного аккаунта.",
    },
    "custom": {
        "title": "Свой сервер",
        "kind": "openai",
        "base_url": "",
        "models": [],
        "keys_url": "",
        "note": "Любой сервер с OpenAI-совместимым API, например speaches.",
    },
}


def default_config() -> dict:
    """Настройки распознавания для первого запуска: под то железо, что есть,
    и под то, что уже скачано (см. default_local_model)."""
    return {
        "режим": "local",
        "модель": default_local_model(),
        "язык": "ru",
        "провайдер": "groq",
        "модель_api": "",
        "адрес": "",
        "каталог": "",
    }


def normalize_config(raw: dict | None) -> dict:
    """Чистит то, что пришло из окна или из файла: лишние ключи выкидываются,
    неизвестные значения заменяются значениями по умолчанию."""
    cfg = default_config()
    raw = raw or {}
    if raw.get("режим") in ("local", "api"):
        cfg["режим"] = raw["режим"]
    if raw.get("модель") in {m["id"] for m in LOCAL_MODELS}:
        cfg["модель"] = raw["модель"]
    if raw.get("язык") in LANGUAGES:
        cfg["язык"] = raw["язык"]
    if raw.get("провайдер") in PROVIDERS:
        cfg["провайдер"] = raw["провайдер"]
    for key in ("модель_api", "адрес", "каталог"):
        cfg[key] = str(raw.get(key) or "").strip()
    return cfg


def describe(cfg: dict) -> str:
    """Коротко, чем сейчас распознаём: для верхней полосы окна."""
    if cfg.get("режим") == "api":
        p = PROVIDERS.get(cfg.get("провайдер"), PROVIDERS["custom"])
        model = cfg.get("модель_api") or (p["models"][0] if p["models"] else "")
        return f"{p['title']}, {model}" if model else p["title"]
    where = "на видеокарте" if cuda_available() else "на процессоре"
    return f"{cfg.get('модель', 'large-v3')} {where}"


def make_recognizer(
    cfg: dict,
    glossary: str | None,
    key: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> Recognizer:
    """Собирает распознаватель по настройкам. key подменяет сохранённый ключ,
    это нужно, чтобы проверить связь до того, как ключ сохранён. on_progress
    получает ход скачивания локальной модели, если её ещё нет (см. ensure_model)."""
    cfg = normalize_config(cfg)
    language = cfg["язык"]
    if cfg["режим"] != "api":
        return LocalWhisper(
            cfg["модель"], "auto", language, glossary, on_progress=on_progress
        )

    pid = cfg["провайдер"]
    p = PROVIDERS[pid]
    key = (key or "").strip() or api_keys.get(pid)
    model = cfg["модель_api"] or (p["models"][0] if p["models"] else "")

    if p["kind"] == "yandex":
        return YandexSpeechKit(key, language, cfg["каталог"], model)
    if pid != "custom" and not key:
        raise RecognizerError(f"{p['title']}: нужен API-ключ, вставь его в настройках")
    base_url = cfg["адрес"] if pid == "custom" else p["base_url"]
    return OpenAICompatible(base_url, model, key, language, glossary, p["title"])


def probe(recognizer: Recognizer) -> float:
    """Пробный запрос секундой тихого шума. Возвращает, сколько секунд он занял.

    Текста в ответе не будет, зато видно, пускает ли сервер с этим ключом
    и сколько идёт ответ. Шум, а не ровные нули: так запрос похож на настоящий.
    """
    noise = (np.random.default_rng(0).standard_normal(SAMPLE_RATE) * 0.003).astype(np.float32)
    t0 = time.perf_counter()
    recognizer.transcribe(noise)
    return time.perf_counter() - t0
