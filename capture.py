"""Захват звука с WASAPI: микрофон и системный вывод (loopback).

Каждый источник крутится в своём потоке, приводит звук к моно 16 кГц,
режет поток на реплики по паузам и складывает их в общую очередь.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import pyaudiowpatch as pa
import soxr

import bootstrap  # noqa: F401  CUDA DLL и путь к моделям, должен идти до faster_whisper
from faster_whisper.vad import get_vad_model

TARGET_RATE = 16000  # Whisper работает только на 16 кГц
FRAME_LEN = 512  # окно Silero VAD для 16 кГц, менять нельзя
FRAME_MS = FRAME_LEN * 1000 // TARGET_RATE  # 32 мс
VAD_CONTEXT = 64  # сколько сэмплов предыдущего окна модель видит как контекст

# Сколько звука держать «про запас» перед началом речи, чтобы не срезать первый слог
PREROLL_MS = 400
# Пауза, после которой считаем реплику законченной
HANGOVER_MS = 700
# Сколько подряд речевых окон нужно, чтобы начать реплику, а не поймать щелчок
ONSET_FRAMES = 3
# Реплику длиннее этого дорезаем принудительно. Чем короче, тем быстрее сказанное
# окажется в файле: длинный монолог иначе висел бы в памяти до самой паузы
MAX_SEGMENT_SEC = 15.0
# Совсем короткие обрывки не отдаём в модель
MIN_SEGMENT_SEC = 0.35
# Вероятность речи, с которой начинаем реплику
SPEECH_ON = 0.5
# Выходим из речи по порогу пониже, иначе реплика рвётся на каждом тихом слоге
SPEECH_OFF = 0.35


@dataclass
class Segment:
    """Кусок звука с речью, готовый к расшифровке."""

    speaker: str
    audio: np.ndarray  # float32 моно 16 кГц
    t_start: float  # секунды от старта сессии
    duration: float


def list_devices() -> dict:
    """Возвращает описание доступных устройств и разумные значения по умолчанию.

    Зовётся только из потока устройств (в app.py это _audio_loop): PortAudio не
    переносит обращений из разных потоков. И ещё одно: он читает список
    устройств при первой инициализации и держит его, пока жив хоть один
    PyAudio. Свежий список получится, только если все остальные захваты
    закрыты.
    """
    p = pa.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)

        # Устройства вывода по умолчанию может не быть вовсе (все наушники и
        # колонки отключены). Микрофоны от этого списка не пропадают: просто нет
        # звука собеседника по умолчанию
        loopback = None
        default_out_name = ""
        try:
            default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            default_out_name = default_out["name"]
            for d in p.get_loopback_device_info_generator():
                if default_out_name in d["name"]:
                    loopback = d
                    break
        except Exception:
            loopback = None

        inputs, loopbacks = [], []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] < 1:
                continue
            (loopbacks if "[Loopback]" in d["name"] else inputs).append(
                {"index": i, "name": d["name"], "channels": d["maxInputChannels"]}
            )

        return {
            "mics": inputs,
            "loopbacks": loopbacks,
            "default_mic": wasapi["defaultInputDevice"],
            "default_loopback": loopback["index"] if loopback else None,
            "default_output_name": default_out_name,
        }
    finally:
        p.terminate()


class _SileroStream:
    """Потоковая обёртка над Silero VAD: окно на входе, вероятность речи на выходе.

    Модель, которая идёт с faster-whisper, рассчитана на разбор целого файла и
    сбрасывает состояние на каждом вызове. Здесь состояние переносится между
    окнами, поэтому детектор работает на живом потоке.
    """

    def __init__(self) -> None:
        self.session = get_vad_model().session
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, VAD_CONTEXT), dtype=np.float32)

    def speech_prob(self, window: np.ndarray) -> float:
        inp = np.concatenate(
            [self.context, window.reshape(1, -1).astype(np.float32)], axis=1
        )
        out, self.h, self.c = self.session.run(
            None, {"input": inp, "h": self.h, "c": self.c}
        )
        self.context = window[-VAD_CONTEXT:].reshape(1, -1).astype(np.float32)
        return float(np.ravel(out)[0])


class _VoiceSplitter:
    """Режет непрерывный поток на реплики по паузам.

    Речь определяется нейросетевым детектором, а не громкостью: на созвоне в
    наушниках постоянно есть фон, музыка и шум канала, и любой порог по энергии
    либо тонет в них, либо режет тихую речь.
    """

    def __init__(self) -> None:
        self.vad = _SileroStream()
        self.preroll: list[tuple[np.ndarray, float]] = []
        self.preroll_max = PREROLL_MS // FRAME_MS
        self.voiced: list[np.ndarray] = []
        self.loud_run = 0
        self.quiet_run = 0
        self.hangover_frames = HANGOVER_MS // FRAME_MS
        self.in_speech = False
        self.seg_start_time = 0.0

    def push(self, frame: np.ndarray, t: float):
        """Скармливает окно с его временем. Возвращает (audio, t_start) на границе реплики.

        Время берётся по часам, а не по числу обработанных окон: так реплики
        микрофона и наушников ложатся на общий таймлайн, даже если один из
        источников подтормаживает.
        """
        prob = self.vad.speech_prob(frame)
        # Гистерезис: начать реплику труднее, чем её продолжить
        loud = prob >= (SPEECH_OFF if self.in_speech else SPEECH_ON)

        if not self.in_speech:
            self.preroll.append((frame, t))
            if len(self.preroll) > self.preroll_max:
                self.preroll.pop(0)

            self.loud_run = self.loud_run + 1 if loud else 0
            if self.loud_run >= ONSET_FRAMES:
                self.in_speech = True
                self.voiced = [f for f, _ in self.preroll]
                self.seg_start_time = self.preroll[0][1]
                self.preroll = []
                self.quiet_run = 0
            return None

        self.voiced.append(frame)
        self.quiet_run = 0 if loud else self.quiet_run + 1

        too_long = len(self.voiced) * FRAME_MS / 1000 >= MAX_SEGMENT_SEC
        if self.quiet_run >= self.hangover_frames or too_long:
            return self._close()
        return None

    def _close(self):
        # Хвост тишины модели не нужен, но немного оставляем, так ровнее звучит конец слова
        keep = len(self.voiced) - max(0, self.quiet_run - 4)
        audio = np.concatenate(self.voiced[:keep]) if keep > 0 else np.array([], np.float32)
        start = self.seg_start_time

        self.in_speech = False
        self.voiced = []
        self.loud_run = 0
        self.quiet_run = 0

        if len(audio) / TARGET_RATE < MIN_SEGMENT_SEC:
            return None
        return audio, start

    def flush(self):
        """Отдаёт недоговорённую реплику при остановке записи."""
        if self.in_speech and self.voiced:
            return self._close()
        return None


class AudioSource(threading.Thread):
    """Один поток захвата: устройство -> реплики -> очередь."""

    def __init__(
        self,
        stream,
        rate: int,
        channels: int,
        chunk: int,
        speaker: str,
        out_queue: "queue.Queue[Segment]",
        session_start: float,
        on_level=None,
        on_error=None,
    ) -> None:
        super().__init__(daemon=True, name=f"capture-{speaker}")
        self.stream = stream
        self.rate = rate
        self.channels = channels
        self.chunk = chunk
        self.speaker = speaker
        self.out_queue = out_queue
        self.session_start = session_start
        self.on_level = on_level
        # Зовётся один раз из этого потока, если чтение оборвалось не по нашей команде
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.error: Exception | None = None
        # Программное усиление: тихий вход можно вытянуть, не трогая настройки Windows.
        # Читается на каждом блоке, поэтому ползунок работает прямо во время записи
        self.gain = 1.0

    def run(self) -> None:
        stream, rate, channels, chunk = self.stream, self.rate, self.channels, self.chunk
        splitter = None
        try:
            resampler = soxr.ResampleStream(rate, TARGET_RATE, 1, dtype="float32", quality="HQ")
            splitter = _VoiceSplitter()
            tail = np.array([], dtype=np.float32)

            while not self.stop_event.is_set():
                raw = stream.read(chunk, exception_on_overflow=False)
                now = time.time()
                mono = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if channels > 1:
                    mono = mono.reshape(-1, channels).mean(axis=1)

                gain = self.gain
                if gain != 1.0:
                    # Обрезаем по краям, иначе громкие места захрипят и распознавание
                    # станет хуже, чем было до усиления
                    mono = np.clip(mono * gain, -1.0, 1.0)

                tail = np.concatenate([tail, resampler.resample_chunk(mono)])

                # Дробим на ровные фреймы по 30 мс, остаток переносим в следующий проход
                n = len(tail) // FRAME_LEN
                for i in range(n):
                    frame = tail[i * FRAME_LEN : (i + 1) * FRAME_LEN]
                    if self.on_level is not None:
                        self.on_level(self.speaker, float(np.sqrt(np.mean(frame**2))))
                    # Последний фрейм пачки это «сейчас», предыдущие на 30 мс раньше
                    t = now - (n - 1 - i) * FRAME_MS / 1000
                    result = splitter.push(frame, t)
                    if result is not None:
                        self._emit(result)
                tail = tail[n * FRAME_LEN :]

        except Exception as exc:  # поток не должен ронять всё приложение молча
            self.error = exc
        finally:
            # Недоговорённую реплику отдаём и при штатной остановке, и при сбое
            # чтения (выдернули наушники): иначе последние секунды речи пропадают
            self._flush(splitter)

        # Сбой при остановке (устройство закрывают под нами) не новость, а вот
        # обрыв посреди записи окну знать нужно: иначе оно так и пишет «Идёт запись»
        if self.error is not None and not self.stop_event.is_set() and self.on_error is not None:
            try:
                self.on_error(self.speaker, self.error)
            except Exception:
                pass

    def _flush(self, splitter) -> None:
        if splitter is None:
            return
        try:
            leftover = splitter.flush()
            if leftover is not None:
                self._emit(leftover)
        except Exception:
            pass  # поток уже умирает, второй сбой поверх первого ничего не добавит

    def _emit(self, result) -> None:
        audio, start_time = result
        self.out_queue.put(
            Segment(
                speaker=self.speaker,
                audio=audio,
                t_start=max(0.0, start_time - self.session_start),
                duration=len(audio) / TARGET_RATE,
            )
        )

    def stop(self) -> None:
        self.stop_event.set()


class Capture:
    """Владеет единственным экземпляром PyAudio и всеми открытыми потоками.

    PortAudio нельзя инициализировать из нескольких потоков одновременно: два
    параллельных `PyAudio()` роняют процесс без traceback. Поэтому устройства
    открываются здесь, в одном потоке (в app.py это поток устройств, и список
    устройств читается там же), а потокам захвата остаётся только чтение.
    """

    def __init__(self) -> None:
        self._pa = pa.PyAudio()
        self.sources: list[AudioSource] = []
        self._closed = False

    def add(
        self,
        device_index: int,
        speaker: str,
        out_queue: "queue.Queue[Segment]",
        session_start: float,
        on_level=None,
        on_error=None,
    ) -> AudioSource:
        info = self._pa.get_device_info_by_index(device_index)
        rate = int(info["defaultSampleRate"])
        channels = int(info["maxInputChannels"])
        chunk = max(1, rate * FRAME_MS // 1000)  # блок в 30 мс

        stream = self._pa.open(
            format=pa.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=chunk,
        )
        src = AudioSource(
            stream, rate, channels, chunk, speaker, out_queue, session_start,
            on_level, on_error,
        )
        self.sources.append(src)
        return src

    def start(self) -> None:
        for s in self.sources:
            s.start()

    def set_gain(self, speaker: str, value: float) -> None:
        """Меняет усиление на лету: ползунок должен работать во время записи."""
        for s in self.sources:
            if s.speaker == speaker:
                s.gain = value

    def close(self) -> list[str]:
        """Останавливает захват. Возвращает имена источников, которые не завершились.

        Закрывать устройство под живым читающим потоком нельзя: PortAudio уронит
        процесс без traceback. Поэтому зависший источник просто оставляем ОС, но
        здоровые рядом с ним закрываются всё равно: иначе вместе с одним
        зависшим дорожка, которая работала, оставалась бы открытой до выхода.
        """
        if self._closed:
            return []
        self._closed = True

        for s in self.sources:
            s.stop()

        # Один общий срок на всех: у зависшего источника ждать пять секунд, и у
        # его соседа ещё пять, незачем
        deadline = time.monotonic() + 5
        stuck = []
        for s in self.sources:
            # Если второе устройство не открылось, поток первого так и не стартовал
            if s.ident is not None:
                s.join(timeout=max(0.0, deadline - time.monotonic()))
            if s.is_alive():
                stuck.append(s.speaker)

        for s in self.sources:
            if s.is_alive():
                continue  # под читающим потоком поток звука не трогаем
            try:
                s.stream.stop_stream()
            except Exception:
                pass
            try:
                s.stream.close()
            except Exception:
                pass

        # Terminate закрывает и все оставшиеся потоки PortAudio, в том числе
        # зависший под живым читателем, поэтому при зависшем источнике его не зовём
        if not stuck:
            try:
                self._pa.terminate()
            except Exception:
                pass
        return stuck
