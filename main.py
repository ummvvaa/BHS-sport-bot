"""Точка входа: создание бота и диспетчера, подключение роутеров, polling."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, ErrorEvent, Message

import sheets
from config import settings
from handlers import admin_router, student_router
from texts import LANGS, both, t

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def on_error(event: ErrorEvent, state: FSMContext | None = None) -> bool:
    """Единая обработка ошибок: пользователю — короткое сообщение, в лог — traceback."""
    logger.exception("Ошибка при обработке апдейта %s", event.update.update_id, exc_info=event.exception)

    lang: str | None = None
    if state is not None:
        try:
            lang = (await state.get_data()).get("lang")
        except Exception:  # noqa: BLE001
            lang = None
    text = t(lang, "error") if lang in LANGS else both("error")

    update = event.update
    try:
        if isinstance(update.callback_query, CallbackQuery):
            await update.callback_query.answer()
            if isinstance(update.callback_query.message, Message):
                await update.callback_query.message.answer(text)
        elif isinstance(update.message, Message):
            await update.message.answer(text)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить сообщение об ошибке")
    return True


async def set_commands(bot: Bot) -> None:
    """Меню команд: ученикам — базовые, админам (по chat_id) — плюс админские."""
    user_commands = [
        BotCommand(command="start", description=t("ru", "cmd_start")),
        BotCommand(command="my", description=t("ru", "cmd_my")),
        BotCommand(command="lang", description=t("ru", "cmd_lang")),
        BotCommand(command="cancel", description=t("ru", "cmd_cancel")),
    ]
    admin_commands = user_commands + [
        BotCommand(command="stats", description=t("ru", "cmd_stats")),
        BotCommand(command="analytics", description=t("ru", "cmd_analytics")),
        BotCommand(command="remind", description=t("ru", "cmd_remind")),
        BotCommand(command="export", description=t("ru", "cmd_export")),
        BotCommand(command="reload", description=t("ru", "cmd_reload")),
    ]
    await bot.set_my_commands(user_commands)
    for admin_id in sorted(settings.admin_ids):
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramBadRequest as exc:
            # Обычно «chat not found»: админ ещё ни разу не писал боту.
            logger.warning("Не удалось задать меню команд для админа %s: %s", admin_id, exc)


async def main() -> None:
    setup_logging()

    created = await sheets.ensure_section_sheets()
    if created:
        logger.info("Созданы листы секций: %s", ", ".join(created))
    else:
        logger.info("Все листы секций на месте")
    await sheets.refresh_analytics()
    logger.info("Лист «%s» обновлён", sheets.ANALYTICS_SHEET)
    sheets.start_background_tasks()

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(student_router)
    dp.error.register(on_error)

    await set_commands(bot)
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Бот запущен, админы: %s", sorted(settings.admin_ids))
    try:
        await dp.start_polling(bot)
    finally:
        await sheets.stop_background_tasks()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
