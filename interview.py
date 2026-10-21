"""Тренажёр собеседования: банк вопросов, разбор ответа, добивающие вопросы.

Смысл здесь не в том, чтобы прогнать много вопросов, а в том, чтобы гонять один
до чистого ответа. Поэтому после каждой попытки видно, что изменилось с прошлой.

Правильность ответа проверяется не по смыслу, а по тезисам: к вопросу приложен
список того, что должно прозвучать, и слова-маркеры для каждого пункта. Грубо,
зато мгновенно и без всякой модели. Пропущенный тезис превращается в добивающий
вопрос, как переспросил бы живой интервьюер.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

from archive import _FORBIDDEN, _is_device_name
from speech_metrics import (
    MAX_YAML_BYTES,
    Metrics,
    SpeechAnalyzer,
    YamlRefused,
    _PlainLoader,
    _yaml_error_text,
    limit_problems,
    normalize,
    plural,
    read_yaml,
    scalar_text,
    to_number,
)

HERE = Path(__file__).resolve().parent
BANKS_DIR = HERE / "вопросы"
FORMAT_EXAMPLE = "пример.yaml"
BANKS_FIRST = ("Пробный собес.yaml", "Общие вопросы.yaml")
SESSIONS_DIR = HERE / "мок-собесы"

# Больше этого числа попыток на один вопрос не даём: если не идёт сегодня,
# надо двигаться дальше, а не буксовать
MAX_ATTEMPTS = 3

# Файл разбора больше этого не читаем: настоящий весит десятки килобайт, а в папку
# могут положить что угодно
MAX_SESSION_BYTES = 8 << 20
MAX_BANK_NAME = 100


@dataclass
class Level:
    """Насколько строго спрашиваем."""

    name: str
    max_question_level: int
    time_factor: float  # во сколько раз мягче целевое время
    filler_factor: float  # во сколько раз больше заминок прощаем
    followups: bool
    allow_repeat: bool


LEVELS: dict[str, Level] = {
    "разминка": Level("разминка", 1, 1.6, 2.5, False, True),
    "рабочий": Level("рабочий", 2, 1.0, 1.0, True, True),
    "боевой": Level("боевой", 3, 0.9, 0.7, True, False),
}
DEFAULT_LEVEL = "рабочий"


@lru_cache(maxsize=8192)
def _marker(raw: str) -> tuple[str, re.Pattern | None] | None:
    """Маркер, подготовленный к поиску: (нормализованный текст, регулярка или None).

    Маркер, в котором после normalize не осталось букв и цифр («@», «...», «—», «-», пробелы),
    даёт None: он нашёлся бы в любом ответе, поэтому тезис он не закрывает никогда.

    Маркер без пробела ищется подстрокой, на этом держатся все банки: «нормир»
    находит «нормированная». Составной маркер задаёт фразу: каждое его слово это
    начало слова в ответе, слова идут подряд, а первое начинается с границы слова.
    Так «стандартн отклонен» находит «стандартное отклонение» и «стандартного
    отклонения», «до един» находит «до единицы», но не «чудо единица». Обрезать
    окончание можно у каждого слова, а не только у последнего.
    """
    text = normalize(raw)
    if not re.search(r"\w", text):  # пусто или одни дефисы
        return None
    parts = text.split()
    if len(parts) < 2:
        return text, None
    # Граница слова та же, что в speech_metrics._compile: буква, цифра или дефис
    rx = re.compile(
        r"(?<![\w-])" + r"[\w-]*\s+".join(re.escape(p) for p in parts), re.UNICODE
    )
    return text, rx


@dataclass
class Thesis:
    text: str
    words: list[str] = field(default_factory=list)
    followup: str = ""

    @property
    def checkable(self) -> bool:
        """Без слов-маркеров тезис остаётся просто пунктом для самопроверки."""
        return bool(self.words)

    def covered_by(self, normalized_answer: str) -> bool:
        for w in self.words:
            found = _marker(w)
            if found is None:  # в маркере нет букв: он нашёлся бы в любом ответе
                continue
            text, rx = found
            if rx is None:
                if text in normalized_answer:
                    return True
            elif rx.search(normalized_answer):
                return True
        return False


@dataclass
class Question:
    id: str
    text: str
    category: str = "общий"
    level: int = 1
    target_sec: tuple[float, float] | None = None
    max_fillers: int | None = None
    theses: list[Thesis] = field(default_factory=list)
    note: str = ""


@dataclass
class Attempt:
    text: str
    metrics: Metrics
    covered: list[bool]
    # Пары «уточняющий вопрос и что ответил». Живой интервьюер тоже переспрашивает,
    # и договорённое засчитывается, а не пропадает
    followups: list[tuple[str, str]] = field(default_factory=list)

    @property
    def missing(self) -> list[int]:
        return [i for i, ok in enumerate(self.covered) if not ok]

    @property
    def full_text(self) -> str:
        """Основной ответ вместе со всем, что договорил после уточнений."""
        return " ".join([self.text] + [a for _, a in self.followups])


@dataclass
class Verdict:
    """Что показать после ответа."""

    attempt_no: int
    duration: float
    target: tuple[float, float] | None
    time_verdict: str  # "коротко" | "долго" | "в норме" | ""
    fillers: int
    fillers_limit: int | None
    hedges: int
    covered: list[bool]
    missing_theses: list[Thesis]
    followup: str
    should_retry: bool
    can_retry: bool
    delta: str = ""
    # сколько тезисов закрылось именно ответом на уточняющий вопрос
    just_closed: list[str] = field(default_factory=list)
    after_followup: bool = False
    # сравнение с лучшим прошлым результатом по этому же вопросу
    versus_past: str = ""

    @property
    def clean(self) -> bool:
        """Ответ, к которому нет претензий."""
        return (
            self.time_verdict in ("", "в норме")
            and (self.fillers_limit is None or self.fillers <= self.fillers_limit)
            and not self.missing_theses
        )


class BankError(ValueError):
    """Банк не читается. Текст короткий и называет вопрос и поле: его увидит человек."""


_KINDS = {dict: "набор полей", list: "список", str: "текст"}


def _kind(value: object) -> str:
    return _KINDS.get(type(value), "число или другое значение")


def _clip(text: object, limit: int = 40) -> str:
    """Кусок файла для сообщения об ошибке: одна короткая строка, не больше."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _level(value: object, label: str, notes: list[str]) -> int:
    if value is None:
        return 1
    number = to_number(value)
    if number is None:
        notes.append(f"{label}: «уровень» не число ({_clip(value, 20)}), взят 1")
        return 1
    level = int(number)
    if not 1 <= level <= 3:
        fixed = min(3, max(1, level))
        notes.append(f"{label}: «уровень» {level} вне диапазона 1–3, взят {fixed}")
        return fixed
    return level


def _target(value: object, label: str, notes: list[str]) -> tuple[float, float] | None:
    if value is None:
        return None
    pair = [to_number(x) for x in value] if isinstance(value, list) and len(value) == 2 else None
    if pair is None or any(x is None or x < 0 for x in pair):
        notes.append(f"{label}: «цель_сек» должна быть парой чисел [от, до], цель не задана")
        return None
    lo, hi = sorted(pair)
    return lo, hi


def _max_fillers(value: object, label: str, notes: list[str]) -> int | None:
    if value is None:
        return None
    number = to_number(value)
    if number is None or number < 0:
        notes.append(f"{label}: «паразитов_макс» должно быть целым числом от нуля, лимита нет")
        return None
    return int(number)


def _optional_text(value: object, field_name: str, label: str, notes: list[str]) -> str:
    text = scalar_text(value)
    if text is None:
        notes.append(f"{label}: «{field_name}» должно быть текстом, а не {_kind(value)}, поле пропущено")
        return ""
    return text


def _parse_thesis(
    t: object, k: int, label: str, notes: list[str], advise: bool = False
) -> Thesis:
    """Один тезис. advise включают при разборе ответа нейросети: там маркеры
    придумывает модель, и общие слова из них убираются, а пустое замечается."""
    where = f"{label}, тезис №{k}"
    if not isinstance(t, dict):
        raise BankError(f"{where}: ожидался набор полей (текст, слова, добить), а не {_kind(t)}")
    text = scalar_text(t.get("текст"))
    if text is None:
        raise BankError(f"{where}: поле «текст» должно быть текстом, а не {_kind(t.get('текст'))}")
    if not text:
        raise BankError(f"{where}: поле «текст» пустое или отсутствует")

    raw_words = t.get("слова")
    if isinstance(raw_words, dict):
        notes.append(f"{where}: «слова» должны быть списком, а не набором полей, маркеры пропущены")
        items: list = []
    elif isinstance(raw_words, list):
        items = raw_words
    else:
        # «слова: кеш» одной строкой это один маркер, а не набор букв
        items = [] if raw_words is None else [raw_words]
    words: list[str] = []
    seen: set[str] = set()
    for w in items:
        s = scalar_text(w)
        # Маркер без букв («», «...») нашёлся бы в любом ответе и закрыл тезис даром
        if s and normalize(s) and normalize(s) not in seen:
            seen.add(normalize(s))
            if advise and Thesis("", [s]).covered_by(_NEUTRAL_NORM):
                notes.append(
                    f"{where}: маркер «{_clip(s, 30)}» слишком общий, он находится "
                    "в любой речи, убран"
                )
                continue
            words.append(s)
    if items and not words:
        notes.append(
            f"{where}: в «слова» нет ни одного рабочего маркера, "
            "тезис остаётся пунктом для самопроверки и засчитывается всегда"
        )
    elif advise and not items:
        notes.append(
            f"{where}: нет маркеров в поле «слова», "
            "тезис остаётся пунктом для самопроверки и засчитывается всегда"
        )
    return Thesis(
        text=text,
        words=words,
        followup=_optional_text(t.get("добить"), "добить", where, notes),
    )


def _parse_question(
    q: object, n: int, notes: list[str], advise: bool = False
) -> tuple[Question, str]:
    """Один вопрос банка и его id из файла ("" если не задан). Кривое поле: BankError."""
    label = f"вопрос №{n}"
    if not isinstance(q, dict):
        raise BankError(f"{label}: ожидался набор полей (id, вопрос, тезисы), а не {_kind(q)}")
    text = scalar_text(q.get("вопрос"))
    if text is None:
        raise BankError(f"{label}: поле «вопрос» должно быть текстом, а не {_kind(q.get('вопрос'))}")
    if not text:
        raise BankError(f"{label}: поле «вопрос» пустое или отсутствует")
    label = f"вопрос №{n} «{_clip(text)}»"

    raw_id = scalar_text(q.get("id"))
    if raw_id is None:
        notes.append(f"{label}: «id» должен быть текстом, а не {_kind(q.get('id'))}, взят свой")
        raw_id = ""

    raw_theses = q.get("тезисы")
    if raw_theses is None:
        raw_theses = []
    elif not isinstance(raw_theses, list):
        raise BankError(f"{label}: поле «тезисы» должно быть списком, а не {_kind(raw_theses)}")
    theses = [
        _parse_thesis(t, k, label, notes, advise)
        for k, t in enumerate((t for t in raw_theses if t is not None), 1)
    ]
    if advise and not theses:
        notes.append(
            f"{label}: нет тезисов, ответ нельзя проверить по смыслу, "
            "останутся только время и слова-паразиты"
        )

    category = _optional_text(q.get("категория"), "категория", label, notes)
    question = Question(
        id="",
        text=text,
        category=category or "общий",
        level=_level(q.get("уровень"), label, notes),
        target_sec=_target(q.get("цель_сек"), label, notes),
        max_fillers=_max_fillers(q.get("паразитов_макс"), label, notes),
        theses=theses,
        note=_optional_text(q.get("заметка"), "заметка", label, notes),
    )
    return question, raw_id


class Bank:
    def __init__(
        self, name: str, questions: list[Question], problems: list[str] | None = None
    ) -> None:
        self.name = name
        self.questions = questions
        # Что в файле было не так, но банк при этом читается: поправили значение
        # или пропустили поле. Окно может показать это рядом с банком
        self.problems: list[str] = problems or []

    @classmethod
    def load(cls, path: Path) -> "Bank":
        """Читает банк. Кривое поле, битый YAML, слишком большой файл дают BankError.

        Банк пишет нейросеть или другой человек, поэтому файлу не верим: типы
        приводятся на входе, а непоправимое называется ошибкой с номером вопроса
        и именем поля.
        """
        path = Path(path)
        try:
            raw = read_yaml(path)
        except YamlRefused as exc:
            raise BankError(str(exc)) from None
        try:
            return cls._from_raw(raw, path.stem)
        except BankError:
            raise
        except Exception as exc:  # страховка: чужой файл не должен ронять окно
            raise BankError(f"банк не удалось разобрать ({type(exc).__name__})") from None

    @classmethod
    def _from_raw(
        cls,
        raw: object,
        stem: str,
        errors: list[str] | None = None,
        advise: bool = False,
    ) -> "Bank":
        """Банк из разобранного YAML или JSON. Первое же кривое поле это BankError.

        Если передан список errors, вопрос с кривым полем пропускается, а его ошибка
        ложится в список: так ответ нейросети показывает все беды сразу. advise
        добавляет советы по содержанию (см. _parse_thesis).
        """
        notes: list[str] = []
        name = stem
        if raw is None:
            items: object = None
        elif isinstance(raw, list):
            items = raw  # корень-список значит просто список вопросов
        elif isinstance(raw, dict):
            if "вопросы" not in raw:
                raise BankError("нет поля «вопросы»")
            items = raw["вопросы"]
            title = scalar_text(raw.get("название"))
            if title is None:
                notes.append(f"«название» должно быть текстом, а не {_kind(raw['название'])}")
            elif title:
                name = _clip(title, MAX_BANK_NAME)
        else:
            raise BankError(f"в корне файла {_kind(raw)}, а нужен набор полей (название, вопросы)")
        if items is None:
            items = []
        elif not isinstance(items, list):
            raise BankError(f"поле «вопросы» должно быть списком, а не {_kind(items)}")

        parsed: list[tuple[int, Question, str]] = []
        for n, q in enumerate(items, 1):
            if q is None:  # пустой «-» в списке
                continue
            try:
                question, explicit_id = _parse_question(q, n, notes, advise)
            except BankError as exc:
                if errors is None:
                    raise
                errors.append(str(exc))
                continue
            parsed.append((n, question, explicit_id))
        if not parsed:
            raise BankError(
                "в банке нет ни одного рабочего вопроса" if errors else "в банке нет вопросов"
            )

        # Id нужен окну и истории, повторы склеили бы разные вопросы. Заданные
        # руками id сначала занимает первый хозяин, остальным подбираем свободные
        taken: set[str] = set()
        for _, question, explicit_id in parsed:
            if explicit_id and explicit_id not in taken:
                taken.add(explicit_id)
                question.id = explicit_id
        # Для каждого base помним, с какого номера искать дальше: иначе банк из
        # тысяч одинаковых id перебирал бы заново всё занятое и грузился минутами
        next_number: dict[str, int] = {}
        for n, question, explicit_id in parsed:
            if question.id:
                continue
            base = explicit_id or f"q{n}"
            candidate, k = base, next_number.get(base, 2)
            if candidate in taken:
                candidate = f"{base}_{k}"
                while candidate in taken:
                    k += 1
                    candidate = f"{base}_{k}"
                next_number[base] = k + 1
            taken.add(candidate)
            question.id = candidate
            if explicit_id:
                notes.append(f"вопрос №{n}: id «{_clip(base)}» повторяется, заменён на «{_clip(candidate)}»")

        return cls(name, [question for _, question, _ in parsed], limit_problems(notes))

    def categories(self) -> list[str]:
        """Темы в порядке первого появления: так они идут от простого к сложному."""
        seen = []
        for q in self.questions:
            if q.category not in seen:
                seen.append(q.category)
        return seen

    @staticmethod
    def available() -> list[dict]:
        BANKS_DIR.mkdir(parents=True, exist_ok=True)
        out = []
        # Образец формата для тех, кто пишет банк руками, в списке тренажёра не нужен.
        # Пробный собес и общие вопросы идут первыми: с них новичок и начинает
        files = [p for p in BANKS_DIR.glob("*.yaml") if p.name != FORMAT_EXAMPLE]
        files.sort(key=lambda p: (BANKS_FIRST.index(p.name) if p.name in BANKS_FIRST else len(BANKS_FIRST), p.name))
        for p in files:
            try:
                bank = Bank.load(p)
                out.append(
                    {
                        "file": p.name,
                        "name": bank.name,
                        "count": len(bank.questions),
                        "categories": bank.categories(),
                        # Что в файле поправили или пропустили при чтении
                        "problems": bank.problems,
                        # Раскладка вопросов, чтобы окно само считало, сколько их
                        # останется при выбранных теме и уровне и сколько это займёт
                        "questions": [
                            {
                                "category": q.category,
                                "level": q.level,
                                "target": list(q.target_sec) if q.target_sec else None,
                            }
                            for q in bank.questions
                        ],
                    }
                )
            except Exception as exc:
                out.append(
                    {
                        "file": p.name,
                        "name": f"{p.stem} (битый файл: {exc})",
                        "count": 0,
                        "categories": [],
                        "problems": [],
                        "questions": [],
                    }
                )
        return out


class Session:
    """Одно прохождение: какой вопрос сейчас, что уже отвечено."""

    def __init__(
        self,
        bank: Bank,
        level_name: str,
        analyzer: SpeechAnalyzer,
        categories: list[str] | None = None,
        shuffle: bool = False,
        only_ids: list[str] | None = None,
    ) -> None:
        self.bank = bank
        self.level = LEVELS.get(level_name, LEVELS[DEFAULT_LEVEL])
        self.analyzer = analyzer
        # Пустой список тем означает «все»: так проще и в интерфейсе, и здесь
        self.categories = list(categories) if categories else []
        # Прогон слабых: только вопросы, которые в прошлый раз не получились
        only = set(only_ids or [])
        self.questions = [
            q
            for q in bank.questions
            if q.level <= self.level.max_question_level
            and (not self.categories or q.category in self.categories)
            and (not only or q.id in only)
        ]
        if shuffle:
            # Иначе с третьего прогона отвечаешь по памяти о порядке, а не по существу
            random.shuffle(self.questions)
        self.shuffled = shuffle
        self.index = 0
        self.attempts: dict[str, list[Attempt]] = {}
        self.started = datetime.now()
        self.best_before = past_results()

    # ------------------------------------------------------------------ ход

    @property
    def finished(self) -> bool:
        return self.index >= len(self.questions)

    def current(self) -> Question | None:
        return None if self.finished else self.questions[self.index]

    def advance(self) -> None:
        self.index += 1

    def limits(self, q: Question) -> tuple[tuple[float, float] | None, int | None]:
        """Целевое время и лимит заминок с поправкой на уровень."""
        target = None
        if q.target_sec:
            lo, hi = q.target_sec
            target = (lo * self.level.time_factor, hi * self.level.time_factor)
        limit = None
        if q.max_fillers is not None:
            limit = max(0, round(q.max_fillers * self.level.filler_factor))
        return target, limit

    # --------------------------------------------------------------- ответ

    def answer(self, text: str, duration: float, spans=None) -> Verdict:
        q = self.current()
        if q is None:
            raise RuntimeError("вопросы закончились")

        metrics = self.analyzer.analyze(text, duration_sec=duration, speech_spans=spans)
        norm = normalize(text)
        covered = [t.covered_by(norm) if t.checkable else True for t in q.theses]

        attempt = Attempt(text=text, metrics=metrics, covered=covered)
        history = self.attempts.setdefault(q.id, [])
        history.append(attempt)

        target, limit = self.limits(q)
        time_verdict = ""
        if target:
            lo, hi = target
            time_verdict = "коротко" if duration < lo else "долго" if duration > hi else "в норме"

        missing = [t for t, ok in zip(q.theses, covered) if not ok and t.checkable]
        followup = next((t.followup for t in missing if t.followup), "") if self.level.followups else ""

        problems = (
            time_verdict in ("коротко", "долго")
            or (limit is not None and metrics.fillers_total > limit)
            or bool(missing)
        )
        can_retry = self.level.allow_repeat and len(history) < MAX_ATTEMPTS

        delta = ""
        if len(history) > 1:
            delta = _delta(history[-2], attempt, target)

        versus = ""
        past = self.best_before.get(_history_key(self.bank.name, q.id, q.text))
        if past and past.get("total"):
            now_closed = sum(covered)
            was = f"{past['closed']}/{past['total']}"
            now = f"{now_closed}/{len(covered)}"
            if now_closed > past["closed"]:
                versus = f"лучше, чем {past['when']}: тезисы {was} → {now}"
            elif now_closed < past["closed"]:
                versus = f"хуже, чем {past['when']}: тезисы {was} → {now}"
            else:
                versus = f"как и {past['when']}: тезисы {now}"

        return Verdict(
            attempt_no=len(history),
            duration=duration,
            target=target,
            time_verdict=time_verdict,
            fillers=metrics.fillers_total,
            fillers_limit=limit,
            hedges=metrics.hedges_total,
            covered=covered,
            missing_theses=missing,
            followup=followup,
            should_retry=problems and can_retry,
            can_retry=can_retry,
            delta=delta,
            versus_past=versus,
        )

    def answer_followup(self, question_text: str, text: str, duration: float) -> Verdict:
        """Дописывает ответ на уточняющий вопрос к последней попытке.

        Новая попытка не создаётся: это тот же ответ, просто договорённый.
        Время основного ответа тоже не трогаем: за то, что тебя переспросили,
        наказывать секундомером нечестно, важно другое: закрылся тезис или нет.
        """
        q = self.current()
        if q is None:
            raise RuntimeError("вопросы закончились")
        history = self.attempts.get(q.id)
        if not history:
            raise RuntimeError("сначала нужно ответить на сам вопрос")

        attempt = history[-1]
        was = list(attempt.covered)
        attempt.followups.append((question_text, text))

        norm = normalize(attempt.full_text)
        attempt.covered = [t.covered_by(norm) if t.checkable else True for t in q.theses]

        just_closed = [
            t.text for t, before, after in zip(q.theses, was, attempt.covered)
            if after and not before
        ]

        target, limit = self.limits(q)
        m = attempt.metrics
        time_verdict = ""
        if target:
            lo, hi = target
            time_verdict = (
                "коротко" if m.duration_sec < lo else "долго" if m.duration_sec > hi else "в норме"
            )

        missing = [t for t, ok in zip(q.theses, attempt.covered) if not ok and t.checkable]
        asked = {qq for qq, _ in attempt.followups}
        # следующий добивающий вопрос из тех, что ещё не задавали
        followup = ""
        if self.level.followups:
            followup = next(
                (t.followup for t in missing if t.followup and t.followup not in asked), ""
            )

        return Verdict(
            attempt_no=len(history),
            duration=m.duration_sec,
            target=target,
            time_verdict=time_verdict,
            fillers=m.fillers_total,
            fillers_limit=limit,
            hedges=m.hedges_total,
            covered=attempt.covered,
            missing_theses=missing,
            followup=followup,
            should_retry=False,
            can_retry=self.level.allow_repeat and len(history) < MAX_ATTEMPTS,
            delta="",
            just_closed=just_closed,
            after_followup=True,
        )

    # ---------------------------------------------------------------- итоги

    def progress(self) -> dict:
        return {
            "index": min(self.index + 1, len(self.questions)),
            "total": len(self.questions),
            "level": self.level.name,
            "bank": self.bank.name,
            "categories": self.categories,
        }

    def summary(self) -> dict:
        """Итог для последнего экрана: по каждому пройденному вопросу последний дубль.

        Слабым считается вопрос, где последний дубль не чистый: не прозвучал тезис,
        перебор слов-паразитов или время мимо цели. Их можно прогнать ещё раз.
        """
        rows = []
        for i, q in enumerate(self.questions):
            if i > self.index:
                break
            tries = self.attempts.get(q.id, [])
            target, limit = self.limits(q)
            row = {
                "id": q.id,
                "text": q.text,
                "category": q.category,
                "takes": len(tries),
                "target": [round(target[0]), round(target[1])] if target else None,
                "fillers_limit": limit,
            }
            if not tries:
                if i == self.index:
                    break  # до этого вопроса дошли, но не ответили и не пропустили
                rows.append({**row, "skipped": True})
                continue
            last = tries[-1]
            duration = last.metrics.duration_sec
            time_ok = not target or target[0] <= duration <= target[1]
            fillers_ok = limit is None or last.metrics.fillers_total <= limit
            rows.append({
                **row,
                "skipped": False,
                "duration": round(duration),
                "fillers": last.metrics.fillers_total,
                "covered": sum(last.covered),
                "total": len(last.covered),
                "followups": len(last.followups),
                "clean": time_ok and fillers_ok and all(last.covered),
            })
        answered = [r for r in rows if not r["skipped"]]
        return {
            "rows": rows,
            "answered": len(answered),
            "takes": sum(r["takes"] for r in rows),
            "clean": sum(1 for r in answered if r["clean"]),
            "weak_ids": [r["id"] for r in answered if not r["clean"]],
        }

    def prompt_for_review(self) -> str:
        """Готовый текст запроса к ИИ-агенту вместе с разбором.

        Программа меряет только то, что можно посчитать: время, слова-паразиты, тезисы.
        Оценить, насколько ответ убедителен, может лишь человек или модель.
        """
        return (
            "Разбери мою тренировку собеседования. Ниже расшифровка ответов вслух.\n\n"
            "Что нужно от тебя:\n"
            "1. По каждому ответу: что звучит убедительно, а что рассыпается.\n"
            "2. Где я говорю общими словами вместо конкретики и цифр.\n"
            "3. Что выкинуть из ответа, а что добавить.\n"
            "4. Один-два ответа, которые стоит переписать полностью, "
            "и как именно.\n\n"
            "Не хвали за то, что и так нормально. Мне нужны слабые места.\n\n"
            "---\n\n" + self._markdown()
        )

    def save(self) -> Path | None:
        """Кладёт разбор в markdown и цифры в json рядом с ним."""
        if not self.attempts:
            return None
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = self.started.strftime("%Y-%m-%d_%H-%M")
        safe = "".join(c for c in self.bank.name if c.isalnum() or c in " -_")
        safe = " ".join(safe.split())  # убираем двойные пробелы от выкинутых символов
        path = SESSIONS_DIR / f"{stamp} {safe}.md"
        # Прогон слабых часто укладывается в ту же минуту, что и прошлая
        # тренировка: без номера он затёр бы её разбор
        n = 2
        while path.exists():
            path = SESSIONS_DIR / f"{stamp} {safe} ({n}).md"
            n += 1
        path.write_text(self._markdown(), encoding="utf-8")
        path.with_suffix(".json").write_text(
            json.dumps(self._json(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return path

    def _markdown(self) -> str:
        themes = ", ".join(self.categories) if self.categories else "все темы"
        # Шапка полями «ключ: значение»: в файле обычный markdown, а окно
        # раскладывает её карточкой над разбором
        out = [
            f"# Тренировка собеседования: {self.bank.name}",
            "",
            f"- Дата: {self.started.strftime('%d.%m.%Y %H:%M')}",
            f"- Уровень: {self.level.name}",
            f"- Темы: {themes}",
            f"- Отвечено вопросов: {len(self.attempts)} из {len(self.questions)}",
            "",
            "Разбери этот файл: что звучит слабо, где ответ рассыпается, "
            "что выкинуть и что добавить.",
            "",
        ]

        for n, q in enumerate(self.questions, 1):
            history = self.attempts.get(q.id)
            if not history:
                continue
            out += ["---", "", f"## {n}. {q.text}", "", f"Категория: {q.category}."]
            target, limit = self.limits(q)
            if target:
                out[-1] += f" Цель {target[0]:.0f}–{target[1]:.0f} с."
            if limit is not None:
                out[-1] += f" Слов-паразитов не больше {limit}."
            out.append("")

            for i, a in enumerate(history, 1):
                m = a.metrics
                head = f"### Попытка {i}: {m.duration_sec:.0f} с, слов-паразитов {m.fillers_total}"
                if m.hedges_total:
                    head += f", неуверенных слов {m.hedges_total}"
                out += [head, "", "> " + a.text.replace("\n", "\n> "), ""]

                if m.fillers:
                    out.append("Слова-паразиты: " + ", ".join(f"«{w}» {c}" for w, c in m.fillers.items()))
                if m.hedges:
                    out.append(
                        "Неуверенные слова: " + ", ".join(f"«{w}» {c}" for w, c in m.hedges.items())
                    )
                if m.slips:
                    out.append(
                        "Оговорки: " + ", ".join(f"сказал «{k}», надо «{v}»" for k, v in m.slips.items())
                    )

                for fq, fa in a.followups:
                    out += ["", f"**Пришлось переспросить:** {fq}", "", "> " + fa.replace("\n", "\n> ")]

                miss = [t.text for t, ok in zip(q.theses, a.covered) if not ok]
                out.append("")
                out.append("Не прозвучало: " + ("; ".join(miss) if miss else "всё на месте"))
                if a.followups:
                    out.append(
                        f"Тезисов закрыто только после уточнения: {len(a.followups)}. "
                        "На собесе это значит, что сам ты об этом не сказал."
                    )
                out.append("")
        return "\n".join(out)

    def _json(self) -> dict:
        return {
            "дата": self.started.isoformat(timespec="seconds"),
            "банк": self.bank.name,
            "уровень": self.level.name,
            "темы": self.categories or "все",
            "вопросы": [
                {
                    "id": q.id,
                    "вопрос": q.text,
                    "категория": q.category,
                    "попытки": [
                        {
                            "секунд": round(a.metrics.duration_sec, 1),
                            "слов": a.metrics.words,
                            "темп": round(a.metrics.wpm),
                            "заминок": a.metrics.fillers_total,
                            "самоподрезаний": a.metrics.hedges_total,
                            "тезисов_закрыто": sum(a.covered),
                            "тезисов_всего": len(a.covered),
                            "переспросов": len(a.followups),
                            "текст": a.text,
                            "добивки": [
                                {"вопрос": fq, "ответ": fa} for fq, fa in a.followups
                            ],
                        }
                        for a in self.attempts[q.id]
                    ],
                }
                for q in self.questions
                if q.id in self.attempts
            ],
        }


def _delta(before: Attempt, after: Attempt, target: tuple[float, float] | None = None) -> str:
    """Что изменилось с прошлой попытки: по этой строке видно, растёшь ли ты.

    Время лучше не тогда, когда меньше, а когда ближе к окну цели: после «слишком
    коротко» более длинный ответ это рост. Без цели время сравнивается без оценки."""

    def timing(old: float, new: float) -> str | None:
        if old == new:
            return None
        change = f"{old:g} с→{new:g} с"
        if not target:
            return f"время {change}"
        lo, hi = target
        miss_old, miss_new = (max(0.0, lo - d, d - hi) for d in (old, new))
        if miss_old == miss_new:
            return f"время {change}"
        return f"время {'лучше' if miss_new < miss_old else 'хуже'} {change}"

    def arrow(name: str, old: float, new: float, unit: str = "") -> str | None:
        if old == new:
            return None
        mark = "лучше" if new < old else "хуже"
        return f"{name} {mark} {old:g}{unit}→{new:g}{unit}"

    parts = [
        timing(round(before.metrics.duration_sec), round(after.metrics.duration_sec)),
        arrow("слова-паразиты", before.metrics.fillers_total, after.metrics.fillers_total),
        arrow("неуверенные слова", before.metrics.hedges_total, after.metrics.hedges_total),
    ]
    was, now = sum(before.covered), sum(after.covered)
    if was != now:
        total = len(after.covered)
        parts.append(f"тезисы {'лучше' if now > was else 'хуже'} {was}/{total}→{now}/{total}")

    live = [p for p in parts if p]
    return ", ".join(live) if live else "без изменений"


def _history_key(bank_name: object, question_id: object, text: object) -> tuple[str, str, str]:
    """Ключ вопроса в истории: банк, id и текст вопроса вместе.

    Id вроде q1 есть почти в каждом банке от нейросети, поэтому один id без банка
    сравнил бы ответ про одно с ответом про другое. Банк различаем по названию:
    оно лежит в json разбора с первой версии, а файла банка там нет. Текст вопроса
    там тоже есть, и по нему видно, что вопрос под этим id не переписали: сравнивать
    ответы на разные вопросы нечестно. Пробелы в тексте не в счёт.
    """
    return str(bank_name).strip(), str(question_id).strip(), " ".join(str(text).split())


def _count(value: object) -> int:
    """Неотрицательное целое из json-значения, чужие типы дают 0."""
    number = to_number(value)
    return max(0, int(number)) if number is not None else 0


def _read_session(path: Path) -> dict | None:
    """Разбор тренировки как словарь. Чужой json (список, null, число) даёт None."""
    if path.stat().st_size > MAX_SESSION_BYTES:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _list_of_dicts(value: object) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _session_best(path: Path) -> dict[tuple[str, str, str], dict]:
    """Лучшие попытки из одного файла разбора. Файл без названия банка не берём."""
    data = _read_session(path)
    bank = data.get("банк") if data else None
    if not isinstance(bank, str) or not bank.strip():
        return {}
    when = data.get("дата")
    when = when[:10] if isinstance(when, str) else ""

    best: dict[tuple[str, str, str], dict] = {}
    for q in _list_of_dicts(data.get("вопросы")):
        qid, text = scalar_text(q.get("id")), scalar_text(q.get("вопрос"))
        if not qid or not text:  # без текста вопроса сравнивать не с чем
            continue
        key = _history_key(bank, qid, text)
        for a in _list_of_dicts(q.get("попытки")):
            total = _count(a.get("тезисов_всего"))
            closed = min(_count(a.get("тезисов_закрыто")), total)
            fillers = _count(a.get("заминок"))
            score = (closed / total if total else 0, -fillers)
            prev = best.get(key)
            if prev is None or score > prev["_score"]:
                best[key] = {
                    "_score": score,
                    "closed": closed,
                    "total": total,
                    "fillers": fillers,
                    "seconds": to_number(a.get("секунд")) or 0,
                    "when": when,
                }
    return best


def past_results() -> dict[tuple[str, str, str], dict]:
    """Лучший прошлый результат по каждому вопросу, из сохранённых сессий.

    Нужен, чтобы показать движение между днями, а не только внутри одного прогона:
    «в прошлый раз закрыл 2 тезиса из 4, сейчас 4 из 4».

    Ключ это (название банка, id, текст вопроса), см. _history_key. Файл разбора, который
    не прочитался или не похож на наш (список, null, нет банка, не те типы),
    пропускается целиком, остальные работают.
    """
    best: dict[tuple[str, str, str], dict] = {}
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(SESSIONS_DIR.glob("*.json"))
    except OSError:
        return best
    for p in files:
        try:
            found = _session_best(p)
        except Exception:
            continue
        for key, cand in found.items():
            prev = best.get(key)
            if prev is None or cand["_score"] > prev["_score"]:
                best[key] = cand
    for v in best.values():
        v.pop("_score", None)
    return best


def _history_row(path: Path) -> dict | None:
    data = _read_session(path)
    if not data:
        return None
    questions = _list_of_dicts(data.get("вопросы"))
    attempts = [a for q in questions for a in _list_of_dicts(q.get("попытки"))]
    if not attempts:
        return None
    when, bank = data.get("дата"), data.get("банк")
    return {
        "дата": when[:16].replace("T", " ") if isinstance(when, str) else "",
        "банк": _clip(bank, MAX_BANK_NAME) if isinstance(bank, str) and bank.strip() else "без названия",
        "вопросов": len(questions),
        "попыток": len(attempts),
        "заминок_на_ответ": round(sum(_count(a.get("заминок")) for a in attempts) / len(attempts), 1),
    }


def summarize_history(limit: int = 10) -> list[dict]:
    """Прошлые сессии, чтобы видеть движение между днями, а не только внутри одной.

    Файл, который не читается или не похож на разбор тренировки, пропускается, а
    в список попадают limit последних нормальных.
    """
    out: list[dict] = []
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(SESSIONS_DIR.glob("*.json"), reverse=True)
    except OSError:
        return out
    for p in files:
        if len(out) >= limit:
            break
        try:
            row = _history_row(p)
        except Exception:
            continue
        if row:
            out.append(row)
    return out


def describe_levels() -> list[dict]:
    return [
        {
            "id": name,
            "name": name,
            # окно само считает, сколько вопросов останется при выбранных теме и уровне
            "max_level": LEVELS[name].max_question_level,
            # и во сколько раз мягче на этом уровне целевое время: по нему оценка прогона
            "time_factor": LEVELS[name].time_factor,
            "hint": {
                "разминка": "Простые вопросы, время с запасом. Можно ответить заново.",
                "рабочий": "Простые и средние. Упустишь главное, программа переспросит.",
                "боевой": "Все вопросы, время строже. Второй попытки нет.",
            }[name],
        }
        for name in LEVELS
    ]


# ---------------------------------------------- банк из ответа нейросети

# Речь, в которой нет ни одной мысли по теме. Маркер, который в ней находится,
# сработал бы на любом ответе и закрыл тезис даром («задача», «команда», «проект»).
_NEUTRAL_SPEECH = (
    "ну вот смотрите я думаю что это в принципе нормально просто сейчас сложно сказать "
    "если честно наверное так и есть в целом да конечно хороший вопрос давайте подумаем "
    "то есть как бы я бы сказал что это зависит от ситуации у нас в команде было по разному "
    "мы обсуждали с коллегами и решили что так будет правильно и удобно для всех "
    "я работал над проектом где была задача и мы её сделали в срок все были довольны "
    "спасибо за вопрос можно я немного подумаю хорошо значит так первое второе третье "
    "в общем вот такой опыт у меня был мне это было интересно и полезно "
    "наша команда решала задачу в проекте и работа шла своим чередом "
    "система и процесс были понятны а результат всех устроил"
)
_NEUTRAL_NORM = normalize(_NEUTRAL_SPEECH)

DEFAULT_BANK_TITLE = "Банк из нейросети"
ASKED_QUESTIONS = 15  # столько вопросов просит bank_prompt
MAX_VACANCY_CHARS = 20000  # вакансия длиннее обрезается: запрос уйдёт в буфер обмена
MAX_BANK_FILENAME = 80  # знаков в имени файла без расширения

# Фиксированная часть запроса. {intro} и {tail} подставляются в bank_prompt. Ключи
# английские и короткие: так нейросеть реже путает их с текстом. Без длинных
# тире и точек с запятой, это правило для любых текстов программы.
_PROMPT_TEMPLATE = """Сделай банк вопросов для тренировки собеседования.
Вопросы нужны {intro}.

Ответь одним блоком кода ```json и ничего не добавляй: без пояснений до и после блока.
Нужно {count} вопросов, от простых к сложным: примерно 5 вопросов уровня 1, 6 уровня 2 и 4 уровня 3.
Текст пиши по-русски, а ключи оставь английскими, как в примере.

Вот один вопрос целиком, чтобы был понятен формат. Это пример формата, свой вопрос не копируй:

```json
{{
  "title": "Название банка из двух-четырёх слов",
  "questions": [
    {{
      "id": "corr_cov",
      "question": "Чем корреляция отличается от ковариации?",
      "category": "статистика",
      "level": 1,
      "target_sec": [30, 60],
      "max_fillers": 2,
      "theses": [
        {{
          "text": "ковариация зависит от единиц измерения",
          "markers": ["единиц", "масштаб", "размерност", "шкал"],
          "followup": "От чего зависит величина ковариации?"
        }},
        {{
          "text": "корреляция это нормированная ковариация",
          "markers": ["нормир", "делен", "стандартн отклонен", "сигм"],
          "followup": "Как одно получается из другого?"
        }},
        {{
          "text": "корреляция лежит от минус единицы до единицы",
          "markers": ["минус един", "до един", "диапазон", "ограничен"],
          "followup": "В каких пределах лежит корреляция?"
        }}
      ]
    }}
  ]
}}
```

Что значат поля:
- id: короткий латинский идентификатор вопроса, у каждого вопроса свой.
- question: сам вопрос, как его задал бы интервьюер.
- category: тема вопроса, одно-два слова. Вопросы про опыт и поведение помечай категорией «поведенческий».
- level: 1 разминка, 2 рабочий, 3 боевой.
- target_sec: [от, до], сколько секунд длится хороший устный ответ, обычно от 30 до 120.
- max_fillers: сколько слов-паразитов («ну», «как бы», «типа») допустимо в таком ответе, обычно от 2 до 5.
- theses: два-четыре тезиса, то есть мысли, которые должны прозвучать в сильном ответе.
- text: сам тезис одной фразой.
- followup: уточняющий вопрос интервьюера на случай, если тезис не прозвучал.
- markers: от 3 до 6 маркеров. Программа ищет их в расшифровке живой речи и по ним решает, что тезис прозвучал.

Правила для markers:
- Это начала слов без окончаний, в нижнем регистре: «нормир» найдёт и «нормированная», и «нормировать».
- Фраза из двух-трёх слов пишется через пробел, каждое слово тоже обрезано: «стандартн отклонен» найдёт «стандартное отклонение».
- Не бери слова, которые звучат в любом ответе: «задача», «команда», «проект», «работа», «система», «данные», «процесс». Маркер должен выдавать именно этот тезис.
- Добавь разные формулировки, которыми человек скажет это вслух, включая русские слова для английских терминов.

Правила для JSON:
- Только валидный JSON: двойные кавычки, без запятой после последнего элемента, без комментариев.
- Кавычки внутри текста заменяй на «ёлочки», иначе JSON сломается.
- Других ключей, кроме перечисленных, не добавляй.

{tail}
"""


def bank_prompt(vacancy: str = "") -> str:
    """Запрос для любой чат-нейросети: по вакансии она отвечает банком вопросов.

    Ответ вставляется в окно и разбирается через parse_bank_reply. Просим JSON
    одним блоком кода с короткими английскими ключами: так ответ проще срезать
    с обёртки и он реже ломается. Текст вакансии идёт в самом конце. Без вакансии
    запрос просит нейросеть дождаться описания роли.
    """
    text = (vacancy or "").strip()[:MAX_VACANCY_CHARS].strip()
    if text:
        intro = "под вакансию, описание которой дано в конце"
        tail = "Вакансия:\n\n" + text
    else:
        intro = "для собеседования на роль, которую я опишу ниже"
        tail = "Роль и требования:\n\n"
    return _PROMPT_TEMPLATE.format(intro=intro, count=ASKED_QUESTIONS, tail=tail)


# Ключи запроса (английские) и ключи формата банка (русские). Ключ приводится к
# нижнему регистру, пробелы и дефисы в нём считаются подчёркиванием. Незнакомые
# ключи остаются как есть: Bank их не читает
_ROOT_KEYS = {
    "название": "название", "title": "название", "name": "название",
    "вопросы": "вопросы", "questions": "вопросы",
}
_QUESTION_KEYS = {
    "id": "id",
    "вопрос": "вопрос", "question": "вопрос",
    "категория": "категория", "category": "категория",
    "уровень": "уровень", "level": "уровень",
    "цель_сек": "цель_сек", "target_sec": "цель_сек", "target_seconds": "цель_сек",
    "target": "цель_сек",
    "паразитов_макс": "паразитов_макс", "max_fillers": "паразитов_макс",
    "заметка": "заметка", "note": "заметка",
    "тезисы": "тезисы", "theses": "тезисы", "thesis": "тезисы",
}
_THESIS_KEYS = {
    "текст": "текст", "text": "текст",
    "слова": "слова", "markers": "слова", "keywords": "слова", "words": "слова",
    "добить": "добить", "followup": "добить", "follow_up": "добить",
}

# Для fix_request: русское имя поля из текста ошибки → то, что видела нейросеть
_ENGLISH_FIELDS = {
    "название": "title", "вопросы": "questions", "вопрос": "question",
    "категория": "category", "уровень": "level", "цель_сек": "target_sec",
    "паразитов_макс": "max_fillers", "заметка": "note", "тезисы": "theses",
    "текст": "text", "слова": "markers", "добить": "followup",
}

_SECONDS_RANGE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:-|–|—|\.\.|до|to)\s*(\d+(?:[.,]\d+)?)\s*(?:с|сек|s)?\.?\s*$")
# Без \s* вокруг: на длинной серии пробелов два соседних \s* перебирались бы квадратично
_MARKER_SEPARATORS = re.compile(r"[,;\n]")


def _canon_key(key: object) -> str:
    return re.sub(r"[\s-]+", "_", str(key).strip().lower())


def _rename(data: dict, table: dict[str, str]) -> dict:
    """Ключи словаря по таблице. Двух хозяев у одного ключа не бывает: побеждает первый."""
    out: dict = {}
    for key, value in data.items():
        target = table.get(_canon_key(key), key)
        if target not in out:
            out[target] = value
    return out


def _thesis_shape(t: object, label: str, k: int, notes: list[str]) -> object:
    if not isinstance(t, dict):
        return t
    t = _rename(t, _THESIS_KEYS)
    words = t.get("слова")
    if isinstance(words, str) and _MARKER_SEPARATORS.search(words.strip()):
        # «слова: кеш, ttl» одной строкой: нейросеть забыла про список
        t["слова"] = [w.strip() for w in _MARKER_SEPARATORS.split(words.strip()) if w.strip()]
        notes.append(f"{label}, тезис №{k}: маркеры пришли одной строкой, разделил по запятым")
    return t


def _question_shape(q: object, n: int, notes: list[str]) -> object:
    if not isinstance(q, dict):
        return q
    q = _rename(q, _QUESTION_KEYS)
    text = scalar_text(q.get("вопрос"))
    label = f"вопрос №{n}" + (f" «{_clip(text)}»" if text else "")
    target = q.get("цель_сек")
    if isinstance(target, str):
        # Серии пробелов схлопываем до разбора: соседние \s* в шаблоне на них квадратичны
        m = _SECONDS_RANGE.match(" ".join(target.split()))
        if m:
            q["цель_сек"] = [m.group(1), m.group(2)]
            notes.append(f"{label}: «цель_сек» {_clip(target, 20)} понял как [{m.group(1)}, {m.group(2)}]")
    theses = q.get("тезисы")
    if isinstance(theses, list):
        q["тезисы"] = [
            _thesis_shape(t, label, k, notes)
            for k, t in enumerate((t for t in theses if t is not None), 1)
        ]
    return q


def _bank_shape(obj: object, notes: list[str]) -> object | None:
    """Разобранный ответ в виде, который понимает Bank, или None, если это не банк.

    Английские ключи запроса меняются на русские ключи формата, можно мешать оба набора.
    Банк, завёрнутый в один лишний ключ ({"bank": {...}}), разворачивается.
    """
    for _ in range(3):
        if isinstance(obj, dict) and len(obj) == 1:
            (key, value), = obj.items()
            if _canon_key(key) not in _ROOT_KEYS and isinstance(value, (dict, list)):
                obj = value
                continue
        break
    if isinstance(obj, dict):
        obj = _rename(obj, _ROOT_KEYS)
        if "вопросы" not in obj:
            return None
        items = obj["вопросы"]
        if isinstance(items, list):
            obj["вопросы"] = [
                _question_shape(q, n, notes) for n, q in enumerate(items, 1)
            ]
        return obj
    if isinstance(obj, list):
        # корень-список значит просто список вопросов, если это вопросы
        if not any(isinstance(q, dict) and "вопрос" in _rename(q, _QUESTION_KEYS) for q in obj):
            return None
        return [_question_shape(q, n, notes) for n, q in enumerate(obj, 1)]
    return None


@dataclass
class ParsedBank:
    """Что получилось из ответа нейросети.

    bank: банк из вопросов, которые прочитались (None, если не прочитался ни один).
    notes: что программа поправила или заметила, банк при этом рабочий.
    errors: что не так, по одной строке на беду, с номером вопроса и полем.
    format: "json" или "yaml", каким прочитался ответ.
    """

    bank: Bank | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    format: str = ""

    @property
    def ok(self) -> bool:
        """Банк целиком прочитан и его можно сохранять."""
        return self.bank is not None and not self.errors

    def info(self) -> dict:
        """То же для окна: простые типы, которые уходят в JavaScript."""
        bank = self.bank
        return {
            "ok": self.ok,
            "name": bank.name if bank else "",
            "count": len(bank.questions) if bank else 0,
            "categories": bank.categories() if bank else [],
            "notes": list(self.notes),
            "errors": list(self.errors),
            "format": self.format,
        }


class _Unreadable(Exception):
    """Кусок текста не прочитался ни как JSON, ни как YAML. text: короткая причина."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


_JSON_PROBLEMS = (
    ("Expecting value", "ждали значение, а его нет"),
    ("Expecting ',' delimiter", "не хватает запятой или закрывающей скобки"),
    ("Expecting ':' delimiter", "не хватает двоеточия после имени поля"),
    ("Expecting property name enclosed in double quotes",
     "ждали имя поля в двойных кавычках: лишняя запятая или одинарные кавычки"),
    ("Unterminated string", "текст без закрывающей кавычки"),
    ("Extra data", "после блока идёт лишний текст"),
    ("Invalid control character", "в тексте недопустимый служебный знак"),
    ("Invalid \\escape", "в тексте недопустимая обратная косая черта"),
    ("Invalid \\uXXXX escape", "в тексте битый знак \\u"),
)


def _json_problem(exc: ValueError, text: str) -> str:
    """Причина по-русски. Если ответ оборвался, говорит об этом прямо."""
    if not isinstance(exc, json.JSONDecodeError):
        return "это не JSON"
    body = text.rstrip()
    cut = exc.pos >= len(body) or (
        exc.msg.startswith("Unterminated string") and '"' not in text[exc.pos + 1 :]
    )
    if cut:
        return "ответ оборвался на полуслове, JSON не закрыт"
    why = next((ru for en, ru in _JSON_PROBLEMS if exc.msg.startswith(en)), "ошибка в записи")
    return f"{why}, строка {exc.lineno}, столбец {exc.colno}"


# Чат-интерфейсы подсовывают в код неразрывные и узкие пробелы вместо обычных и
# невидимые знаки нулевой ширины: JSON с такими отступами не читается, а ключ с
# невидимым знаком впереди теряется. Пробелы заменяются на обычный, невидимое убирается
_SPACES = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
_INVISIBLE = re.compile("[\u200b-\u200d\u2060\ufeff]")


def _clean_invisible(text: str) -> str:
    return _INVISIBLE.sub("", _SPACES.sub(" ", text))


def _drop_json_comments(text: str) -> str:
    """Вырезает // и /* */ вне строк. Знаки внутри строк в кавычках не трогает."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _drop_trailing_commas(text: str) -> str:
    """Убирает запятую перед закрывающей скобкой вне строк."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
            continue
        if c == ",":
            k = i + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _load_text(text: str) -> tuple[object, str, str]:
    """Текст как данные: (данные, "json" или "yaml", замечание о том, что пришлось поправить).

    Сначала строгий JSON, потом JSON без комментариев и лишних запятых перед
    закрывающей скобкой, потом YAML: он разбирает и одинарные кавычки, и имена без
    кавычек, и сам YAML. Загрузчик тот же, что у файлов банка: без якорей и без
    своих типов.
    """
    text = text.strip()
    try:
        return json.loads(text, strict=False), "json", ""
    except RecursionError:
        raise _Unreadable("слишком глубокая вложенность") from None
    except ValueError as exc:
        problem = _json_problem(exc, text)
    bare = _drop_json_comments(text)
    repaired = _drop_trailing_commas(bare)
    if repaired != text:
        removed = []
        if bare != text:
            removed.append("комментарии")
        if repaired != bare:
            removed.append("лишние запятые перед закрывающей скобкой")
        try:
            return (
                json.loads(repaired, strict=False),
                "json",
                f"В JSON были {' и '.join(removed)}, убрал.",
            )
        except (ValueError, RecursionError):
            pass
        if bare != text:
            problem += ". В тексте есть комментарии // или /* */, они мешают"
    try:
        data = yaml.load(text.replace("\t", "  "), Loader=_PlainLoader)
    except yaml.YAMLError as exc:
        yaml_problem = _yaml_error_text(exc)
        raise _Unreadable(problem if text[:1] in ("{", "[") else yaml_problem) from None
    except RecursionError:
        raise _Unreadable("слишком глубокая вложенность") from None
    except Exception:
        raise _Unreadable("в тексте тег с недопустимым значением (например !!int abc)") from None
    # Ответ, который с самого начала был YAML, не «поправлен»: замечать нечего
    note = f"В JSON была ошибка ({problem}), ответ прочитан мягче, как YAML." if text[:1] in ("{", "[") else ""
    return data, "yaml", note


_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:\n[ \t]*```|\Z)", re.DOTALL)
MAX_CANDIDATES = 8


def _candidates(text: str) -> list[tuple[int, str]]:
    """Куски ответа, в которых может лежать банк, самые вероятные первыми.

    Каждый кусок идёт с рангом: 0 блок кода, 1 весь текст, 2 всё между скобками.
    По ошибке куска с меньшим рангом объясняем, что не так.

    Блоки кода от самого большого, потом весь текст, потом всё от первой «{» до
    последней «}» (и то же для «[»): так находится банк, который чат выдал
    без ``` или с пояснением вокруг. Блок без закрывающих ``` (ответ оборвался) тоже берётся.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    blocks = sorted(
        (m.group(1) for m in _FENCE.finditer(text) if m.group(1).strip()), key=len, reverse=True
    )
    whole = text.strip()
    found = [(0, b) for b in blocks] + [(1, whole)]
    for opening, closing in ("{", "}"), ("[", "]"):
        i, j = whole.find(opening), whole.rfind(closing)
        if 0 <= i < j:
            found.append((2, whole[i : j + 1]))
    unique: list[tuple[int, str]] = []
    for rank, piece in found:
        piece = piece.strip()
        if piece and all(piece != seen for _, seen in unique):
            unique.append((rank, piece))
    return unique[:MAX_CANDIDATES]


_BANK_WORDS = re.compile(r"вопрос|question|тезис|thes[ei]s|marker|слова", re.IGNORECASE)


def parse_bank_reply(text: str) -> ParsedBank:
    """Банк из ответа чат-нейросети на bank_prompt.

    Срезает обёртку ```json ... ``` и пояснения до и после, читает JSON, а если он
    с ошибками, то мягче, как YAML. Принимает и английские ключи запроса, и русские
    ключи формата банка. Дальше та же проверка, что у Bank.load: типы приводятся,
    непоправимое становится ошибкой с номером вопроса и полем. Вопрос с ошибкой
    пропускается, остальные читаются, так что ошибки приходят все сразу.
    Размер текста ограничен, как у файла банка.
    """
    result = ParsedBank()
    if not isinstance(text, str) or not text.strip():
        result.errors.append("Пусто: вставь сюда ответ нейросети целиком.")
        return result
    if len(text.encode("utf-8", "replace")) > MAX_YAML_BYTES:
        result.errors.append(
            f"Текст слишком большой (больше {max(1, MAX_YAML_BYTES >> 10)} КБ). "
            "Банк на 15 вопросов занимает от 10 до 20 КБ: проверь, что вставлен только ответ нейросети."
        )
        return result

    text = _clean_invisible(text)
    shape = None
    shape_notes: list[str] = []
    repaired = ""
    problem, problem_rank = "", 9
    for rank, piece in _candidates(text):
        try:
            data, fmt, fixed = _load_text(piece)
        except _Unreadable as exc:
            if rank < problem_rank:
                problem, problem_rank = exc.text, rank
            continue
        notes: list[str] = []
        found = _bank_shape(data, notes)
        if found is not None:
            shape, shape_notes, repaired, result.format = found, notes, fixed, fmt
            break
        problem = problem or "в ответе нет списка вопросов"

    if shape is None:
        if problem.startswith("ответ оборвался"):
            result.errors.append(
                "Ответ оборвался на полуслове: JSON не закрыт. Либо нейросеть остановилась на длинном "
                "ответе (напиши ей «продолжи» или попроси банк покороче), либо вставился не весь текст."
            )
        elif _BANK_WORDS.search(text) and re.search(r"[{\[]|^\s*\w+\s*:", text, re.MULTILINE):
            result.errors.append(
                f"Не получилось прочитать ответ: {problem or 'нет списка вопросов'}. "
                "Проверь, что вставлен весь ответ целиком, от первой скобки до последней."
            )
        else:
            result.errors.append(
                "Это не похоже на банк вопросов. Вставь сюда ответ нейросети целиком, "
                "тот, где есть блок с вопросами."
            )
        return result

    row_errors: list[str] = []
    try:
        bank = Bank._from_raw(shape, DEFAULT_BANK_TITLE, row_errors, advise=True)
    except BankError as exc:
        result.errors += row_errors
        if str(exc) not in row_errors:
            result.errors.append(str(exc))
        return result

    notes = ([repaired] if repaired else []) + shape_notes
    title = shape.get("название") if isinstance(shape, dict) else None
    if not (scalar_text(title) or ""):
        notes.append(f"Название не указано, взято «{DEFAULT_BANK_TITLE}».")
    notes += bank.problems
    if len(bank.questions) < ASKED_QUESTIONS:
        notes.append(
            f"В банке {len(bank.questions)} {plural(len(bank.questions), 'вопрос', 'вопроса', 'вопросов')} "
            f"вместо {ASKED_QUESTIONS} запрошенных."
        )
    bank.problems = limit_problems(notes)
    result.bank = bank
    result.notes = list(bank.problems)
    result.errors += row_errors
    return result


def fix_request(errors: list[str]) -> str:
    """Просьба к той же нейросети: исправить ответ. errors это ParsedBank.errors.

    Названия полей в ошибках русские (так их видит человек), а нейросеть работала
    с английскими ключами запроса, поэтому в просьбе поля названы по-английски.
    """
    shown = [_for_model(" ".join(str(e).split())) for e in errors[:12]]
    if len(errors) > len(shown):
        shown.append(f"и ещё {len(errors) - len(shown)}")
    lines = "\n".join(f"- {e}" for e in shown) or "- ответ не читается как банк вопросов"
    return (
        "В твоём ответе есть ошибки, из-за которых банк вопросов не открывается:\n"
        f"{lines}\n\n"
        "Исправь это и пришли банк целиком заново. Формат тот же, что в первом запросе: "
        "один блок кода ```json, ключи английские, без пояснений до и после блока."
    )


def _for_model(error: str) -> str:
    """Текст ошибки для нейросети. Сообщения о том, что вставлено не то, написаны человеку."""
    if error.startswith("Не получилось прочитать ответ: "):
        why = error[len("Не получилось прочитать ответ: "):].split(". Проверь")[0]
        return f"ответ не читается как JSON: {why}"
    if error.startswith("Ответ оборвался"):
        return "ответ оборвался на полуслове, JSON не закрыт"
    if error.startswith("Это не похоже на банк"):
        return "в ответе нет блока с банком вопросов в нужном формате"
    return _english_fields(error)


def _english_fields(message: str) -> str:
    """«тезисы» → «theses» в тексте ошибки Bank, чтобы нейросеть узнала поле."""
    message = re.sub(
        r"«([^«»]+)»", lambda m: f"«{_ENGLISH_FIELDS.get(m.group(1), m.group(1))}»", message
    )
    return (
        message.replace("(id, вопрос, тезисы)", "(id, question, theses)")
        .replace("(текст, слова, добить)", "(text, markers, followup)")
        .replace("(название, вопросы)", "(title, questions)")
    )


def _safe_stem(title: str) -> str:
    """Имя файла из названия банка: без знаков, запрещённых в Windows, и без имён устройств."""
    name = "".join(c for c in str(title) if c not in _FORBIDDEN and ord(c) >= 32)
    name = " ".join(name.split())[:MAX_BANK_FILENAME].rstrip(". ")
    if not name:
        return DEFAULT_BANK_TITLE
    if _is_device_name(name):  # CON, NUL, COM1 и их же с расширением («nul.txt»)
        name = "Банк " + name
    return name


class _BankDumper(yaml.SafeDumper):
    """Без якорей и с отступом у списков, как в вопросы/пример.yaml."""

    def ignore_aliases(self, data):
        return True

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _bank_yaml(bank: Bank) -> str:
    """Банк в YAML русского формата: порядок полей человеческий, пустое не пишется."""
    questions = []
    for q in bank.questions:
        item: dict = {"id": q.id, "вопрос": q.text, "категория": q.category, "уровень": q.level}
        if q.target_sec:
            item["цель_сек"] = [_number(x) for x in q.target_sec]
        if q.max_fillers is not None:
            item["паразитов_макс"] = q.max_fillers
        if q.note:
            item["заметка"] = q.note
        if q.theses:
            item["тезисы"] = [
                {
                    "текст": t.text,
                    **({"слова": list(t.words)} if t.words else {}),
                    **({"добить": t.followup} if t.followup else {}),
                }
                for t in q.theses
            ]
        questions.append(item)
    body = yaml.dump(
        {"название": bank.name, "вопросы": questions},
        Dumper=_BankDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=None,
        width=10_000,
    )
    body = re.sub(r"\n(  - id:)", r"\n\n\1", body)  # пустая строка между вопросами
    head = (
        f"# Банк собран из ответа нейросети {datetime.now().strftime('%d.%m.%Y')}.\n"
        "# Файл можно править руками, формат тот же, что в пример.yaml.\n\n"
    )
    return head + body


def save_bank(parsed: ParsedBank, allow_partial: bool = False) -> str:
    """Сохраняет разобранный банк в вопросы/ как YAML русского формата. Возвращает имя файла.

    Имя берётся из названия банка. Существующий файл не перезаписывается: к имени
    добавляется « (2)», « (3)». Банк с ошибками не сохраняется, если не передан
    allow_partial: тогда пишутся только вопросы, которые прочитались.
    Сохранить не вышло: BankError с понятным текстом.
    """
    bank = parsed.bank
    if bank is None or (parsed.errors and not allow_partial):
        raise BankError("Банк нельзя сохранить: в ответе нейросети есть ошибки. Исправь их и вставь ответ заново.")
    text = _bank_yaml(bank)
    size = len(text.encode("utf-8"))
    if size > MAX_YAML_BYTES:
        # Такой файл Bank.load потом не откроет (лимит тот же, что у read_yaml)
        raise BankError(
            f"Банк слишком большой: {-(-size // 1024)} КБ, предел {MAX_YAML_BYTES >> 10} КБ. "
            "Убери часть вопросов или попроси у нейросети банк покороче."
        )
    try:
        BANKS_DIR.mkdir(parents=True, exist_ok=True)
        stem, n = _safe_stem(bank.name), 1
        while True:
            name = f"{stem}.yaml" if n == 1 else f"{stem} ({n}).yaml"
            try:
                # "x" не даёт затереть чужой файл, даже если он появился только что
                with open(BANKS_DIR / name, "x", encoding="utf-8", newline="\n") as out:
                    out.write(text)
                break
            except FileExistsError:
                n += 1
                if n > 999:
                    raise BankError("В папке вопросов слишком много банков с таким названием.") from None
    except OSError as exc:
        raise BankError(f"Не удалось сохранить банк: {exc.strerror or type(exc).__name__}.") from None
    # Файл должен читаться так же, как писался: иначе лучше сказать сразу
    try:
        again = Bank.load(BANKS_DIR / name)
        same = [q.id for q in again.questions] == [q.id for q in bank.questions]
    except BankError:
        same = False
    if not same:
        try:
            (BANKS_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass
        raise BankError("Банк не сохранился без потерь, файл удалён. Попробуй вставить ответ ещё раз.")
    return name


__all__ = [
    "Bank",
    "BankError",
    "Session",
    "Question",
    "Thesis",
    "Verdict",
    "LEVELS",
    "DEFAULT_LEVEL",
    "MAX_ATTEMPTS",
    "describe_levels",
    "summarize_history",
    "ParsedBank",
    "bank_prompt",
    "parse_bank_reply",
    "fix_request",
    "save_bank",
    "BANKS_DIR",
    "SESSIONS_DIR",
]
