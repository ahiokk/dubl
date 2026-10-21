"""Точка входа. Проверяет окружение, чинит побитое и запускает приложение.

Отдельный файл нужен потому, что проверять надо ДО импорта тяжёлых модулей:
если антивирус испортил av или ctranslate2, обычный запуск падает с ImportError
ещё на строке импорта, и пользователь видит только молчащий ярлык.

Здесь используется исключительно то, что входит в сам Python (tkinter, subprocess),
поэтому лаунчер работает даже когда из venv выгрызли половину пакетов. tkinter
подключается внутри функций: в урезанной установке Python его может не быть, и это
не должно ронять запуск молча. Любой сбой пишется в ошибка.log и показывается окном.
"""

from __future__ import annotations

import re
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "ошибка.log"

# Подключается в _run(), а не здесь: если побит сам bootstrap.py, это тоже должно
# попасть в журнал и в окно, а не убить запуск до первой строки
bootstrap = None

VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def show_message(title: str, text: str, error: bool = True) -> None:
    """Окно с текстом. Если tkinter нет или окно не открылось, молча ничего не показывает."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(title, text)
        root.destroy()
    except Exception:
        pass


# Ключи в журнал попадать не должны: их несут тексты ошибок HTTP-библиотек,
# например «Illegal header value b'Bearer …'» или «Authorization: Api-Key …»
_SCHEME = r"(?:bearer|basic|api-key|token)"
_VALUE = r"[^\s'\",;)}\]]"
_SECRET_PATTERNS = (
    # схема и ключ: Bearer …, Api-Key …, Token …, где бы они ни стояли
    (re.compile(rf"(?i)\b({_SCHEME})\s+{_VALUE}{{8,}}"), r"\1 ***"),
    # имя поля и значение: api_key=…, "token": "…", Authorization: [схема] …
    (re.compile(
        rf"(?i)\b(api[_-]?key|token|secret|password|authorization)\b(['\"]?\s*[:=]\s*['\"]?)"
        rf"(?:{_SCHEME}\s+)?{_VALUE}+"
    ), r"\1\2***"),
    (re.compile(r"\b(?:sk-|gsk_|xai-|hf_|ghp_|AIza)[A-Za-z0-9_\-]{10,}"), "***"),
)


def scrub(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def write_error_log(text: str) -> None:
    head = f"{datetime.now():%Y-%m-%d %H:%M:%S}  Python {sys.version.split()[0]}\n\n"
    try:
        LOG.write_text(head + scrub(text), encoding="utf-8")
    except OSError:
        pass


class HealWindow:
    """Маленькое окно на время починки. Показывается, только если что-то сломано."""

    def __init__(self, broken: list[str]) -> None:
        import tkinter as tk

        self.root = tk.Tk()
        self.root.title("Восстановление")
        self.root.geometry("520x210")
        self.root.configure(bg="#141821")
        self.root.resizable(False, False)
        # Крестик посреди переустановки не закрывает окно: процесс ждёт её конца
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        tk.Label(
            self.root, text="Восстанавливаю приложение",
            bg="#141821", fg="#e6e9ef", font=("Segoe UI", 13, "bold"),
        ).pack(pady=(22, 6))

        tk.Label(
            self.root,
            text=f"Повреждены файлы пакетов: {', '.join(broken)}.\n"
                 "Переустанавливаю их. Это займёт до минуты.",
            bg="#141821", fg="#8892a4", font=("Segoe UI", 10), justify="center",
        ).pack()

        self.status = tk.Label(
            self.root, text="", bg="#141821", fg="#4f8ef7",
            font=("Consolas", 10), wraplength=470,
        )
        self.status.pack(pady=16)

        self.broken = broken
        self.failed: list[str] = []
        self.done = False

    def log(self, text: str) -> None:
        self.status.configure(text=text)
        self.root.update_idletasks()

    def on_close(self) -> None:
        self.log("Идёт переустановка. Окно закроется само, когда она закончится.")

    def work(self) -> None:
        self.failed = bootstrap.repair(self.broken, self.log)
        self.done = True

    def run(self) -> list[str]:
        def worker():
            try:
                self.work()
            finally:
                self.root.after(0, self.root.destroy)

        threading.Thread(target=worker, daemon=True).start()
        self.root.mainloop()
        # Сюда попадаем, когда поток закончил. Если он упал с исключением, это не успех
        return self.failed if self.done else list(self.broken)


def heal(broken: list[str]) -> list[str]:
    """Переставляет пакеты под окном. Без tkinter делает то же молча."""
    try:
        window = HealWindow(broken)
    except Exception:
        return bootstrap.repair(broken)
    return window.run()


def check_own_files() -> bool:
    """Проверяет файлы самого приложения и чинит их из бэкапа.

    Антивирус портит не только пакеты, но и код проекта. Один раз испорченный
    файл успел попасть в git-коммит, и восстанавливать стало неоткуда. Поэтому
    рядом держится своя копия, которая обновляется только когда всё цело.
    """
    broken = bootstrap.check_sources()
    if not broken:
        bootstrap.backup_sources()
        return True

    healed, failed = bootstrap.restore_sources(broken)

    if failed:
        text = (
            "Файлы приложения повреждены или сохранены не в UTF-8, а целых копий для них нет:\n\n"
            + "\n".join("  " + p.name for p in failed)
            + "\n\nСкачай программу заново или верни эти файлы на место."
        )
        write_error_log(text)
        show_message("Файлы приложения повреждены", text)
        return False

    show_message(
        "Файлы восстановлены",
        "Эти файлы приложения оказались повреждены:\n\n"
        + "\n".join("  " + p.name for p in healed)
        + "\n\nОни восстановлены из резервной копии. Приложение запустится дальше.",
        error=False,
    )
    return True


WEBVIEW2_PAGE = "https://developer.microsoft.com/microsoft-edge/webview2/"


def webview2_missing() -> bool:
    """Нет WebView2: pywebview тогда молча берёт движок Internet Explorer, и окно
    открывается пустым. Решает он это сам при импорте winforms, его флаг и берём."""
    try:
        from webview.platforms import winforms
    except Exception:
        return False  # сломанный импорт покажет сам запуск, с подробностями в логе
    return not winforms.is_chromium


def import_failure_text(errors: dict[str, str]) -> str:
    """Текст для случая, когда пакеты не импортируются и после переустановки."""
    lines = "\n".join(f"  {pkg}: {err[:300]}" for pkg, err in errors.items())
    text = "Пакеты не загружаются, хотя их уже переустанавливали:\n\n" + lines
    if any("dll load failed" in err.lower() for err in errors.values()):
        text += (
            "\n\nОшибка DLL обычно значит, что в Windows нет Microsoft Visual C++ "
            "Redistributable (x64). Установи его со страницы\n  " + VC_REDIST_URL
            + "\nи запусти Дубль снова."
        )
    return text


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _merge(*lists: list[str]) -> list[str]:
    """Объединяет списки пакетов без повторов (имена сравниваются без учёта -_. и регистра)."""
    out: list[str] = []
    seen: set[str] = set()
    for items in lists:
        for name in items:
            if _canon(name) not in seen:
                seen.add(_canon(name))
                out.append(name)
    return out


def _run() -> int:
    global bootstrap
    import bootstrap

    if not check_own_files():
        return 1

    healed = bootstrap.already_healed()

    # Пакеты, у которых файлы побиты, а импорт ещё проходит: метка фоновой проверки
    # и стек окна. Чинятся до всяких импортов, пока Windows даёт заменить их DLL.
    # Не вышло — не страшно, запускаемся как есть, а метку после попытки снимаем:
    # иначе она будет мешать каждому старту. Без связи с PyPI чинить нечем (uv ждал бы
    # отказа соединения по 15 секунд на пакет): сразу запускаемся, метка остаётся.
    known = _merge(bootstrap.damaged_packages(bootstrap.WINDOW_STACK), bootstrap.marked_broken())
    if known and not healed and not bootstrap.online():
        bootstrap.mark_broken(known)
    else:
        if known and not healed:
            failed = heal(known)
            if failed:
                # Запуск всё равно пробуем: окно само скажет, если без пакета не открылось
                write_error_log("Не удалось переставить пакеты: " + ", ".join(failed))
        bootstrap.clear_mark()

    errors = bootstrap.import_errors()
    if not errors:
        if webview2_missing():
            text = ("Окну Дубля нужен компонент Microsoft Edge WebView2, а на этом компьютере "
                    "его нет. Он бесплатный, ставится за минуту. После ОК откроется страница "
                    "загрузки: возьми там Evergreen Bootstrapper, поставь и запусти Дубль снова.")
            write_error_log(text)
            show_message("Нужен WebView2", text)
            import webbrowser

            webbrowser.open(WEBVIEW2_PAGE)
            return 1
        import app

        app.main()
        return 0

    if healed:
        # Уже переустанавливали и перезапускались, а импорт снова падает: дело не
        # в файлах пакетов (нет VC++ Redistributable, чужая версия Windows)
        text = import_failure_text(errors)
        write_error_log(text)
        show_message(
            "Дубль не запускается",
            text + "\n\nПодробности в файле ошибка.log рядом с программой.",
        )
        return 1

    if not bootstrap.online():
        # Импорт сломан, а переустановить нечем. Запомним, что чинить, и починим
        # на следующем запуске ещё до импортов
        bootstrap.mark_broken(_merge(known, list(errors)))
        text = (
            "Часть пакетов не загружается:\n\n"
            + "\n".join(f"  {pkg}: {err[:300]}" for pkg, err in errors.items())
            + "\n\nЧтобы переустановить их, нужен интернет, а pypi.org не отвечает. "
            "Подключись к сети и запусти Дубль снова, починка начнётся сама."
        )
        write_error_log(text)
        show_message("Нет связи с интернетом", text)
        return 1

    failed = heal(list(errors))
    if failed:
        write_error_log(
            "Не получилось переустановить: " + ", ".join(failed) + "\n\n"
            + "\n".join(f"{pkg}: {err}" for pkg, err in errors.items())
        )
        show_message(
            "Не удалось восстановить",
            "Не получилось переустановить: " + ", ".join(failed) + "\n\n"
            "Закрой Дубль и выполни в папке программы:\n  "
            + bootstrap.manual_repair_command(failed) + "\n\n"
            "И добавь папку программы в исключения антивируса, "
            "иначе он испортит файлы снова.",
        )
        return 1

    # Часть модулей могла успеть загрузиться в память в битом виде,
    # поэтому не продолжаем в этом процессе, а стартуем заново
    bootstrap.restart_self()
    return 0


def main() -> int:
    try:
        return _run()
    except Exception:
        text = traceback.format_exc()
        write_error_log(text)
        last = scrub(text.strip().splitlines()[-1])[:500]
        show_message(
            "Дубль не запустился",
            f"{last}\n\nПодробности в файле:\n{LOG}",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
