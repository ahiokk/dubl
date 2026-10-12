"""Архив: список записей, чтение, поиск и краткая выжимка встречи.

Всё, что программа сохранила, лежит в двух папках: расшифровки встреч и разборы
мок-собесов. Здесь они приводятся к одному виду, чтобы окно показало их списком.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEETINGS_DIR = HERE / "расшифровки"
INTERVIEWS_DIR = HERE / "мок-собесы"

ME = "Я"
THEM = "Собеседник"

_LINE = re.compile(r"^\*\*\[(\d+:\d+:\d+)\]\s*([^:*]+):\*\*\s*(.+)$", re.MULTILINE)

# Строка шапки вида «- Длительность: 00:01:19»
_FIELD = re.compile(r"^[-*]\s+([^:]{2,40}):\s+(.+?)\s*$")
_RULE = ("---", "***", "___")
# Дальше этой строки шапку не ищем: значит, её просто нет
_HEAD_LIMIT = 40


@dataclass
class Replica:
    time: str
    speaker: str
    text: str


@dataclass
class Entry:
    path: str
    name: str
    kind: str  # "встреча" | "мок-собес"
    when: str
    size: int
    replicas: int = 0
    duration: str = ""
    title: str = ""
    preview: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "when": self.when,
            "size": self.size,
            "replicas": self.replicas,
            "duration": self.duration,
            "title": self.title,
            "preview": self.preview,
        }


def parse_replicas(text: str) -> list[Replica]:
    """Достаёт реплики из markdown-расшифровки."""
    return [Replica(t, s.strip(), body.strip()) for t, s, body in _LINE.findall(text)]


def split_head(text: str) -> tuple[str, str]:
    """Делит файл на шапку и тело по первой горизонтальной линии.

    Шапкой считается только начало файла с заголовком: у записей, набранных
    не приложением, её может не быть вовсе.
    """
    lines = text.splitlines()
    if not lines or not lines[0].lstrip().startswith("#"):
        return "", text
    for i, line in enumerate(lines[:_HEAD_LIMIT]):
        if line.strip() in _RULE:
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return "", text


def parse_head(head: str) -> dict:
    """Разбирает шапку на заголовок, поля «ключ: значение» и заметки.

    Текст файла при этом не меняется: то же самое окно показывает карточкой,
    чтобы не заставлять читать служебные строки как часть разговора.
    """
    title, fields, notes, buf = "", [], [], []

    def flush() -> None:
        if buf:
            notes.append("\n".join(buf))
            buf.clear()

    for line in head.splitlines():
        s = line.strip()
        if not s:
            flush()
            continue
        if s.startswith("#"):
            flush()
            heading = s.lstrip("#").strip()
            if title:
                notes.append(s)
            else:
                title = heading
            continue
        m = _FIELD.match(s)
        if m:
            flush()
            fields.append([m.group(1).strip(), m.group(2).strip()])
            continue
        buf.append(s)

    flush()
    return {"title": title, "fields": fields, "notes": notes}


def _tail_after_replicas(body: str) -> str:
    """Всё, что дописано после последней реплики: разбор речи, выжимка."""
    last = None
    for m in _LINE.finditer(body):
        last = m
    return body[last.end():] if last else ""


def _headline(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _short(text: str, limit: int = 160) -> str:
    reps = parse_replicas(text)
    if reps:
        body = " ".join(r.text for r in reps[:3])
    else:
        body = " ".join(
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith(("#", ">", "-", "*", "|"))
        )
    return (body[:limit] + "…") if len(body) > limit else body


def _scan(folder: Path, kind: str) -> list[Entry]:
    if not folder.exists():
        return []
    out = []
    for p in folder.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8")
            st = p.stat()
        except OSError:
            continue
        reps = parse_replicas(text)
        duration = reps[-1].time if reps else ""
        out.append(
            Entry(
                path=str(p),
                name=p.stem,
                kind=kind,
                when=datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M"),
                size=st.st_size,
                replicas=len(reps),
                duration=duration,
                title=_headline(text),
                preview=_short(text),
            )
        )
    return out


def listing() -> list[dict]:
    """Все записи, новые сверху."""
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    INTERVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    items = _scan(MEETINGS_DIR, "встреча") + _scan(INTERVIEWS_DIR, "мок-собес")
    items.sort(key=lambda e: e.path, reverse=True)
    # сортируем по времени файла, а не по имени: имена бывают заданы вручную
    items.sort(key=lambda e: Path(e.path).stat().st_mtime, reverse=True)
    return [e.as_dict() for e in items]


def read(path: str) -> dict:
    p = Path(path)
    if not _inside_archive(p):
        return {"error": "файл вне папок приложения"}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": str(exc)}
    head, body = split_head(text)
    reps = parse_replicas(body)
    return {
        "text": text,
        "name": p.stem,
        "path": str(p),
        # шапка отдельно от разговора: окно покажет её карточкой
        "meta": parse_head(head) if head else {"title": "", "fields": [], "notes": []},
        "body": body,
        "tail": _tail_after_replicas(body) if reps else "",
        "replicas": [
            {"time": r.time, "speaker": r.speaker, "text": r.text,
             "who": "me" if r.speaker == ME else "them"}
            for r in reps
        ],
    }


def _inside_archive(p: Path) -> bool:
    """Защита от чтения и удаления чего попало по подсунутому пути."""
    try:
        p = p.resolve()
        return any(p.is_relative_to(d.resolve()) for d in (MEETINGS_DIR, INTERVIEWS_DIR))
    except OSError:
        return False


def delete(path: str) -> dict:
    p = Path(path)
    if not _inside_archive(p):
        return {"error": "файл вне папок приложения"}
    try:
        p.unlink()
        # у разборов собеседований рядом лежит json с цифрами
        p.with_suffix(".json").unlink(missing_ok=True)
    except OSError as exc:
        return {"error": str(exc)}
    return {"ok": True}


def rename(path: str, new_name: str) -> dict:
    p = Path(path)
    if not _inside_archive(p):
        return {"error": "файл вне папок приложения"}
    clean = "".join(c for c in new_name if c not in '<>:"/\\|?*').strip()
    if not clean:
        return {"error": "пустое имя"}
    target = p.with_name(clean + p.suffix)
    if target.exists() and target != p:
        return {"error": "файл с таким именем уже есть"}
    try:
        js = p.with_suffix(".json")
        p.rename(target)
        if js.exists():
            js.rename(target.with_suffix(".json"))
    except OSError as exc:
        return {"error": str(exc)}
    return {"ok": True, "path": str(target)}


def search(query: str) -> list[dict]:
    """Ищет по тексту внутри файлов, возвращает записи с найденными строками."""
    q = query.strip().lower()
    if not q:
        return listing()

    out = []
    for item in listing():
        try:
            text = Path(item["path"]).read_text(encoding="utf-8")
        except OSError:
            continue
        if q not in text.lower():
            continue
        hits = [
            ln.strip() for ln in text.splitlines() if q in ln.lower()
        ][:3]
        item["hits"] = hits
        out.append(item)
    return out


# ------------------------------------------------------------------ выжимка

# Маркеры, по которым в разговоре видно договорённости и задачи.
# Это не пересказ смысла, а подсветка мест, куда стоит посмотреть.
MARKERS: dict[str, tuple[str, ...]] = {
    "Договорились": (
        "договорились", "решили", "остановимся на", "принято", "будем делать",
        "окей давай", "тогда так", "фиксируем",
    ),
    "Задачи": (
        "нужно ", "надо ", "сделай", "сделаю", "подготов", "отправ", "пришли",
        "скинь", "займусь", "возьму на себя", "с меня",
    ),
    "Сроки": (
        "понедельник", "вторник", "сред", "четверг", "пятниц", "выходн",
        "завтра", "послезавтра", "сегодня", "на следующей неделе", "в течение",
        "дедлайн", "до конца недели", "до конца месяца", "к утру", "к вечеру",
        "успе", "срок",
    ),
    "Вопросы": ("?",),
    "Проблемы": (
        "проблема", "не работает", "сломал", "риск", "не успева", "блокир",
        "мешает", "не получится",
    ),
}


@dataclass
class Digest:
    replicas: int = 0
    duration: str = ""
    by_speaker: dict[str, int] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)


def digest(text: str) -> dict:
    """Структурная выжимка встречи без всякой языковой модели.

    Смысл не пересказывается. По полкам раскладываются реплики, в которых
    обычно прячется суть: договорённости, задачи, сроки, вопросы, проблемы.
    Пересказ смысла остаётся работой ИИ-агента по полному файлу.
    """
    reps = parse_replicas(text)
    d = Digest(replicas=len(reps), duration=reps[-1].time if reps else "")

    for r in reps:
        d.by_speaker[r.speaker] = d.by_speaker.get(r.speaker, 0) + len(r.text.split())

    for name, markers in MARKERS.items():
        found = []
        for r in reps:
            low = r.text.lower()
            if name == "Вопросы":
                # вопрос ловим по знаку, но только если это не одно слово
                if "?" not in r.text or len(r.text.split()) < 3:
                    continue
            elif not any(m in low for m in markers):
                continue
            line = f"[{r.time}] {r.speaker}: {r.text}"
            if line not in found:
                found.append(line)
        if found:
            d.sections[name] = found[:12]

    return {
        "replicas": d.replicas,
        "duration": d.duration,
        "by_speaker": d.by_speaker,
        "sections": d.sections,
    }


def digest_markdown(name: str, data: dict) -> str:
    """Выжимка в виде markdown, её дописывают в файл расшифровки."""
    lines = ["## Выжимка", ""]
    words = data.get("by_speaker") or {}
    total = sum(words.values()) or 1
    share = ", ".join(
        f"{who} {round(n * 100 / total)}%" for who, n in
        sorted(words.items(), key=lambda x: -x[1])
    )
    lines.append(
        f"Реплик {data['replicas']}, длительность {data['duration'] or 'неизвестна'}. "
        f"Доля слов: {share}." if share else f"Реплик {data['replicas']}."
    )
    lines.append("")

    for section, items in (data.get("sections") or {}).items():
        lines.append(f"**{section}**")
        lines.append("")
        for it in items:
            lines.append(f"- {it}")
        lines.append("")

    if not data.get("sections"):
        lines.append("Ничего похожего на договорённости и задачи не нашлось.")
        lines.append("")
    return "\n".join(lines)
