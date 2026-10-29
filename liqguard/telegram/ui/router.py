"""
Callback router: decode → validate (signature, owner, version, size, action,
arguments, age) → dispatch to the feature handler → render. The callback is
always answered exactly once, whatever happens.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .actions import ACTIONS
from .codec import CallbackCodec, CallbackError
from .screen import Request, Screen, Signer

logger = logging.getLogger(__name__)

INVALID = "⚠️ 這個按鈕無效或不屬於你。"
EXPIRED = "⌛ 按鈕已過期，已為你開啟最新的主選單。"
ERROR = "❌ 發生錯誤，請稍後再試。"


class CallbackRouter:
    def __init__(self, codec: CallbackCodec, services: Any, handlers: dict[str, Callable],
                 clock: Callable[[], float] = time.time):
        missing, unknown = set(ACTIONS) - set(handlers), set(handlers) - set(ACTIONS)
        if missing or unknown:
            raise RuntimeError(f"dispatch table mismatch: missing {sorted(missing)}, unknown {sorted(unknown)}")
        self.codec = codec
        self.services = services
        self.handlers = handlers
        self.clock = clock

    def signer(self, user_id: int) -> Signer:
        return Signer(self.codec, user_id, self.clock())

    async def run(self, user_id: int, action: str, args: tuple[str, ...] = (), chat_type: str = "private",
                  state: Any = None, message: Any = None) -> Screen:
        req = Request(user_id=user_id, action=action, args=args, services=self.services, sign=self.signer(user_id),
                      chat_type=chat_type, state=state, message=message)
        return await self.handlers[action](req)

    async def resolve(self, user_id: int, data: Optional[str], chat_type: str = "private", state: Any = None,
                      message: Any = None) -> tuple[Screen, Optional[str]]:
        """(screen, action that handled it) for one tap; `action` is None for a rejected button."""
        now = self.clock()
        try:
            decoded = self.codec.decode(user_id, data, now=now)
        except CallbackError as e:
            logger.info("Rejected callback from %s: %s", user_id, e.reason)
            return Screen(INVALID, toast=INVALID, alert=True, edit=False), None
        spec = ACTIONS.get(decoded.action)
        if spec is None or not spec.valid_args(decoded.args):
            logger.info("Rejected callback from %s: unknown action or bad arguments", user_id)
            return Screen(INVALID, toast=INVALID, alert=True, edit=False), None
        if now - decoded.issued_at > spec.ttl:
            screen = await self.run(user_id, "m", chat_type=chat_type, state=state, message=message)
            screen.text = f"{EXPIRED}\n\n{screen.text}"
            screen.toast = "按鈕已過期"
            return screen, "m"
        screen = await self.run(user_id, decoded.action, decoded.args, chat_type, state, message)
        return screen, decoded.action

    async def handle(self, callback: Any, state: Any = None) -> Optional[str]:
        toast: Optional[str] = None
        alert = False
        handled = None
        try:
            chat = getattr(getattr(callback, "message", None), "chat", None)
            chat_type = str(getattr(chat, "type", "private") or "private")
            screen, handled = await self.resolve(callback.from_user.id, callback.data, chat_type, state, callback.message)
            toast, alert = screen.toast, screen.alert
            await render(callback, screen)
        except Exception as e:
            logger.error("UI callback failed for %s: %s", getattr(callback.from_user, "id", "?"), type(e).__name__, exc_info=True)
            toast, alert = ERROR, True
        finally:
            try:
                await callback.answer(toast, show_alert=alert)
            except Exception as e:
                logger.warning("answerCallbackQuery failed: %s", type(e).__name__)
        return handled


async def render(callback: Any, screen: Screen) -> None:
    message = callback.message
    if screen.strip_keyboard and message is not None:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    if not screen.edit and not screen.new_message:
        return
    kwargs = {"reply_markup": screen.keyboard, "parse_mode": None, "disable_web_page_preview": True}
    if not screen.new_message and message is not None:
        try:
            await message.edit_text(screen.text, **kwargs)
            return
        except Exception as e:
            if "not modified" in str(e):
                return
    await send(callback.bot, callback.from_user.id, message, screen)


async def send(bot: Any, chat_id: int, message: Any, screen: Screen) -> None:
    kwargs = {"reply_markup": screen.keyboard, "parse_mode": None, "disable_web_page_preview": True}
    answer = getattr(message, "answer", None) if message is not None else None
    if answer is not None:
        try:
            await answer(screen.text, **kwargs)
            return
        except Exception:
            pass
    await bot.send_message(chat_id, screen.text, **kwargs)
