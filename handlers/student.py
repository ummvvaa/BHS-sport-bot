"""Хендлеры ученика: /start, /cancel, регистрация, выбор секции, /my, отмена, /lang."""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import sheets
from keyboards import (
    BACK_TEXTS,
    CB_CANCEL_ASK,
    CB_CANCEL_NO,
    CB_CANCEL_YES,
    CB_DATA_EDIT,
    CB_DATA_OK,
    CB_ENROLL_BACK,
    CB_ENROLL_EDIT,
    CB_ENROLL_YES,
    CB_LANG_CHANGE,
    CB_LANG_REGISTER,
    CB_RESTART,
    CB_SECTION,
    CB_SECTION_FULL,
    CB_SECTIONS_BACK,
    CLASS_BUTTONS,
    cancel_confirm_kb,
    class_kb,
    confirm_data_kb,
    confirm_enroll_kb,
    lang_kb,
    my_kb,
    name_kb,
    phone_kb,
    remove_kb,
    sections_kb,
)
from sheets import EnrollStatus, Student
from states import Registration
from texts import LANGS, both, format_phone, t

logger = logging.getLogger(__name__)
router = Router(name="student")

NAME_RE = re.compile(r"[A-Za-zЀ-ӿ \-]{2,50}")


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name)) and any(ch.isalpha() for ch in name)


def _normalize_phone(phone: str) -> str:
    """Убирает пробелы/скобки/дефисы и добавляет ведущий «+». В таблицу пишем без пробелов."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"+{digits}" if digits else phone.strip()


async def _lang_from_state(state: FSMContext) -> str | None:
    data = await state.get_data()
    lang = data.get("lang")
    return lang if lang in LANGS else None


async def _remember_lang(state: FSMContext, lang: str) -> None:
    await state.update_data(lang=lang)


def _callback_message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    return message if isinstance(message, Message) else None


async def _safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await message.answer(text, reply_markup=kb)


async def _drop_inline_kb(message: Message | None) -> None:
    """Убирает inline-кнопки с сообщения, чтобы по ним нельзя было нажать повторно."""
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


def _my_text(student: Student) -> str:
    return t(student.lang, "my_enrolled", section=student.section, class_num=student.class_num)


def _data_text(lang: str, name: str, class_num: int, phone: str) -> str:
    return t(lang, "confirm_data", name=name, class_num=class_num, phone=format_phone(phone))


def _final_text(lang: str, student: Student, section: str) -> str:
    return t(
        lang,
        "confirm_enroll",
        name=student.name,
        class_num=student.class_num,
        phone=format_phone(student.phone),
        section=section,
    )


async def _sections_view(
    lang: str, class_num: int, intro: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    availability = await sheets.get_availability(class_num)
    if not availability:
        text = t(lang, "no_sections")
        return (f"{intro}\n\n{text}" if intro else text), None
    text = t(lang, "choose_section")
    return (f"{intro}\n\n{text}" if intro else text), sections_kb(lang, availability)


REPLY_STEPS: frozenset[str] = frozenset(
    state.state for state in (Registration.name, Registration.class_num, Registration.phone) if state.state
)


def _on_reply_step(state_name: str | None) -> bool:
    """True, если пользователь находится на шаге с reply-клавиатурой (имя / класс / телефон)."""
    return state_name in REPLY_STEPS


async def _send_inline(
    message: Message, text: str, kb: InlineKeyboardMarkup | None, *, remove_reply: bool = False
) -> None:
    """Отправляет inline-экран, при необходимости предварительно сняв reply-клавиатуру.

    В одном сообщении нельзя передать два markup, а сообщение с ReplyKeyboardRemove
    Telegram не даёт редактировать. Поэтому при remove_reply=True уходит короткая
    заглушка с ReplyKeyboardRemove, тут же удаляется (клавиатура при этом не
    возвращается), и затем отправляется сам экран с inline-кнопками.
    """
    if remove_reply:
        placeholder = await message.answer("…", reply_markup=remove_kb())
        try:
            await placeholder.delete()
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=kb)


async def _send_sections(
    message: Message, lang: str, class_num: int, intro: str | None = None, *, remove_reply: bool = False
) -> None:
    text, kb = await _sections_view(lang, class_num, intro)
    await _send_inline(message, text, kb, remove_reply=remove_reply)


async def _edit_sections(message: Message, lang: str, class_num: int, intro: str | None = None) -> None:
    text, kb = await _sections_view(lang, class_num, intro)
    await _safe_edit(message, text, kb)


async def _ask_language(
    message: Message, state: FSMContext, note: str | None, *, remove_reply: bool = False
) -> None:
    """Полный сброс состояния и переход к выбору языка. `note` — префикс в том же сообщении."""
    await state.clear()
    await state.set_state(Registration.lang)
    text = f"{note}\n\n{both('choose_lang')}" if note else both("choose_lang")
    await _send_inline(message, text, lang_kb(CB_LANG_REGISTER), remove_reply=remove_reply)


async def _restart(message: Message, state: FSMContext, lang: str | None, *, remove_reply: bool = False) -> None:
    note = t(lang, "restarting") if lang else both("restarting")
    await _ask_language(message, state, note, remove_reply=remove_reply)


async def _ask_name(message: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    await state.set_state(Registration.name)
    previous = data.get("name")
    text = t(lang, "ask_name_again", name=previous) if previous else t(lang, "ask_name")
    await message.answer(text, reply_markup=name_kb(lang))


async def _ask_class(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(Registration.class_num)
    await message.answer(t(lang, "ask_class"), reply_markup=class_kb(lang))


async def _ask_phone(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(Registration.phone)
    await message.answer(t(lang, "ask_phone"), reply_markup=phone_kb(lang))


# --------------------------------------------------------------------------- #
# /start и /cancel — регистрируются первыми, чтобы работать в любом состоянии
# --------------------------------------------------------------------------- #


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    from_reply_step = _on_reply_step(await state.get_state())
    await state.clear()
    student = await sheets.get_student(message.from_user.id)
    lang = student.lang if student else None
    if lang:
        await _remember_lang(state, lang)

    if await sheets.is_closed():
        await message.answer(t(lang, "closed") if lang else both("closed"), reply_markup=remove_kb())
        return

    if student is None:
        await _ask_language(message, state, None, remove_reply=from_reply_step)
        return

    if student.section:
        await _send_inline(message, _my_text(student), my_kb(student.lang), remove_reply=from_reply_step)
        return

    await _send_sections(message, student.lang, student.class_num, remove_reply=from_reply_step)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    from_reply_step = _on_reply_step(await state.get_state())
    await _restart(message, state, await _lang_from_state(state), remove_reply=from_reply_step)


@router.callback_query(F.data == CB_RESTART)
async def restart_callback(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    lang = await _lang_from_state(state)
    await callback.answer()
    await _drop_inline_kb(message)
    if message:
        await _restart(message, state, lang)
    else:
        await state.clear()
        await state.set_state(Registration.lang)
        await callback.bot.send_message(
            callback.from_user.id, both("choose_lang"), reply_markup=lang_kb(CB_LANG_REGISTER)
        )


@router.message(StateFilter(Registration), Command("my", "lang"))
async def commands_during_registration(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state)
    await message.answer(t(lang, "finish_first") if lang else both("finish_first"))


# --------------------------------------------------------------------------- #
# Регистрация: язык → имя → класс → телефон → проверка данных
# --------------------------------------------------------------------------- #


@router.callback_query(Registration.lang, F.data.startswith(f"{CB_LANG_REGISTER}:"))
async def registration_lang(callback: CallbackQuery, state: FSMContext) -> None:
    lang = callback.data.split(":", 1)[1]
    if lang not in LANGS:
        await callback.answer()
        return
    await _remember_lang(state, lang)
    await callback.answer()
    message = _callback_message(callback)
    if message:
        await _safe_edit(message, t(lang, "lang_changed"))
        await _ask_name(message, state, lang)
    else:
        await state.set_state(Registration.name)
        await callback.bot.send_message(callback.from_user.id, t(lang, "ask_name"), reply_markup=name_kb(lang))


# --- имя ---


@router.message(Registration.name, F.text.in_(BACK_TEXTS))
async def name_back(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    data = await state.get_data()
    await state.set_state(Registration.lang)
    await state.set_data({k: v for k, v in data.items() if k in ("lang", "section")})
    await _send_inline(
        message,
        f"{t(lang, 'back_to_lang')}\n\n{both('choose_lang')}",
        lang_kb(CB_LANG_REGISTER),
        remove_reply=True,
    )


@router.message(Registration.name, F.text)
async def registration_name(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    name = " ".join(message.text.split())
    if not _valid_name(name):
        await message.answer(t(lang, "bad_name"), reply_markup=name_kb(lang))
        return
    await state.update_data(name=name)
    await _ask_class(message, state, lang)


@router.message(Registration.name)
async def registration_name_invalid(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    await message.answer(t(lang, "bad_name"), reply_markup=name_kb(lang))


# --- класс ---


@router.message(Registration.class_num, F.text.in_(BACK_TEXTS))
async def class_back(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    await _ask_name(message, state, lang)


@router.message(Registration.class_num, F.text.in_(CLASS_BUTTONS))
async def registration_class(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    await state.update_data(class_num=int(message.text))
    await _ask_phone(message, state, lang)


@router.message(Registration.class_num)
async def registration_class_invalid(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    await message.answer(t(lang, "bad_class"), reply_markup=class_kb(lang))


# --- телефон ---


@router.message(Registration.phone, F.text.in_(BACK_TEXTS))
async def phone_back(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    await _ask_class(message, state, lang)


@router.message(Registration.phone, F.contact)
async def registration_phone(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer(t(lang, "bad_phone"), reply_markup=phone_kb(lang))
        return

    phone = _normalize_phone(contact.phone_number)
    await state.update_data(phone=phone)
    data = await state.get_data()
    await state.set_state(Registration.confirm)
    await _send_inline(
        message,
        _data_text(lang, data["name"], int(data["class_num"]), phone),
        confirm_data_kb(lang),
        remove_reply=True,
    )


@router.message(Registration.phone)
async def registration_phone_invalid(message: Message, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    await message.answer(t(lang, "bad_phone"), reply_markup=phone_kb(lang))


# --- проверка данных ---


@router.callback_query(Registration.confirm, F.data == CB_DATA_OK)
async def data_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    data = await state.get_data()
    lang = data.get("lang") if data.get("lang") in LANGS else "ru"
    name = data.get("name")
    class_num = data.get("class_num")
    phone = data.get("phone")
    await callback.answer()
    if message is None:
        return
    if not (name and class_num and phone):
        await _drop_inline_kb(message)
        await _ask_name(message, state, lang)
        return

    student = await sheets.upsert_student(callback.from_user.id, str(name), int(class_num), str(phone), lang)
    logger.info("Ученик %s сохранён: %s, %s кл.", student.tg_id, student.name, student.class_num)
    pending_section = data.get("section")
    await state.set_state(None)
    await state.set_data({"lang": lang, "section": pending_section})

    if student.section:
        # Запись была до перерегистрации — она сохраняется.
        await _safe_edit(message, _my_text(student), my_kb(lang))
        return
    if await sheets.is_closed():
        await _safe_edit(message, t(lang, "closed"))
        return
    if pending_section:
        await _safe_edit(message, _final_text(lang, student, pending_section), confirm_enroll_kb(lang))
        return
    await _edit_sections(message, lang, student.class_num)


@router.callback_query(Registration.confirm, F.data == CB_DATA_EDIT)
async def data_edit(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    lang = await _lang_from_state(state) or "ru"
    await callback.answer()
    await _drop_inline_kb(message)
    if message:
        await _ask_name(message, state, lang)


@router.callback_query(F.data == CB_SECTIONS_BACK)
async def sections_back(callback: CallbackQuery, state: FSMContext) -> None:
    """«← Назад» под списком секций → экран проверки данных."""
    message = _callback_message(callback)
    data = await state.get_data()
    student = await sheets.get_student(callback.from_user.id)
    lang = student.lang if student else (data.get("lang") if data.get("lang") in LANGS else "ru")
    name = data.get("name") or (student.name if student else None)
    class_num = data.get("class_num") or (student.class_num if student else None)
    phone = data.get("phone") or (student.phone if student else None)
    await callback.answer()
    if message is None:
        return
    if not (name and class_num and phone):
        await _safe_edit(message, t(lang, "not_registered"))
        return
    await state.set_state(Registration.confirm)
    await state.update_data(lang=lang, name=name, class_num=int(class_num), phone=phone)
    await _safe_edit(message, _data_text(lang, str(name), int(class_num), str(phone)), confirm_data_kb(lang))


# --------------------------------------------------------------------------- #
# Выбор секции и финальное подтверждение
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == CB_SECTION_FULL)
async def section_full(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _lang_from_state(state) or "ru"
    await callback.answer(t(lang, "no_places_alert"), show_alert=False)


@router.callback_query(F.data.startswith(f"{CB_SECTION}:"))
async def section_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    student = await sheets.get_student(callback.from_user.id)
    if student is None:
        await callback.answer()
        if message:
            await _safe_edit(message, t(await _lang_from_state(state) or "ru", "not_registered"))
        return
    lang = student.lang
    await _remember_lang(state, lang)

    if await sheets.is_closed():
        await callback.answer()
        if message:
            await _safe_edit(message, t(lang, "closed"))
        return

    if student.section:
        await callback.answer()
        if message:
            await _safe_edit(message, t(lang, "already_enrolled", section=student.section), my_kb(lang))
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except ValueError:
        index = -1

    availability = await sheets.get_availability(student.class_num)
    if index < 0 or index >= len(availability):
        await callback.answer()
        if message:
            await _edit_sections(message, lang, student.class_num, t(lang, "stale"))
        return

    item = availability[index]
    if not item.available:
        await callback.answer(t(lang, "no_places_alert"), show_alert=False)
        if message:
            await _edit_sections(message, lang, student.class_num)
        return

    await state.update_data(section=item.name)
    await callback.answer()
    if message:
        await _safe_edit(message, _final_text(lang, student, item.name), confirm_enroll_kb(lang))


@router.callback_query(F.data == CB_ENROLL_BACK)
async def enroll_back(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    await state.update_data(section=None)
    student = await sheets.get_student(callback.from_user.id)
    await callback.answer()
    if student is None:
        if message:
            await _safe_edit(message, t(await _lang_from_state(state) or "ru", "not_registered"))
        return
    await _remember_lang(state, student.lang)
    if message:
        await _edit_sections(message, student.lang, student.class_num)


@router.callback_query(F.data == CB_ENROLL_EDIT)
async def enroll_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """«✏️ Изменить данные»: на шаг имени, выбранная секция остаётся в FSM."""
    message = _callback_message(callback)
    student = await sheets.get_student(callback.from_user.id)
    await callback.answer()
    if message is None:
        return
    if student is None:
        await _safe_edit(message, t(await _lang_from_state(state) or "ru", "not_registered"))
        return
    data = await state.get_data()
    await state.set_data({"lang": student.lang, "section": data.get("section"), "name": student.name})
    await _drop_inline_kb(message)
    await _ask_name(message, state, student.lang)


@router.callback_query(F.data == CB_ENROLL_YES)
async def enroll_yes(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    data = await state.get_data()
    section = data.get("section")

    student = await sheets.get_student(callback.from_user.id)
    if student is None:
        await callback.answer()
        if message:
            await _safe_edit(message, t(await _lang_from_state(state) or "ru", "not_registered"))
        return
    lang = student.lang
    await _remember_lang(state, lang)

    if await sheets.is_closed():
        await callback.answer()
        if message:
            await _safe_edit(message, t(lang, "closed"))
        return

    if not section:
        await callback.answer()
        if message:
            await _edit_sections(message, lang, student.class_num, t(lang, "stale"))
        return

    result = await sheets.enroll(student.tg_id, section)
    await state.update_data(section=None)
    await callback.answer()
    if message is None:
        return

    if result.status is EnrollStatus.OK:
        logger.info("Ученик %s (%s кл.) записан на «%s»", student.tg_id, student.class_num, section)
        await _safe_edit(
            message,
            t(
                lang,
                "enrolled",
                name=student.name,
                class_num=student.class_num,
                phone=format_phone(student.phone),
                section=section,
            ),
        )
    elif result.status is EnrollStatus.ALREADY:
        await _safe_edit(message, t(lang, "already_enrolled", section=result.section), my_kb(lang))
    elif result.status is EnrollStatus.NOT_REGISTERED:
        await _safe_edit(message, t(lang, "not_registered"))
    else:  # FULL или UNKNOWN_SECTION — показать актуальный список
        await _edit_sections(message, lang, student.class_num, t(lang, "taken"))


# --------------------------------------------------------------------------- #
# /my и отмена
# --------------------------------------------------------------------------- #


@router.message(Command("my"))
async def cmd_my(message: Message, state: FSMContext) -> None:
    student = await sheets.get_student(message.from_user.id)
    if student is None:
        lang = await _lang_from_state(state)
        await message.answer(t(lang, "not_registered") if lang else both("not_registered"))
        return
    await _remember_lang(state, student.lang)
    if student.section:
        await message.answer(_my_text(student), reply_markup=my_kb(student.lang))
        return
    if await sheets.is_closed():
        await message.answer(t(student.lang, "closed"))
        return
    await _send_sections(message, student.lang, student.class_num, t(student.lang, "not_enrolled"))


@router.callback_query(F.data == CB_CANCEL_ASK)
async def cancel_ask(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    student = await sheets.get_student(callback.from_user.id)
    await callback.answer()
    if message is None:
        return
    if student is None:
        await _safe_edit(message, t(await _lang_from_state(state) or "ru", "not_registered"))
        return
    lang = student.lang
    await _remember_lang(state, lang)
    if await sheets.is_closed():
        await _safe_edit(message, t(lang, "closed"))
        return
    if not student.section:
        await _edit_sections(message, lang, student.class_num, t(lang, "nothing_to_cancel"))
        return
    await _safe_edit(message, t(lang, "cancel_confirm", section=student.section), cancel_confirm_kb(lang))


@router.callback_query(F.data == CB_CANCEL_NO)
async def cancel_no(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    student = await sheets.get_student(callback.from_user.id)
    await callback.answer()
    if message is None:
        return
    if student is None:
        await _safe_edit(message, t(await _lang_from_state(state) or "ru", "not_registered"))
        return
    await _remember_lang(state, student.lang)
    if student.section:
        await _safe_edit(message, f"{t(student.lang, 'cancel_kept')}\n\n{_my_text(student)}", my_kb(student.lang))
    else:
        await _edit_sections(message, student.lang, student.class_num, t(student.lang, "nothing_to_cancel"))


@router.callback_query(F.data == CB_CANCEL_YES)
async def cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    student = await sheets.get_student(callback.from_user.id)
    await callback.answer()
    if message is None:
        return
    if student is None:
        await _safe_edit(message, t(await _lang_from_state(state) or "ru", "not_registered"))
        return
    lang = student.lang
    await _remember_lang(state, lang)
    if await sheets.is_closed():
        await _safe_edit(message, t(lang, "closed"))
        return

    cancelled = await sheets.cancel_enrollment(student.tg_id)
    if cancelled:
        logger.info("Ученик %s отменил запись на «%s»", student.tg_id, student.section)
        await _edit_sections(message, lang, student.class_num, t(lang, "cancelled"))
    else:
        await _edit_sections(message, lang, student.class_num, t(lang, "nothing_to_cancel"))


# --------------------------------------------------------------------------- #
# /lang
# --------------------------------------------------------------------------- #


@router.message(Command("lang"))
async def cmd_lang(message: Message) -> None:
    await message.answer(both("choose_lang"), reply_markup=lang_kb(CB_LANG_CHANGE))


@router.callback_query(F.data.startswith(f"{CB_LANG_CHANGE}:"))
async def change_lang(callback: CallbackQuery, state: FSMContext) -> None:
    lang = callback.data.split(":", 1)[1]
    if lang not in LANGS:
        await callback.answer()
        return
    await _remember_lang(state, lang)
    await sheets.update_student_lang(callback.from_user.id, lang)
    await callback.answer()
    message = _callback_message(callback)
    if message:
        await _safe_edit(message, t(lang, "lang_changed"))


# --------------------------------------------------------------------------- #
# Устаревшие кнопки — просто закрыть «часики»
# --------------------------------------------------------------------------- #


@router.callback_query()
async def unknown_callback(callback: CallbackQuery) -> None:
    await callback.answer()
