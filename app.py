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
import json
import os
import queue
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
    LANGUAGES,
    LOCAL_MODELS,
    PROVIDERS,
    Recognizer,
    RecognizerError,
    cuda_available,
    describe,
    load_glossary,
    make_recognizer,
    normalize_config,
    probe,
)
from capture import Capture, Segment, list_devices
from interview import BANKS_DIR, Bank, Session, describe_levels, summarize_history
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

# Как часто отправлять в окно накопившиеся реплики и уровни звука
PUMP_SEC = 0.1
# Как часто заглядывать в папку с банками вопросов
BANKS_POLL_SEC = 2.0
# Сколько ждать хвост расшифровки после остановки. Процессор и далёкий сервер
# отстают от речи, и к остановке в очереди ещё лежат реплики
STOP_WAIT_SEC = 600
# Одна и та же ошибка распознавания повторяется в окне не чаще этого
ERROR_REPEAT_SEC = 30


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class Api:
    """Тонкий мост для окна: только то, что вправе звать JavaScript.

    Отдельный класс нужен не для красоты. pywebview обходит все публичные
    атрибуты объекта, отданного в js_api, и если внутри лежит ссылка на само
    окно, обход уходит в бесконечную рекурсию и захлёбывается ошибками.
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

    def open_dir(self) -> dict:
        return self._rec.open_dir()

    def open_url(self, url: str) -> dict:
        return self._rec.open_url(url)

    def open_banks_dir(self) -> dict:
        return self._rec.open_banks_dir()

    # -------------------------------------------------------- распознавание

    def asr_settings(self) -> dict:
        return self._rec.asr_settings()

    def asr_save(self, cfg: dict, key: str = "") -> dict:
        return self._rec.asr_save(cfg, key)

    def asr_test(self, cfg: dict, key: str = "") -> dict:
        return self._rec.asr_test(cfg, key)

    def asr_forget_key(self, provider: str) -> dict:
        return self._rec.asr_forget_key(provider)

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

    def set_mic_gain(self, value: float) -> dict:
        return self._rec.set_mic_gain(value)

    # ---------------------------------------------------------- мок-собес

    def interview_setup(self) -> dict:
        return self._rec.interview_setup()

    def interview_start(self, bank_file: str, level: str, mic_index: int,
                        mic_name: str = "", categories: list[str] | None = None,
                        shuffle: bool = False) -> dict:
        return self._rec.interview_start(
            bank_file, level, mic_index, mic_name, categories, shuffle
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
        self.session_start = 0.0
        # Реплики, которые не распознались: (время, кто, причина). В файле остаются
        # видимой дырой, иначе кусок разговора пропадёт молча
        self.gaps: list[tuple[float, str, str]] = []
        self.last_error = ("", 0.0)

        # режим тренажёра
        self.session: Session | None = None
        self.interview_mic = -1
        self.interview_mic_name = ""
        # если заполнено, следующий записанный ответ идёт как уточнение к прошлому
        self.pending_followup: str | None = None

        self.settings = self._load_settings()
        self.mic_gain = float(self.settings.get("усиление_микрофона", 1.0))
        self.asr_cfg = normalize_config(self.settings.get("распознавание"))

        self.recording = threading.Event()
        self.checking = threading.Event()  # открыт микрофон ради проверки уровня
        self.mic_peak = 0.0
        self.worker_stop = threading.Event()
        self.worker: threading.Thread | None = None

        threading.Thread(target=self._audio_loop, daemon=True, name="audio").start()
        threading.Thread(target=self._pump_loop, daemon=True, name="pump").start()
        threading.Thread(target=self._banks_loop, daemon=True, name="banks").start()

    # -------------------------------------------------------------- настройки

    @staticmethod
    def _load_settings() -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_settings(self) -> None:
        try:
            SETTINGS_FILE.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            pass  # не смогли запомнить, но ронять из-за этого запись незачем

    def set_mic_gain(self, value: float) -> dict:
        """Усиление микрофона. Применяется сразу, в том числе посреди записи."""
        self.mic_gain = max(GAIN_MIN, min(GAIN_MAX, float(value)))
        if self.capture is not None:
            self.capture.set_gain(ME, self.mic_gain)
        self.settings["усиление_микрофона"] = self.mic_gain
        self._save_settings()
        return {"ok": True, "gain": self.mic_gain}

    def get_settings(self) -> dict:
        return {
            "gain": self.mic_gain,
            "gain_min": GAIN_MIN,
            "gain_max": GAIN_MAX,
            "asr_label": describe(self.asr_cfg),
        }

    # ---------------------------------------------------------- распознавание

    def asr_settings(self) -> dict:
        """Всё для экрана настроек: текущий выбор, варианты, есть ли ключи."""
        return {
            "config": self.asr_cfg,
            "providers": PROVIDERS,
            "local_models": LOCAL_MODELS,
            "languages": LANGUAGES,
            "cuda": cuda_available(),
            "keys": {pid: api_keys.has(pid) for pid in PROVIDERS},
            "label": describe(self.asr_cfg),
        }

    def asr_save(self, cfg: dict, key: str = "") -> dict:
        if self.recording.is_set():
            return {"error": "Сначала останови запись"}
        clean = normalize_config(cfg)
        if clean["режим"] == "api" and clean["провайдер"] == "custom":
            if not clean["адрес"].startswith(("http://", "https://")):
                return {"error": "Адрес сервера должен начинаться с http:// или https://"}
        if key.strip():
            try:
                api_keys.put(clean["провайдер"], key)
            except Exception as exc:
                return {"error": f"Ключ не сохранился: {exc}"}
        self.asr_cfg = clean
        self.settings["распознавание"] = clean
        self._save_settings()
        # Ключ мог смениться при тех же настройках, поэтому собираем заново всегда
        self.transcriber_cfg = None
        return {
            "ok": True,
            "label": describe(clean),
            "keys": {pid: api_keys.has(pid) for pid in PROVIDERS},
        }

    def asr_test(self, cfg: dict, key: str = "") -> dict:
        """Пробный запрос с тем, что сейчас введено в окне, ещё до сохранения."""
        clean = normalize_config(cfg)
        if clean["режим"] != "api":
            return {"error": "Проверка нужна только для распознавания через интернет"}
        try:
            recognizer = make_recognizer(clean, None, key)
        except RecognizerError as exc:
            return {"error": str(exc)}
        try:
            seconds = probe(recognizer)
        except RecognizerError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Сбой: {exc}"}
        finally:
            recognizer.close()
        return {"ok": True, "ms": round(seconds * 1000)}

    def asr_forget_key(self, provider: str) -> dict:
        if provider not in PROVIDERS:
            return {"error": "Нет такого сервиса"}
        api_keys.forget(provider)
        self.transcriber_cfg = None
        return {"ok": True, "keys": {pid: api_keys.has(pid) for pid in PROVIDERS}}

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
            self.window.evaluate_js(code)
        except Exception:
            pass  # окно закрывают, доставлять уже некому

    def _call(self, fn: str, *args) -> None:
        payload = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        self._js(f"window.ui.{fn}({payload})")

    def _status(self, text: str, kind: str = "idle", sub: str = "") -> None:
        """Состояние записи для окна.

        kind красит точку и рамку: idle, loading, ready, live, stopping, done, error.
        sub это строка объяснения под ним. Во время записи окно пишет её само,
        подставляя время и число реплик.
        """
        self._call("setStatus", text, kind, sub)

    # -------------------------------------------------------- вызовы из окна

    def get_devices(self) -> dict:
        try:
            d = list_devices()
        except Exception as exc:
            return {"mics": [], "loopbacks": [], "error": str(exc)}
        return {
            "mics": d["mics"],
            "loopbacks": d["loopbacks"],
            "default_mic": d["default_mic"],
            "default_loopback": d["default_loopback"],
        }

    @staticmethod
    def _resolve(index: int, name: str, kind: str) -> int:
        """Находит устройство заново, по имени.

        Номера устройств в Windows плавают: стоит появиться или исчезнуть
        виртуальному входу (Steam поднимает свой вместе с собой), и весь список
        сдвигается. Номер, выбранный при открытии окна, к моменту записи может
        указывать уже на соседнее устройство.
        """
        if index < 0:
            return index
        if not name:
            return index
        try:
            d = list_devices()
        except Exception:
            return index
        for item in d["mics" if kind == "mic" else "loopbacks"]:
            if item["name"] == name:
                return item["index"]
        return index

    def start(self, mic_index: int, spk_index: int,
              mic_name: str = "", spk_name: str = "") -> dict:
        if self.recording.is_set():
            return {"error": "запись уже идёт"}
        mic_index = self._resolve(mic_index, mic_name, "mic")
        spk_index = self._resolve(spk_index, spk_name, "loopback")
        self.commands.put(("start", mic_index, spk_index))
        return {"ok": True}

    def stop(self) -> dict:
        if self.recording.is_set():
            self.commands.put(("stop", None, None))
        return {"ok": True}

    def open_dir(self) -> dict:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(self.out_path.parent if self.out_path else OUT_DIR)
        return {"ok": True}

    def archive_open(self, path: str) -> dict:
        """Открывает файл в том, чем в системе открываются markdown."""
        p = Path(path)
        if not p.exists():
            return {"error": "файла больше нет"}
        try:
            os.startfile(p)
        except OSError as exc:
            return {"error": str(exc)}
        return {"ok": True}

    def archive_digest(self, path: str) -> dict:
        """Считает выжимку и дописывает её в конец файла, если её там ещё нет."""
        data = archive.read(path)
        if data.get("error"):
            return data

        text = data["text"]
        result = archive.digest(text)
        md = archive.digest_markdown(data["name"], result)

        if "## Выжимка" not in text:
            try:
                Path(path).write_text(text.rstrip() + "\n\n---\n\n" + md, encoding="utf-8")
            except OSError as exc:
                return {"error": str(exc)}
        return {"ok": True, "digest": result, "markdown": md}

    # --------------------------------------------------------- поток устройств

    def _audio_loop(self) -> None:
        while True:
            cmd, a, b = self.commands.get()
            try:
                if cmd == "start":
                    self._do_start(a, b)
                elif cmd == "stop":
                    self._do_stop()
                elif cmd == "load_model":
                    self._load_model()
                elif cmd == "answer_start":
                    b.put(self._do_answer_start(a))
                elif cmd == "answer_stop":
                    b.put(self._do_answer_stop())
                elif cmd == "mic_check":
                    b.put(self._do_mic_check(a))
                elif cmd == "mic_check_stop":
                    b.put(self._do_mic_check_stop())
            except Exception:
                last = traceback.format_exc(limit=2).strip().split("\n")[-1]
                # Ответ ждут на той стороне: без него интерфейс висит до таймаута
                if isinstance(b, queue.Queue):
                    b.put({"error": last})
                else:
                    self._call("error", last)
            finally:
                # при закрытии окна вызвавшая сторона ждёт этого сигнала
                if cmd == "stop" and isinstance(b, threading.Event):
                    b.set()

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
            self._status("Готовлю модель", "loading",
                         f"{describe(cfg)}, при первом запуске модель скачивается")
        else:
            self._status("Подключаю распознавание", "loading", describe(cfg))
        self.transcriber = make_recognizer(
            cfg, load_glossary(HERE / "glossary.txt", cfg["язык"])
        )
        self.transcriber_cfg = cfg
        self._status("Распознавание готово", "ready", "можно начинать")

    def _do_start(self, mic_index: int, spk_index: int) -> None:
        try:
            self._load_model()
        except Exception as exc:
            self._call("error", f"Распознавание не запустилось: {exc}")
            return

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d_%H-%M")
        self.out_path = OUT_DIR / f"{stamp} расшифровка.md"
        # Шапку пишем полями «ключ: значение»: в файле это обычный markdown,
        # а окно раскладывает её карточкой вместо служебных строк в тексте
        self.out_path.write_text(
            f"# Расшифровка {now.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"- Начата: {now.strftime('%d.%m.%Y %H:%M')}\n"
            f"- Распознавание: {self.transcriber.label}\n"
            f"- Спикеры: «{ME}» это микрофон, «{THEM}» это звук из наушников\n"
            "- Состояние: файл дополняется по ходу разговора\n\n---\n\n",
            encoding="utf-8",
        )

        self.lines.clear()
        self.gaps.clear()
        self.session_start = time.time()
        self.capture = Capture()
        try:
            if mic_index >= 0:
                self.capture.add(mic_index, ME, self.segments, self.session_start,
                                 on_level=self._on_level)
            if spk_index >= 0:
                self.capture.add(spk_index, THEM, self.segments, self.session_start,
                                 on_level=self._on_level)
            self.capture.set_gain(ME, self.mic_gain)
            self.capture.start()
        except Exception as exc:
            self.capture = None
            self._call("error", f"Устройство не открылось: {exc}")
            return

        self.recording.set()
        self.worker_stop.clear()
        self.worker = threading.Thread(target=self._transcribe_loop, daemon=True, name="asr")
        self.worker.start()

        self._call("started", str(self.out_path))
        # подпись под статусом окно ведёт само: там время и число реплик
        self._status("Идёт запись", "live")

    def _do_stop(self) -> None:
        self.recording.clear()
        self._status("Останавливаю", "stopping", "дописываю последние реплики в файл")

        if self.capture is not None:
            stuck = self.capture.close()
            self.capture = None
            if stuck:
                self._call("error", f"Источник «{', '.join(stuck)}» не отозвался")

        self.worker_stop.set()
        if self.worker is not None:
            # Медленный распознаватель отстаёт от речи. Ждём, пока он дорасшифрует
            # очередь, и показываем, сколько осталось: иначе хвост разговора
            # потеряется, а итоговый файл перезапишется поверх недописанного
            deadline = time.time() + STOP_WAIT_SEC
            shown = -1
            while self.worker.is_alive() and time.time() < deadline:
                left = self.segments.qsize()
                if left and left != shown:
                    self._status("Дорасшифровываю", "stopping", f"осталось реплик: {left}")
                    shown = left
                self.worker.join(timeout=0.5)
            self.worker = None

        summary = self._finalize()
        self._call("stopped", session_ui(summary))
        if self.lines:
            lost = f" · не распознано {len(self.gaps)}" if self.gaps else ""
            self._status(
                "Записано", "done",
                f"реплик {len(self.lines)}{lost} · файл в папке «расшифровки»",
            )
        elif self.gaps:
            self._status("Ничего не распозналось", "error", self.gaps[-1][2])
        else:
            self._status(
                "Речи не было", "error",
                "проверь, тот ли выбран вход и слышно ли собеседника в наушниках",
            )

    def _on_level(self, speaker: str, level: float) -> None:
        self.levels[speaker] = level
        if self.checking.is_set() and speaker == ME:
            self.mic_peak = max(self.mic_peak, level)

    # ------------------------------------------------------- поток расшифровки

    def _transcribe_loop(self) -> None:
        while not self.worker_stop.is_set():
            try:
                seg = self.segments.get(timeout=0.2)
            except queue.Empty:
                continue
            self._handle(seg)

        # добираем то, что источники успели дописать при остановке
        while True:
            try:
                seg = self.segments.get_nowait()
            except queue.Empty:
                break
            self._handle(seg)

    def _handle(self, seg: Segment) -> None:
        try:
            text = self.transcriber.transcribe(seg.audio)
        except Exception as exc:
            self._lost(seg, exc)
            return
        if not text:
            return

        self.lines.append((seg.t_start, seg.speaker, text, seg.duration))
        # Пишем сразу и закрываем файл: агент, читающий его параллельно,
        # должен видеть последнюю реплику через секунду, а не в конце созвона
        with self.out_path.open("a", encoding="utf-8") as f:
            f.write(f"**[{hhmmss(seg.t_start)}] {seg.speaker}:** {text}\n\n")

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
        self.gaps.append((seg.t_start, seg.speaker, reason))
        with self.out_path.open("a", encoding="utf-8") as f:
            f.write(f"**[{hhmmss(seg.t_start)}] {seg.speaker}:** _(не распознано: {reason})_\n\n")

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
        # Легла сеть, и каждая реплика падает с той же ошибкой. Сыпать
        # одинаковыми сообщениями каждые две секунды незачем
        last_reason, last_at = self.last_error
        now = time.time()
        if reason != last_reason or now - last_at > ERROR_REPEAT_SEC:
            self.ready.put(("warn", reason))
            self.last_error = (reason, now)

    # ------------------------------------------------------------ поток вывода

    def _pump_loop(self) -> None:
        last_levels = None
        while True:
            time.sleep(PUMP_SEC)
            while True:
                try:
                    kind, payload = self.ready.get_nowait()
                except queue.Empty:
                    break
                if kind == "line":
                    self._call("addLine", payload)
                elif kind == "warn":
                    self._call("warn", payload)
                elif kind == "error":
                    self._call("error", payload)

            if not (self.recording.is_set() or self.checking.is_set()):
                last_levels = None
                continue

            # В тишине уровень дрожит в шестом знаке. Округляем и молчим, пока
            # картинка не изменилась, иначе шлём в окно десять пустых вызовов в секунду
            now = (round(self.levels.get(ME, 0.0), 3), round(self.levels.get(THEM, 0.0), 3))
            if now != last_levels:
                self._call("setLevels", now[0], now[1])
                last_levels = now

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
        last = self._banks_stamp()
        while True:
            time.sleep(BANKS_POLL_SEC)
            stamp = self._banks_stamp()
            if stamp == last:
                continue

            # Файл могут писать прямо сейчас: дочитаем на следующем круге,
            # иначе поймаем YAML на середине и покажем «битый файл»
            now = time.time_ns()
            if any(now - mtime < 1_200_000_000 for _, mtime, _ in stamp):
                continue

            appeared = [n for n, _, _ in stamp if n not in {x[0] for x in last}]
            last = stamp
            self._call("banksChanged", Bank.available(),
                       self.settings.get("темы", {}), appeared)

    # ---------------------------------------------------------------- итог

    def _finalize(self):
        """Пересобирает файл по времени реплик и считает разбор речи.

        Живой вывод идёт в порядке готовности расшифровки, а он не всегда
        совпадает с порядком реплик: длинная фраза считается дольше короткой.
        """
        if not (self.lines or self.gaps) or self.out_path is None:
            return None

        ordered = sorted(self.lines, key=lambda x: x[0])
        # Дыры встают в ленту на своё время, но в разбор речи не идут
        rows = [(t, who, txt) for t, who, txt, _ in ordered]
        rows += [(t, who, f"_(не распознано: {why})_") for t, who, why in self.gaps]
        rows.sort(key=lambda x: x[0])
        body = "".join(f"**[{hhmmss(t)}] {who}:** {txt}\n\n" for t, who, txt in rows)

        # Разбираем только свою речь: собеседника оценивать смысла нет
        summary = analyze_session(
            self.analyzer, [(txt, dur) for _, who, txt, dur in ordered if who == ME]
        ) if ordered else None
        report = session_report(summary) if summary else ""

        lost = f"- Не распознано: {len(self.gaps)}\n" if self.gaps else ""
        label = self.transcriber.label if self.transcriber else describe(self.asr_cfg)
        self.out_path.write_text(
            f"# Расшифровка {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"- Длительность: {hhmmss(rows[-1][0])}\n"
            f"- Реплик: {len(ordered)}\n"
            + lost
            + f"- Распознавание: {label}\n"
            f"- Спикеры: «{ME}» это микрофон, «{THEM}» это звук из наушников\n\n---\n\n"
            + body
            + ("\n---\n\n" + report if report else ""),
            encoding="utf-8",
        )
        return summary

    # ------------------------------------------------------------- мок-собес

    def interview_setup(self) -> dict:
        """Что показать на экране выбора: банки, уровни, прошлые сессии."""
        return {
            "banks": Bank.available(),
            "levels": describe_levels(),
            "history": summarize_history(),
            "saved_categories": self.settings.get("темы", {}),
        }

    def interview_start(self, bank_file: str, level: str, mic_index: int,
                        mic_name: str = "", categories: list[str] | None = None,
                        shuffle: bool = False) -> dict:
        try:
            bank = Bank.load(BANKS_DIR / bank_file)
        except Exception as exc:
            return {"error": f"Банк вопросов не читается: {exc}"}
        if not bank.questions:
            return {"error": "В банке нет вопросов"}

        self.session = Session(bank, level, self.analyzer, categories, shuffle)
        if not self.session.questions:
            self.session = None
            return {"error": "На этих темах и уровне вопросов не осталось"}

        # Запоминаем выбор тем по каждому банку: под техническую тренировку
        # и под поведенческую наборы разные, переключаться придётся часто
        self.settings.setdefault("темы", {})[bank_file] = list(categories or [])
        self._save_settings()

        self.interview_mic = mic_index
        self.interview_mic_name = mic_name
        self.commands.put(("load_model", None, None))
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
        if self.recording.is_set():
            return {"error": "идёт запись"}
        done: "queue.Queue[dict]" = queue.Queue()
        self.commands.put(("mic_check", self._resolve(mic_index, mic_name, "mic"), done))
        try:
            return done.get(timeout=20)
        except queue.Empty:
            return {"error": "микрофон не отозвался"}

    def mic_check_stop(self) -> dict:
        done: "queue.Queue[dict]" = queue.Queue()
        self.commands.put(("mic_check_stop", None, done))
        try:
            return done.get(timeout=20)
        except queue.Empty:
            return {"error": "не удалось закрыть микрофон"}

    def followup_start(self, question_text: str) -> dict:
        """Запись ответа на уточняющий вопрос. Пишется так же, разбирается иначе."""
        if self.session is None:
            return {"error": "сессия не начата"}
        self.pending_followup = question_text
        return self.answer_start()

    def answer_start(self) -> dict:
        """Начинает запись ответа. Слушаем только микрофон: собеседника тут нет."""
        if self.session is None:
            return {"error": "сессия не начата"}
        if self.recording.is_set():
            return {"error": "запись уже идёт"}
        done: "queue.Queue[dict]" = queue.Queue()
        mic = self._resolve(self.interview_mic, self.interview_mic_name, "mic")
        self.commands.put(("answer_start", mic, done))
        return done.get()

    def answer_stop(self) -> dict:
        """Останавливает запись, расшифровывает ответ и разбирает его."""
        if self.session is None:
            return {"error": "сессия не начата"}
        done: "queue.Queue[dict]" = queue.Queue()
        self.commands.put(("answer_stop", None, done))
        # Ждём сколько понадобится: длинный ответ расшифровывается долго, и обрывать
        # его по таймеру нельзя, записанное просто пропадёт
        return done.get()

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
        self.session = None
        return {"ok": True, "path": str(path) if path else "", "prompt": prompt}

    # ----------------------------------------------------- запись одного ответа

    def _do_mic_check(self, mic_index: int) -> dict:
        """Открывает вход и гонит уровень в окно. Расшифровка не нужна."""
        self.segments = queue.Queue()
        try:
            self.capture = Capture()
            self.capture.add(mic_index, ME, self.segments, time.time(),
                             on_level=self._on_level)
            self.capture.set_gain(ME, self.mic_gain)
            self.capture.start()
        except Exception as exc:
            self.capture = None
            return {"error": f"Микрофон не открылся: {exc}"}
        self.checking.set()
        self.mic_peak = 0.0
        return {"ok": True}

    def _do_mic_check_stop(self) -> dict:
        self.checking.clear()
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        peak = self.mic_peak
        # Речь обычного человека даёт пик около 0.1. Всё, что тише сотой доли,
        # VAD речью не признает, и ответы будут уходить в пустоту
        verdict = "хорошо" if peak > 0.05 else "тихо" if peak > 0.01 else "почти ничего"
        return {"ok": True, "peak": round(peak, 4), "verdict": verdict}

    def _do_answer_start(self, mic_index: int) -> dict:
        # Зовём всегда: если настройки распознавания поменялись, соберётся заново,
        # а если нет, вернётся сразу
        try:
            self._load_model()
        except Exception as exc:
            return {"error": f"Распознавание не запустилось: {exc}"}

        self.answer_segments = []
        self.segments = queue.Queue()
        self.session_start = time.time()
        try:
            self.capture = Capture()
            self.capture.add(mic_index, ME, self.segments, self.session_start,
                             on_level=self._on_level)
            self.capture.set_gain(ME, self.mic_gain)
            self.capture.start()
        except Exception as exc:
            self.capture = None
            return {"error": f"Микрофон не открылся: {exc}"}

        self.recording.set()
        return {"ok": True}

    def _do_answer_stop(self) -> dict:
        started = self.session_start
        self.recording.clear()

        if self.capture is not None:
            self.capture.close()
            self.capture = None

        # Длительность меряем по часам, а не по сумме кусков речи:
        # паузы и заминки это часть ответа, за них тоже спрашивают
        duration = max(0.1, time.time() - started)

        segments: list[Segment] = []
        while True:
            try:
                segments.append(self.segments.get_nowait())
            except queue.Empty:
                break
        segments.sort(key=lambda s: s.t_start)

        parts, spans = [], []
        for seg in segments:
            try:
                text = self.transcriber.transcribe(seg.audio)
            except Exception as exc:
                return {"error": f"Сбой расшифровки: {exc}"}
            if text:
                parts.append(text)
                spans.append((seg.t_start, seg.t_start + seg.duration))

        answer = " ".join(parts).strip()
        followup_q, self.pending_followup = self.pending_followup, None
        if not answer:
            return {"empty": True, "duration": round(duration), "was_followup": bool(followup_q)}

        if followup_q:
            v = self.session.answer_followup(followup_q, answer, duration)
        else:
            v = self.session.answer(answer, duration, spans)
        return {
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

    def shutdown(self) -> None:
        """Останавливает запись перед закрытием окна и ждёт, пока файл допишется.

        Останавливать напрямую нельзя: устройства принадлежат аудио-потоку,
        а сюда мы попадаем из потока окна.
        """
        if not self.recording.is_set():
            return
        done = threading.Event()
        self.commands.put(("stop", None, done))
        done.wait(timeout=90)


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
    try:
        use_own_taskbar_identity()
        rec = Recorder()
        window = webview.create_window(
            "Дубль",
            str(HERE / "web" / "index.html"),
            js_api=Api(rec),
            width=1120,
            height=780,
            min_size=(860, 580),
            background_color=window_background(),
        )
        rec.window = window
        window.events.closing += lambda: rec.shutdown()
        loaded_once = threading.Event()

        def on_loaded() -> None:
            if not loaded_once.is_set():
                loaded_once.set()
                check_packages_later(rec)

        window.events.loaded += on_loaded
        # В справке pywebview иконка «только для GTK и QT», но на Windows
        # winforms её тоже берёт из этого параметра, нужен именно .ico
        webview.start(icon=str(ICON) if ICON.exists() else None)
    except Exception:
        (HERE / "ошибка.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
