"""Скачивает модель распознавания во время установки, чтобы первая запись не ждала.

Запускает Установка.bat после библиотек, интерпретатором из папки .venv. Какая модель
нужна, решает asr.default_local_model(): пока библиотек CUDA нет, это small, около
0.5 ГБ. Другую можно назвать первым аргументом. Если модель уже лежит на диске, в том
числе в HF_HOME, который задал сам человек, ничего не качается.

Ход печатается одной строкой, которая обновляется на месте. Сбой установку не
останавливает: модель скачает программа при первом запуске. Поэтому наружу не
летят исключения, а итог виден по коду выхода:
    0  модель на диске (уже была или скачалась)
    1  модели нет, она скачается при первом запуске
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import threading

# Предупреждения huggingface_hub (про символические ссылки на Windows без режима
# разработчика, про запросы без токена) на работу не влияют, а в консоли установщика
# врезаются в строку хода. Ошибки остаются
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")

LABEL = "Модель распознавания"
INDENT = " " * 8
# Десятичные мегабайты, как в подписях моделей: small это 0.5 ГБ, то есть около 486 МБ
MB = 1_000_000


def say(text: str = "") -> None:
    """Обычная строка. Сбой записи в консоль (закрыт канал) установку не роняет."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def progress_text(done: int, total: int, source: str) -> str:
    if total <= 0:
        text = f"{LABEL}: подключаюсь…"
    else:
        text = f"{LABEL}: {done / MB:.0f} из {total / MB:.0f} МБ"
    if source == "github":
        text += " (запасная копия)"
    return INDENT + text


class Line:
    """Строка хода. В консоли обновляется на месте, в файле или канале печатается
    только при смене десятка процентов или источника, чтобы не забить журнал."""

    def __init__(self) -> None:
        try:
            self.live = sys.stdout.isatty()
        except (AttributeError, ValueError):
            self.live = False
        self.width = 0
        self.mark: tuple | None = None
        self.open = False  # в консоли висит недописанная строка

    def show(self, text: str, mark: tuple) -> None:
        try:
            if self.live:
                sys.stdout.write("\r" + text.ljust(self.width))
                sys.stdout.flush()
                self.width = len(text)
                self.open = True
            elif mark != self.mark:
                print(text, flush=True)
            self.mark = mark
        except (OSError, ValueError):
            pass

    def end(self, text: str | None = None) -> None:
        """Закрывает живую строку. Есть text: пишет его на её месте, иначе стирает."""
        try:
            if self.live and self.open:
                shown = text if text is not None else ""
                sys.stdout.write("\r" + shown.ljust(self.width) + ("\n" if text is not None else "\r"))
                sys.stdout.flush()
            elif text is not None:
                print(text, flush=True)
        except (OSError, ValueError):
            pass
        self.open = False
        self.width = 0


def fail(why: str, ready_text: bool = False) -> int:
    """Модель не скачалась. Установка идёт дальше, поэтому тон спокойный.

    ready_text: why это готовая фраза от asr («Не удалось скачать модель…»), без
    приставки «Модель не скачалась:», иначе одно и то же прозвучит дважды.
    """
    text = why if ready_text else f"Модель не скачалась: {why}"
    # Длинный путь к папке модели не рвём посреди слова
    for line in textwrap.wrap(text, 72, break_on_hyphens=False, break_long_words=False):
        say(INDENT + line)
    if "не хватает места" in text:
        # При полном диске первый запуск упадёт так же, «ничего страшного» тут неправда
        say(INDENT + "Когда освободишь место, модель скачается при первом запуске программы.")
    else:
        say(INDENT + "Ничего страшного: модель скачается при первом запуске программы.")
    return 1


def main(argv: list[str]) -> int:
    # Ctrl+C останавливает загрузку по-хорошему, недокачанное останется для докачки
    cancel = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    try:
        import asr
    except Exception as exc:
        return fail(f"не загрузились библиотеки ({type(exc).__name__}: {exc}).")

    line = Line()
    try:
        model = argv[1] if len(argv) > 1 else asr.default_local_model()
        if asr.model_ready(model):
            say(f"{INDENT}{LABEL}: {model} уже на диске, качать не нужно.")
            return 0

        size = next((m["size"] for m in asr.LOCAL_MODELS if m["id"] == model), "")
        say(f"{INDENT}Нужна модель {model}" + (f", около {size.replace('.', ',')}." if size else "."))
        last = {"total": 0, "source": "huggingface"}

        def on_progress(done: int, total: int, source: str) -> None:
            last.update(total=total, source=source)
            step = done * 10 // total if total > 0 else -1
            line.show(progress_text(done, total, source), (source, step))

        asr.ensure_model(model, on_progress, cancel=cancel)
        if not asr.model_ready(model):
            line.end()
            return fail("файлы модели на диске оказались неполными.")
        details = []
        if last["source"] == "github":
            details.append("запасная копия")
        if last["total"]:
            details.append(f"{last['total'] / MB:.0f} МБ")
        line.end(f"{INDENT}{LABEL}: {model} скачана" + (f" ({', '.join(details)})." if details else "."))
        return 0
    except asr.DownloadCancelled:
        line.end()
        say(INDENT + "Загрузка остановлена. Модель скачается при первом запуске программы.")
        return 1
    except (asr.ModelDownloadError, ValueError) as exc:
        line.end()
        return fail(str(exc), ready_text=True)
    except Exception as exc:
        line.end()
        return fail(asr.mask_secrets(f"{type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
