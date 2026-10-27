"""Telegram delivery layer (aiogram). Holds no business logic of its own."""

from .handlers import BotHandlers
from .sim_bridge import TelegramNotifier, TelegramSimulationBridge, attach_bridges, format_advice_message

__all__ = [
    "BotHandlers",
    "TelegramNotifier",
    "TelegramSimulationBridge",
    "attach_bridges",
    "format_advice_message",
]
