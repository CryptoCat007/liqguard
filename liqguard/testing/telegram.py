"""
A fake Telegram Bot API behind a real aiogram `Bot` and the production `Dispatcher`.

Updates are real `Update` objects fed through `Dispatcher.feed_update`, so routers, filters,
FSM state, middlewares and message shortcuts (`callback.answer`, `message.edit_text`) all run
exactly as in production; only the HTTP layer is replaced. Every Bot API call is recorded.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)

from liqguard.core import AppContext
from liqguard.telegram.app import BotApp, wire_bot

BOT_ID = 4242
MESSAGE_METHODS = {"SendMessage", "EditMessageText", "EditMessageReplyMarkup", "SendPhoto"}


@dataclass
class Sent:
    """One message the bot sent or edited."""

    method: str
    chat_id: int
    message_id: int
    text: str
    keyboard: Optional[InlineKeyboardMarkup]
    seq: int

    def buttons(self) -> list[tuple[str, str]]:
        if self.keyboard is None:
            return []
        return [(b.text, b.callback_data or "") for row in self.keyboard.inline_keyboard for b in row]


@dataclass
class Call:
    method: str
    params: dict
    seq: int


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Call] = []
        self.sent: list[Sent] = []
        self.texts: dict[tuple[int, int], str] = {}
        self._ids = itertools.count(1000)
        self._seq = itertools.count(1)

    async def close(self) -> None:
        pass

    async def stream_content(self, url: str, headers: Optional[dict] = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: Optional[int] = None) -> Any:
        name = type(method).__name__
        params = method.model_dump(exclude_none=True)
        seq = next(self._seq)
        self.calls.append(Call(name, params, seq))
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="LiqGuard", username="liqguard_test_bot")
        if name not in MESSAGE_METHODS:
            return True
        chat_id = int(params.get("chat_id") or 0)
        message_id = params.get("message_id") or next(self._ids)
        text = params.get("text") or params.get("caption") or self.texts.get((chat_id, message_id), "")
        self.texts[(chat_id, message_id)] = text
        keyboard = getattr(method, "reply_markup", None)
        keyboard = keyboard if isinstance(keyboard, InlineKeyboardMarkup) else None
        self.sent.append(Sent(name, chat_id, message_id, text, keyboard, seq))
        return Message.model_validate({
            "message_id": message_id, "date": datetime.now(timezone.utc),
            "chat": {"id": chat_id, "type": "private"}, "from": {"id": BOT_ID, "is_bot": True, "first_name": "LiqGuard"},
            "text": text, "reply_markup": keyboard.model_dump() if keyboard else None,
        }, context={"bot": bot})


class UpdateFailed(AssertionError):
    pass


@dataclass
class FakeTelegram:
    """The production bot wiring on a fake Bot API. Use `async with FakeTelegram.create(ctx) as tg`."""

    ctx: AppContext
    bot: Bot
    session: FakeSession
    app: BotApp
    timeout: float = 10.0
    unhandled: list[str] = field(default_factory=list)
    _update_ids: Any = field(default_factory=lambda: itertools.count(1))

    @classmethod
    def create(cls, ctx: AppContext, timeout: float = 10.0, escalation_check_seconds: float = 0.02) -> "FakeTelegram":
        session = FakeSession()
        bot = Bot(token="123456:" + "A" * 35, session=session, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
        return cls(ctx, bot, session, wire_bot(ctx, bot, escalation_check_seconds), timeout)

    async def __aenter__(self) -> "FakeTelegram":
        self.app.escalation.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.app.escalation.stop()
        for sim in list(getattr(self.ctx.simulations, "_sims", {}).values()):
            await sim.stop(source="shutdown")

    # ------------------------------------------------------------------ users and updates

    @staticmethod
    def user(user_id: int, language_code: Optional[str] = "en") -> User:
        return User(id=user_id, is_bot=False, first_name=f"user{user_id}", username=f"user{user_id}", language_code=language_code)

    async def feed(self, update: Update, what: str) -> Any:
        try:
            result = await asyncio.wait_for(self.app.dispatcher.feed_update(self.bot, update), self.timeout)
        except asyncio.TimeoutError:
            raise UpdateFailed(f"{what}: handler did not finish within {self.timeout:.0f}s (stuck)") from None
        if result is UNHANDLED:
            self.unhandled.append(what)
            raise UpdateFailed(f"{what}: no handler matched (UNHANDLED)")
        return result

    async def send_text(self, user_id: int, text: str, language_code: Optional[str] = "en") -> Any:
        entities = None
        if text.startswith("/"):
            command = text.split()[0]
            entities = [MessageEntity(type="bot_command", offset=0, length=len(command))]
        message = Message(
            message_id=next(self.session._ids), date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"), from_user=self.user(user_id, language_code),
            text=text, entities=entities,
        )
        update = Update(update_id=next(self._update_ids), message=message)
        return await self.feed(update, f"message {text.split()[0] if text.startswith('/') else '<text>'}")

    async def tap(self, user_id: int, sent: Sent, data: str, language_code: Optional[str] = "en") -> Any:
        """Tap one inline button of a message the bot sent to this user."""
        message = Message.model_validate({
            "message_id": sent.message_id, "date": datetime.now(timezone.utc),
            "chat": {"id": sent.chat_id, "type": "private"}, "from": {"id": BOT_ID, "is_bot": True, "first_name": "LiqGuard"},
            "text": sent.text, "reply_markup": sent.keyboard.model_dump() if sent.keyboard else None,
        })
        query = CallbackQuery(id=str(next(self._update_ids)), from_user=self.user(user_id, language_code),
                              chat_instance="fake", data=data, message=message)
        before = len(self.answers())
        result = await self.feed(Update(update_id=next(self._update_ids), callback_query=query), f"button {data[:24]}")
        if len(self.answers()) == before:
            raise UpdateFailed(f"button {data[:24]}: answerCallbackQuery was never called")
        return result

    async def press(self, user_id: int, sent: Sent, label: str, language_code: Optional[str] = "en") -> Any:
        """Tap the button whose text contains `label`."""
        for text, data in sent.buttons():
            if label in text:
                return await self.tap(user_id, sent, data, language_code)
        raise UpdateFailed(f"no button {label!r} on message: {[t for t, _ in sent.buttons()]}")

    # ------------------------------------------------------------------ inspection

    def to(self, chat_id: int, since: int = 0) -> list[Sent]:
        return [s for s in self.session.sent if s.chat_id == chat_id and s.seq > since]

    def new_messages(self, chat_id: int, since: int = 0) -> list[Sent]:
        return [s for s in self.to(chat_id, since) if s.method == "SendMessage"]

    def last(self, chat_id: int) -> Sent:
        sent = self.to(chat_id)
        if not sent:
            raise UpdateFailed(f"nothing was sent to {chat_id}")
        return sent[-1]

    def mark(self) -> int:
        return self.session.calls[-1].seq if self.session.calls else 0

    def answers(self) -> list[Call]:
        return [c for c in self.session.calls if c.method == "AnswerCallbackQuery"]
