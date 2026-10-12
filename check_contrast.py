"""Проверка контраста по WCAG для обеих тем.

Цвета читаются прямо из web/style.css, поэтому копия в этом файле не разъедется
с настоящими. Подложки пузырей (--me-soft и другие) считаются так же, как их
смешивает браузер в color-mix.

    .venv\\Scripts\\python.exe check_contrast.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parent / "web" / "style.css"

# Доля смыслового цвета в подложке, как в color-mix внутри style.css
SOFT = {"me_soft": ("me", 0.12), "them_soft": ("them", 0.12),
        "rec_soft": ("rec", 0.10), "warn_soft": ("warn", 0.12)}

# (что, на чём, минимум). 4.5 для обычного текста, 3.0 для крупного и значков,
# 1.5 для границ полей
CHECKS = [
    ("ink", "paper", 4.5), ("ink", "chrome", 4.5), ("ink", "fill", 4.5),
    ("ink_2", "paper", 4.5), ("ink_2", "chrome", 4.5), ("ink_2", "fill", 4.5),
    ("on_ink", "ink", 4.5), ("on_rec", "rec", 4.5),
    ("me", "paper", 4.5), ("me", "chrome", 4.5),
    ("them", "paper", 4.5), ("them", "chrome", 4.5),
    ("rec", "paper", 4.5), ("rec", "chrome", 4.5), ("rec", "rec_soft", 4.5),
    ("warn", "paper", 4.5), ("warn", "warn_soft", 4.5),
    ("ink", "me_soft", 4.5), ("ink", "them_soft", 4.5),
    ("me", "me_soft", 3.0), ("rec", "rec_soft", 3.0),
    ("line_strong", "paper", 1.5), ("line_strong", "chrome", 1.5),
]


def tokens(block: str) -> dict[str, str]:
    return {k.replace("-", "_"): v.lower()
            for k, v in re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", block)}


def themes() -> dict[str, dict[str, str]]:
    css = CSS.read_text(encoding="utf-8")
    light = tokens(css.split("@media (prefers-color-scheme: dark)")[0])
    dark_block = css.split("@media (prefers-color-scheme: dark)")[1].split("\n}\n")[0]
    dark = {**light, **tokens(dark_block)}
    return {"светлая": light, "тёмная": dark}


def rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def mix(a: str, b: str, share: float) -> str:
    ca, cb = rgb(a), rgb(b)
    return "#" + "".join(f"{round((x * share + y * (1 - share)) * 255):02x}" for x, y in zip(ca, cb))


def lum(hex_color: str) -> float:
    conv = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb(hex_color)]
    return 0.2126 * conv[0] + 0.7152 * conv[1] + 0.0722 * conv[2]


def ratio(a: str, b: str) -> float:
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def main() -> int:
    bad = 0
    for name, c in themes().items():
        for key, (base, share) in SOFT.items():
            c[key] = mix(c[base], c["paper"], share)
        print(f"\n=== {name} ===")
        for fg, bgk, need in CHECKS:
            r = ratio(c[fg], c[bgk])
            ok = r >= need
            bad += not ok
            print(f"  {'ок  ' if ok else 'МАЛО'} {fg:13} на {bgk:10} {r:5.2f}  (нужно {need})")
    print(f"\nпроблем: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
