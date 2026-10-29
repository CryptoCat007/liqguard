"""
Inline-keyboard UI (zh-Hant-TW): 🏠 主選單 and everything reachable from it.

    codec      signed, versioned callback_data (≤ 64 bytes, bound to one user)
    actions    every button: owning feature, argument shapes, TTL
    keyboards  pure InlineKeyboardMarkup builders
    router     decode → validate → dispatch table → render; always answers
    services   the only gateway to the domain, keyed by the caller's user id
    features/  one module per menu entry; features never import each other

Legacy callbacks (`rescue:`, `ack:`, `settings:` …) keep working through
`BotHandlers`; this router only claims payloads that start with "1.".
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, CallbackQuery, Message

from liqguard.core import AppContext

from . import keyboards
from .codec import CallbackCodec, derive_secret, is_ui_payload
from .features import binding as binding_feature
from .features import dispatch_table
from .router import CallbackRouter, send
from .screen import Screen, Signer
from .services import UIServices

logger = logging.getLogger(__name__)

BOT_COMMANDS = (
    ("start", "開始／主選單"),
    ("menu", "🏠 主選單"),
    ("positions", "📊 我的倉位"),
    ("risk", "🚨 風險總覽"),
    ("demo", "🎬 Demo 模式"),
    ("settings", "🔔 通知設定"),
    ("bind", "🔑 綁定帳號"),
    ("dashboard", "🖥️ 網頁看板"),
    ("help", "❓ 幫助"),
)

COMMAND_ACTIONS = {"start": "m", "menu": "m", "help": "h", "positions": "pl", "demo": "dm",
                   "settings": "se", "bind": "b", "unbind": "bu"}


class TelegramUI:
    def __init__(self, ctx: AppContext, services: Optional[UIServices] = None, codec: Optional[CallbackCodec] = None):
        self.ctx = ctx
        self.services = services or UIServices(ctx)
        self.codec = codec or CallbackCodec(derive_secret(ctx.settings.session_secret, ctx.settings.telegram_token))
        self.router = CallbackRouter(self.codec, self.services, dispatch_table())
        self.aiogram_router = Router(name="liqguard-ui")

    def signer(self, user_id: int) -> Signer:
        return self.router.signer(user_id)

    def register(self, dp: Dispatcher) -> None:
        r = self.aiogram_router
        r.message.register(self.on_command, CommandStart())
        r.message.register(self.on_command, Command(*COMMAND_ACTIONS))
        r.message.register(
            self.on_bind_message,
            StateFilter(*binding_feature.BindStates.__states__),
            F.text,
        )
        r.callback_query.register(self.on_callback, F.data.func(is_ui_payload))
        dp.include_router(r)

    async def set_commands(self, bot: Bot) -> None:
        try:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
        except Exception as e:
            logger.warning("setMyCommands failed: %s", type(e).__name__)

    async def on_command(self, message: Message, state: FSMContext, command: Optional[CommandObject] = None) -> None:
        name = (command.command if command else "start").lower()
        if command is not None and command.args and name in ("settings", "start"):
            raise SkipHandler()
        await state.clear()
        screen = await self.router.run(message.from_user.id, COMMAND_ACTIONS.get(name, "m"),
                                       chat_type=str(message.chat.type), state=state, message=message)
        await send(message.bot, message.chat.id, message, screen)

    async def on_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        await self.router.handle(callback, state)

    async def on_bind_message(self, message: Message, state: FSMContext) -> None:
        if (message.text or "").startswith("/") and message.text.strip().lower() not in binding_feature.CANCEL_WORDS:
            raise SkipHandler()
        screen = await binding_feature.on_message(self.services, message, state, self.signer(message.from_user.id))
        await send(message.bot, message.chat.id, None, screen)

    # ------------------------------------------------------------------ alert buttons

    def alert_markup(self, user_id: int, actions: list[tuple[str, str]]) -> Any:
        """
        Turn an alert's legacy actions into the UI buttons 🚑 先處理這個 / ✅ 我醒了 /
        😴 暫停提醒 30 分鐘, keeping any other legacy buttons after them.
        """
        source = symbol = ack_id = None
        rest = []
        for label, data in actions:
            if data.startswith("rescue:") and source is None and data.count(":") == 2:
                _, source, symbol = data.split(":")
            elif data.startswith("ack:") and ack_id is None:
                ack_id = data[4:]
            else:
                rest.append((label, data))
        try:
            rows = keyboards.alert_rows(self.signer(user_id), source, symbol, ack_id)
        except ValueError:
            return None
        rows += [[keyboards.Button(text=label, callback_data=data)] for label, data in rest]
        return keyboards.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_ui(ctx: AppContext) -> TelegramUI:
    return TelegramUI(ctx)


__all__ = ["BOT_COMMANDS", "CallbackCodec", "CallbackRouter", "Screen", "Signer", "TelegramUI", "UIServices", "build_ui"]
