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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from speech_metrics import Metrics, SpeechAnalyzer, normalize, plural

HERE = Path(__file__).resolve().parent
BANKS_DIR = HERE / "вопросы"
SESSIONS_DIR = HERE / "мок-собесы"

# Больше этого числа попыток на один вопрос не даём: если не идёт сегодня,
# надо двигаться дальше, а не буксовать
MAX_ATTEMPTS = 3


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
        return any(normalize(w) in normalized_answer for w in self.words)


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


class Bank:
    def __init__(self, name: str, questions: list[Question]) -> None:
        self.name = name
        self.questions = questions

    @classmethod
    def load(cls, path: Path) -> "Bank":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items = raw.get("вопросы", raw if isinstance(raw, list) else [])

        questions = []
        for i, q in enumerate(items):
            target = q.get("цель_сек")
            theses = [
                Thesis(
                    text=t.get("текст", ""),
                    words=[str(w) for w in (t.get("слова") or [])],
                    followup=t.get("добить", ""),
                )
                for t in (q.get("тезисы") or [])
            ]
            questions.append(
                Question(
                    id=str(q.get("id") or f"q{i + 1}"),
                    text=q.get("вопрос", ""),
                    category=q.get("категория", "общий"),
                    level=int(q.get("уровень", 1)),
                    target_sec=(float(target[0]), float(target[1])) if target else None,
                    max_fillers=q.get("паразитов_макс"),
                    theses=theses,
                    note=q.get("заметка", ""),
                )
            )
        return cls(raw.get("название", path.stem), questions)

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
        for p in sorted(BANKS_DIR.glob("*.yaml")):
            try:
                bank = Bank.load(p)
                out.append(
                    {
                        "file": p.name,
                        "name": bank.name,
                        "count": len(bank.questions),
                        "categories": bank.categories(),
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
            delta = _delta(history[-2], attempt)

        versus = ""
        past = self.best_before.get(q.id)
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


def _delta(before: Attempt, after: Attempt) -> str:
    """Что изменилось с прошлой попытки: по этой строке видно, растёшь ли ты."""

    def arrow(name: str, old: float, new: float, unit: str = "") -> str | None:
        if old == new:
            return None
        mark = "лучше" if new < old else "хуже"
        return f"{name} {mark} {old:g}{unit}→{new:g}{unit}"

    parts = [
        arrow("время", round(before.metrics.duration_sec), round(after.metrics.duration_sec), " с"),
        arrow("слова-паразиты", before.metrics.fillers_total, after.metrics.fillers_total),
        arrow("неуверенные слова", before.metrics.hedges_total, after.metrics.hedges_total),
    ]
    was, now = sum(before.covered), sum(after.covered)
    if was != now:
        total = len(after.covered)
        parts.append(f"тезисы {'лучше' if now > was else 'хуже'} {was}/{total}→{now}/{total}")

    live = [p for p in parts if p]
    return ", ".join(live) if live else "без изменений"


def past_results() -> dict[str, dict]:
    """Лучший прошлый результат по каждому вопросу, из сохранённых сессий.

    Нужен, чтобы показать движение между днями, а не только внутри одного прогона:
    «в прошлый раз закрыл 2 тезиса из 4, сейчас 4 из 4».
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    best: dict[str, dict] = {}
    for p in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for q in data.get("вопросы", []):
            qid = q.get("id")
            for a in q.get("попытки", []):
                total = a.get("тезисов_всего") or 0
                closed = a.get("тезисов_закрыто") or 0
                prev = best.get(qid)
                score = (closed / total if total else 0, -a.get("заминок", 0))
                if prev is None or score > prev["_score"]:
                    best[qid] = {
                        "_score": score,
                        "closed": closed,
                        "total": total,
                        "fillers": a.get("заминок", 0),
                        "seconds": a.get("секунд", 0),
                        "when": (data.get("дата") or "")[:10],
                    }
    for v in best.values():
        v.pop("_score", None)
    return best


def summarize_history(limit: int = 10) -> list[dict]:
    """Прошлые сессии, чтобы видеть движение между днями, а не только внутри одной."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        attempts = [a for q in data.get("вопросы", []) for a in q.get("попытки", [])]
        if not attempts:
            continue
        out.append(
            {
                "дата": data.get("дата", "")[:16].replace("T", " "),
                "банк": data.get("банк", ""),
                "вопросов": len(data.get("вопросы", [])),
                "попыток": len(attempts),
                "заминок_на_ответ": round(
                    sum(a["заминок"] for a in attempts) / len(attempts), 1
                ),
            }
        )
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


__all__ = [
    "Bank",
    "Session",
    "Question",
    "Thesis",
    "Verdict",
    "LEVELS",
    "DEFAULT_LEVEL",
    "MAX_ATTEMPTS",
    "describe_levels",
    "summarize_history",
    "BANKS_DIR",
    "SESSIONS_DIR",
]
