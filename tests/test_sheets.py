"""Тесты слоя sheets: копия таблицы в памяти, лимиты, ретраи, debounce аналитики."""
from __future__ import annotations

import asyncio

import pytest

import sheets
from tests.conftest import install, make_spreadsheet, run
from tests.fake_sheets import FakeResponse, rate_limit_error
from gspread.exceptions import APIError


def boot(spreadsheet) -> None:
    run(sheets.ensure_section_sheets())
    spreadsheet.reset_count()


# --------------------------------------------------------------------------- #
# Загрузка копии
# --------------------------------------------------------------------------- #


def test_startup_reads_whole_table_in_three_requests(sheet):
    created = run(sheets.ensure_section_sheets())

    assert created == []
    assert sheet.requests == ["spreadsheets.get", "values.batchGet 3", "values.batchGet 2"]
    assert [limit.name for limit in sheets._store.limits] == ["Футбол", "Шахматы"]
    assert sheets._store.enrollments == {"Футбол": [], "Шахматы": []}


def test_existing_rows_land_in_memory(sheet):
    sheet.sheet(sheets.STUDENTS_SHEET).values += [
        ["1001", "Аня", "8", "+7700", "ru", "Футбол", "01.09.2025 10:00", "01.09.2025 09:00"],
        ["1002", "Бек", "10", "+7701", "kk", "", "", "01.09.2025 09:30"],
    ]
    sheet.sheet("Футбол").values.append(["Аня", "8", "+7700", "1001", "01.09.2025 10:00"])
    boot(sheet)

    anya = run(sheets.get_student(1001))
    assert anya is not None and anya.section == "Футбол" and anya.row == 2
    assert run(sheets.get_student(1002)).section is None
    assert [e.tg_id for e in sheets._store.section("Футбол")] == [1001]
    assert sheet.count == 0, "чтение ученика не должно ходить в API"


def test_missing_section_sheet_is_created(sheet):
    del sheet._sheets["Шахматы"]

    created = run(sheets.ensure_section_sheets())

    assert created == ["Шахматы"]
    assert sheet.rows("Шахматы")[0] == sheets.SECTION_HEADERS


def test_required_sheet_missing_raises(sheet):
    del sheet._sheets[sheets.LIMITS_SHEET]
    with pytest.raises(sheets.SheetsError, match="Лимиты"):
        run(sheets.ensure_section_sheets())


# --------------------------------------------------------------------------- #
# Чтения обслуживаются копией
# --------------------------------------------------------------------------- #


def test_all_reads_are_served_from_memory(sheet):
    boot(sheet)

    async def scenario():
        await sheets.get_student(1001)
        await sheets.is_closed()
        await sheets.get_deadline()
        await sheets.get_limits()
        await sheets.get_availability(8)
        await sheets.get_stats()
        await sheets.get_export_data()
        await sheets.get_analytics(7)
        await sheets.get_students_without_section(None)

    run(scenario())
    assert sheet.requests == []


def test_stats_and_analytics_use_copy(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(1, "Аня", 8, "+7700", "ru")
        await sheets.upsert_student(2, "Бек", 10, "+7701", "ru")
        await sheets.enroll(1, "Футбол")
        return await sheets.get_stats(), await sheets.get_analytics(None)

    sheet.reset_count()
    stats, analytics = run(scenario())

    assert stats.registered == 2
    assert stats.enrolled == 1
    assert analytics.per_class[8].registered == 1
    assert analytics.per_class[8].enrolled == 1
    assert analytics.per_class[10].without_section == 1
    assert sheet.reads() == [], "статистика не должна читать таблицу"


# --------------------------------------------------------------------------- #
# Запись: таблица и копия меняются вместе
# --------------------------------------------------------------------------- #


def test_upsert_writes_row_and_updates_copy(sheet):
    boot(sheet)

    student = run(sheets.upsert_student(555, "Аня", 8, "+7700", "ru"))

    assert student.row == 2
    assert sheet.rows(sheets.STUDENTS_SHEET)[1][:5] == ["555", "Аня", "8", "+7700", "ru"]
    assert sheets._store.students[555] == student
    assert sheet.reads() == [], "регистрация не перечитывает лист «Ученики»"
    assert len(sheet.writes()) == 1


def test_reregistration_updates_same_row_and_keeps_section(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(555, "Аня", 8, "+7700", "ru")
        await sheets.enroll(555, "Футбол")
        return await sheets.upsert_student(555, "Анна", 9, "+7702", "kk")

    student = run(scenario())

    assert student.row == 2
    assert (student.name, student.class_num, student.section) == ("Анна", 9, "Футбол")
    assert sheet.rows(sheets.STUDENTS_SHEET)[1][:6] == ["555", "Анна", "9", "+7702", "kk", "Футбол"]
    assert len(sheet.rows(sheets.STUDENTS_SHEET)) == 2, "вторая строка для того же tg_id не создаётся"


def test_enroll_writes_two_rows_and_updates_copy(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(555, "Аня", 8, "+7700", "ru")
        sheet.reset_count()
        return await sheets.enroll(555, "Футбол")

    result = run(scenario())

    assert result.status is sheets.EnrollStatus.OK
    assert len(sheet.writes()) == 2 and sheet.reads() == []
    assert sheet.rows("Футбол")[1][:4] == ["Аня", "8", "+7700", "555"]
    assert sheets._store.students[555].section == "Футбол"
    assert [e.row for e in sheets._store.section("Футбол")] == [2]


def test_group_limit_is_enforced_from_copy(sheet):
    boot(sheet)

    async def scenario():
        for tg_id, name in ((1, "А"), (2, "Б"), (3, "В")):
            await sheets.upsert_student(tg_id, name, 8, "+7700", "ru")
        first = await sheets.enroll(1, "Футбол")   # лимит 8-9 = 2
        second = await sheets.enroll(2, "Футбол")
        sheet.reset_count()
        third = await sheets.enroll(3, "Футбол")
        return first, second, third

    first, second, third = run(scenario())

    assert first.status is second.status is sheets.EnrollStatus.OK
    assert third.status is sheets.EnrollStatus.FULL
    assert sheet.requests == [], "отказ по лимиту не стоит ни одного запроса"


def test_other_group_has_its_own_limit(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(1, "А", 8, "+7700", "ru")
        await sheets.upsert_student(2, "Б", 9, "+7700", "ru")
        await sheets.upsert_student(3, "В", 10, "+7700", "ru")
        await sheets.enroll(1, "Футбол")
        await sheets.enroll(2, "Футбол")
        return await sheets.enroll(3, "Футбол"), await sheets.get_availability(10)

    result, availability = run(scenario())

    assert result.status is sheets.EnrollStatus.OK  # у 10-11 свой лимит
    assert [(a.name, a.free_for_group) for a in availability] == [("Футбол", 0)]


def test_enroll_twice_and_unknown_section(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(1, "А", 8, "+7700", "ru")
        await sheets.enroll(1, "Футбол")
        return await sheets.enroll(1, "Шахматы"), await sheets.enroll(999, "Футбол")

    already, missing = run(scenario())

    assert already.status is sheets.EnrollStatus.ALREADY and already.section == "Футбол"
    assert missing.status is sheets.EnrollStatus.NOT_REGISTERED
    assert run(sheets.enroll(1, "Гребля")).status is sheets.EnrollStatus.ALREADY


def test_cancel_deletes_row_and_shifts_rows_in_copy(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(1, "А", 8, "+7700", "ru")
        await sheets.upsert_student(2, "Б", 9, "+7701", "ru")
        await sheets.enroll(1, "Футбол")
        await sheets.enroll(2, "Футбол")
        sheet.reset_count()
        return await sheets.cancel_enrollment(1)

    assert run(scenario()) is True
    assert sheet.reads() == [] and len(sheet.writes()) == 2
    # На листе осталась одна запись, и в копии её номер строки съехал вслед за таблицей.
    assert [row[3] for row in sheet.rows("Футбол")[1:]] == ["2"]
    assert [(e.tg_id, e.row) for e in sheets._store.section("Футбол")] == [(2, 2)]
    assert sheets._store.students[1].section is None
    assert sheet.rows(sheets.STUDENTS_SHEET)[1][5:7] == ["", ""]
    # Освободившееся место снова доступно.
    assert run(sheets.get_availability(8))[0].free_for_group == 1


def test_cancel_without_enrollment(sheet):
    boot(sheet)
    run(sheets.upsert_student(1, "А", 8, "+7700", "ru"))
    sheet.reset_count()

    assert run(sheets.cancel_enrollment(1)) is False
    assert run(sheets.cancel_enrollment(999)) is False
    assert sheet.requests == []


def test_update_lang(sheet):
    boot(sheet)
    run(sheets.upsert_student(1, "А", 8, "+7700", "ru"))
    sheet.reset_count()

    assert run(sheets.update_student_lang(1, "kk")) is True
    assert run(sheets.get_student(1)).lang == "kk"
    assert sheet.rows(sheets.STUDENTS_SHEET)[1][4] == "kk"
    assert len(sheet.writes()) == 1


# --------------------------------------------------------------------------- #
# Фоновая синхронизация с таблицей
# --------------------------------------------------------------------------- #


def test_refresh_picks_up_manual_edits(sheet):
    boot(sheet)
    run(sheets.upsert_student(1, "А", 8, "+7700", "ru"))

    # Админ правит таблицу руками: новый ученик, новый дедлайн, запись в секцию.
    sheet.sheet(sheets.STUDENTS_SHEET).values.append(
        ["777", "Гость", "11", "+7999", "ru", "", "", "01.09.2025 09:00"]
    )
    sheet.sheet(sheets.SETTINGS_SHEET).values[1] = ["deadline", "2000-01-01 00:00"]
    sheet.sheet("Шахматы").values.append(["Гость", "11", "+7999", "777", "02.09.2025 10:00"])
    sheet.reset_count()

    run(sheets.refresh())

    assert len(sheet.reads()) == 2, "полная синхронизация — ровно два запроса"
    assert run(sheets.get_student(777)).name == "Гость"
    assert run(sheets.get_student(1)).name == "А", "локальные записи не потерялись"
    assert run(sheets.is_closed()) is True
    assert [e.tg_id for e in sheets._store.section("Шахматы")] == [777]


def test_reload_rereads_metadata_and_writes_analytics(sheet):
    boot(sheet)

    run(sheets.reload())

    assert sheet.requests.count("spreadsheets.get") == 1
    assert len(sheet.reads()) == 3  # метаданные + два batch-чтения
    assert any(r.startswith("values.update 'Аналитика'") for r in sheet.writes())


# --------------------------------------------------------------------------- #
# Ретраи при 429
# --------------------------------------------------------------------------- #


def test_retries_on_429_then_succeeds(sheet):
    boot(sheet)
    sheet.fail_next = [rate_limit_error(), rate_limit_error(), None]

    student = run(sheets.upsert_student(1, "А", 8, "+7700", "ru"))

    assert student.tg_id == 1
    assert len(sheet.requests) == 3, "две неудачные попытки и одна удачная"
    assert sheet.rows(sheets.STUDENTS_SHEET)[1][0] == "1"


def test_gives_up_after_three_retries(sheet):
    boot(sheet)
    sheet.fail_next = [rate_limit_error()] * 4

    with pytest.raises(sheets.SheetsError, match="429"):
        run(sheets.upsert_student(1, "А", 8, "+7700", "ru"))

    assert len(sheet.requests) == len(sheets.RETRY_DELAYS) + 1 == 4
    assert 1 not in sheets._store.students, "копия не меняется, если запись не удалась"


def test_other_api_errors_are_not_retried(sheet):
    boot(sheet)
    sheet.fail_next = [APIError(FakeResponse(403, "The caller does not have permission"))]

    with pytest.raises(sheets.SheetsError, match="403"):
        run(sheets.upsert_student(1, "А", 8, "+7700", "ru"))

    assert len(sheet.requests) == 1


def test_retry_delays_follow_2_4_8(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(sheets.time, "sleep", slept.append)
    calls = {"n": 0}

    def always_429():
        calls["n"] += 1
        raise rate_limit_error()

    with pytest.raises(sheets.SheetsError):
        sheets._api(always_429)

    assert slept == [2.0, 4.0, 8.0]
    assert calls["n"] == 4


# --------------------------------------------------------------------------- #
# Отложенная запись листа «Аналитика»
# --------------------------------------------------------------------------- #


def analytics_writes(spreadsheet) -> list[str]:
    return [r for r in spreadsheet.requests if r.startswith("values.update 'Аналитика'")]


def test_actions_do_not_write_analytics(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(1, "А", 8, "+7700", "ru")
        await sheets.enroll(1, "Футбол")
        await sheets.cancel_enrollment(1)

    run(scenario())

    assert analytics_writes(sheet) == []
    assert sheets.analytics_pending() is True


def test_flush_writes_once_then_stays_quiet(sheet):
    boot(sheet)

    async def scenario():
        await sheets.upsert_student(1, "А", 8, "+7700", "ru")
        await sheets.enroll(1, "Футбол")
        first = await sheets.flush_analytics()
        second = await sheets.flush_analytics()
        return first, second

    first, second = run(scenario())

    assert (first, second) == (True, False)
    assert len(analytics_writes(sheet)) == 1, "три изменения — одна запись листа"
    assert sheets.analytics_pending() is False
    grid = sheet.rows(sheets.ANALYTICS_SHEET)
    assert grid[0][0] == "Обновлено:"
    assert any(row and row[0] == "Всё время" for row in grid)


def test_flush_retries_next_tick_after_failure(sheet):
    boot(sheet)
    run(sheets.upsert_student(1, "А", 8, "+7700", "ru"))
    sheet.fail_next = [APIError(FakeResponse(500, "backend error"))]

    assert run(sheets.flush_analytics()) is False
    assert sheets.analytics_pending() is True, "изменения не потерялись"
    assert run(sheets.flush_analytics()) is True


def test_refresh_analytics_writes_immediately(sheet):
    boot(sheet)

    run(sheets.refresh_analytics())

    assert len(analytics_writes(sheet)) == 1
    assert sheets.analytics_pending() is False


# --------------------------------------------------------------------------- #
# Фоновые задачи
# --------------------------------------------------------------------------- #


def test_background_tasks_sync_copy_and_write_analytics(sheet, monkeypatch):
    boot(sheet)
    monkeypatch.setattr(sheets, "SYNC_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(sheets, "ANALYTICS_DEBOUNCE_SECONDS", 0.01)

    async def scenario():
        sheets.start_background_tasks()
        await sheets.upsert_student(1, "А", 8, "+7700", "ru")  # помечает аналитику устаревшей
        sheet.sheet(sheets.STUDENTS_SHEET).values.append(       # админ правит таблицу руками
            ["777", "Гость", "11", "+7999", "ru", "", "", "01.09.2025 09:00"]
        )
        await asyncio.sleep(0.1)
        await sheets.stop_background_tasks()

    run(scenario())

    assert sheets._store.students[777].name == "Гость", "синхронизация подхватила ручную правку"
    assert analytics_writes(sheet), "таймер перезаписал лист «Аналитика»"
    assert sheets._tasks == []


def test_stop_flushes_pending_analytics(sheet):
    boot(sheet)

    async def scenario():
        sheets.start_background_tasks()          # интервалы штатные, таймер не успеет сработать
        await sheets.upsert_student(1, "А", 8, "+7700", "ru")
        await sheets.stop_background_tasks()

    run(scenario())

    assert len(analytics_writes(sheet)) == 1, "при остановке отложенная аналитика дописывается"
    assert sheets.analytics_pending() is False


# --------------------------------------------------------------------------- #
# Блоки по секциям на листе «Аналитика»
# --------------------------------------------------------------------------- #

SECTION_LIMITS = [
    ["Секция", "8-9", "10-11"],
    ["Футбол", "3", "2"],
    ["Шахматы", "2", ""],
    ["Плавание", "", ""],   # скрытая: пустые лимиты в обеих группах
    ["Волейбол", "", "2"],
]


@pytest.fixture
def filled(monkeypatch):
    """Таблица с четырьмя секциями (одна скрытая) и пятью записанными учениками."""
    sheet = install(make_spreadsheet(**{
        sheets.LIMITS_SHEET: SECTION_LIMITS,
        "Волейбол": [list(sheets.SECTION_HEADERS)],
        "Плавание": [list(sheets.SECTION_HEADERS)],
    }), monkeypatch)

    async def scenario():
        await sheets.ensure_section_sheets()
        for tg_id, name, class_num in ((1, "Аня", 8), (2, "Бек", 9), (3, "Вика", 10),
                                       (4, "Гоша", 11), (5, "Дана", 8), (6, "Ержан", 10)):
            await sheets.upsert_student(tg_id, name, class_num, "+7700", "ru")
        for tg_id, section in ((1, "Футбол"), (2, "Футбол"), (3, "Футбол"),
                               (4, "Волейбол"), (5, "Шахматы")):
            assert (await sheets.enroll(tg_id, section)).status is sheets.EnrollStatus.OK
        await sheets.flush_analytics()

    run(scenario())
    return sheet


def grid(spreadsheet) -> list[list]:
    return spreadsheet.rows(sheets.ANALYTICS_SHEET)


def block(spreadsheet, title: str) -> list[list]:
    """Строки блока: от заголовка до ближайшей пустой строки."""
    rows = grid(spreadsheet)
    start = next(i for i, row in enumerate(rows) if row and row[0] == title)
    end = next(i for i in range(start + 1, len(rows)) if not any(str(c).strip() for c in rows[i]))
    return rows[start:end]


def test_section_blocks_stand_between_updated_and_periods(filled):
    rows = grid(filled)
    first_column = [row[0] if row else "" for row in rows]

    assert first_column[0] == "Обновлено:"
    assert first_column.index("СЕКЦИИ — ПО ГРУППАМ") == 3
    assert (first_column.index("СЕКЦИИ — ПО ГРУППАМ")
            < first_column.index("СЕКЦИИ — ПО КЛАССАМ")
            < first_column.index("Сегодня")
            < first_column.index("Всё время"))


def test_by_group_block(filled):
    rows = block(filled, "СЕКЦИИ — ПО ГРУППАМ")

    assert rows[1] == [
        "Секция", "8-9 занято", "8-9 лимит", "8-9 осталось",
        "10-11 занято", "10-11 лимит", "10-11 осталось", "Всего записано",
    ]
    assert [row[0] for row in rows[2:]] == ["Футбол", "Шахматы", "Волейбол", "ИТОГО"]
    assert rows[2][1:] == [2, 3, 1, 1, 2, 1, 3]   # Футбол: 8-9 занято 2 из 3, 10-11 — 1 из 2
    assert rows[3][1:] == [1, 2, 1, 0, 0, 0, 1]   # Шахматы: группа 10-11 не допущена
    assert rows[4][1:] == [0, 0, 0, 1, 2, 1, 1]   # Волейбол: только 10-11
    assert rows[5][1:] == [3, 5, 2, 2, 4, 2, 5]   # ИТОГО


def test_by_class_block(filled):
    rows = block(filled, "СЕКЦИИ — ПО КЛАССАМ")

    assert rows[1] == ["Секция", "8 кл", "9 кл", "10 кл", "11 кл", "Всего", "", ""]
    assert [row[0] for row in rows[2:]] == ["Футбол", "Шахматы", "Волейбол", "ИТОГО"]
    assert rows[2][1:6] == [1, 1, 1, 0, 3]
    assert rows[3][1:6] == [1, 0, 0, 0, 1]
    assert rows[4][1:6] == [0, 0, 0, 1, 1]
    assert rows[5][1:6] == [2, 1, 1, 1, 5]


def test_hidden_sections_are_not_listed(filled):
    names = {row[0] for row in block(filled, "СЕКЦИИ — ПО ГРУППАМ")}
    names |= {row[0] for row in block(filled, "СЕКЦИИ — ПО КЛАССАМ")}

    assert "Плавание" not in names, "секция с пустыми лимитами скрыта"


def test_section_numbers_are_written_as_numbers(filled):
    for title in ("СЕКЦИИ — ПО ГРУППАМ", "СЕКЦИИ — ПО КЛАССАМ"):
        for row in block(filled, title)[2:]:
            for cell in (c for c in row[1:] if c != ""):
                assert isinstance(cell, int) and not isinstance(cell, bool), (title, row, cell)


def test_every_row_is_padded_to_sheet_width(filled):
    assert {len(row) for row in grid(filled)} == {sheets.ANALYTICS_COLS}


def test_grid_widened_once_and_update_stays_single_request(filled):
    # Лист достался от прошлой версии бота — 6 колонок, новые блоки требуют 8.
    assert filled.sheet(sheets.ANALYTICS_SHEET).col_count == sheets.ANALYTICS_COLS
    assert filled.requests.count("updateSheetProperties Аналитика") == 1

    filled.reset_count()

    async def scenario():
        await sheets.upsert_student(9, "Зара", 9, "+7700", "ru")
        await sheets.flush_analytics()

    run(scenario())

    assert filled.writes() == ["values.append Ученики", "values.update 'Аналитика'!A1"]
