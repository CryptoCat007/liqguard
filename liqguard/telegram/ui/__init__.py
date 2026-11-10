"""
Inline-keyboard UI (English / 繁體中文, per user): 🏠 Main menu and everything reachable from it.

    codec      signed, versioned callback_data (≤ 64 bytes, bound to one user)
    actions    every button: owning feature, argument shapes, TTL
    keyboards  pure InlineKeyboardMarkup builders
    router     decode → validate → dispatch table → render; always answers
    services   the only gateway to the domain, keyed by the caller's user id
    features/  one module per menu entry; features never import each other
    i18n       every string is a locale key (liqguard/i18n/locales/*.toml)

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
from liqguard.i18n import EN, LANGUAGES, TELEGRAM_CODES, Translator, t

from . import keyboards
from .codec import CallbackCodec, derive_secret, is_ui_payload
from .features import billing as billing_feature
from .features import binding as binding_feature
from .features import dispatch_table
from .router import CallbackRouter, send
from .screen import Request, Screen, Signer
from .services import UIServices

logger = logging.getLogger(__name__)

BOT_COMMANDS = ("start", "menu", "risk", "positions", "ask", "demo", "stopdemo", "settings", "bind", "status", "dashboard",
                "lang", "help")

COMMAND_ACTIONS = {"start": "m", "menu": "m", "help": "h", "risk": "ro", "positions": "pl", "demo": "dm", "stopdemo": "dx",
                   "settings": "se", "threshold": "se", "bind": "b", "unbind": "bu", "lang": "l", "language": "l",
                   "status": "ac", "actions": "ac", "killswitch": "ak"}
"""Commands this UI answers itself with the screen their menu button opens."""

MENU_ROUTES = {**COMMAND_ACTIONS, "ask": "qa", "dashboard": "db"}
"""Every slash command → the inline-keyboard action that offers the same thing. /ask (free-form question)
and /dashboard (one-time link, or `revoke`) stay on `BotHandlers`; their buttons are 🤖 Ask Muse and 🖥️ Dashboard."""


def bot_commands(lang: str = EN) -> list[BotCommand]:
    return [BotCommand(command=c, description=t(lang, f"commands.{c}")) for c in BOT_COMMANDS]


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
        r.message.register(self.on_ask_checkout, Command("ask"))
        r.message.register(
            self.on_bind_message,
            StateFilter(*binding_feature.BindStates.__states__),
            F.text,
        )
        r.message.register(self.on_agent_text, F.chat.type == "private", F.text)
        r.callback_query.register(self.on_callback, F.data.func(is_ui_payload))
        dp.include_router(r)

    async def set_commands(self, bot: Bot) -> None:
        """English for everyone by default, 繁體中文 for Telegram apps set to any zh* language."""
        for lang in LANGUAGES:
            try:
                await bot.set_my_commands(bot_commands(lang), language_code=TELEGRAM_CODES[lang])
            except Exception as e:
                logger.warning("setMyCommands(%s) failed: %s", lang, type(e).__name__)

    async def translator_for(self, message: Message) -> Translator:
        return await self.router.translator(message.from_user.id, getattr(message.from_user, "language_code", None))

    async def on_command(self, message: Message, state: FSMContext, command: Optional[CommandObject] = None) -> None:
        name = (command.command if command else "start").lower()
        if command is not None and command.args and name in ("settings", "start"):
            raise SkipHandler()
        await state.clear()
        T = await self.translator_for(message)
        action = COMMAND_ACTIONS.get(name, "m")
        args: tuple[str, ...] = ()
        if action == "ak":
            args = ("off" if self.ctx.actions.is_kill_switch_active(message.from_user.id) else "on",)
        screen = await self.router.run(message.from_user.id, action, args,
                                       chat_type=str(message.chat.type), state=state, message=message, T=T)
        self.router.follow_up(await send(message.bot, message.chat.id, message, screen), screen)

    async def on_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        await self.router.handle(callback, state)

    async def on_bind_message(self, message: Message, state: FSMContext) -> None:
        if (message.text or "").startswith("/") and message.text.strip().lower() not in binding_feature.CANCEL_WORDS:
            raise SkipHandler()
        T = await self.translator_for(message)
        screen = await binding_feature.on_message(self.services, message, state, self.signer(message.from_user.id), T)
        await send(message.bot, message.chat.id, None, screen)

    async def on_ask_checkout(self, message: Message, state: FSMContext, command: Optional[CommandObject] = None) -> None:
        question = ((command.args if command else None) or "").strip()
        if not question:
            question = (message.text or "").replace("/ask", "", 1).strip()
        from liqguard.payments.agent import intent_from_text

        if intent_from_text(question) is None:
            raise SkipHandler()
        await self._send_agent_checkout(message, state, question)

    async def on_agent_text(self, message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            raise SkipHandler()
        from liqguard.payments.agent import intent_from_text

        if intent_from_text(text) is None:
            raise SkipHandler()
        await self._send_agent_checkout(message, state, text)

    async def _send_agent_checkout(self, message: Message, state: FSMContext, text: str) -> None:
        T = await self.translator_for(message)
        req = Request(
            user_id=message.from_user.id, action="px", args=("auto",), services=self.services,
            sign=self.signer(message.from_user.id), chat_type=str(message.chat.type),
            state=state, message=message, extra={"text": text}, T=T,
        )
        screen = await billing_feature.agent_checkout_screen(req)
        await send(message.bot, message.chat.id, None, screen)

    # ------------------------------------------------------------------ alert buttons

    def alert_markup(self, user_id: int, actions: list[tuple[str, str]], lang: str = EN, demo: bool = False) -> Any:
        """
        Turn an alert's legacy actions into the UI buttons 🚑 Handle this first / ✅ I'm awake /
        😴 Snooze (plus ⏹ Stop demo on demo alerts), keeping any other legacy buttons after them.
        `lang` is the language the alert body was written in, so body and buttons always match.
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
            rows = keyboards.alert_rows(
                Translator(lang), self.signer(user_id), source, symbol, ack_id, demo,
                agent=self.ctx.settings.paypal_enabled,
            )
        except ValueError:
            return None
        rows += [[keyboards.Button(text=label, callback_data=data)] for label, data in rest]
        return keyboards.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_ui(ctx: AppContext) -> TelegramUI:
    return TelegramUI(ctx)


__all__ = ["BOT_COMMANDS", "COMMAND_ACTIONS", "MENU_ROUTES", "bot_commands", "CallbackCodec", "CallbackRouter", "Screen", "Signer", "TelegramUI", "UIServices", "build_ui"]
