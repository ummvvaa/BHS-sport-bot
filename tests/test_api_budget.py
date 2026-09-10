"""Сквозной тест: сколько запросов к Sheets API стоит /start и полная регистрация.

Хендлеры прогоняются по-настоящему — через Dispatcher aiogram с поддельной
сессией Telegram, — поэтому счётчик отражает реальное поведение бота.
"""
from __future__ import annotations

import sheets
from handlers import admin_router, student_router
from tests.conftest import run
from tests.fake_telegram import FakeTelegram

# Бюджет из задачи: один /start и полная регистрация — не больше 3 запросов, и только записи.
BUDGET = 3


async def register(tg: FakeTelegram, name: str = "Аня Петрова", class_num: str = "8",
                   phone: str = "+77001234567") -> None:
    """/start → язык → имя → класс → телефон → «Всё верно» → секция → «Записаться»."""
    await tg.send("/start")
    await tg.press("setlang:ru")
    await tg.send(name)
    await tg.send(class_num)
    await tg.send_contact(phone)
    await tg.press("data:ok")
    await tg.press("sec:0")
    await tg.press("enroll:yes")


def full_registration(tg: FakeTelegram) -> None:
    run(register(tg))


def test_start_and_full_registration_fit_the_budget(sheet):
    run(sheets.ensure_section_sheets())
    run(sheets.refresh_analytics())
    tg = FakeTelegram(admin_router, student_router)
    sheet.reset_count()

    full_registration(tg)

    reads, writes = sheet.reads(), sheet.writes()
    print(f"\n/start + регистрация: запросов {sheet.count} (чтений {len(reads)}, записей {len(writes)})")
    for request in sheet.requests:
        print(f"  · {request}")

    assert reads == [], "во время работы бот не читает таблицу"
    assert len(writes) <= BUDGET
    assert sheet.count == 3  # строка ученика + строка секции + отметка секции у ученика

    # Данные действительно доехали до таблицы и до копии в памяти.
    student = run(sheets.get_student(555001))
    assert student is not None and student.section == "Футбол"
    assert sheet.rows("Футбол")[1][:4] == ["Аня Петрова", "8", "+77001234567", "555001"]


def test_analytics_is_written_once_by_the_timer(sheet):
    run(sheets.ensure_section_sheets())
    run(sheets.refresh_analytics())
    tg = FakeTelegram(admin_router, student_router)
    sheet.reset_count()

    full_registration(tg)
    assert sheets.analytics_pending() is True
    run(sheets.flush_analytics())

    analytics = [r for r in sheet.requests if r.startswith("values.update 'Аналитика'")]
    assert len(analytics) == 1, "аналитика пишется таймером один раз, а не после каждого действия"
    assert sheet.count == BUDGET + 1


def test_second_student_costs_the_same(sheet):
    run(sheets.ensure_section_sheets())
    full_registration(FakeTelegram(admin_router, student_router))
    sheet.reset_count()

    other = FakeTelegram(admin_router, student_router, user_id=555002)
    run(register(other, name="Бек", class_num="10", phone="+77009999999"))

    assert sheet.reads() == []
    assert sheet.count == BUDGET
    assert run(sheets.get_student(555002)).section == "Футбол"
