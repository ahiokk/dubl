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
import queue
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from asr import (
    LocalWhisper,
    Recognizer,
    RecognizerError,
    cuda_available,
    load_glossary,
    make_recognizer,
)
from capture import Capture, Segment, list_devices
from speech_metrics import SpeechAnalyzer, analyze_session, session_report

HERE = Path(__file__).resolve().parent
ME = "Я"
THEM = "Собеседник"
STYLES = {ME: "bold green", THEM: "bold cyan"}
console = Console()


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


class Recorder:
    def __init__(self, args) -> None:
        self.args = args
        self.queue: queue.Queue[Segment] = queue.Queue()
        # (время от начала, кто, текст, длительность реплики)
        self.lines: list[tuple[float, str, str, float]] = []
        self.analyzer = SpeechAnalyzer()
        self.capture: Capture | None = None
        self.session_start = time.time()

        out_dir = Path(args.out) if args.out else HERE / "расшифровки"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.out_path = out_dir / f"{stamp}{' ' + args.title if args.title else ''}.md"

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
                console.print("[yellow]Не нашёл loopback для текущего вывода звука.[/yellow]")
                console.print("[yellow]Укажи его вручную: --speakers НОМЕР[/yellow]")
            else:
                self.capture.add(spk_idx, THEM, self.queue, self.session_start)

        if not self.capture.sources:
            console.print("[red]Нечего слушать.[/red]")
            sys.exit(1)

        self.capture.start()
        listening = ", ".join(
            f"[{STYLES[s.speaker]}]{s.speaker}[/]" for s in self.capture.sources
        )
        console.print(f"Слушаю: {listening}")

    def _write_line(self, seg: Segment, text: str) -> None:
        """Пишет реплику на диск сразу, чтобы ничего не потерялось при обрыве."""
        with self.out_path.open("a", encoding="utf-8") as f:
            f.write(f"**[{hhmmss(seg.t_start)}] {seg.speaker}:** {text}\n\n")

    def _handle(self, seg: Segment, transcriber: Recognizer) -> None:
        try:
            text = transcriber.transcribe(seg.audio)
        except RecognizerError as exc:
            console.print(f"[red]{hhmmss(seg.t_start)} {seg.speaker}: не распознано, {exc}[/red]")
            return
        if not text:
            return
        self.lines.append((seg.t_start, seg.speaker, text, seg.duration))
        console.print(
            f"[dim]{hhmmss(seg.t_start)}[/dim] "
            f"[{STYLES[seg.speaker]}]{seg.speaker}:[/] {text}"
        )
        self._write_line(seg, text)

    def _check_sources(self) -> None:
        if self.capture is None:
            return
        for s in self.capture.sources:
            if s.error is not None:
                console.print(f"[red]Источник «{s.speaker}» отвалился: {s.error}[/red]")
                s.error = None

    def _make_recognizer(self) -> Recognizer:
        glossary = load_glossary(
            Path(self.args.glossary) if self.args.glossary else None, self.args.language
        )
        if not self.args.api:
            # Без видеокарты large-v3 не успевает за речью, на процессоре берём small
            on_gpu = cuda_available() and not self.args.cpu
            return LocalWhisper(
                self.args.model or ("large-v3" if on_gpu else "small"),
                device="cpu" if self.args.cpu else "auto",
                language=self.args.language,
                glossary=glossary,
            )
        # Сервис, модель и ключ те же, что выбраны в окне: дублировать
        # их флагами незачем, а ключ в командной строке осядет в истории
        try:
            saved = json.loads((HERE / "настройки.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        cfg = {**saved.get("распознавание", {}), "режим": "api", "язык": self.args.language}
        return make_recognizer(cfg, glossary)

    def run(self) -> int:
        console.print("[dim]Готовлю распознавание…[/dim]")
        t0 = time.perf_counter()
        try:
            transcriber = self._make_recognizer()
        except Exception as exc:
            console.print(f"[red]Распознавание не запустилось: {exc}[/red]")
            if not self.args.api:
                console.print("[yellow]Попробуй --cpu или --api[/yellow]")
            return 1
        self.label = transcriber.label
        console.print(f"[dim]{self.label}, готово за {time.perf_counter() - t0:.1f} c[/dim]")

        header = (
            f"# Расшифровка {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Распознавание: {self.label}. «{ME}» это микрофон, «{THEM}» это звук из наушников.\n\n"
        )
        self.out_path.write_text(header, encoding="utf-8")

        self._start_sources()
        console.print(f"[dim]Пишу в {self.out_path}[/dim]")
        console.print("[dim]Остановить: Ctrl+C[/dim]\n")

        deadline = time.time() + self.args.duration if self.args.duration else None
        stopping = False
        try:
            while True:
                if deadline and time.time() > deadline:
                    console.print("\n[dim]Время вышло, останавливаю запись.[/dim]")
                    break
                try:
                    seg = self.queue.get(timeout=0.3)
                except queue.Empty:
                    self._check_sources()
                    continue
                self._handle(seg, transcriber)
        except KeyboardInterrupt:
            stopping = True
            console.print("\n[dim]Останавливаю запись, дорабатываю очередь…[/dim]")

        if self.capture is not None:
            stuck = self.capture.close()
            if stuck:
                console.print(f"[yellow]Источник «{', '.join(stuck)}» не отозвался.[/yellow]")

        # Реплики, которые источники успели дописать при остановке
        while True:
            try:
                seg = self.queue.get_nowait()
            except queue.Empty:
                break
            self._handle(seg, transcriber)

        self._finalize()
        return 0 if stopping else 0

    def _finalize(self) -> None:
        """Пересобирает файл по времени начала реплик.

        Живой вывод идёт в порядке готовности расшифровки, а он не всегда совпадает
        с порядком, в котором говорили: длинная реплика обрабатывается дольше короткой.
        """
        if not self.lines:
            console.print("[yellow]Речи не было, файл пустой.[/yellow]")
            return

        self.lines.sort(key=lambda x: x[0])
        body = "".join(
            f"**[{hhmmss(t)}] {who}:** {text}\n\n" for t, who, text, _ in self.lines
        )
        duration = self.lines[-1][0]
        header = (
            f"# Расшифровка {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Длительность {hhmmss(duration)}, реплик {len(self.lines)}. "
            f"Распознавание: {self.label}. «{ME}» это микрофон, «{THEM}» это звук из наушников.\n\n"
            "---\n\n"
        )
        # Разбор только твоей речи: собеседника оценивать смысла нет
        report = session_report(
            analyze_session(
                self.analyzer, [(txt, dur) for _, who, txt, dur in self.lines if who == ME]
            )
        )
        self.out_path.write_text(
            header + body + ("\n---\n\n" + report if report else ""), encoding="utf-8"
        )

        console.print(f"\n[bold green]Готово.[/bold green] Реплик: {len(self.lines)}")
        console.print(f"Файл: [bold]{self.out_path}[/bold]")


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
             "По умолчанию large-v3 на видеокарте и small на процессоре",
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

    return Recorder(args).run()


if __name__ == "__main__":
    sys.exit(main())
