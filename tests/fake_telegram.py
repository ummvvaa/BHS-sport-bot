"""Поддельный Telegram: диспетчер aiogram работает без сети.

Позволяет прогнать реальные хендлеры (/start, шаги регистрации, выбор секции)
и посчитать, сколько запросов к Sheets API стоит весь сценарий.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncGenerator

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User

DEFAULT_USER_ID = 555001


class FakeSession(BaseSession):
    """Ничего не отправляет: запоминает вызовы и возвращает правдоподобные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._message_id = 1000

    async def close(self) -> None:  # pragma: no cover — сеть не используется
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        returning = getattr(type(method), "__returning__", bool)
        if Message in getattr(returning, "__args__", (returning,)):
            self._message_id += 1
            return make_message(bot, "ok", message_id=self._message_id)
        return True

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls if (text := getattr(call, "text", None))]


def make_message(bot: Bot, text: str, *, message_id: int = 1, user_id: int = DEFAULT_USER_ID) -> Message:
    user = User(id=user_id, is_bot=False, first_name="Тест")
    chat = Chat(id=user_id, type="private")
    return Message(
        message_id=message_id, date=datetime(2025, 9, 10, 12, 0), chat=chat, from_user=user, text=text
    ).as_(bot)


class FakeTelegram:
    """Обёртка над Dispatcher: send(текст), send_contact(телефон), press(callback_data)."""

    def __init__(self, *routers: Router, user_id: int = DEFAULT_USER_ID) -> None:
        self.user_id = user_id
        self.session = FakeSession()
        self.bot = Bot(token="42:TEST", session=self.session)
        self.dp = Dispatcher(storage=MemoryStorage())
        for router in routers:
            # Роутеры в проекте — модульные синглтоны; отвязываем от прошлого диспетчера.
            router._parent_router = None
            self.dp.include_router(router)
        self._update_id = 0
        self._message_id = 1

    def _next_update(self) -> int:
        self._update_id += 1
        return self._update_id

    def _message(self, text: str | None) -> Message:
        self._message_id += 1
        return make_message(self.bot, text or "", message_id=self._message_id, user_id=self.user_id)

    async def _feed(self, update: Update) -> None:
        await self.dp.feed_update(self.bot, update)

    async def send(self, text: str) -> None:
        await self._feed(Update(update_id=self._next_update(), message=self._message(text)))

    async def send_contact(self, phone: str) -> None:
        """Кнопка «Отправить номер»: сообщение с контактом, а не с текстом."""
        contact = Contact(phone_number=phone, first_name="Тест", user_id=self.user_id)
        message = self._message(None).model_copy(update={"text": None, "contact": contact}).as_(self.bot)
        await self._feed(Update(update_id=self._next_update(), message=message))

    async def press(self, data: str) -> None:
        """Нажатие на inline-кнопку последнего сообщения бота."""
        message = self._message("предыдущее сообщение")
        callback = CallbackQuery(
            id=str(self._next_update()),
            from_user=message.from_user,
            chat_instance="test",
            message=message,
            data=data,
        ).as_(self.bot)
        await self._feed(Update(update_id=self._next_update(), callback_query=callback))

    def last_keyboard(self) -> list[str]:
        """callback_data всех кнопок последнего отправленного сообщения."""
        for call in reversed(self.session.calls):
            markup = getattr(call, "reply_markup", None)
            rows = getattr(markup, "inline_keyboard", None)
            if rows:
                return [b.callback_data for row in rows for b in row if b.callback_data]
        return []
