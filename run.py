"""Точка входа. Проверяет окружение, чинит побитое и запускает приложение.

Отдельный файл нужен потому, что проверять надо ДО импорта тяжёлых модулей:
если антивирус испортил av или ctranslate2, обычный запуск падает с ImportError
ещё на строке импорта, и пользователь видит только молчащий ярлык.

Здесь используется исключительно то, что входит в сам Python (tkinter, subprocess),
поэтому лаунчер работает даже когда из venv выгрызли половину пакетов.
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from tkinter import messagebox

import bootstrap


class HealWindow:
    """Маленькое окно на время починки. Показывается, только если что-то сломано."""

    def __init__(self, broken: list[str]) -> None:
        self.root = tk.Tk()
        self.root.title("Восстановление")
        self.root.geometry("520x210")
        self.root.configure(bg="#141821")
        self.root.resizable(False, False)

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

    def work(self) -> None:
        self.failed = bootstrap.repair(self.broken, self.log)
        self.done = True

    def run(self) -> list[str]:
        def worker():
            self.work()
            self.root.after(0, self.root.destroy)

        threading.Thread(target=worker, daemon=True).start()
        self.root.mainloop()
        return self.failed


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
    root = tk.Tk()
    root.withdraw()

    if failed:
        messagebox.showerror(
            "Файлы приложения повреждены",
            "Антивирус испортил файлы, и целых копий для них нет:\n\n"
            + "\n".join("  " + p.name for p in failed)
            + "\n\nВосстанови их из репозитория:\n"
            "  git checkout -- " + " ".join(p.name for p in failed) + "\n\n"
            "И добавь папку приложения в исключения антивируса.",
        )
        root.destroy()
        return False

    messagebox.showinfo(
        "Файлы восстановлены",
        "Антивирус испортил файлы приложения:\n\n"
        + "\n".join("  " + p.name for p in healed)
        + "\n\nОни восстановлены из резервной копии. Приложение запустится дальше.",
    )
    root.destroy()
    return True


def main() -> int:
    if not check_own_files():
        return 1

    broken = bootstrap.find_broken()
    extra = bootstrap.damaged_packages(bootstrap.WINDOW_STACK)
    extra += bootstrap.marked_broken()
    broken += [p for p in extra if p not in broken]

    if broken:
        failed = HealWindow(broken).run()
        if not failed:
            bootstrap.clear_mark()
        if failed:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Не удалось восстановить",
                "Не получилось переустановить: " + ", ".join(failed) + "\n\n"
                "Выполни вручную в папке приложения:\n"
                "  uv pip install --force-reinstall --no-cache " + " ".join(failed) + "\n\n"
                "И добавь папку приложения в исключения антивируса, "
                "иначе он испортит файлы снова.",
            )
            return 1

        # Часть модулей могла успеть загрузиться в память в битом виде,
        # поэтому не продолжаем в этом процессе, а стартуем заново
        bootstrap.restart_self()
        return 0

    import app

    app.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
