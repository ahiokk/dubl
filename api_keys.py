"""Ключи API лежат в диспетчере учётных данных Windows, а не в файле настроек.

Файл настроек живёт рядом с программой, его легко отправить в git или в чат
вместе с папкой. Ключу там не место.

Для консоли и тех, кто не хочет хранить ключ в системе, работает переменная
окружения DUBL_KEY_<СЕРВИС>, например DUBL_KEY_GROQ. Она важнее сохранённого ключа.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import NoKeyringError, PasswordDeleteError

SERVICE = "dubl"

# Настоящие ключи длиннее двух десятков знаков. Нижняя граница отсекает
# случайно вставленное слово, верхняя не даёт упереться в предел хранилища.
# Для своего сервера (custom) нижней границы нет, см. put
MIN_LEN = 6
MAX_LEN = 1024


class KeyStoreError(Exception):
    """Ключ не принят или не сохранился. Текст можно показать человеку как есть.

    В тексте ключа нет никогда: он уходит в окно и в журнал.
    """


class KeyFormatError(KeyStoreError):
    """Ключ выглядит неправильно: не то вставили или вставили с лишним."""


def check_format(key: str, min_len: int = MIN_LEN) -> str:
    """Ключ без пробелов по краям или KeyFormatError.

    Ключ идёт в HTTP-заголовок, поэтому годятся только печатные ASCII без
    пробелов и переводов строк. Кириллица и пробелы внутри обычно значат, что
    вместе с ключом вставили подпись («Ключ: …») или русский текст.
    """
    key = (key or "").strip()
    if not key:
        raise KeyFormatError("Ключ пустой")
    if any(not ("!" <= c <= "~") for c in key):
        raise KeyFormatError(
            "Похоже, ключ вставился с лишними символами: в нём пробел, перенос строки, "
            "кириллица или невидимый знак. Скопируй сам ключ ещё раз, без подписи и пробелов"
        )
    if len(key) < min_len:
        raise KeyFormatError(f"Ключ слишком короткий (меньше {min_len} знаков), похоже, скопировано не всё")
    if len(key) > MAX_LEN:
        raise KeyFormatError("Ключ слишком длинный, похоже, вместе с ним вставили что-то ещё")
    return key


def get(provider: str) -> str:
    env = os.environ.get(f"DUBL_KEY_{provider.upper()}", "").strip()
    if env:
        return env
    try:
        return keyring.get_password(SERVICE, provider) or ""
    except Exception:
        # KeyringError, а на Windows ещё и pywintypes.error: без хранилища
        # ключа просто нет, а окно настроек не должно из-за этого падать
        return ""


def has(provider: str) -> bool:
    return bool(get(provider))


def put(provider: str, key: str) -> None:
    """Сохраняет ключ. Не вышло: KeyStoreError с понятным текстом."""
    # У своего сервера токен бывает любым («EMPTY» у vLLM, «local»), там нижней
    # границы нет. Допустимые знаки и верхняя граница проверяются всё равно
    key = check_format(key, min_len=1 if provider == "custom" else MIN_LEN)
    try:
        keyring.set_password(SERVICE, provider, key)
    except NoKeyringError:
        raise KeyStoreError(
            "не удалось сохранить ключ в Диспетчере учётных данных Windows: "
            "хранилище недоступно. "
            f"Ключ можно задать переменной окружения DUBL_KEY_{provider.upper()}"
        ) from None
    except Exception as exc:
        # KeyringError и ошибки самой Windows: детали полезны, ключ в них не попадёт
        why = " ".join(str(exc).replace(key, "***").split()) or type(exc).__name__
        raise KeyStoreError(
            f"не удалось сохранить ключ в Диспетчере учётных данных Windows: {why}"
        ) from None


def forget(provider: str) -> None:
    """Удаляет сохранённый ключ. Не вышло: KeyStoreError с понятным текстом."""
    try:
        keyring.delete_password(SERVICE, provider)
    except (PasswordDeleteError, NoKeyringError):
        pass  # и так не было: без хранилища сохранённого ключа быть не могло
    except Exception as exc:  # KeyringError и ошибки самой Windows
        why = " ".join(str(exc).split()) or type(exc).__name__
        raise KeyStoreError(
            f"не удалось удалить ключ из Диспетчера учётных данных Windows: {why}"
        ) from None
