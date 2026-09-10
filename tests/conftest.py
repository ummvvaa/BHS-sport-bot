"""Общая обвязка тестов: подменяем настройки и Google-таблицу на поддельные."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Значения подставляются до импорта config: python-dotenv не перетирает уже заданное окружение.
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("SPREADSHEET_ID", "test-spreadsheet")
os.environ.setdefault("ADMIN_IDS", "1")

import sheets  # noqa: E402
from tests.fake_sheets import FakeSpreadsheet  # noqa: E402

SETTINGS_ROWS = [["Ключ", "Значение"], ["deadline", "2099-01-01 00:00"]]
LIMITS_ROWS = [
    ["Секция", "8-9", "10-11"],
    ["Футбол", "2", "1"],
    ["Шахматы", "1", ""],
]
STUDENTS_ROWS = [sheets.STUDENTS_HEADERS]


def make_spreadsheet(**extra: list[list[str]]) -> FakeSpreadsheet:
    data = {
        sheets.SETTINGS_SHEET: [list(row) for row in SETTINGS_ROWS],
        sheets.LIMITS_SHEET: [list(row) for row in LIMITS_ROWS],
        sheets.STUDENTS_SHEET: [list(row) for row in STUDENTS_ROWS],
        sheets.ANALYTICS_SHEET: [],
        "Футбол": [list(sheets.SECTION_HEADERS)],
        "Шахматы": [list(sheets.SECTION_HEADERS)],
    }
    data.update(extra)
    # «Аналитика» в проде создана прошлой версией бота: 100 строк × 6 колонок.
    return FakeSpreadsheet(data, grids={sheets.ANALYTICS_SHEET: (100, 6)})


def install(spreadsheet: FakeSpreadsheet, monkeypatch: pytest.MonkeyPatch) -> FakeSpreadsheet:
    """Ставит модуль sheets в исходное состояние поверх поддельной таблицы."""
    monkeypatch.setattr(sheets, "_spreadsheet", lambda: spreadsheet)
    monkeypatch.setattr(sheets, "_worksheets_cache", None, raising=False)
    monkeypatch.setattr(sheets, "_store", sheets._Store())
    monkeypatch.setattr(sheets, "_lock", asyncio.Lock())
    monkeypatch.setattr(sheets, "_analytics_dirty", False, raising=False)
    monkeypatch.setattr(sheets, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(sheets, "_tasks", [])
    sheets.reset_api_call_count()
    return spreadsheet


@pytest.fixture
def sheet(monkeypatch: pytest.MonkeyPatch) -> FakeSpreadsheet:
    return install(make_spreadsheet(), monkeypatch)


def run(coro):
    """Тесты синхронные: гоняем корутины через asyncio.run, без pytest-asyncio.

    asyncio.Lock привязывается к первому event loop, а asyncio.run каждый раз
    создаёт новый — поэтому перед запуском берём свежий Lock.
    """
    sheets._lock = asyncio.Lock()
    return asyncio.run(coro)
