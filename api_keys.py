"""Ключи API лежат в диспетчере учётных данных Windows, а не в файле настроек.

Файл настроек живёт рядом с программой, его легко отправить в git или в чат
вместе с папкой. Ключу там не место.

Для консоли и тех, кто не хочет хранить ключ в системе, работает переменная
окружения DUBL_KEY_<СЕРВИС>, например DUBL_KEY_GROQ. Она важнее сохранённого ключа.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "dubl"


def get(provider: str) -> str:
    env = os.environ.get(f"DUBL_KEY_{provider.upper()}", "").strip()
    if env:
        return env
    try:
        return keyring.get_password(SERVICE, provider) or ""
    except KeyringError:
        return ""


def has(provider: str) -> bool:
    return bool(get(provider))


def put(provider: str, key: str) -> None:
    keyring.set_password(SERVICE, provider, key.strip())


def forget(provider: str) -> None:
    try:
        keyring.delete_password(SERVICE, provider)
    except PasswordDeleteError:
        pass  # и так не было
