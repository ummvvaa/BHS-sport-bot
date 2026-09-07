"""Все тексты бота на русском и казахском."""
from __future__ import annotations

from typing import Any

LANGS: tuple[str, ...] = ("ru", "kk")
DEFAULT_LANG = "ru"

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        # Общие
        "choose_lang": "Выбери язык",
        "lang_name": "Русский",
        "closed": "Запись закрыта. Срок записи на секции истёк.",
        "error": "Что-то пошло не так, попробуй позже.",
        "not_registered": "Ты ещё не зарегистрирован. Нажми /start",
        "lang_changed": "Язык изменён: русский.",
        # Регистрация
        "ask_name": (
            "Привет! Это бот записи на спортивные секции Beta High School.\n\n"
            "Напиши своё имя и фамилию."
        ),
        "bad_name": (
            "Имя должно быть от 2 до 50 символов и содержать только буквы, "
            "пробел и дефис. Напиши ещё раз."
        ),
        "ask_class": "В каком классе ты учишься? Нажми кнопку ниже.",
        "bad_class": "Нажми одну из кнопок: 8, 9, 10 или 11.",
        "ask_phone": "Теперь отправь свой номер телефона — нажми кнопку ниже.",
        "btn_phone": "📱 Отправить номер",
        "bad_phone": (
            "Нажми кнопку «📱 Отправить номер». "
            "Номер, введённый вручную, не принимается."
        ),
        "confirm_data": (
            "Проверь данные:\n\n"
            "👤 Имя: {name}\n"
            "🏫 Класс: {class_num}\n"
            "📱 Телефон: {phone}\n\n"
            "Всё верно?"
        ),
        "btn_data_ok": "✅ Всё верно",
        "btn_data_edit": "✏️ Изменить",
        "ask_name_again": "Сейчас: {name}. Введи имя заново.",
        "btn_back_step": "← Назад",
        "btn_restart": "🔄 Начать заново",
        "restarting": "Начинаем заново.",
        "back_to_lang": "Возвращаемся к выбору языка.",
        "finish_first": "Сначала закончи регистрацию или нажми /start, чтобы начать заново.",
        # Секции
        "choose_section": "Выбери секцию. Можно записаться только на одну.",
        "no_sections": "Секций пока нет. Попробуй позже.",
        "no_places": "мест нет",
        "no_places_alert": "Мест нет",
        "stale": "Выбор устарел. Выбери секцию заново.",
        "confirm_enroll": (
            "Проверь запись:\n\n"
            "👤 Имя: {name}\n"
            "🏫 Класс: {class_num}\n"
            "📱 Телефон: {phone}\n"
            "⚽ Секция: {section}\n\n"
            "Всё верно?"
        ),
        "btn_enroll": "✅ Записаться",
        "btn_edit_data": "✏️ Изменить данные",
        "btn_other_section": "↩️ Другая секция",
        "btn_yes": "✅ Да",
        "btn_no": "❌ Нет",
        "enrolled": (
            "✅ Готово! Ты записан.\n\n"
            "👤 {name}, {class_num} класс\n"
            "📱 {phone}\n"
            "⚽ {section}\n\n"
            "Посмотреть или отменить запись — /my"
        ),
        "taken": "К сожалению, место уже занято. Выбери другую секцию.",
        "already_enrolled": (
            "Ты уже записан на {section}. "
            "Чтобы сменить секцию, сначала отмени запись — /my"
        ),
        # /my
        "my_enrolled": "Твоя запись: {section}, {class_num} класс",
        "not_enrolled": "Ты пока не записан ни на одну секцию.",
        "btn_cancel": "❌ Отменить запись",
        "cancel_confirm": "Точно отменить запись на {section}?",
        "cancelled": "Запись отменена. Можешь выбрать новую секцию.",
        "cancel_kept": "Запись сохранена.",
        "nothing_to_cancel": "У тебя нет активной записи.",
        # Напоминание от админа
        "remind": "Ты ещё не выбрал секцию. Свободные места пока есть — жми /start",
        # Админ
        "admin_no_sections": "В листе «Лимиты» нет ни одной секции.",
        "stats_line": "{section}: {count}/{limit} ({by_group})",
        "stats_group": "{group}кл {count}/{limit}",
        "analytics_title_all": "📊 Аналитика за всё время",
        "analytics_title_days": "📊 Аналитика за последние {period}",
        "analytics_passive": "Самый пассивный: {class_num} класс ({count} чел.)",
        "analytics_total": "Всего: {registered} зарегались, {enrolled} записались",
        "analytics_by_day": "📈 По дням:",
        "analytics_by_day_empty": "📈 По дням: нет данных (у строк нет даты регистрации)",
        "analytics_by_day_truncated": "(показаны последние {n} дней)",
        "analytics_usage": (
            "Использование: /analytics — за всё время, /analytics N — за последние N дней (1–365)."
        ),
        "remind_usage": "Использование: /remind 9 (класс 8–11) или /remind all",
        "remind_closed": "Запись закрыта — напоминание отправлять нет смысла.",
        "remind_nobody": "Некому отправлять: у всех учеников ({target}) уже есть секция.",
        "remind_confirm": "Отправить напоминание {n} ученикам ({target})?",
        "remind_sending": "Отправляю {n} ученикам…",
        "remind_report": "Отправлено: {sent}, не доставлено: {failed}",
        "remind_cancelled": "Рассылка отменена.",
        "remind_target_class": "{class_num} класс",
        "remind_target_all": "все классы",
        "stats_total": "Всего зарегистрировано: {registered}, записано: {enrolled}",
        "stats_deadline": "Дедлайн: {deadline} (Asia/Almaty)",
        "stats_no_deadline": "Дедлайн: не задан",
        "stats_closed": "Запись закрыта",
        "stats_open": "Запись открыта",
        "reload_done": "Кэш лимитов и настроек сброшен.",
        "export_caption": "Выгрузка записей на {when}",
        # Команды
        "cmd_start": "Начать / Бастау",
        "cmd_my": "Моя запись / Менің жазылуым",
        "cmd_lang": "Сменить язык / Тілді өзгерту",
        "cmd_cancel": "Начать заново / Қайта бастау",
        "cmd_stats": "Сводка по секциям (админ)",
        "cmd_export": "Выгрузка в .xlsx (админ)",
        "cmd_analytics": "Аналитика по классам (админ)",
        "cmd_remind": "Напомнить ученикам без секции (админ)",
        "cmd_reload": "Сбросить кэш лимитов (админ)",
    },
    "kk": {
        # Общие
        "choose_lang": "Тілді таңда",
        "lang_name": "Қазақша",
        "closed": "Жазылу жабылды. Секцияларға жазылу мерзімі аяқталды.",
        "error": "Бірдеңе дұрыс болмады, кейінірек қайталап көр.",
        "not_registered": "Сен әлі тіркелмегенсің. /start командасын бас",
        "lang_changed": "Тіл өзгертілді: қазақша.",
        # Регистрация
        "ask_name": (
            "Сәлем! Бұл Beta High School спорт секцияларына жазылу боты.\n\n"
            "Атың мен тегіңді жаз."
        ),
        "bad_name": (
            "Аты 2-ден 50-ге дейін таңбадан тұруы керек және тек әріптерден, "
            "бос орыннан және сызықшадан құралуы тиіс. Қайтадан жаз."
        ),
        "ask_class": "Нешінші сыныпта оқисың? Төмендегі батырманы бас.",
        "bad_class": "Батырмалардың бірін бас: 8, 9, 10 немесе 11.",
        "ask_phone": "Енді телефон нөміріңді жібер — төмендегі батырманы бас.",
        "btn_phone": "📱 Нөмірді жіберу",
        "bad_phone": (
            "«📱 Нөмірді жіберу» батырмасын бас. "
            "Қолмен жазылған нөмір қабылданбайды."
        ),
        "confirm_data": (
            "Деректерді тексер:\n\n"
            "👤 Аты: {name}\n"
            "🏫 Сынып: {class_num}\n"
            "📱 Телефон: {phone}\n\n"
            "Бәрі дұрыс па?"
        ),
        "btn_data_ok": "✅ Бәрі дұрыс",
        "btn_data_edit": "✏️ Өзгерту",
        "ask_name_again": "Қазір: {name}. Атыңды қайта жаз.",
        "btn_back_step": "← Артқа",
        "btn_restart": "🔄 Қайта бастау",
        "restarting": "Қайтадан бастаймыз.",
        "back_to_lang": "Тіл таңдауға ораламыз.",
        "finish_first": "Алдымен тіркелуді аяқта немесе қайта бастау үшін /start бас.",
        # Секции
        "choose_section": "Секцияны таңда. Тек бір секцияға жазылуға болады.",
        "no_sections": "Секциялар әзірге жоқ. Кейінірек қайталап көр.",
        "no_places": "орын жоқ",
        "no_places_alert": "Орын жоқ",
        "stale": "Таңдау ескірді. Секцияны қайтадан таңда.",
        "confirm_enroll": (
            "Жазылуды тексер:\n\n"
            "👤 Аты: {name}\n"
            "🏫 Сынып: {class_num}\n"
            "📱 Телефон: {phone}\n"
            "⚽ Секция: {section}\n\n"
            "Бәрі дұрыс па?"
        ),
        "btn_enroll": "✅ Жазылу",
        "btn_edit_data": "✏️ Деректерді өзгерту",
        "btn_other_section": "↩️ Басқа секция",
        "btn_yes": "✅ Иә",
        "btn_no": "❌ Жоқ",
        "enrolled": (
            "✅ Дайын! Сен жазылдың.\n\n"
            "👤 {name}, {class_num}-сынып\n"
            "📱 {phone}\n"
            "⚽ {section}\n\n"
            "Жазылуды көру немесе болдырмау — /my"
        ),
        "taken": "Өкінішке қарай, бұл орын бос емес. Басқа секция таңда.",
        "already_enrolled": (
            "Сен {section} секциясына жазылғансың. "
            "Секцияны ауыстыру үшін алдымен жазылуды болдырма — /my"
        ),
        # Напоминание от админа
        "remind": "Сен әлі секция таңдаған жоқсың. Бос орындар әзірге бар — /start бас",
        # /my
        "my_enrolled": "Сенің жазылуың: {section}, {class_num}-сынып",
        "not_enrolled": "Сен әлі ешбір секцияға жазылмағансың.",
        "btn_cancel": "❌ Жазылуды болдырмау",
        "cancel_confirm": "{section} секциясына жазылуды шынымен болдырмайсың ба?",
        "cancelled": "Жазылу болдырылмады. Жаңа секция таңдай аласың.",
        "cancel_kept": "Жазылу сақталды.",
        "nothing_to_cancel": "Сенде белсенді жазылу жоқ.",
        # Команды
        "cmd_start": "Бастау",
        "cmd_my": "Менің жазылуым",
        "cmd_lang": "Тілді өзгерту",
        "cmd_cancel": "Қайта бастау",
    },
}


def t(lang: str, key: str, **kwargs: Any) -> str:
    """Возвращает фразу на нужном языке; если ключа нет в `lang` — берёт из `ru`."""
    table = TEXTS.get(lang, TEXTS[DEFAULT_LANG])
    template = table.get(key)
    if template is None:
        template = TEXTS[DEFAULT_LANG][key]
    return template.format(**kwargs) if kwargs else template


def both(key: str, **kwargs: Any) -> str:
    """Фраза на обоих языках (когда язык пользователя ещё неизвестен)."""
    return "\n\n".join(t(lang, key, **kwargs) for lang in LANGS)


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        return few
    return many


def days_ru(n: int) -> str:
    """«1 день» / «2 дня» / «7 дней»."""
    return f"{n} {_plural_ru(n, 'день', 'дня', 'дней')}"


def places(lang: str, n: int) -> str:
    """«3 места» / «3 орын»."""
    if lang == "kk":
        return f"{n} орын"
    return f"{n} {_plural_ru(n, 'место', 'места', 'мест')}"


def format_phone(phone: str) -> str:
    """«+77011234567» → «+7 701 123 45 67». Иначе номер возвращается как есть."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits[0]} {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:11]}"
    return phone
