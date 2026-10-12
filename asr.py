"""Распознавание речи: Whisper на этом компьютере или сервер по API.

Снаружи все распознаватели одинаковые (см. Recognizer): подпись label и метод
transcribe, который берёт реплику и отдаёт строку. Окну и консоли всё равно,
где считается текст, на своей видеокарте, на процессоре или на чужом сервере.

Whisper на тишине и шуме любит выдумывать фразы из титров ютуба
(«Продолжение следует...», «Субтитры сделал...»). Здесь они отсекаются,
иначе расшифровка созвона обрастает репликами, которых никто не говорил.
"""

from __future__ import annotations

import functools
import io
import re
import time
import unicodedata
import wave
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

import api_keys
import bootstrap  # noqa: F401  CUDA DLL и путь к моделям, должен идти до faster_whisper

SAMPLE_RATE = 16000  # так режет capture.py, и так хочет Whisper

# Реплика выбрасывается целиком, если содержит любой из этих кусков.
# Именно куски, а не фразы целиком: модель тасует фамилии и порядок слов,
# и «Редактор субтитров А.Семкин Корректор А.Егорова» точному списку не равен.
HALLUCINATION_MARKERS = (
    "субтитр",  # покрывает и «субтитры сделал», и «редактор субтитров»
    "dimatorzok",
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
    # Прощания из видеороликов. В рабочем созвоне так не заканчивают,
    # а Whisper дописывает их на хвосте тишины
    "до новых встреч",
    "на этом все спасибо за внимание",
    "с вами был",
)

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
}


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    text = _PUNCT.sub("", text)
    return _SPACE.sub(" ", text).strip()


def _is_hallucination(text: str) -> bool:
    norm = _normalize(text)
    if not norm:
        return True
    if norm in HALLUCINATION_EXACT:
        return True
    return any(m in norm for m in HALLUCINATION_MARKERS)


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


def _read_terms(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def load_glossary(path: Path | None, language: str = "ru") -> str | None:
    """Собирает подсказку для модели: имена, термины и аббревиатуры пишутся правильнее.

    Личные термины идут первыми. Подсказка ограничена по длине, и если она не
    влезает, обрезается конец списка (см. fit_prompt), а имена и компания остаются.
    """
    if path is None:
        return None
    terms = list(dict.fromkeys(_read_terms(local_glossary(path)) + _read_terms(path)))
    if not terms:
        return None
    # Whisper смотрит на initial_prompt как на «предыдущий текст», поэтому оформляем
    # его обычным предложением, а не списком через запятую
    return _GLOSSARY_LEAD.get(language, "") + ", ".join(terms) + "."


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


class RecognizerError(Exception):
    """Ошибка распознавания, текст которой можно показать человеку как есть."""


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
    {"id": "large-v3-turbo", "size": "1.6 ГБ", "note": "Почти как large-v3, но в разы быстрее."},
    {"id": "medium", "size": "1.5 ГБ", "note": "Середина между качеством и скоростью."},
    {"id": "small", "size": "0.5 ГБ", "note": "Тянет даже процессор, но путает термины."},
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


class LocalWhisper:
    """faster-whisper на этом компьютере: на видеокарте, если она есть, иначе на процессоре."""

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "auto",
        language: str = "ru",
        glossary: str | None = None,
        download_root: str | None = None,
    ) -> None:
        # Импорт здесь, а не в начале файла: тем, кто распознаёт по API,
        # незачем грузить CTranslate2 и ждать CUDA
        from faster_whisper import WhisperModel

        if device == "auto":
            device = "cuda" if cuda_available() else "cpu"
        # На видеокарте float16 быстрее всего: int8_float16 на RTX 5060 Ti давал
        # ×8 к реальному времени против ×10. На процессоре float16 нет, там int8
        compute_type = "float16" if device == "cuda" else "int8"
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
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


def _explain(service: str, status: int, detail: str) -> str:
    """Код ответа сервера → что случилось, человеческими словами."""
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
    detail = " ".join(detail.split())[:160]
    return f"{service}: ответ {status}. {detail}".strip()


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
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        self.model = model
        self.language = None if language == "auto" else language
        self.prompt = _short_prompt(glossary)
        self.service = service
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
        resp = self._send(audio)
        if resp.status_code == 400 and self.verbose and "response_format" in resp.text:
            self.verbose = False
            resp = self._send(audio)
        if resp.status_code != 200:
            raise RecognizerError(_explain(self.service, resp.status_code, resp.text))
        try:
            data = resp.json()
        except ValueError:
            raise RecognizerError(f"{self.service}: ответ не похож на JSON") from None

        segments = data.get("segments") or []
        if segments:
            return _clean([
                (s.get("text", ""), s.get("no_speech_prob"), s.get("avg_logprob"))
                for s in segments
            ])
        return _clean([(data.get("text", ""), None, None)])

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
        step = self.MAX_SEC * SAMPLE_RATE
        parts = []
        for i in range(0, len(audio), step):
            resp = _post(self.client, self.SERVICE, self.URL,
                         params=self.params, content=_pcm16(audio[i : i + step]))
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("error_message", resp.text)
                except ValueError:
                    detail = resp.text
                raise RecognizerError(_explain(self.SERVICE, resp.status_code, detail))
            parts.append(resp.json().get("result", "").strip())
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
        "note": "Whisper large-v3 на их серверах. Из России может понадобиться VPN.",
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
    """Настройки распознавания для первого запуска: под то железо, что есть."""
    return {
        "режим": "local",
        "модель": "large-v3" if cuda_available() else "small",
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


def make_recognizer(cfg: dict, glossary: str | None, key: str | None = None) -> Recognizer:
    """Собирает распознаватель по настройкам. key подменяет сохранённый ключ,
    это нужно, чтобы проверить связь до того, как ключ сохранён."""
    cfg = normalize_config(cfg)
    language = cfg["язык"]
    if cfg["режим"] != "api":
        return LocalWhisper(cfg["модель"], "auto", language, glossary)

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
