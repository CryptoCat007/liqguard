"""One INFO line when a button tap arrives and one when it finishes: user, action, outcome, duration.

Only the action name is logged, never the raw callback_data (it carries a signature) or any
arguments, so ack ids, symbols and binding details stay out of the log. A "received" line
without its "done" line means the handler is stuck.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import CallbackQuery

from .ui.codec import CallbackCodec, CallbackError, is_ui_payload

logger = logging.getLogger("liqguard.telegram.callbacks")


def describe(codec: Optional[CallbackCodec], user_id: int, data: Optional[str]) -> tuple[str, Optional[str]]:
    """(action name, rejection reason). Legacy payloads are named by their prefix (`ack`, `democtl` …)."""
    if not data:
        return "empty", "no data"
    if is_ui_payload(data):
        if codec is None:
            return "ui", None
        try:
            return f"ui:{codec.decode(user_id, data).action}", None
        except CallbackError as e:
            return "ui:?", e.reason
    return data.split(":", 1)[0][:16] or "?", None


class CallbackLogMiddleware(BaseMiddleware):
    def __init__(self, codec: Optional[CallbackCodec] = None):
        self.codec = codec

    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]], event: Any, data: dict) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        uid = event.from_user.id if event.from_user else 0
        lang = (event.from_user.language_code if event.from_user else None) or "-"
        action, rejected = describe(self.codec, uid, event.data)
        logger.info("Callback received: user=%s lang=%s action=%s", uid, lang, action)
        started = time.monotonic()
        outcome = "rejected (%s)" % rejected if rejected else "handled"
        try:
            result = await handler(event, data)
            if result is UNHANDLED:
                outcome = "UNHANDLED (no handler matched)"
            return result
        except Exception as e:
            outcome = f"error {type(e).__name__}"
            raise
        finally:
            ms = (time.monotonic() - started) * 1000
            level = logging.WARNING if outcome.startswith(("UNHANDLED", "error")) else logging.INFO
            logger.log(level, "Callback done: user=%s action=%s outcome=%s %.0f ms", uid, action, outcome, ms)
