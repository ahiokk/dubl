"""Разбор речи по расшифровке: заминки, самоподрезание, темп, паузы.

Считает то, что видно объективно и без всякой LLM. Смысл ответа тут не
оценивается. Этим занимается сверка с тезисами вопроса или разбор потом.

Словари живут в speech_patterns.yaml, чтобы пополнять их без правки кода.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PATTERNS_FILE = HERE / "speech_patterns.yaml"

# Паузу короче этого считаем дыханием, а не заминкой
PAUSE_MIN_SEC = 0.6


@dataclass
class Metrics:
    """Результат разбора одного ответа или одной реплики."""

    words: int = 0
    duration_sec: float = 0.0
    wpm: float = 0.0
    tempo_note: str = ""

    fillers: dict[str, int] = field(default_factory=dict)
    hedges: dict[str, int] = field(default_factory=dict)
    connectors: dict[str, int] = field(default_factory=dict)
    slips: dict[str, str] = field(default_factory=dict)

    pauses: list[float] = field(default_factory=list)
    longest_pause: float = 0.0

    @property
    def fillers_total(self) -> int:
        return sum(self.fillers.values())

    @property
    def hedges_total(self) -> int:
        return sum(self.hedges.values())

    @property
    def connectors_total(self) -> int:
        return sum(self.connectors.values())

    def per_100_words(self, count: int) -> float:
        """Нормировка, без неё длинный и короткий ответы не сравнить."""
        return round(count * 100 / self.words, 1) if self.words else 0.0

    def summary(self) -> str:
        """Одна строка для показа в интерфейсе."""
        parts = [f"{self.words} слов", f"{self.duration_sec:.0f} с", f"{self.wpm:.0f} сл/мин"]
        if self.tempo_note:
            parts.append(self.tempo_note)
        if self.fillers_total:
            parts.append(f"слов-паразитов {self.fillers_total}")
        if self.hedges_total:
            parts.append(f"неуверенных слов {self.hedges_total}")
        line = ", ".join(parts)
        if self.slips:
            line += ". Оговорки: " + ", ".join(f"{k}→{v}" for k, v in self.slips.items())
        return line


_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Текст к общему виду: без регистра, без ё и без знаков препинания."""
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def _compile(phrase: str) -> re.Pattern:
    """Ищет фразу целиком.

    Границей считается не только буква, но и дефис. Иначе «ну» находится внутри
    «нужно», а «вот-вот» (наречие «скоро», не заминка) разбирается на два «вот».
    Пробелы в многословных фразах допускают любое их количество.
    """
    parts = [re.escape(w) for w in phrase.lower().replace("ё", "е").split()]
    return re.compile(r"(?<![\w-])" + r"\s+".join(parts) + r"(?![\w-])", re.UNICODE)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числа: 1 слово, 2 слова, 5 слов, 21 слово."""
    if n % 100 in range(11, 15):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def load_patterns(path: Path) -> dict:
    """Общие словари плюс личные из speech_patterns.local.yaml, если он есть.

    Личный файл не попадает в git: туда идут свои словечки и оговорки.
    Списки из него дописываются к общим, словари дополняют и перекрывают.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local = path.with_name(f"{path.stem}.local{path.suffix}")
    if not local.exists():
        return raw
    extra = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
    for key, value in extra.items():
        base = raw.get(key)
        if isinstance(base, list) and isinstance(value, list):
            raw[key] = list(dict.fromkeys(base + value))
        elif isinstance(base, dict) and isinstance(value, dict):
            raw[key] = {**base, **value}
        else:
            raw[key] = value
    return raw


class SpeechAnalyzer:
    def __init__(self, patterns_file: Path | None = None) -> None:
        raw = load_patterns(patterns_file or PATTERNS_FILE)

        self._fillers = {p: _compile(p) for p in raw.get("паразиты", [])}
        self._hedges = {p: _compile(p) for p in raw.get("самоподрезание", [])}
        self._connectors = {p: _compile(p) for p in raw.get("связки", [])}
        self._slips = {
            said: (should, _compile(said)) for said, should in (raw.get("подмены") or {}).items()
        }

        tempo = raw.get("темп") or {}
        self.slow_wpm = float(tempo.get("медленно", 90))
        self.fast_wpm = float(tempo.get("быстро", 160))

    def _count(self, text: str, table: dict[str, re.Pattern]) -> dict[str, int]:
        found = {}
        for phrase, rx in table.items():
            n = len(rx.findall(text))
            if n:
                found[phrase] = n
        return found

    def analyze(
        self,
        text: str,
        duration_sec: float = 0.0,
        speech_spans: list[tuple[float, float]] | None = None,
    ) -> Metrics:
        """Разбирает ответ.

        speech_spans: отрезки речи (начало, конец) в секундах от начала ответа.
        Если переданы, паузы считаются по ним, а не угадываются по тексту:
        VAD знает, где ты молчал, точнее любой эвристики.
        """
        norm = normalize(text)
        m = Metrics()
        m.words = len(norm.split()) if norm else 0
        m.duration_sec = duration_sec

        if duration_sec > 0 and m.words:
            m.wpm = m.words * 60 / duration_sec
            if m.wpm < self.slow_wpm:
                m.tempo_note = "медленно"
            elif m.wpm > self.fast_wpm:
                m.tempo_note = "тараторишь"

        m.fillers = self._count(norm, self._fillers)
        m.hedges = self._count(norm, self._hedges)
        m.connectors = self._count(norm, self._connectors)

        for said, (should, rx) in self._slips.items():
            if rx.search(norm):
                m.slips[said] = should

        if speech_spans:
            m.pauses = self._gaps(speech_spans)
            m.longest_pause = max(m.pauses, default=0.0)

        return m

    @staticmethod
    def _gaps(spans: list[tuple[float, float]]) -> list[float]:
        """Промежутки тишины между отрезками речи."""
        ordered = sorted(spans)
        gaps = []
        for (_, end), (start, _) in zip(ordered, ordered[1:]):
            gap = start - end
            if gap >= PAUSE_MIN_SEC:
                gaps.append(round(gap, 2))
        return gaps


@dataclass
class Summary:
    """Разбор всей твоей речи за сессию."""

    metrics: Metrics
    replicas: int


def analyze_session(
    analyzer: SpeechAnalyzer, replicas: list[tuple[str, float]]
) -> Summary | None:
    """Склеивает все твои реплики и считает по ним.

    Именно склеивает: по отдельной фразе о темпе и частоте заминок не судят,
    слишком короткая выборка.
    """
    replicas = [(t, d) for t, d in replicas if t.strip()]
    if not replicas:
        return None
    text = " ".join(t for t, _ in replicas)
    total = sum(d for _, d in replicas)
    return Summary(analyzer.analyze(text, duration_sec=total), len(replicas))


def _listing(m: Metrics, found: dict[str, int]) -> str:
    """«6 (10.2 на 100 слов): „ну“ 2, „вот“ 2» либо «ни одного»."""
    if not found:
        return "ни одного"
    total_n = sum(found.values())
    top = sorted(found.items(), key=lambda x: -x[1])
    return (
        f"{total_n} ({m.per_100_words(total_n)} на 100 слов): "
        + ", ".join(f"«{w}» {n}" for w, n in top)
    )


def _headline(s: Summary) -> str:
    m = s.metrics
    return (
        f"{s.replicas} {plural(s.replicas, 'реплика', 'реплики', 'реплик')}, "
        f"суммарно {m.duration_sec:.0f} с, "
        f"{m.words} {plural(m.words, 'слово', 'слова', 'слов')}, "
        f"темп {m.wpm:.0f} сл/мин" + (f", {m.tempo_note}" if m.tempo_note else "")
    )


def session_report(summary: Summary | None) -> str:
    """Готовый кусок markdown для файла расшифровки."""
    if summary is None:
        return ""
    m = summary.metrics
    lines = ["## Как ты говорил", "", _headline(summary), ""]

    for title, found in (
        ("Слова-паразиты", m.fillers),
        ("Неуверенные слова", m.hedges),
        ("Связки", m.connectors),
    ):
        lines += [f"**{title}:** {_listing(m, found)}", ""]

    if m.slips:
        lines += [
            "**Оговорки:** "
            + ", ".join(f"сказал «{k}», надо «{v}»" for k, v in m.slips.items()),
            "",
        ]
    return "\n".join(lines)


def session_ui(summary: Summary | None) -> dict | None:
    """То же самое, но полями: интерфейс раскладывает их по строкам сам."""
    if summary is None:
        return None
    m = summary.metrics
    data = {
        "replicas": summary.replicas,
        "duration": round(m.duration_sec),
        "words": m.words,
        "wpm": round(m.wpm),
        "tempo": m.tempo_note,
        "fillers": _listing(m, m.fillers),
        "hedges": _listing(m, m.hedges),
        "connectors": _listing(m, m.connectors),
    }
    if m.slips:
        data["slips"] = ", ".join(f"сказал «{k}», надо «{v}»" for k, v in m.slips.items())
    return data


def compare(before: Metrics, after: Metrics) -> str:
    """Строка изменений между попытками."""

    def arrow(old: float, new: float, less_is_better: bool = True) -> str:
        if old == new:
            return "="
        better = new < old if less_is_better else new > old
        return ("лучше " if better else "хуже ") + f"{old:g}→{new:g}"

    return ", ".join(
        [
            f"время {arrow(round(before.duration_sec), round(after.duration_sec))}",
            f"слова-паразиты {arrow(before.fillers_total, after.fillers_total)}",
            f"неуверенные слова {arrow(before.hedges_total, after.hedges_total)}",
        ]
    )
