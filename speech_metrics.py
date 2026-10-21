"""Разбор речи по расшифровке: заминки, самоподрезание, темп, паузы.

Считает то, что видно объективно и без всякой LLM. Смысл ответа тут не
оценивается. Этим занимается сверка с тезисами вопроса или разбор потом.

Словари живут в speech_patterns.yaml, чтобы пополнять их без правки кода.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PATTERNS_FILE = HERE / "speech_patterns.yaml"

# Паузу короче этого считаем дыханием, а не заминкой
PAUSE_MIN_SEC = 0.6

# Больше этого YAML-файл не читаем: банки и словари пишет человек или нейросеть,
# настоящий файл занимает килобайты (общий банк около 17 КБ)
MAX_YAML_BYTES = 256 << 10

# Сколько замечаний о файле хранить: чужой файл может породить их тысячи
MAX_PROBLEMS = 50


class YamlRefused(ValueError):
    """YAML нельзя прочитать. Текст короткий, его показывают человеку как есть."""


# Эти типы YAML угадывает по виду значения: yes/no/on/off становятся булевыми,
# 2024-10-05 датой, 0123 восьмеричным числом. В банках и словарях это всегда
# текст («слова: [no, on]»), а числа мы разбираем сами там, где они нужны
_PLAIN_TAGS = {
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
}


class _PlainLoader(yaml.SafeLoader):
    """SafeLoader без якорей и алиасов, где скаляры остаются строками.

    Алиасы (`&a`, `*a`) нужны только для повторов, а чужой файл с девятью
    уровнями алиасов за килобайты текста превращается в миллиарды узлов: стоит
    обойти такую структуру, и программа зависает или съедает память. Поэтому
    при первом же якоре или алиасе загрузка обрывается.
    """

    def compose_node(self, parent, index):
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise yaml.composer.ComposerError(
                None, None, "якоря (&) и алиасы (*) в этих файлах запрещены", event.start_mark
            )
        return super().compose_node(parent, index)


# Таблица неявных типов общая у всех загрузчиков, поэтому копия своя
_PlainLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in _PLAIN_TAGS]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _short(text: object, limit: int = 160) -> str:
    """Одна строка не длиннее limit: в сообщение не должно попадать полфайла."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _yaml_error_text(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or getattr(exc, "reason", None) or "ошибка разбора"
    where = f"строка {mark.line + 1}, столбец {mark.column + 1}: " if mark else ""
    return _short(f"некорректный YAML, {where}{problem}")


def limit_problems(problems: list[str], limit: int = MAX_PROBLEMS) -> list[str]:
    """Первые limit замечаний и строка «и ещё N» вместо остальных."""
    if len(problems) <= limit:
        return problems
    return problems[:limit] + [f"и ещё {len(problems) - limit} замечаний"]


def read_yaml(path: Path, max_bytes: int = MAX_YAML_BYTES):
    """Читает YAML, которому нельзя доверять (банк вопросов, личные словари).

    Любая беда превращается в YamlRefused с коротким русским текстом: файл
    слишком большой, не UTF-8, с синтаксической ошибкой, с якорями или слишком
    глубоко вложенный. Пустой файл даёт None.
    """
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes + 1)
    except OSError as exc:
        raise YamlRefused(f"файл не читается ({_short(exc.strerror or type(exc).__name__, 60)})") from None
    if len(data) > max_bytes:
        raise YamlRefused(f"файл слишком большой (больше {max(1, max_bytes >> 10)} КБ)")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise YamlRefused("файл не в кодировке UTF-8") from None
    try:
        return yaml.load(text, Loader=_PlainLoader)
    except yaml.YAMLError as exc:
        raise YamlRefused(_yaml_error_text(exc)) from None
    except RecursionError:
        raise YamlRefused("слишком глубокая вложенность") from None
    except Exception:
        # Явный тег с чужим значением: «!!int abc», «!!timestamp 2024-13-45», «!!bool maybe».
        # Конструктор SafeLoader бросает обычное исключение питона (ValueError, TypeError,
        # OverflowError, KeyError), а не YAMLError
        raise YamlRefused("в файле тег с недопустимым значением (например !!int abc)") from None


def scalar_text(value: object) -> str | None:
    """Скаляр YAML как строка без крайних пробелов.

    Пустое значение даёт "". Список или словарь дают None: вызывающий решает,
    считать это ошибкой или пропустить.
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        return str(value).strip()
    except ValueError:  # целое из тысяч цифр не превращается в строку
        return None


def to_number(value: object) -> float | None:
    """Конечное число из скаляра: 30, "30", "1,5". Остальное, включая inf и NaN, даёт None."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            number = float(value.strip().replace(",", "."))
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


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


_LIST_SECTIONS = ("паразиты", "самоподрезание", "связки")
_SECTIONS = (*_LIST_SECTIONS, "подмены", "темп")
DEFAULT_SLOW_WPM = 90.0
DEFAULT_FAST_WPM = 160.0


def _clean_phrases(value: object) -> tuple[list[str], int]:
    """Список фраз из раздела словаря и сколько элементов пришлось выкинуть.

    Одиночное слово вместо списка принимаем за список из одного слова. Пустые
    элементы и те, что после нормализации пусты («...», «—»), выкидываем: фраза без
    букв находилась бы на каждой границе слов.
    """
    if value is None:
        return [], 0
    items = value if isinstance(value, list) else [value]
    out: dict[str, str] = {}
    dropped = 0
    for item in items:
        phrase = scalar_text(item)
        if not phrase or not normalize(phrase):
            dropped += 1
        else:
            out.setdefault(normalize(phrase), phrase)
    return list(out.values()), dropped


def _clean_slips(value: object) -> tuple[dict[str, str], int]:
    out: dict[str, str] = {}
    dropped = 0
    for said, should in value.items():
        a, b = scalar_text(said), scalar_text(should)
        if not a or not b or not normalize(a):
            dropped += 1
        else:
            out[a] = b
    return out, dropped


def _apply_patterns_file(result: dict, path: Path, problems: list[str]) -> None:
    """Дописывает словари из одного файла в result. Ошибки уходят в problems.

    Файл с битым YAML или не тем корнем пропускается целиком, а в нормальном
    файле пропускается только испорченный раздел или элемент.
    """
    name = path.name
    try:
        data = read_yaml(path)
    except YamlRefused as exc:
        problems.append(f"{name}: {exc}, файл пропущен")
        return
    if data is None:
        return
    if not isinstance(data, dict):
        problems.append(
            f"{name}: на верхнем уровне должны быть разделы (паразиты:, подмены: и так далее), файл пропущен"
        )
        return

    for key, value in data.items():
        title = scalar_text(key) or ""
        try:
            if title in _LIST_SECTIONS:
                if isinstance(value, dict):
                    problems.append(f"{name}: раздел «{title}» должен быть списком слов, пропущен")
                    continue
                phrases, dropped = _clean_phrases(value)
                if dropped:
                    problems.append(f"{name}: в разделе «{title}» пропущено пустых элементов: {dropped}")
                known = {normalize(p) for p in result[title]}
                result[title] += [p for p in phrases if normalize(p) not in known]
            elif title == "подмены":
                if not isinstance(value, dict):
                    if value is not None:
                        problems.append(f"{name}: раздел «подмены» должен быть словарём «сказанное: верное», пропущен")
                    continue
                slips, dropped = _clean_slips(value)
                if dropped:
                    problems.append(f"{name}: в разделе «подмены» пропущено неполных пар: {dropped}")
                result["подмены"].update(slips)
            elif title == "темп":
                if not isinstance(value, dict):
                    if value is not None:
                        problems.append(f"{name}: раздел «темп» должен быть словарём (медленно, быстро), пропущен")
                    continue
                for sub, number in value.items():
                    sub_name = scalar_text(sub) or ""
                    if sub_name not in ("медленно", "быстро"):
                        problems.append(f"{name}: в разделе «темп» неизвестный ключ «{_short(sub_name, 30)}»")
                        continue
                    wpm = to_number(number)
                    if wpm is None or wpm <= 0:
                        problems.append(f"{name}: «темп → {sub_name}» должно быть положительным числом, оставлено прежнее")
                        continue
                    result["темп"][sub_name] = wpm
            else:
                problems.append(
                    f"{name}: неизвестный раздел «{_short(title, 40)}», ждём: {', '.join(_SECTIONS)}"
                )
        except Exception as exc:  # раздел испорчен неожиданным образом: остальные работают
            problems.append(f"{name}: раздел «{_short(title, 40)}» не разобран ({type(exc).__name__})")


def load_patterns(path: Path, problems: list[str] | None = None) -> dict:
    """Общие словари плюс личные из speech_patterns.local.yaml, если он есть.

    Личный файл не попадает в git: туда идут свои словечки и оговорки.
    Списки из него дописываются к общим, словари дополняют и перекрывают.

    Ошибка в любом из файлов не роняет программу: испорченное пропускается, а
    что именно, дописывается в problems по строке на случай.
    """
    if problems is None:
        problems = []
    result: dict = {
        **{section: [] for section in _LIST_SECTIONS},
        "подмены": {},
        "темп": {"медленно": DEFAULT_SLOW_WPM, "быстро": DEFAULT_FAST_WPM},
    }
    _apply_patterns_file(result, path, problems)
    local = path.with_name(f"{path.stem}.local{path.suffix}")
    try:
        has_local = local.exists()
    except OSError:
        has_local = False
    if has_local:
        _apply_patterns_file(result, local, problems)
    problems[:] = limit_problems(problems)
    return result


class SpeechAnalyzer:
    def __init__(self, patterns_file: Path | None = None) -> None:
        # Что не удалось прочитать в словарях. Окно может показать это человеку,
        # а разбор речи работает по тому, что прочиталось
        self.problems: list[str] = []
        raw = load_patterns(patterns_file or PATTERNS_FILE, self.problems)

        self._fillers = {p: _compile(normalize(p)) for p in raw["паразиты"]}
        self._hedges = {p: _compile(normalize(p)) for p in raw["самоподрезание"]}
        self._connectors = {p: _compile(normalize(p)) for p in raw["связки"]}
        self._slips = {
            said: (should, _compile(normalize(said))) for said, should in raw["подмены"].items()
        }

        self.slow_wpm = raw["темп"]["медленно"]
        self.fast_wpm = raw["темп"]["быстро"]

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
