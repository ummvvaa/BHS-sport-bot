"""Админские команды: /export, /stats, /reload. Доступны только ADMIN_IDS."""
from __future__ import annotations

import io
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

import sheets
from config import settings
from sheets import CLASSES, DATE_FORMAT, TZ, Enrollment, ExportData, SectionLimit, Stats
from texts import t

logger = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.from_user.id.in_(settings.admin_ids))

LANG = "ru"
SUMMARY_SHEET = "Сводка"
FORBIDDEN_SHEET_CHARS = set('[]:*?/\\')


def _safe_sheet_title(title: str, used: set[str]) -> str:
    cleaned = "".join("_" if ch in FORBIDDEN_SHEET_CHARS else ch for ch in title).strip() or "Секция"
    cleaned = cleaned[:31]
    candidate = cleaned
    counter = 2
    while candidate in used:
        suffix = f" ({counter})"
        candidate = cleaned[: 31 - len(suffix)] + suffix
        counter += 1
    used.add(candidate)
    return candidate


def _write_table(ws: Worksheet, headers: list[str], rows: list[list[object]]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(row)
    for col_index, header in enumerate(headers, start=1):
        width = len(str(header))
        for row in rows:
            value = row[col_index - 1] if col_index - 1 < len(row) else ""
            width = max(width, len(str(value)))
        ws.column_dimensions[get_column_letter(col_index)].width = min(width + 2, 50)


def _section_rows(items: list[Enrollment]) -> list[list[object]]:
    return [
        [index, item.name, item.class_num, item.phone, item.enrolled_at]
        for index, item in enumerate(items, start=1)
    ]


def _summary_rows(stats: Stats) -> list[list[object]]:
    rows: list[list[object]] = []
    for section in stats.sections:
        row: list[object] = [section.name, section.count_total, section.limit.total]
        for class_num in CLASSES:
            row.append(f"{section.count_by_class.get(class_num, 0)}/{section.limit.per_class.get(class_num, 0)}")
        rows.append(row)
    return rows


def build_xlsx(data: ExportData) -> bytes:
    wb = Workbook()
    summary = wb.active
    summary.title = SUMMARY_SHEET
    _write_table(
        summary,
        ["Секция", "Записано", "Общий лимит", *[str(c) for c in CLASSES]],
        _summary_rows(data.stats),
    )

    used: set[str] = {SUMMARY_SHEET}
    limit: SectionLimit
    for limit, items in data.sections:
        ws = wb.create_sheet(title=_safe_sheet_title(limit.name, used))
        _write_table(ws, ["№", "Имя", "Класс", "Телефон", "Дата записи"], _section_rows(items))

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    data = await sheets.get_export_data()
    if not data.sections:
        await message.answer(t(LANG, "admin_no_sections"))
        return
    now = datetime.now(TZ)
    content = build_xlsx(data)
    filename = f"sections_{now.strftime('%Y-%m-%d_%H-%M')}.xlsx"
    await message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption=t(LANG, "export_caption", when=now.strftime(DATE_FORMAT)),
    )
    logger.info("Админ %s выгрузил %s", message.from_user.id, filename)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    stats = await sheets.get_stats()
    lines: list[str] = []
    for section in stats.sections:
        by_class = ", ".join(
            t(
                LANG,
                "stats_class",
                class_num=class_num,
                count=section.count_by_class.get(class_num, 0),
                limit=section.limit.per_class.get(class_num, 0),
            )
            for class_num in CLASSES
        )
        lines.append(
            t(LANG, "stats_line", section=section.name, count=section.count_total, limit=section.limit.total, by_class=by_class)
        )
    if not lines:
        lines.append(t(LANG, "admin_no_sections"))
    lines.append("")
    lines.append(t(LANG, "stats_total", registered=stats.registered, enrolled=stats.enrolled))
    if stats.deadline is None:
        lines.append(t(LANG, "stats_no_deadline"))
    else:
        lines.append(t(LANG, "stats_deadline", deadline=stats.deadline.strftime(DATE_FORMAT)))
    lines.append(t(LANG, "stats_closed") if stats.closed else t(LANG, "stats_open"))
    await message.answer("\n".join(lines))


@router.message(Command("reload"))
async def cmd_reload(message: Message) -> None:
    sheets.reload_cache()
    logger.info("Админ %s сбросил кэш", message.from_user.id)
    await message.answer(t(LANG, "reload_done"))
