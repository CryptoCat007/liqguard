"""Types shared by the router, keyboards and feature handlers (no aiogram I/O here)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from aiogram.types import InlineKeyboardMarkup

from liqguard.i18n import Translator

from .codec import CallbackCodec


class Signer:
    """Renders signed callback data for one user; keyboards receive this instead of the codec."""

    def __init__(self, codec: CallbackCodec, user_id: int, now: Optional[float] = None):
        self.codec = codec
        self.user_id = user_id
        self.now = now

    def __call__(self, action: str, *args: str) -> str:
        return self.codec.encode(self.user_id, action, *args, now=self.now)


@dataclass
class Screen:
    """What a handler wants shown: the router edits the tapped message in place (or sends a new one)."""

    text: str
    keyboard: Optional[InlineKeyboardMarkup] = None
    toast: Optional[str] = None
    alert: bool = False
    new_message: bool = False
    edit: bool = True
    strip_keyboard: bool = False


@dataclass
class Request:
    user_id: int
    action: str
    args: tuple[str, ...]
    services: Any
    sign: Signer
    chat_type: str = "private"
    state: Any = None
    message: Any = None
    extra: dict = field(default_factory=dict)
    T: Translator = field(default_factory=Translator)


Handler = Callable[[Request], "Screen"]
