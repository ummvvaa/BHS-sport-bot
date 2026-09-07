"""Чтение конфигурации из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(Exception):
    """Ошибка конфигурации: отсутствуют или некорректны переменные окружения."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    spreadsheet_id: str
    admin_ids: frozenset[int]
    credentials_path: str


def _clean(value: str | None) -> str:
    """Убирает пробелы и хвостовые комментарии вида `# ...` из значения."""
    if value is None:
        return ""
    return value.split("#", 1)[0].strip()


def load_settings() -> Settings:
    load_dotenv()

    bot_token = _clean(os.getenv("BOT_TOKEN"))
    spreadsheet_id = _clean(os.getenv("SPREADSHEET_ID"))
    admin_ids_raw = _clean(os.getenv("ADMIN_IDS"))
    credentials_path = _clean(os.getenv("GOOGLE_CREDENTIALS_PATH")) or "credentials.json"

    missing: list[str] = []
    if not bot_token:
        missing.append("BOT_TOKEN")
    if not spreadsheet_id:
        missing.append("SPREADSHEET_ID")
    if not admin_ids_raw:
        missing.append("ADMIN_IDS")
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
            + ". Скопируй .env.example в .env и заполни значения."
        )

    admin_ids: set[int] = set()
    for part in admin_ids_raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(
                f"ADMIN_IDS должен содержать числовые tg_id через запятую, получено: {part!r}"
            )
        admin_ids.add(int(part))
    if not admin_ids:
        raise ConfigError("ADMIN_IDS не содержит ни одного tg_id.")

    return Settings(
        bot_token=bot_token,
        spreadsheet_id=spreadsheet_id,
        admin_ids=frozenset(admin_ids),
        credentials_path=credentials_path,
    )


settings: Settings = load_settings()
