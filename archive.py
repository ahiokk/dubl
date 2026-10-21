"""Архив: список записей, чтение, поиск и краткая выжимка встречи.

Всё, что программа сохранила, лежит в двух папках: расшифровки встреч и разборы
мок-собесов. Здесь они приводятся к одному виду, чтобы окно показало их списком.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEETINGS_DIR = HERE / "расшифровки"
INTERVIEWS_DIR = HERE / "мок-собесы"

ME = "Я"
THEM = "Собеседник"

REPLACEMENT = chr(0xFFFD)  # знак, которым decode(errors="replace") метит битый байт

# Без соседних пробельных квантификаторов: на строке из пробелов они перебирались бы
# квадратично, и один такой файл подвешивал архив. Пробелы по краям срезает strip
_LINE = re.compile(r"^\*\*\[(\d+:\d+:\d+)\]([^:*\n]+):\*\*[ \t]*(.+)$", re.MULTILINE)

# Строка шапки вида «- Длительность: 00:01:19»
_FIELD = re.compile(r"^[-*]\s+([^:]{2,40}):\s+(.+)$")
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
    mtime: float = 0.0  # только для сортировки, в окно не уходит
    damaged: bool = False  # файл не в UTF-8: часть знаков заменена

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
            "damaged": self.damaged,
        }


def _utf16_without_bom(raw: bytes) -> str | None:
    """UTF-16 без BOM или None, если это не он.

    Знак ASCII в UTF-16 это пара из байта и нуля, и нули стоят на одном и том же
    месте каждой пары: на нечётных позициях у LE, на чётных у BE. Кириллица
    нулей не даёт, но пробелы, цифры и разметка в расшифровке есть всегда.
    """
    even, odd = raw[0::2].count(0), raw[1::2].count(0)
    big, small = max(even, odd), min(even, odd)
    if big == 0 or small * 8 > big:
        return None
    try:
        text = raw.decode("utf-16-le" if odd > even else "utf-16-be")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def _decode_bytes(raw: bytes) -> tuple[str, bool]:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16"), False
        except UnicodeDecodeError:
            pass
    elif b"\x00" in raw:
        text = _utf16_without_bom(raw)
        if text is not None:
            return text, False
    try:
        return raw.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        pass
    text = raw.decode("utf-8", errors="replace")
    # В UTF-8 с одним битым байтом «�» единицы на тысячи знаков. В cp1251
    # он на каждую букву: кириллица там выглядит как невалидный UTF-8
    high = sum(1 for b in raw if b >= 0x80)
    if text.count(REPLACEMENT) * 5 > high:
        try:
            return raw.decode("cp1251"), False
        except UnicodeDecodeError:
            # В cp1251 не определён байт 0x98. Остальная кириллица читается,
            # а замена одного знака не должна превращать в «�» весь файл
            return raw.decode("cp1251", errors="replace"), True
    return text, True


def _decode(raw: bytes) -> tuple[str, bool]:
    """Текст файла и признак «часть знаков пришлось заменить».

    Файл пользователь мог положить сам: Блокнот в «ANSI», PowerShell с `>`.
    UTF-8 и UTF-8 с BOM читаются как есть, UTF-16 с BOM и без него тоже, а
    файл, где почти каждый не-ASCII байт не складывается в UTF-8, это cp1251.
    Остальное читается с заменой битых знаков на «�», и запись помечается: один
    такой файл не должен ломать ни список, ни поиск, ни чтение остальных.
    """
    text, damaged = _decode_bytes(raw)
    # Нулевой байт в тексте значит, что это был не он: UTF-16, который не
    # удалось разобрать, или вообще не текст. Дописывать в такой файл нельзя
    return text, damaged or "\x00" in text


def _read_text(p: Path) -> tuple[str, bool]:
    return _decode(p.read_bytes())


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


def _scan(folder: Path, kind: str, texts: dict[str, str] | None = None) -> list[Entry]:
    """Записи одной папки. texts, если передан, получает тексты файлов по пути."""
    if not folder.exists():
        return []
    out = []
    for p in folder.glob("*.md"):
        try:
            st = p.stat()
            text, damaged = _read_text(p)
        except OSError:
            continue
        try:
            when = datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")
        except (OSError, OverflowError, ValueError):
            when = ""
        if texts is not None:
            texts[str(p)] = text
        reps = parse_replicas(text)
        duration = reps[-1].time if reps else ""
        out.append(
            Entry(
                path=str(p),
                name=p.stem,
                kind=kind,
                when=when,
                size=st.st_size,
                replicas=len(reps),
                duration=duration,
                title=_headline(text),
                preview=_short(text),
                mtime=st.st_mtime,
                damaged=damaged,
            )
        )
    return out


def _entries(texts: dict[str, str] | None = None) -> list[Entry]:
    """Все записи, новые сверху. Время файла берётся из того, что собрал _scan."""
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    INTERVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    items = _scan(MEETINGS_DIR, "встреча", texts) + _scan(INTERVIEWS_DIR, "мок-собес", texts)
    # по времени файла, а не по имени: имена бывают заданы вручную
    items.sort(key=lambda e: (e.mtime, e.path), reverse=True)
    return items


def listing() -> list[dict]:
    """Все записи, новые сверху."""
    return [e.as_dict() for e in _entries()]


def read(path: str) -> dict:
    p = Path(path)
    if not _inside_archive(p):
        return {"error": "файл вне папок приложения"}
    try:
        text, damaged = _read_text(p)
    except OSError as exc:
        return {"error": str(exc)}
    head, body = split_head(text)
    reps = parse_replicas(body)
    return {
        "text": text,
        "name": p.stem,
        "path": str(p),
        # True: файл не в UTF-8, часть знаков заменена на «�». Дописывать в
        # такой файл прочитанное нельзя, оно испортит оригинал
        "damaged": damaged,
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


# Имена устройств Windows: файл с таким именем создать нельзя, и CON.md с
# любым расширением тоже. COM¹–COM³ и LPT¹–LPT³ Windows считает за них же
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{kind}{n}" for kind in ("COM", "LPT") for n in "0123456789¹²³"}
)
_FORBIDDEN = '<>:"/\\|?*'


def _is_device_name(name: str) -> bool:
    # Windows смотрит на часть до первой точки и не замечает пробелов перед ней
    return name.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES


def rename(path: str, new_name: str) -> dict:
    p = Path(path)
    if not _inside_archive(p):
        return {"error": "файл вне папок приложения"}
    asked = new_name.strip()
    if not asked:
        return {"error": "пустое имя"}
    clean = "".join(c for c in asked if c not in _FORBIDDEN and ord(c) >= 32)
    # Windows молча отбрасывает точки и пробелы в конце, и имя файла расходится с тем,
    # что мы вернули окну
    clean = clean.strip().rstrip(". ")
    if not clean:
        if set(asked) <= {".", " "}:
            return {"error": "имя не может состоять из одних точек"}
        return {"error": "в имени нет ни одного подходящего знака"}
    if _is_device_name(clean):
        return {"error": "это имя занято Windows (CON, PRN, AUX, NUL, COM1–9, LPT1–9), выбери другое"}
    target = p.with_name(clean + p.suffix)
    if target.exists() and target != p:
        return {"error": "файл с таким именем уже есть"}
    js = p.with_suffix(".json")
    target_js = target.with_suffix(".json")
    if js.exists() and target_js.exists() and target_js != js:
        return {"error": "рядом уже лежит файл с цифрами под таким именем"}
    try:
        p.rename(target)
        if js.exists():
            js.rename(target_js)
    except OSError as exc:
        return {"error": str(exc)}
    return {"ok": True, "path": str(target)}


# Разметка в начале и внутри строки: подсветка в окне ищет по готовому тексту,
# и в найденной строке она не нужна
_MD_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)")
_MD_MARKS = re.compile(r"\*\*|__|`")


def _fold(text: str) -> str:
    """Для сравнения: регистр не важен, «ё» и «е» одна буква (как в подсветке окна)."""
    return " ".join(text.lower().replace("ё", "е").split())


def _plain(line: str) -> str:
    return _MD_MARKS.sub("", _MD_PREFIX.sub("", line)).strip()


def _search_lines(text: str) -> list[str]:
    """Строки, по которым ищем: реплики без времени и имени, а у записей, набранных
    не приложением, весь текст после шапки. Служебная шапка не ищется."""
    _, body = split_head(text)
    reps = parse_replicas(body)
    if reps:
        return [r.text for r in reps]
    return [line for line in (_plain(ln) for ln in body.splitlines()) if line]


def _snippet(line: str, folded_query: str, limit: int = 200) -> str:
    """Найденная строка; длинную обрезаем так, чтобы совпадение осталось внутри."""
    flat = " ".join(line.split())
    if len(flat) <= limit:
        return flat
    low = flat.lower().replace("ё", "е")
    at = low.find(folded_query) if len(low) == len(flat) else 0
    start = max(0, at - limit // 3)
    end = min(len(flat), start + limit)
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def search(query: str) -> list[dict]:
    """Ищет по репликам внутри файлов, возвращает записи с найденными строками."""
    q = _fold(query)
    if not q:
        return listing()

    texts: dict[str, str] = {}
    out = []
    for entry in _entries(texts):
        hits = [
            _snippet(line, q)
            for line in _search_lines(texts.get(entry.path, ""))
            if q in _fold(line)
        ]
        if not hits:
            continue
        item = entry.as_dict()
        item["hits"] = hits[:3]
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
