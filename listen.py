"""Живая расшифровка созвона: слушает микрофон и звук из наушников, пишет текст.

Кто говорит, определяется источником звука, а не разбором голосов: всё, что пришло
с микрофона, это ты, а всё, что с системного вывода, это собеседник. На созвоне это
надёжнее любой диаризации.

    python listen.py                  запись со всеми настройками по умолчанию
    python listen.py --list-devices   посмотреть устройства
    python listen.py --only mic       писать только себя

    python listen.py --api            распознавать через сервис из настроек окна

Остановка по Ctrl+C. Расшифровка складывается в папку «расшифровки».
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from asr import (
    LocalWhisper,
    Recognizer,
    RecognizerError,
    default_local_model,
    make_recognizer,
    mask_secrets,
    read_glossary,
)
from capture import Capture, Segment, list_devices
from speech_metrics import SpeechAnalyzer, analyze_session, session_report

HERE = Path(__file__).resolve().parent
ME = "Я"
THEM = "Собеседник"
STYLES = {ME: "bold green", THEM: "bold cyan"}
console = Console()

# Эти знаки в имени файла Windows не принимает
_BAD_NAME_CHARS = '<>:"/\\|?*'


def say(message: str) -> None:
    """Печать в консоль, которая не роняет запись.

    Консоль могут перенаправить в файл с кодировкой, где нет знака из реплики,
    или закрыть. Реплика к этому времени уже на диске, терять её из-за печати нельзя.
    """
    # Одинокий суррогат (так выглядит битый символ из распознавания) rich не
    # печатает и после этого застревает: буфер не очищается, и каждая следующая
    # строка падает с той же ошибкой
    message = message.encode("utf-8", errors="replace").decode("utf-8")
    try:
        console.print(message)
    except Exception:
        try:
            del console._buffer[:]
        except Exception:
            pass


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def show_devices() -> None:
    d = list_devices()
    table = Table(title="Устройства записи", header_style="bold")
    table.add_column("№", justify="right")
    table.add_column("Тип")
    table.add_column("Устройство")
    table.add_column("По умолчанию", justify="center")

    for m in d["mics"]:
        mark = "да" if m["index"] == d["default_mic"] else ""
        table.add_row(str(m["index"]), "микрофон", m["name"], mark)
    for lb in d["loopbacks"]:
        mark = "да" if lb["index"] == d["default_loopback"] else ""
        table.add_row(str(lb["index"]), "звук системы", lb["name"], mark)

    console.print(table)
    console.print(f"\nЗвук сейчас идёт в: [bold]{d['default_output_name']}[/bold]")
    console.print("Номера подставляются в --mic и --speakers.")


class NoSources(RuntimeError):
    """Не из чего писать: ни микрофона, ни loopback."""


class Recorder:
    def __init__(self, args) -> None:
        self.args = args
        self.queue: queue.Queue[Segment] = queue.Queue()
        # (время от начала, кто, текст, длительность реплики)
        self.lines: list[tuple[float, str, str, float]] = []
        # реплики, которые не распознались: (время, кто, причина)
        self.gaps: list[tuple[float, str, str]] = []
        self.analyzer = SpeechAnalyzer()
        self.capture: Capture | None = None
        self.session_start = time.time()
        self.label = ""
        self._disk_warned = False

        self.out_dir = Path(args.out) if args.out else HERE / "расшифровки"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        title = "".join(
            c for c in (args.title or "") if c not in _BAD_NAME_CHARS and ord(c) >= 32
        ).strip().rstrip(". ")
        self.stem = f"{stamp} {title}" if title else stamp
        # Настоящий путь появится в _claim_file, когда распознавание заработает:
        # неудачный запуск не должен оставлять файлов
        self.out_path: Path | None = None

    def _claim_file(self, header: str) -> None:
        """Создаёт файл записи, не трогая чужие.

        Имя по минуте: два запуска в одну минуту раньше затирали друг друга.
        Та же схема, что в тренажёре и в окне: к занятому имени добавляется
        « (2)», « (3)». Файл создаётся режимом «x», так что гонки за имя нет.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for n in range(1, 1000):
            name = f"{self.stem}.md" if n == 1 else f"{self.stem} ({n}).md"
            path = self.out_dir / name
            try:
                with path.open("x", encoding="utf-8") as f:
                    f.write(header)
            except FileExistsError:
                continue
            self.out_path = path
            return
        raise OSError(f"в {self.out_dir} слишком много расшифровок с именем «{self.stem}»")

    def _discard_if_empty(self) -> None:
        """Файл, куда не попало ничего, кроме шапки, не оставляем."""
        if self.out_path is None or self.lines or self.gaps:
            return
        try:
            self.out_path.unlink()
        except OSError:
            pass
        self.out_path = None

    def _start_sources(self) -> None:
        # Отсчёт ведём от начала прослушивания, а не от запуска скрипта:
        # между ними несколько секунд на загрузку модели
        self.session_start = time.time()
        d = list_devices()
        mic_idx = self.args.mic if self.args.mic is not None else d["default_mic"]
        spk_idx = self.args.speakers if self.args.speakers is not None else d["default_loopback"]

        self.capture = Capture()
        if self.args.only in ("both", "mic"):
            self.capture.add(mic_idx, ME, self.queue, self.session_start)
        if self.args.only in ("both", "speakers"):
            if spk_idx is None:
                say("[yellow]Не нашёл loopback для текущего вывода звука.[/yellow]")
                say("[yellow]Укажи его вручную: --speakers НОМЕР[/yellow]")
            else:
                self.capture.add(spk_idx, THEM, self.queue, self.session_start)

        if not self.capture.sources:
            raise NoSources("Нечего слушать.")

        self.capture.start()
        listening = ", ".join(
            f"[{STYLES[s.speaker]}]{s.speaker}[/]" for s in self.capture.sources
        )
        say(f"Слушаю: {listening}")

    def _append(self, text: str) -> None:
        """Пишет на диск сразу, чтобы ничего не потерялось при обрыве.

        Сбой диска запись не обрывает: реплики остаются в памяти, итоговая
        пересборка в конце попробует ещё раз.
        """
        if self.out_path is None:
            return
        try:
            with self.out_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except OSError as exc:
            if not self._disk_warned:
                self._disk_warned = True
                say(f"[red]Не пишется в файл: {escape(str(exc))}. Запись идёт дальше в памяти.[/red]")

    def _write_line(self, seg: Segment, text: str) -> None:
        self._append(f"**[{hhmmss(seg.t_start)}] {seg.speaker}:** {text}\n\n")

    def _lost(self, seg: Segment, reason: str) -> None:
        """Реплика не распозналась: дыра остаётся пометкой в файле и в консоли."""
        # Причина попадёт в файл и в консоль: ключ, если он вдруг в ней, прячем
        reason = mask_secrets(" ".join(reason.split()))
        self.gaps.append((seg.t_start, seg.speaker, reason))
        say(f"[red]{hhmmss(seg.t_start)} {seg.speaker}: не распознано, {escape(reason)}[/red]")
        self._append(f"**[{hhmmss(seg.t_start)}] {seg.speaker}:** _(не распознано: {reason})_\n\n")

    def _handle(self, seg: Segment, transcriber: Recognizer) -> None:
        try:
            text = transcriber.transcribe(seg.audio)
        except KeyboardInterrupt:
            self._lost(seg, "остановлено до конца расшифровки")
            raise
        except RecognizerError as exc:
            self._lost(seg, str(exc))
            return
        except Exception as exc:
            # Любой другой сбой на одной реплике не должен обрывать всю запись
            self._lost(seg, f"сбой расшифровки: {type(exc).__name__}: {exc}")
            return
        if not text:
            return
        self.lines.append((seg.t_start, seg.speaker, text, seg.duration))
        self._write_line(seg, text)
        say(
            f"[dim]{hhmmss(seg.t_start)}[/dim] "
            f"[{STYLES[seg.speaker]}]{seg.speaker}:[/] {escape(text)}"
        )

    def _check_sources(self) -> None:
        if self.capture is None:
            return
        for s in self.capture.sources:
            if s.error is not None:
                say(f"[red]Источник «{s.speaker}» отвалился: {escape(str(s.error))}[/red]")
                s.error = None

    def _make_recognizer(self) -> Recognizer:
        glossary = read_glossary(
            Path(self.args.glossary) if self.args.glossary else None, self.args.language
        )
        for warning in glossary.warnings:
            say(f"[yellow]{escape(warning)}[/yellow]")
        if not self.args.api:
            # Модель по умолчанию выбирает asr: на видеокарте turbo (или уже скачанная
            # большая), на процессоре small. Флаг --cpu держит small при любой карте
            return LocalWhisper(
                self.args.model or ("small" if self.args.cpu else default_local_model()),
                device="cpu" if self.args.cpu else "auto",
                language=self.args.language,
                glossary=glossary.prompt,
            )
        # Сервис, модель и ключ те же, что выбраны в окне: дублировать
        # их флагами незачем, а ключ в командной строке осядет в истории
        try:
            saved = json.loads((HERE / "настройки.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        asr_cfg = saved.get("распознавание") if isinstance(saved, dict) else None
        if not isinstance(asr_cfg, dict):
            asr_cfg = {}
        cfg = {**asr_cfg, "режим": "api", "язык": self.args.language}
        return make_recognizer(cfg, glossary.prompt)

    def run(self) -> int:
        say("[dim]Готовлю распознавание…[/dim]")
        t0 = time.perf_counter()
        try:
            transcriber = self._make_recognizer()
        except Exception as exc:
            say(f"[red]Распознавание не запустилось: {escape(str(exc))}[/red]")
            if not self.args.api:
                say("[yellow]Попробуй --cpu или --api[/yellow]")
            return 1
        self.label = transcriber.label
        say(f"[dim]{escape(self.label)}, готово за {time.perf_counter() - t0:.1f} c[/dim]")

        header = (
            f"# Расшифровка {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Распознавание: {self.label}. «{ME}» это микрофон, «{THEM}» это звук из наушников.\n\n"
        )
        try:
            self._claim_file(header)
            self._start_sources()
        except Exception as exc:
            say(f"[red]Запись не началась: {escape(str(exc))}[/red]")
            self._close_devices()
            self._discard_if_empty()
            self._close_recognizer(transcriber)
            return 1

        say(f"[dim]Пишу в {escape(str(self.out_path))}[/dim]")
        say("[dim]Остановить: Ctrl+C[/dim]\n")

        code = 0
        deadline = time.time() + self.args.duration if self.args.duration else None
        try:
            while True:
                if deadline and time.time() > deadline:
                    say("\n[dim]Время вышло, останавливаю запись.[/dim]")
                    break
                try:
                    seg = self.queue.get(timeout=0.3)
                except queue.Empty:
                    self._check_sources()
                    continue
                self._handle(seg, transcriber)
        except KeyboardInterrupt:
            say("\n[dim]Останавливаю запись, дорабатываю очередь…[/dim]")
        except BaseException as exc:
            # Что бы ни случилось, файл в finally всё равно собирается
            say(f"[red]Запись прервалась: {escape(repr(exc))}[/red]")
            code = 1
        finally:
            self._wrap_up(transcriber)
        return code

    def _close_devices(self) -> None:
        """Закрывает источники звука. Ошибка закрытия не мешает остальным шагам."""
        if self.capture is None:
            return
        try:
            stuck = self.capture.close()
        except BaseException as exc:
            stuck = []
            say(f"[yellow]Устройства закрылись с ошибкой: {escape(repr(exc))}[/yellow]")
        if stuck:
            say(f"[yellow]Источник «{', '.join(stuck)}» не отозвался.[/yellow]")

    @staticmethod
    def _close_recognizer(transcriber: Recognizer) -> None:
        try:
            transcriber.close()
        except Exception:
            pass

    def _wrap_up(self, transcriber: Recognizer) -> None:
        """Остановка при любом выходе: источники, остаток очереди, итоговый файл."""
        self._close_devices()

        # Реплики, которые источники успели дописать при остановке. Ещё один
        # Ctrl+C бросает остаток очереди, но не итоговый файл
        try:
            while True:
                try:
                    seg = self.queue.get_nowait()
                except queue.Empty:
                    break
                self._handle(seg, transcriber)
        except KeyboardInterrupt:
            say("[yellow]Остаток очереди брошен, собираю файл.[/yellow]")

        try:
            self._finalize()
        except BaseException as exc:
            say(
                f"[red]Итоговый файл не собрался: {escape(repr(exc))}. "
                "Реплики остались в файле как были записаны.[/red]"
            )
        self._close_recognizer(transcriber)

    def _finalize(self) -> None:
        """Пересобирает файл по времени начала реплик.

        Живой вывод идёт в порядке готовности расшифровки, а он не всегда совпадает
        с порядком, в котором говорили: длинная реплика обрабатывается дольше короткой.
        """
        if self.out_path is None:
            return
        if not (self.lines or self.gaps):
            say("[yellow]Речи не было, файл пустой.[/yellow]")
            return

        self.lines.sort(key=lambda x: x[0])
        # Дыры встают в файл на своё время, но в разбор речи не идут
        rows = [(t, who, text) for t, who, text, _ in self.lines]
        rows += [(t, who, f"_(не распознано: {why})_") for t, who, why in self.gaps]
        rows.sort(key=lambda x: x[0])
        body = "".join(f"**[{hhmmss(t)}] {who}:** {text}\n\n" for t, who, text in rows)
        lost = f" Не распознано: {len(self.gaps)}." if self.gaps else ""
        header = (
            f"# Расшифровка {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Длительность {hhmmss(rows[-1][0])}, реплик {len(self.lines)}.{lost} "
            f"Распознавание: {self.label}. «{ME}» это микрофон, «{THEM}» это звук из наушников.\n\n"
            "---\n\n"
        )
        # Разбор только твоей речи: собеседника оценивать смысла нет
        try:
            report = session_report(
                analyze_session(
                    self.analyzer, [(txt, dur) for _, who, txt, dur in self.lines if who == ME]
                )
            )
        except Exception as exc:
            report = ""
            say(f"[yellow]Разбор речи не получился: {escape(str(exc))}[/yellow]")

        text = header + body + ("\n---\n\n" + report if report else "")
        # Через временный файл: сбой посреди записи не должен стереть то,
        # что уже лежит в расшифровке
        tmp = self.out_path.with_name(self.out_path.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8", errors="replace")
            os.replace(tmp, self.out_path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            say(
                f"[red]Итоговый файл не записался: {escape(str(exc))}. "
                "Реплики остались в файле в порядке записи.[/red]"
            )
            return

        say(f"\n[bold green]Готово.[/bold green] Реплик: {len(self.lines)}")
        say(f"Файл: [bold]{escape(str(self.out_path))}[/bold]")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Дубль: живая расшифровка созвона из консоли",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--list-devices", action="store_true", help="показать устройства и выйти")
    p.add_argument("--mic", type=int, help="номер микрофона")
    p.add_argument("--speakers", type=int, help="номер loopback-устройства")
    p.add_argument(
        "--only",
        choices=["both", "mic", "speakers"],
        default="both",
        help="что слушать (по умолчанию оба источника)",
    )
    p.add_argument(
        "--model",
        help="large-v3, large-v3-turbo, medium или small. "
             "По умолчанию large-v3-turbo на видеокарте и small на процессоре",
    )
    p.add_argument("--language", default="ru", help="язык речи, по умолчанию ru")
    p.add_argument("--glossary", default=str(HERE / "glossary.txt"), help="файл с терминами")
    p.add_argument("--out", help="куда складывать расшифровки")
    p.add_argument("--title", help="название встречи, попадёт в имя файла")
    p.add_argument("--duration", type=float, help="остановиться самому через N секунд")
    p.add_argument("--cpu", action="store_true", help="считать на процессоре, без GPU")
    p.add_argument(
        "--api",
        action="store_true",
        help="распознавать через сервис, выбранный в настройках окна (ключ берётся оттуда же)",
    )
    args = p.parse_args()

    if args.list_devices:
        show_devices()
        return 0

    try:
        return Recorder(args).run()
    except KeyboardInterrupt:
        console.print("\n[dim]Остановлено до начала записи.[/dim]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
