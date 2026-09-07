"""Слой работы с Google Sheets. Единственное место в проекте, где используется gspread.

gspread синхронный, поэтому все вызовы обёрнуты в asyncio.to_thread.
Операции записи/отмены сериализуются через asyncio.Lock и повторно проверяют
лимиты непосредственно перед изменением данных.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption

from config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

TZ = ZoneInfo("Asia/Almaty")
CLASSES: tuple[int, ...] = (8, 9, 10, 11)
GROUPS: tuple[str, ...] = ("8-9", "10-11")
CLASS_GROUP: dict[int, str] = {8: "8-9", 9: "8-9", 10: "10-11", 11: "10-11"}
DATE_FORMAT = "%d.%m.%Y %H:%M"
CACHE_TTL_SECONDS = 60

SETTINGS_SHEET = "Настройки"
LIMITS_SHEET = "Лимиты"
STUDENTS_SHEET = "Ученики"
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
# Внутреннее состояние модуля: блокировка и кэш
# --------------------------------------------------------------------------- #

_lock = asyncio.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, loader: Callable[[], T]) -> T:
    entry = _cache.get(key)
    now = time.monotonic()
    if entry is not None and now - entry[0] < CACHE_TTL_SECONDS:
        return entry[1]
    value = loader()
    _cache[key] = (now, value)
    return value


def _invalidate(*keys: str) -> None:
    if keys:
        for key in keys:
            _cache.pop(key, None)
    else:
        _cache.clear()


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
    text = str(value).strip().replace(" ", "").replace(" ", "")
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


def _worksheets() -> dict[str, gspread.Worksheet]:
    return _cached("worksheets", lambda: {ws.title: ws for ws in _spreadsheet().worksheets()})


def _ws(title: str) -> gspread.Worksheet:
    sheets = _worksheets()
    ws = sheets.get(title)
    if ws is None:
        _invalidate("worksheets")
        ws = _worksheets().get(title)
    if ws is None:
        raise SheetsError(f"Лист «{title}» не найден в таблице.")
    return ws


# --------------------------------------------------------------------------- #
# Настройки и лимиты (кэшируются)
# --------------------------------------------------------------------------- #


def _load_settings() -> dict[str, str]:
    rows = _ws(SETTINGS_SHEET).get_all_values()
    result: dict[str, str] = {}
    for row in rows:
        key = _cell(row, 0)
        if key:
            result[key.lower()] = _cell(row, 1)
    return result


def _get_settings() -> dict[str, str]:
    return _cached("settings", _load_settings)


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


def _get_deadline() -> datetime | None:
    return _parse_deadline(_get_settings().get("deadline", ""))


def _is_closed() -> bool:
    deadline = _get_deadline()
    return deadline is not None and _now() >= deadline


def _load_limits() -> list[SectionLimit]:
    """Читает лист «Лимиты»: Секция | 8-9 | 10-11. Секции без лимитов в обеих группах отбрасываются."""
    rows = _ws(LIMITS_SHEET).get_all_values()
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


def _get_limits() -> list[SectionLimit]:
    return _cached("limits", _load_limits)


# --------------------------------------------------------------------------- #
# Лист «Ученики»
# --------------------------------------------------------------------------- #


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


def _all_students() -> list[Student]:
    rows = _ws(STUDENTS_SHEET).get_all_values()
    students: list[Student] = []
    for index, row in enumerate(rows[1:], start=2):
        student = _parse_student(row, index)
        if student is not None:
            students.append(student)
    return students


def _find_student(tg_id: int) -> Student | None:
    for student in _all_students():
        if student.tg_id == tg_id:
            return student
    return None


def _upsert_student_sync(tg_id: int, name: str, class_num: int, phone: str, lang: str) -> Student:
    ws = _ws(STUDENTS_SHEET)
    existing = _find_student(tg_id)
    if existing is None:
        ws.append_row(
            [str(tg_id), name, str(class_num), phone, lang, "", "", _now_str()],
            value_input_option=ValueInputOption.raw,
        )
        student = _find_student(tg_id)
        if student is None:
            raise SheetsError("Не удалось прочитать только что добавленную строку ученика.")
        return student

    # При перерегистрации обновляются только A:E; секция, даты записи и регистрации сохраняются.
    ws.update(
        range_name=f"A{existing.row}:E{existing.row}",
        values=[[str(tg_id), name, str(class_num), phone, lang]],
        value_input_option=ValueInputOption.raw,
    )
    return Student(
        tg_id=tg_id,
        name=name,
        class_num=class_num,
        phone=phone,
        lang=lang,
        section=existing.section,
        enrolled_at=existing.enrolled_at,
        registered_at=existing.registered_at,
        row=existing.row,
    )


def _update_lang_sync(tg_id: int, lang: str) -> bool:
    student = _find_student(tg_id)
    if student is None:
        return False
    _ws(STUDENTS_SHEET).update(
        range_name=f"E{student.row}",
        values=[[lang]],
        value_input_option=ValueInputOption.raw,
    )
    return True


# --------------------------------------------------------------------------- #
# Листы секций
# --------------------------------------------------------------------------- #


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


def _section_enrollments(limits: list[SectionLimit]) -> dict[str, list[Enrollment]]:
    """Читает записи всех секций одним batch-запросом."""
    if not limits:
        return {}
    ranges = [f"{_quote_sheet(limit.name)}!A2:E" for limit in limits]
    response = _spreadsheet().values_batch_get(ranges)
    value_ranges = response.get("valueRanges", [])
    result: dict[str, list[Enrollment]] = {}
    for limit, value_range in zip(limits, value_ranges, strict=False):
        rows = value_range.get("values", [])
        enrollments: list[Enrollment] = []
        for index, row in enumerate(rows, start=2):
            enrollment = _parse_enrollment(row, index)
            if enrollment is not None:
                enrollments.append(enrollment)
        result[limit.name] = enrollments
    for limit in limits:
        result.setdefault(limit.name, [])
    return result


def _single_section_enrollments(section: str) -> list[Enrollment]:
    rows = _ws(section).get_all_values()
    enrollments: list[Enrollment] = []
    for index, row in enumerate(rows[1:], start=2):
        enrollment = _parse_enrollment(row, index)
        if enrollment is not None:
            enrollments.append(enrollment)
    return enrollments


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


def _availability_sync(class_num: int) -> list[Availability]:
    """Секции для группы ученика. Секции с пустым/нулевым лимитом группы не попадают в список."""
    group = group_of(class_num)
    if group is None:
        return []
    limits = [limit for limit in _get_limits() if limit.group_limit(group) > 0]
    enrollments = _section_enrollments(limits)
    result: list[Availability] = []
    for limit in limits:
        items = enrollments.get(limit.name, [])
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


def _ensure_section_sheets_sync() -> list[str]:
    sh = _spreadsheet()
    _invalidate("worksheets", "limits")
    existing = _worksheets()
    for required in (SETTINGS_SHEET, LIMITS_SHEET, STUDENTS_SHEET):
        if required not in existing:
            raise SheetsError(
                f"В таблице нет обязательного листа «{required}». Создай его вручную (см. README)."
            )

    students_ws = existing[STUDENTS_SHEET]
    header = [cell.strip() for cell in students_ws.row_values(1)]
    if not header or len(header) < len(STUDENTS_HEADERS):
        # Пустой лист или старая схема без «Дата регистрации» — дописываем заголовки.
        students_ws.update(
            range_name="A1",
            values=[STUDENTS_HEADERS],
            value_input_option=ValueInputOption.raw,
        )

    created: list[str] = []
    for limit in _get_limits():
        if limit.name in existing:
            continue
        ws = sh.add_worksheet(title=limit.name, rows=200, cols=len(SECTION_HEADERS))
        ws.update(
            range_name="A1",
            values=[SECTION_HEADERS],
            value_input_option=ValueInputOption.raw,
        )
        created.append(limit.name)
    if created:
        _invalidate("worksheets")
    return created


# --------------------------------------------------------------------------- #
# Запись и отмена (вызываются только под _lock)
# --------------------------------------------------------------------------- #


def _enroll_sync(tg_id: int, section: str) -> EnrollResult:
    student = _find_student(tg_id)
    if student is None:
        return EnrollResult(EnrollStatus.NOT_REGISTERED)
    if student.section:
        return EnrollResult(EnrollStatus.ALREADY, student.section)

    limit = next((item for item in _get_limits() if item.name == section), None)
    if limit is None:
        return EnrollResult(EnrollStatus.UNKNOWN_SECTION)
    group = group_of(student.class_num)
    if limit.group_limit(group) <= 0:
        return EnrollResult(EnrollStatus.FULL, section)

    # Повторная проверка лимита группы по свежим данным непосредственно перед записью.
    enrollments = _single_section_enrollments(section)
    if any(item.tg_id == tg_id for item in enrollments):
        return EnrollResult(EnrollStatus.ALREADY, section)
    if _free_for_group(limit, enrollments, group) <= 0:
        return EnrollResult(EnrollStatus.FULL, section)

    when = _now_str()
    _ws(section).append_row(
        [student.name, str(student.class_num), student.phone, str(tg_id), when],
        value_input_option=ValueInputOption.raw,
    )
    _ws(STUDENTS_SHEET).update(
        range_name=f"F{student.row}:G{student.row}",
        values=[[section, when]],
        value_input_option=ValueInputOption.raw,
    )
    return EnrollResult(EnrollStatus.OK, section)


def _cancel_sync(tg_id: int) -> bool:
    student = _find_student(tg_id)
    if student is None or not student.section:
        return False

    section = student.section
    try:
        ws = _ws(section)
    except SheetsError:
        ws = None
    if ws is not None:
        # Удаляем снизу вверх, чтобы индексы не сдвигались.
        rows = [item.row for item in _single_section_enrollments(section) if item.tg_id == tg_id]
        for row in sorted(rows, reverse=True):
            ws.delete_rows(row)

    _ws(STUDENTS_SHEET).update(
        range_name=f"F{student.row}:G{student.row}",
        values=[["", ""]],
        value_input_option=ValueInputOption.raw,
    )
    return True


# --------------------------------------------------------------------------- #
# Статистика, экспорт, аналитика
# --------------------------------------------------------------------------- #


def _collect_sync() -> ExportData:
    limits = _get_limits()
    enrollments = _section_enrollments(limits)
    students = _all_students()

    section_stats: list[SectionStats] = []
    sections: list[tuple[SectionLimit, list[Enrollment]]] = []
    for limit in limits:
        items = enrollments.get(limit.name, [])
        total, by_group = _count(items)
        section_stats.append(SectionStats(limit=limit, count_total=total, count_by_group=by_group))
        sections.append((limit, items))

    stats = Stats(
        sections=section_stats,
        registered=len(students),
        enrolled=sum(1 for s in students if s.section),
        deadline=_get_deadline(),
        closed=_is_closed(),
    )
    return ExportData(sections=sections, stats=stats)


BY_DAY_LIMIT = 30


def _analytics_sync(days: int | None) -> Analytics:
    """Статистика по каждому классу за всё время или за последние `days` дней (по дате регистрации)."""
    students = [s for s in _all_students() if s.class_num in CLASSES]
    cutoff = _now() - timedelta(days=days) if days else None

    if cutoff is not None:
        # Строки без даты регистрации считаются «давними» и в период не попадают.
        students = [
            s for s in students
            if (registered := _parse_date(s.registered_at)) is not None and registered >= cutoff
        ]

    per_class = {
        c: ClassAnalytics(
            registered=sum(1 for s in students if s.class_num == c),
            enrolled=sum(1 for s in students if s.class_num == c and s.section),
        )
        for c in CLASSES
    }

    by_day_map: dict[datetime, dict[int, int]] = {}
    for student in students:
        registered = _parse_date(student.registered_at)
        if registered is None:
            continue
        day = registered.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day_map.setdefault(day, {c: 0 for c in CLASSES})[student.class_num] += 1
    days_sorted = sorted(by_day_map)
    truncated = cutoff is None and len(days_sorted) > BY_DAY_LIMIT
    if truncated:
        days_sorted = days_sorted[-BY_DAY_LIMIT:]
    by_day = [(day.strftime("%d.%m"), by_day_map[day]) for day in days_sorted]

    return Analytics(days=days, per_class=per_class, by_day=by_day, by_day_truncated=truncated)


def _students_without_section_sync(class_num: int | None) -> list[Student]:
    return [
        s for s in _all_students()
        if not s.section and (class_num is None or s.class_num == class_num)
    ]


# --------------------------------------------------------------------------- #
# Публичный async-API
# --------------------------------------------------------------------------- #


async def ensure_section_sheets() -> list[str]:
    """Создаёт недостающие листы секций. Возвращает имена созданных листов."""
    async with _lock:
        return await _run(_ensure_section_sheets_sync)


async def is_closed() -> bool:
    return await _run(_is_closed)


async def get_deadline() -> datetime | None:
    return await _run(_get_deadline)


async def get_limits() -> list[SectionLimit]:
    return await _run(_get_limits)


async def get_student(tg_id: int) -> Student | None:
    return await _run(_find_student, tg_id)


async def upsert_student(tg_id: int, name: str, class_num: int, phone: str, lang: str) -> Student:
    async with _lock:
        return await _run(_upsert_student_sync, tg_id, name, class_num, phone, lang)


async def update_student_lang(tg_id: int, lang: str) -> bool:
    async with _lock:
        return await _run(_update_lang_sync, tg_id, lang)


async def get_availability(class_num: int) -> list[Availability]:
    return await _run(_availability_sync, class_num)


async def enroll(tg_id: int, section: str) -> EnrollResult:
    async with _lock:
        return await _run(_enroll_sync, tg_id, section)


async def cancel_enrollment(tg_id: int) -> bool:
    async with _lock:
        return await _run(_cancel_sync, tg_id)


async def get_stats() -> Stats:
    return (await _run(_collect_sync)).stats


async def get_export_data() -> ExportData:
    return await _run(_collect_sync)


async def get_analytics(days: int | None = None) -> Analytics:
    return await _run(_analytics_sync, days)


async def get_students_without_section(class_num: int | None = None) -> list[Student]:
    return await _run(_students_without_section_sync, class_num)


def reload_cache() -> None:
    """Сбрасывает кэш лимитов, настроек и списка листов."""
    _invalidate()
