"""Все клавиатуры бота (reply + inline)."""
from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from sheets import CLASSES, Availability
from texts import LANGS, TEXTS, places, t

# Префиксы callback_data
CB_LANG_REGISTER = "setlang"      # выбор языка при регистрации
CB_LANG_CHANGE = "chlang"         # смена языка через /lang
CB_SECTION = "sec"                # выбор секции: sec:<index>
CB_SECTION_FULL = "sec_full"      # секция без мест
CB_SECTIONS_BACK = "sections:back"  # «← Назад» под списком секций → экран проверки данных
CB_RESTART = "restart"            # «🔄 Начать заново»
CB_DATA_OK = "data:ok"            # «✅ Всё верно» на экране проверки данных
CB_DATA_EDIT = "data:edit"        # «✏️ Изменить» на экране проверки данных
CB_ENROLL_YES = "enroll:yes"      # «✅ Записаться»
CB_ENROLL_EDIT = "enroll:edit"    # «✏️ Изменить данные»
CB_ENROLL_BACK = "enroll:back"    # «↩️ Другая секция»
CB_CANCEL_ASK = "cancel:ask"
CB_CANCEL_YES = "cancel:yes"
CB_CANCEL_NO = "cancel:no"
CB_REMIND_YES = "remind:yes"     # remind:yes:<класс|all>
CB_REMIND_NO = "remind:no"

CLASS_BUTTONS: tuple[str, ...] = tuple(str(c) for c in CLASSES)
# Тексты reply-кнопки «← Назад» на всех языках — по ним фильтруются хендлеры шагов.
BACK_TEXTS: frozenset[str] = frozenset(TEXTS[lang]["btn_back_step"] for lang in LANGS)


def _back_button(lang: str) -> KeyboardButton:
    return KeyboardButton(text=t(lang, "btn_back_step"))


def _restart_row(lang: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t(lang, "btn_restart"), callback_data=CB_RESTART)]


def lang_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data=f"{prefix}:ru"),
                InlineKeyboardButton(text="🇰🇿 Қазақша", callback_data=f"{prefix}:kk"),
            ]
        ]
    )


def name_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[_back_button(lang)]], resize_keyboard=True)


def class_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=label) for label in CLASS_BUTTONS],
            [_back_button(lang)],
        ],
        resize_keyboard=True,
    )


def phone_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_phone"), request_contact=True)],
            [_back_button(lang)],
        ],
        resize_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def confirm_data_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_data_ok"), callback_data=CB_DATA_OK),
                InlineKeyboardButton(text=t(lang, "btn_data_edit"), callback_data=CB_DATA_EDIT),
            ]
        ]
    )


def sections_kb(lang: str, availability: Sequence[Availability]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(availability):
        if item.available:
            text = f"✅ {item.name} — {places(lang, item.free_for_group)}"
            data = f"{CB_SECTION}:{index}"
        else:
            text = f"🚫 {item.name} — {t(lang, 'no_places')}"
            data = CB_SECTION_FULL
        rows.append([InlineKeyboardButton(text=text, callback_data=data)])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_back_step"), callback_data=CB_SECTIONS_BACK)])
    rows.append(_restart_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_enroll_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_enroll"), callback_data=CB_ENROLL_YES)],
            [InlineKeyboardButton(text=t(lang, "btn_edit_data"), callback_data=CB_ENROLL_EDIT)],
            [InlineKeyboardButton(text=t(lang, "btn_other_section"), callback_data=CB_ENROLL_BACK)],
        ]
    )


def my_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data=CB_CANCEL_ASK)],
            _restart_row(lang),
        ]
    )


def cancel_confirm_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_yes"), callback_data=CB_CANCEL_YES),
                InlineKeyboardButton(text=t(lang, "btn_no"), callback_data=CB_CANCEL_NO),
            ]
        ]
    )


def remind_confirm_kb(target: str) -> InlineKeyboardMarkup:
    """Подтверждение рассылки: target — номер класса или «all»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("ru", "btn_yes"), callback_data=f"{CB_REMIND_YES}:{target}"),
                InlineKeyboardButton(text=t("ru", "btn_no"), callback_data=CB_REMIND_NO),
            ]
        ]
    )
