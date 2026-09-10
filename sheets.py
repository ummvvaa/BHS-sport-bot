"""Слой работы с Google Sheets. Единственное место в проекте, где используется gspread.

Чтения из Google Sheets — самый дефицитный ресурс (квота «Read requests per
minute per user»), поэтому таблица целиком держится в памяти:

* «Настройки», «Лимиты», «Ученики» и все листы секций читаются двумя batch-
  запросами при старте, по /reload и раз в SYNC_INTERVAL_SECONDS;
* все чтения бота (get_student, доступность секций, /stats, /analytics,
  /export, напоминания) обслуживаются копией в памяти — без запросов к API;
* запись (регистрация, запись на секцию, отмена) идёт в таблицу и сразу же
  применяется к копии, поэтому копия не отстаёт от таблицы;
* лист «Аналитика» перезаписывается не чаще раза в ANALYTICS_DEBOUNCE_SECONDS.

Google-таблица остаётся источником правды для админа: ручные правки подхватит
фоновая синхронизация.

gspread синхронный, поэтому все обращения к API обёрнуты в asyncio.to_thread
и проходят через `_api` (ретраи при 429). Копия в памяти меняется только в
event loop — из рабочих потоков к ней никто не прикасается, поэтому гонок нет.
Операции записи сериализуются через asyncio.Lock и перепроверяют лимиты по
копии непосредственно перед изменением данных.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from gspread.utils import ValueInputOption

from config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

TZ = ZoneInfo("Asia/Almaty")
CLASSES: tuple[int, ...] = (8, 9, 10, 11)
GROUPS: tuple[str, ...] = ("8-9", "10-11")
CLASS_GROUP: dict[int, str] = {8: "8-9", 9: "8-9", 10: "10-11", 11: "10-11"}
DATE_FORMAT = "%d.%m.%Y %H:%M"

# Как часто копия в памяти сверяется с таблицей (ручные правки админа).
SYNC_INTERVAL_SECONDS = 300
# Лист «Аналитика» перезаписывается не чаще одного раза за этот интервал.
ANALYTICS_DEBOUNCE_SECONDS = 60
# Паузы перед повторами при HTTP 429 (Rate Limit Exceeded).
RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0)
RATE_LIMIT_STATUS = 429

SETTINGS_SHEET = "Настройки"
LIMITS_SHEET = "Лимиты"
STUDENTS_SHEET = "Ученики"
ANALYTICS_SHEET = "Аналитика"
ANALYTICS_COLS = 8  # самый широкий блок — «Секции — по группам»
ANALYTICS_ROWS = 200
ANALYTICS_BY_DAY_DAYS = 14
STUDENTS_HEADERS: list[str] = [
    "tg_id", "Имя", "Класс", "Телефон", "Язык", "Секция", "Дата записи", "Дата регистрации",
]
SECTION_HEADERS: list[str] = ["Имя", "Класс", "Телефон", "tg_id", "Дата записи"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DEADLINE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y %H:%M:%S",
)


class SheetsError(Exception):
    """Любая ошибка при работе с Google Sheets."""


class EnrollStatus(str, Enum):
    OK = "ok"
    FULL = "full"
    ALREADY = "already"
    NOT_REGISTERED = "not_registered"
    UNKNOWN_SECTION = "unknown_section"


def group_of(class_num: int) -> str | None:
    """8, 9 → «8-9»; 10, 11 → «10-11»; иначе None."""
    return CLASS_GROUP.get(class_num)


@dataclass(frozen=True)
class SectionLimit:
    name: str
    per_group: dict[str, int | None]  # None или 0 — группа не допускается к секции

    def group_limit(self, group: str | None) -> int:
        if group is None:
            return 0
        return self.per_group.get(group) or 0

    @property
    def total(self) -> int:
        return sum(self.group_limit(group) for group in GROUPS)

    @property
    def is_active(self) -> bool:
        return self.total > 0


@dataclass(frozen=True)
class Student:
    tg_id: int
    name: str
    class_num: int
    phone: str
    lang: str
    section: str | None
    enrolled_at: str | None
    registered_at: str | None
    row: int  # номер строки в листе «Ученики» (1-based)


@dataclass(frozen=True)
class Enrollment:
    name: str
    class_num: int
    phone: str
    tg_id: int
    enrolled_at: str
    row: int  # номер строки в листе секции (1-based)


@dataclass(frozen=True)
class Availability:
    name: str
    free_for_group: int
    free_total: int
    available: bool


@dataclass(frozen=True)
class EnrollResult:
    status: EnrollStatus
    section: str | None = None


@dataclass(frozen=True)
class SectionStats:
    limit: SectionLimit
    count_total: int
    count_by_group: dict[str, int]

    @property
    def name(self) -> str:
        return self.limit.name


@dataclass(frozen=True)
class Stats:
    sections: list[SectionStats]
    registered: int
    enrolled: int
    deadline: datetime | None
    closed: bool


@dataclass(frozen=True)
class ExportData:
    sections: list[tuple[SectionLimit, list[Enrollment]]]
    stats: Stats


@dataclass(frozen=True)
class ClassAnalytics:
    registered: int
    enrolled: int

    @property
    def without_section(self) -> int:
        return self.registered - self.enrolled


@dataclass(frozen=True)
class Analytics:
    days: int | None
    per_class: dict[int, ClassAnalytics]
    by_day: list[tuple[str, dict[int, int]]]  # («06.09», {8: 5, 9: 1, ...}) — регистрации по дням
    by_day_truncated: bool

    @property
    def registered(self) -> int:
        return sum(item.registered for item in self.per_class.values())

    @property
    def enrolled(self) -> int:
        return sum(item.enrolled for item in self.per_class.values())

    @property
    def most_passive(self) -> tuple[int, int] | None:
        """(класс, зарегистрировано) с минимальным числом регистраций."""
        if not self.per_class:
            return None
        class_num = min(CLASSES, key=lambda c: (self.per_class[c].registered, c))
        return class_num, self.per_class[class_num].registered


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def _to_int(value: Any, default: int = 0) -> int:
    parsed = _to_int_or_none(value)
    return default if parsed is None else parsed


def _to_int_or_none(value: Any) -> int | None:
    """Терпимый парсинг: пробелы обрезаются, пустая строка и мусор → None."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) and row[index] is not None else ""


def _now() -> datetime:
    return datetime.now(TZ)


def _now_str() -> str:
    return _now().strftime(DATE_FORMAT)


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), DATE_FORMAT).replace(tzinfo=TZ)
    except ValueError:
        return None


def _quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


async def _run(fn: Callable[..., T], *args: Any) -> T:
    """Выполняет синхронную функцию в потоке и переводит любые ошибки в SheetsError."""
    try:
        return await asyncio.to_thread(fn, *args)
    except SheetsError:
        raise
    except Exception as exc:  # noqa: BLE001 — намеренно ловим всё от Google API
        raise SheetsError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Вызовы gspread: счётчик запросов и ретраи при 429
# --------------------------------------------------------------------------- #

_api_calls = 0


def api_call_count() -> int:
    """Сколько запросов к Sheets API сделано за время жизни процесса (диагностика)."""
    return _api_calls


def reset_api_call_count() -> None:
    global _api_calls
    _api_calls = 0


def _is_rate_limited(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code == RATE_LIMIT_STATUS


def _api(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Единственная точка вызова Google API: считает запросы и повторяет при 429.

    Паузы между попытками — RETRY_DELAYS (2, 4, 8 с). Выполняется в рабочем
    потоке, поэтому time.sleep не блокирует event loop.
    """
    global _api_calls
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        _api_calls += 1
        try:
            return fn(*args, **kwargs)
        except APIError as exc:
            if delay is None or not _is_rate_limited(exc):
                raise SheetsError(str(exc)) from exc
            logger.warning(
                "Sheets API 429 (попытка %s из %s), повтор через %s с: %s",
                attempt, len(RETRY_DELAYS) + 1, delay, exc,
            )
            time.sleep(delay)
    raise SheetsError("Не удалось выполнить запрос к Google Sheets.")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Подключение
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=1)
def _spreadsheet() -> gspread.Spreadsheet:
    if settings.credentials_info is not None:
        # Ключ передан через переменную окружения GOOGLE_CREDENTIALS_JSON (Railway и т.п.).
        creds = Credentials.from_service_account_info(settings.credentials_info, scopes=SCOPES)
    else:
        path = Path(settings.credentials_path)
        if not path.is_file():
            raise SheetsError(
                f"Файл сервисного аккаунта не найден: {path.resolve()}. "
                "Проверь GOOGLE_CREDENTIALS_PATH в .env или задай GOOGLE_CREDENTIALS_JSON."
            )
        creds = Credentials.from_service_account_file(str(path), scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(settings.spreadsheet_id)


_worksheets_cache: dict[str, gspread.Worksheet] | None = None


def _worksheets(*, refresh: bool = False) -> dict[str, gspread.Worksheet]:
    """Метаданные листов. Кэшируются до /reload: имена листов меняются редко."""
    global _worksheets_cache
    if refresh or _worksheets_cache is None:
        _worksheets_cache = {ws.title: ws for ws in _api(_spreadsheet().worksheets)}
    return _worksheets_cache


def _ws(title: str) -> gspread.Worksheet:
    ws = _worksheets().get(title)
    if ws is None:
        ws = _worksheets(refresh=True).get(title)
    if ws is None:
        raise SheetsError(f"Лист «{title}» не найден в таблице.")
    return ws


def _batch_get(ranges: list[str]) -> list[list[list[str]]]:
    """Одним запросом читает несколько диапазонов. Возвращает строки в порядке ranges."""
    if not ranges:
        return []
    response = _api(_spreadsheet().values_batch_get, ranges)
    value_ranges = response.get("valueRanges", []) if isinstance(response, dict) else []
    result = [value_range.get("values") or [] for value_range in value_ranges]
    result.extend([] for _ in range(len(ranges) - len(result)))
    return result


# --------------------------------------------------------------------------- #
# Разбор листов
# --------------------------------------------------------------------------- #


def _parse_settings(rows: list[list[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        key = _cell(row, 0)
        if key:
            result[key.lower()] = _cell(row, 1)
    return result


def _parse_limits(rows: list[list[str]]) -> list[SectionLimit]:
    """Разбирает лист «Лимиты»: Секция | 8-9 | 10-11. Секции без лимитов отбрасываются."""
    if not rows:
        return []
    header = [h.strip().lower().replace(" ", "") for h in rows[0]]

    def col(name: str) -> int:
        try:
            return header.index(name.lower())
        except ValueError as exc:
            raise SheetsError(
                f"В листе «{LIMITS_SHEET}» нет колонки «{name}». "
                f"Ожидаются: Секция | {' | '.join(GROUPS)}"
            ) from exc

    name_col = col("Секция")
    group_cols = {group: col(group) for group in GROUPS}

    limits: list[SectionLimit] = []
    seen: set[str] = set()
    for row in rows[1:]:
        name = _cell(row, name_col)
        if not name or name in seen:
            continue
        seen.add(name)
        limit = SectionLimit(
            name=name,
            per_group={group: _to_int_or_none(_cell(row, group_cols[group])) for group in GROUPS},
        )
        if not limit.is_active:
            logger.info("Секция «%s» без лимитов в обеих группах — пропускается", name)
            continue
        limits.append(limit)
    return limits


def _parse_student(row: list[str], row_number: int) -> Student | None:
    tg_raw = _cell(row, 0)
    if not tg_raw.lstrip("-").isdigit():
        return None
    lang = _cell(row, 4).lower() or "ru"
    return Student(
        tg_id=int(tg_raw),
        name=_cell(row, 1),
        class_num=_to_int(_cell(row, 2)),
        phone=_cell(row, 3),
        lang=lang if lang in ("ru", "kk") else "ru",
        section=_cell(row, 5) or None,
        enrolled_at=_cell(row, 6) or None,
        registered_at=_cell(row, 7) or None,
        row=row_number,
    )


def _parse_students(rows: list[list[str]]) -> dict[int, Student]:
    """Строки листа «Ученики» (начиная с заголовка) → копия в памяти по tg_id."""
    students: dict[int, Student] = {}
    for index, row in enumerate(rows[1:], start=2):
        student = _parse_student(row, index)
        if student is None:
            continue
        if student.tg_id in students:
            logger.warning(
                "В листе «%s» повторяется tg_id %s (строки %s и %s) — берём первую",
                STUDENTS_SHEET, student.tg_id, students[student.tg_id].row, student.row,
            )
            continue
        students[student.tg_id] = student
    return students


def _parse_enrollment(row: list[str], row_number: int) -> Enrollment | None:
    name = _cell(row, 0)
    tg_raw = _cell(row, 3)
    if not name and not tg_raw:
        return None
    return Enrollment(
        name=name,
        class_num=_to_int(_cell(row, 1)),
        phone=_cell(row, 2),
        tg_id=int(tg_raw) if tg_raw.lstrip("-").isdigit() else 0,
        enrolled_at=_cell(row, 4),
        row=row_number,
    )


# --------------------------------------------------------------------------- #
# Копия таблицы в памяти
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Snapshot:
    settings: dict[str, str]
    limits: list[SectionLimit]
    students: dict[int, Student]
    enrollments: dict[str, list[Enrollment]]


class _Store:
    """Копия листов «Настройки», «Лимиты», «Ученики» и листов секций.

    Меняется только в event loop, поэтому синхронизация не нужна.
    """

    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.limits: list[SectionLimit] = []
        self.students: dict[int, Student] = {}
        self.enrollments: dict[str, list[Enrollment]] = {}
        self.loaded = False
        self.synced_at: float | None = None

    def apply(self, snapshot: _Snapshot) -> None:
        self.settings = snapshot.settings
        self.limits = snapshot.limits
        self.students = snapshot.students
        self.enrollments = snapshot.enrollments
        self.loaded = True
        self.synced_at = time.monotonic()

    def clear(self) -> None:
        self.settings = {}
        self.limits = []
        self.students = {}
        self.enrollments = {}
        self.loaded = False
        self.synced_at = None

    def section(self, name: str) -> list[Enrollment]:
        return self.enrollments.setdefault(name, [])

    def limit(self, name: str) -> SectionLimit | None:
        return next((item for item in self.limits if item.name == name), None)

    def students_list(self) -> list[Student]:
        return sorted(self.students.values(), key=lambda s: s.row)


_store = _Store()
_lock = asyncio.Lock()  # сериализует запись и полную пересинхронизацию копии


def _load_base_sync() -> tuple[dict[str, str], list[SectionLimit], dict[int, Student], list[str]]:
    """Одним запросом читает «Настройки», «Лимиты» и «Ученики» (+строку заголовков)."""
    values = _batch_get([
        f"{_quote_sheet(SETTINGS_SHEET)}!A:B",
        f"{_quote_sheet(LIMITS_SHEET)}!A:Z",
        f"{_quote_sheet(STUDENTS_SHEET)}!A:H",
    ])
    students_rows = values[2]
    header = [str(cell).strip() for cell in students_rows[0]] if students_rows else []
    return _parse_settings(values[0]), _parse_limits(values[1]), _parse_students(students_rows), header


def _load_enrollments_sync(limits: list[SectionLimit]) -> dict[str, list[Enrollment]]:
    """Одним batch-запросом читает записи всех листов секций."""
    result: dict[str, list[Enrollment]] = {limit.name: [] for limit in limits}
    if not limits:
        return result
    ranges = [f"{_quote_sheet(limit.name)}!A2:E" for limit in limits]
    for limit, rows in zip(limits, _batch_get(ranges), strict=False):
        enrollments: list[Enrollment] = []
        for index, row in enumerate(rows, start=2):
            enrollment = _parse_enrollment(row, index)
            if enrollment is not None:
                enrollments.append(enrollment)
        result[limit.name] = enrollments
    return result


def _load_snapshot_sync() -> _Snapshot:
    """Полное чтение таблицы: ровно два запроса к API."""
    settings_map, limits, students, _ = _load_base_sync()
    return _Snapshot(settings_map, limits, students, _load_enrollments_sync(limits))


async def _ensure_loaded() -> None:
    """Гарантирует, что копия в памяти заполнена (единственное чтение при холодном старте)."""
    if _store.loaded:
        return
    async with _lock:
        if _store.loaded:
            return
        _store.apply(await _run(_load_snapshot_sync))
        logger.info(
            "Копия таблицы загружена: учеников %s, секций %s",
            len(_store.students), len(_store.limits),
        )


# --------------------------------------------------------------------------- #
# Настройки, лимиты, доступность — всё из копии
# --------------------------------------------------------------------------- #


def _parse_deadline(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in DEADLINE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    logger.warning(
        "Не удалось разобрать deadline %r в листе «%s». Ожидается формат YYYY-MM-DD HH:MM. "
        "Дедлайн считается не заданным.",
        raw,
        SETTINGS_SHEET,
    )
    return None


def _deadline() -> datetime | None:
    return _parse_deadline(_store.settings.get("deadline", ""))


def _closed() -> bool:
    deadline = _deadline()
    return deadline is not None and _now() >= deadline


def _count(enrollments: list[Enrollment]) -> tuple[int, dict[str, int]]:
    by_group: dict[str, int] = {group: 0 for group in GROUPS}
    for item in enrollments:
        group = group_of(item.class_num)
        if group is not None:
            by_group[group] += 1
    return len(enrollments), by_group


def _free_for_group(limit: SectionLimit, enrollments: list[Enrollment], group: str | None) -> int:
    if group is None:
        return 0
    _, by_group = _count(enrollments)
    return max(limit.group_limit(group) - by_group.get(group, 0), 0)


def _availability(class_num: int) -> list[Availability]:
    """Секции для группы ученика. Секции с пустым/нулевым лимитом группы не попадают в список."""
    group = group_of(class_num)
    if group is None:
        return []
    result: list[Availability] = []
    for limit in _store.limits:
        if limit.group_limit(group) <= 0:
            continue
        items = _store.section(limit.name)
        total, _ = _count(items)
        free_group = _free_for_group(limit, items, group)
        result.append(
            Availability(
                name=limit.name,
                free_for_group=free_group,
                free_total=max(limit.total - total, 0),
                available=free_group > 0,
            )
        )
    return result


# --------------------------------------------------------------------------- #
# Запись в таблицу (выполняется в потоке, копию не трогает)
# --------------------------------------------------------------------------- #

_RANGE_ROW_RE = re.compile(r"![A-Za-z]+(\d+)")


def _appended_row(response: Any, fallback: int) -> int:
    """Номер добавленной строки из ответа values.append — чтобы не перечитывать лист."""
    updates = response.get("updates") if isinstance(response, dict) else None
    updated_range = updates.get("updatedRange", "") if isinstance(updates, dict) else ""
    match = _RANGE_ROW_RE.search(str(updated_range))
    if match:
        return int(match.group(1))
    logger.warning("Не удалось определить номер строки из ответа API (%r)", updated_range)
    return fallback


def _next_row(rows: list[int]) -> int:
    return max(rows) + 1 if rows else 2


def _append_student_sync(
    tg_id: int, name: str, class_num: int, phone: str, lang: str, registered_at: str, fallback: int
) -> int:
    response = _api(
        _ws(STUDENTS_SHEET).append_row,
        [str(tg_id), name, str(class_num), phone, lang, "", "", registered_at],
        value_input_option=ValueInputOption.raw,
    )
    return _appended_row(response, fallback)


def _update_student_sync(row: int, tg_id: int, name: str, class_num: int, phone: str, lang: str) -> None:
    # При перерегистрации обновляются только A:E; секция, даты записи и регистрации сохраняются.
    _api(
        _ws(STUDENTS_SHEET).update,
        range_name=f"A{row}:E{row}",
        values=[[str(tg_id), name, str(class_num), phone, lang]],
        value_input_option=ValueInputOption.raw,
    )


def _update_lang_sync(row: int, lang: str) -> None:
    _api(
        _ws(STUDENTS_SHEET).update,
        range_name=f"E{row}",
        values=[[lang]],
        value_input_option=ValueInputOption.raw,
    )


def _append_enrollment_sync(section: str, student: Student, when: str, fallback: int) -> int:
    response = _api(
        _ws(section).append_row,
        [student.name, str(student.class_num), student.phone, str(student.tg_id), when],
        value_input_option=ValueInputOption.raw,
    )
    return _appended_row(response, fallback)


def _set_student_section_sync(row: int, section: str, when: str) -> None:
    _api(
        _ws(STUDENTS_SHEET).update,
        range_name=f"F{row}:G{row}",
        values=[[section, when]],
        value_input_option=ValueInputOption.raw,
    )


def _cancel_sync(section: str, rows: list[int], student_row: int) -> None:
    if rows:
        try:
            ws = _ws(section)
        except SheetsError:
            ws = None  # лист секции удалён вручную — чистим только «Учеников»
        if ws is not None:
            for row in sorted(rows, reverse=True):  # снизу вверх, чтобы индексы не сдвигались
                _api(ws.delete_rows, row)
    _api(
        _ws(STUDENTS_SHEET).update,
        range_name=f"F{student_row}:G{student_row}",
        values=[["", ""]],
        value_input_option=ValueInputOption.raw,
    )


def _drop_enrollment_rows(section: str, tg_id: int, removed: list[int]) -> None:
    """Убирает записи ученика из копии и сдвигает номера строк ниже удалённых."""
    kept: list[Enrollment] = []
    for item in _store.section(section):
        if item.tg_id == tg_id:
            continue
        offset = sum(1 for row in removed if row < item.row)
        kept.append(replace(item, row=item.row - offset) if offset else item)
    _store.enrollments[section] = kept


def _ensure_section_sheets_sync() -> tuple[list[str], _Snapshot]:
    """Проверяет обязательные листы, создаёт недостающие листы секций и читает всю таблицу."""
    sh = _spreadsheet()
    existing = _worksheets(refresh=True)
    for required in (SETTINGS_SHEET, LIMITS_SHEET, STUDENTS_SHEET):
        if required not in existing:
            raise SheetsError(
                f"В таблице нет обязательного листа «{required}». Создай его вручную (см. README)."
            )

    settings_map, limits, students, header = _load_base_sync()
    if not header or len(header) < len(STUDENTS_HEADERS):
        # Пустой лист или старая схема без «Дата регистрации» — дописываем заголовки.
        _api(
            existing[STUDENTS_SHEET].update,
            range_name="A1",
            values=[STUDENTS_HEADERS],
            value_input_option=ValueInputOption.raw,
        )

    if ANALYTICS_SHEET not in existing:
        _api(sh.add_worksheet, title=ANALYTICS_SHEET, rows=ANALYTICS_ROWS, cols=ANALYTICS_COLS)
        _worksheets(refresh=True)
        logger.info("Создан лист «%s»", ANALYTICS_SHEET)

    created: list[str] = []
    for limit in limits:
        if limit.name in existing:
            continue
        ws = _api(sh.add_worksheet, title=limit.name, rows=200, cols=len(SECTION_HEADERS))
        _api(
            ws.update,
            range_name="A1",
            values=[SECTION_HEADERS],
            value_input_option=ValueInputOption.raw,
        )
        created.append(limit.name)
    if created:
        _worksheets(refresh=True)

    snapshot = _Snapshot(settings_map, limits, students, _load_enrollments_sync(limits))
    return created, snapshot


# --------------------------------------------------------------------------- #
# Статистика, экспорт, аналитика — всё считается по копии
# --------------------------------------------------------------------------- #


def _sections_snapshot() -> list[tuple[SectionLimit, list[Enrollment]]]:
    """Секции в порядке листа «Лимиты» (скрытые уже отфильтрованы) + копии их записей."""
    return [(limit, list(_store.section(limit.name))) for limit in _store.limits]


def _collect() -> ExportData:
    sections = _sections_snapshot()
    section_stats: list[SectionStats] = []
    for limit, items in sections:
        total, by_group = _count(items)
        section_stats.append(SectionStats(limit=limit, count_total=total, count_by_group=by_group))

    students = _store.students.values()
    stats = Stats(
        sections=section_stats,
        registered=len(students),
        enrolled=sum(1 for s in students if s.section),
        deadline=_deadline(),
        closed=_closed(),
    )
    return ExportData(sections=sections, stats=stats)


BY_DAY_LIMIT = 30


def _in_window(student: Student, start: datetime | None, end: datetime | None) -> bool:
    """Попадает ли дата регистрации в [start, end). Без границ — попадают все.

    Строки без даты регистрации считаются «давними»: попадают только в окно без начала.
    """
    if start is None and end is None:
        return True
    registered = _parse_date(student.registered_at)
    if registered is None:
        return False
    if start is not None and registered < start:
        return False
    if end is not None and registered >= end:
        return False
    return True


def _class_counts(
    students: list[Student], start: datetime | None, end: datetime | None
) -> dict[int, ClassAnalytics]:
    window = [s for s in students if _in_window(s, start, end)]
    return {
        c: ClassAnalytics(
            registered=sum(1 for s in window if s.class_num == c),
            enrolled=sum(1 for s in window if s.class_num == c and s.section),
        )
        for c in CLASSES
    }


def _registrations_by_day(students: list[Student]) -> dict[datetime, dict[int, int]]:
    by_day: dict[datetime, dict[int, int]] = {}
    for student in students:
        registered = _parse_date(student.registered_at)
        if registered is None:
            continue
        day = registered.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day.setdefault(day, {c: 0 for c in CLASSES})[student.class_num] += 1
    return by_day


def _analytics(days: int | None) -> Analytics:
    """Статистика по каждому классу за всё время или за последние `days` дней (по дате регистрации)."""
    students = [s for s in _store.students.values() if s.class_num in CLASSES]
    cutoff = _now() - timedelta(days=days) if days else None
    per_class = _class_counts(students, cutoff, None)

    window = [s for s in students if _in_window(s, cutoff, None)]
    by_day_map = _registrations_by_day(window)
    days_sorted = sorted(by_day_map)
    truncated = cutoff is None and len(days_sorted) > BY_DAY_LIMIT
    if truncated:
        days_sorted = days_sorted[-BY_DAY_LIMIT:]
    by_day = [(day.strftime("%d.%m"), by_day_map[day]) for day in days_sorted]

    return Analytics(days=days, per_class=per_class, by_day=by_day, by_day_truncated=truncated)


def _most_passive(per_class: dict[int, ClassAnalytics]) -> tuple[int, int]:
    class_num = min(CLASSES, key=lambda c: (per_class[c].registered, c))
    return class_num, per_class[class_num].registered


SectionRows = list[tuple[SectionLimit, list[Enrollment]]]


def _by_class(enrollments: list[Enrollment]) -> dict[int, int]:
    counts = {c: 0 for c in CLASSES}
    for item in enrollments:
        if item.class_num in counts:
            counts[item.class_num] += 1
    return counts


def _sections_by_group_rows(sections: SectionRows) -> list[list[Any]]:
    """Секция | <группа> занято | лимит | осталось | ... | Всего записано."""
    rows: list[list[Any]] = [
        ["СЕКЦИИ — ПО ГРУППАМ"],
        ["Секция", *[f"{group} {what}" for group in GROUPS for what in ("занято", "лимит", "осталось")],
         "Всего записано"],
    ]
    totals = [0] * (len(GROUPS) * 3 + 1)
    for limit, items in sections:
        _, by_group = _count(items)
        values: list[int] = []
        for group in GROUPS:
            taken = by_group.get(group, 0)
            allowed = limit.group_limit(group)
            values += [taken, allowed, max(allowed - taken, 0)]
        values.append(len(items))
        rows.append([limit.name, *values])
        totals = [total + value for total, value in zip(totals, values, strict=True)]
    rows.append(["ИТОГО", *totals])
    rows.append([])
    return rows


def _sections_by_class_rows(sections: SectionRows) -> list[list[Any]]:
    """Секция | 8 кл | 9 кл | 10 кл | 11 кл | Всего."""
    rows: list[list[Any]] = [
        ["СЕКЦИИ — ПО КЛАССАМ"],
        ["Секция", *[f"{c} кл" for c in CLASSES], "Всего"],
    ]
    totals = {c: 0 for c in CLASSES}
    for limit, items in sections:
        counts = _by_class(items)
        rows.append([limit.name, *[counts[c] for c in CLASSES], sum(counts.values())])
        for c in CLASSES:
            totals[c] += counts[c]
    rows.append(["ИТОГО", *[totals[c] for c in CLASSES], sum(totals.values())])
    rows.append([])
    return rows


def _analytics_rows(students: list[Student], sections: SectionRows, now: datetime) -> list[list[Any]]:
    """Сетка листа «Аналитика». Размер фиксированный, поэтому одна запись полностью перекрывает старую."""
    students = [s for s in students if s.class_num in CLASSES]
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    windows: list[tuple[str, datetime | None, datetime | None]] = [
        ("Сегодня", today, None),
        ("Вчера", today - timedelta(days=1), today),
        ("7 дней", now - timedelta(days=7), None),
        ("Всё время", None, None),
    ]

    passive_class, passive_count = _most_passive(_class_counts(students, now - timedelta(days=7), None))
    rows: list[list[Any]] = [
        ["Обновлено:", now.strftime(DATE_FORMAT)],
        ["Самый пассивный класс за 7 дней:", f"{passive_class} класс ({passive_count} чел.)"],
        [],
        *_sections_by_group_rows(sections),
        *_sections_by_class_rows(sections),
    ]

    for title, start, end in windows:
        counts = _class_counts(students, start, end)
        rows.append([title])
        rows.append(["Класс", "Зарегались", "Записались", "Без секции"])
        for c in CLASSES:
            rows.append([c, counts[c].registered, counts[c].enrolled, counts[c].without_section])
        rows.append([
            "Итого",
            sum(item.registered for item in counts.values()),
            sum(item.enrolled for item in counts.values()),
            sum(item.without_section for item in counts.values()),
        ])
        rows.append([])

    by_day = _registrations_by_day(students)
    rows.append([f"По дням (последние {ANALYTICS_BY_DAY_DAYS} дней, регистрации)"])
    rows.append(["Дата", *[f"{c} кл" for c in CLASSES], "Всего"])
    for offset in range(ANALYTICS_BY_DAY_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        counts = by_day.get(day, {c: 0 for c in CLASSES})
        rows.append([day.strftime("%d.%m.%Y"), *[counts[c] for c in CLASSES], sum(counts.values())])

    # Выравниваем ширину: пустые ячейки затирают старые значения.
    return [row + [""] * (ANALYTICS_COLS - len(row)) for row in rows]


def _ensure_analytics_grid(ws: gspread.Worksheet, rows_needed: int) -> None:
    """Расширяет лист, если сетка уже, чем данные. Размеры берутся из метаданных, без чтения."""
    if ws.col_count < ANALYTICS_COLS:
        _api(ws.resize, cols=ANALYTICS_COLS)
        logger.info("Лист «%s» расширен до %s колонок", ANALYTICS_SHEET, ANALYTICS_COLS)
    if ws.row_count < rows_needed:
        _api(ws.resize, rows=max(rows_needed, ANALYTICS_ROWS))
        logger.info("Лист «%s» расширен до %s строк", ANALYTICS_SHEET, ws.row_count)


def _write_analytics_sync(students: list[Student], sections: SectionRows, now: datetime) -> None:
    """Перезаписывает лист «Аналитика» одним запросом values_update (RAW)."""
    rows = _analytics_rows(students, sections, now)
    try:
        ws = _ws(ANALYTICS_SHEET)
    except SheetsError:
        ws = _api(_spreadsheet().add_worksheet, title=ANALYTICS_SHEET,
                  rows=ANALYTICS_ROWS, cols=ANALYTICS_COLS)
        _worksheets(refresh=True)
    _ensure_analytics_grid(ws, len(rows))
    _api(
        _spreadsheet().values_update,
        f"{_quote_sheet(ANALYTICS_SHEET)}!A1",
        params={"valueInputOption": "RAW"},
        body={"values": rows},
    )


# --------------------------------------------------------------------------- #
# Отложенная (debounce) запись листа «Аналитика»
# --------------------------------------------------------------------------- #

_analytics_dirty = False


def _mark_analytics_dirty() -> None:
    """Помечает аналитику устаревшей: её перезапишет фоновый таймер, а не сам хендлер."""
    global _analytics_dirty
    _analytics_dirty = True


def analytics_pending() -> bool:
    return _analytics_dirty


async def _write_analytics() -> None:
    await _run(_write_analytics_sync, _store.students_list(), _sections_snapshot(), _now())


async def flush_analytics() -> bool:
    """Записывает «Аналитику», если с прошлой записи что-то менялось. True — если писали."""
    global _analytics_dirty
    if not _analytics_dirty:
        return False
    _analytics_dirty = False
    try:
        await _write_analytics()
    except SheetsError:
        _analytics_dirty = True  # попробуем на следующем тике
        logger.exception("Не удалось обновить лист «%s»", ANALYTICS_SHEET)
        return False
    return True


async def refresh_analytics() -> None:
    """Пересчитать и перезаписать лист «Аналитика» немедленно (старт бота, /reload)."""
    global _analytics_dirty
    await _ensure_loaded()
    _analytics_dirty = False
    try:
        await _write_analytics()
    except SheetsError:
        _analytics_dirty = True  # не потеряли изменения: допишет таймер
        raise


# --------------------------------------------------------------------------- #
# Фоновые задачи: синхронизация копии и debounce аналитики
# --------------------------------------------------------------------------- #

_tasks: list[asyncio.Task[None]] = []


async def _every(interval: float, action: Callable[[], Coroutine[Any, Any, Any]], what: str) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — фоновая задача не должна умирать
            logger.exception("Фоновая задача «%s» упала, продолжаем", what)


def start_background_tasks() -> None:
    """Запускает синхронизацию копии (5 мин) и отложенную запись аналитики (60 с)."""
    if _tasks:
        return
    _tasks.append(asyncio.create_task(
        _every(SYNC_INTERVAL_SECONDS, refresh, "синхронизация копии"),
        name="sheets-sync",
    ))
    _tasks.append(asyncio.create_task(
        _every(ANALYTICS_DEBOUNCE_SECONDS, flush_analytics, "запись аналитики"),
        name="sheets-analytics",
    ))
    logger.info(
        "Фоновая синхронизация каждые %s с, запись «%s» — не чаще раза в %s с",
        SYNC_INTERVAL_SECONDS, ANALYTICS_SHEET, ANALYTICS_DEBOUNCE_SECONDS,
    )


async def stop_background_tasks() -> None:
    """Останавливает фоновые задачи и дописывает отложенную аналитику."""
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — остановка не должна падать
            logger.exception("Фоновая задача %s завершилась с ошибкой", task.get_name())
    _tasks.clear()
    try:
        await flush_analytics()
    except SheetsError:
        logger.exception("Не удалось дописать лист «%s» при остановке", ANALYTICS_SHEET)


# --------------------------------------------------------------------------- #
# Публичный async-API
# --------------------------------------------------------------------------- #


async def ensure_section_sheets() -> list[str]:
    """Создаёт недостающие листы секций и заполняет копию. Возвращает имена созданных листов."""
    async with _lock:
        created, snapshot = await _run(_ensure_section_sheets_sync)
        _store.apply(snapshot)
        return created


async def refresh() -> None:
    """Сверяет копию в памяти с таблицей (два запроса на чтение)."""
    async with _lock:
        _store.apply(await _run(_load_snapshot_sync))
    logger.debug("Копия синхронизирована: учеников %s", len(_store.students))


async def reload() -> None:
    """/reload: заново читает метаданные листов, копию таблицы и перезаписывает аналитику."""
    async with _lock:
        await _run(_worksheets_refresh_sync)
        _store.apply(await _run(_load_snapshot_sync))
    await refresh_analytics()


def _worksheets_refresh_sync() -> None:
    _worksheets(refresh=True)


async def is_closed() -> bool:
    await _ensure_loaded()
    return _closed()


async def get_deadline() -> datetime | None:
    await _ensure_loaded()
    return _deadline()


async def get_limits() -> list[SectionLimit]:
    await _ensure_loaded()
    return list(_store.limits)


async def get_student(tg_id: int) -> Student | None:
    await _ensure_loaded()
    return _store.students.get(tg_id)


async def upsert_student(tg_id: int, name: str, class_num: int, phone: str, lang: str) -> Student:
    await _ensure_loaded()
    async with _lock:
        existing = _store.students.get(tg_id)
        if existing is None:
            registered_at = _now_str()
            fallback = _next_row([s.row for s in _store.students.values()])
            row = await _run(
                _append_student_sync, tg_id, name, class_num, phone, lang, registered_at, fallback
            )
            student = Student(
                tg_id=tg_id, name=name, class_num=class_num, phone=phone, lang=lang,
                section=None, enrolled_at=None, registered_at=registered_at, row=row,
            )
        else:
            await _run(_update_student_sync, existing.row, tg_id, name, class_num, phone, lang)
            student = replace(existing, name=name, class_num=class_num, phone=phone, lang=lang)
        _store.students[tg_id] = student
        _mark_analytics_dirty()
        return student


async def update_student_lang(tg_id: int, lang: str) -> bool:
    await _ensure_loaded()
    async with _lock:
        student = _store.students.get(tg_id)
        if student is None:
            return False
        await _run(_update_lang_sync, student.row, lang)
        _store.students[tg_id] = replace(student, lang=lang)
        return True


async def get_availability(class_num: int) -> list[Availability]:
    await _ensure_loaded()
    return _availability(class_num)


async def enroll(tg_id: int, section: str) -> EnrollResult:
    await _ensure_loaded()
    async with _lock:
        student = _store.students.get(tg_id)
        if student is None:
            return EnrollResult(EnrollStatus.NOT_REGISTERED)
        if student.section:
            return EnrollResult(EnrollStatus.ALREADY, student.section)

        limit = _store.limit(section)
        if limit is None:
            return EnrollResult(EnrollStatus.UNKNOWN_SECTION)
        group = group_of(student.class_num)
        if limit.group_limit(group) <= 0:
            return EnrollResult(EnrollStatus.FULL, section)

        # Лимит проверяется по копии под _lock: параллельные записи не обгонят друг друга.
        items = _store.section(section)
        if any(item.tg_id == tg_id for item in items):
            return EnrollResult(EnrollStatus.ALREADY, section)
        if _free_for_group(limit, items, group) <= 0:
            return EnrollResult(EnrollStatus.FULL, section)

        when = _now_str()
        row = await _run(
            _append_enrollment_sync, section, student, when, _next_row([i.row for i in items])
        )
        items.append(Enrollment(
            name=student.name, class_num=student.class_num, phone=student.phone,
            tg_id=tg_id, enrolled_at=when, row=row,
        ))
        await _run(_set_student_section_sync, student.row, section, when)
        _store.students[tg_id] = replace(student, section=section, enrolled_at=when)
        _mark_analytics_dirty()
        return EnrollResult(EnrollStatus.OK, section)


async def cancel_enrollment(tg_id: int) -> bool:
    await _ensure_loaded()
    async with _lock:
        student = _store.students.get(tg_id)
        if student is None or not student.section:
            return False
        section = student.section
        removed = sorted((i.row for i in _store.section(section) if i.tg_id == tg_id), reverse=True)
        await _run(_cancel_sync, section, removed, student.row)
        _drop_enrollment_rows(section, tg_id, removed)
        _store.students[tg_id] = replace(student, section=None, enrolled_at=None)
        _mark_analytics_dirty()
        return True


async def get_stats() -> Stats:
    await _ensure_loaded()
    return _collect().stats


async def get_export_data() -> ExportData:
    await _ensure_loaded()
    return _collect()


async def get_analytics(days: int | None = None) -> Analytics:
    await _ensure_loaded()
    return _analytics(days)


async def get_students_without_section(class_num: int | None = None) -> list[Student]:
    await _ensure_loaded()
    return [
        s for s in _store.students_list()
        if not s.section and (class_num is None or s.class_num == class_num)
    ]
